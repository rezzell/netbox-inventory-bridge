"""Run with NetBox's Django shell. Credentials are read only from projected files."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path

PLACEMENT_MODELS = {'region': 'region', 'site': 'site', 'location': 'location', 'rack': 'rack',
                    'cluster': 'virtualization.cluster'}

ACCOUNTS = ('netbox-hardware-agent', 'netbox-lifecycle-companion')
MARKER = 'Managed by node-hardware-inventory activation'


def validate_config(config):
    if not {'cluster_id', 'device_ids'} <= set(config) or set(config) - {'cluster_id', 'device_ids', 'placement_read_ids'}:
        raise ValueError('config requires cluster_id, device_ids and optional placement_read_ids')
    if not isinstance(config['cluster_id'], str) or not config['cluster_id'].strip():
        raise ValueError('cluster_id must be a nonempty ownership string')
    ids = config['device_ids']
    if not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError('device_ids must be nonempty positive integers')
    if len(ids) != len(set(ids)):
        raise ValueError('device_ids must be unique')
    placement = config.get('placement_read_ids', {})
    if not isinstance(placement, dict) or set(placement) - set(PLACEMENT_MODELS):
        raise ValueError('invalid placement read scope')
    for ids in placement.values():
        if (not isinstance(ids, list) or not ids or
                any(type(i) is not int or i <= 0 for i in ids) or len(ids) != len(set(ids))):
            raise ValueError('placement read scope requires unique positive IDs')
    return config


def parse_token(raw):
    match = re.fullmatch(r'nbt_([A-Za-z0-9]{12})\.([A-Za-z0-9]{40})', raw.strip())
    if not match:
        raise ValueError('projected credential must be a bare NetBox v2 token')
    return match.groups()


def validate_token_owner(existing, user_id, secret):
    if existing is not None:
        if existing.user_id != user_id:
            raise ValueError('token key is already owned by another account')
        if not existing.validate(secret):
            raise ValueError('existing token key has a different credential; rotate to a new key')
        if existing.description != MARKER:
            raise ValueError('existing token is not managed by this activation')


def overlap_expiry(expires, now):
    return expires if expires is not None else now + timedelta(hours=1)


def permission_specs(device_ids, hardware, placement_read_ids=None):
    specs = [('devices', ['device'], ['view', 'change'], {'id__in': device_ids})]
    if hardware:
        specs += [
            ('catalog', ['manufacturer', 'moduletype'], ['view', 'add'], None),
            ('components', ['modulebay', 'interface'], ['view', 'add'], {'device_id__in': device_ids}),
            ('modules-read-create', ['module'], ['view', 'add'], {'device_id__in': device_ids}),
            ('modules-change', ['module'], ['change'], {
                'device_id__in': device_ids, 'description__startswith': 'node-inventory:'}),
            ('mac-read', ['macaddress'], ['view'], None),
            ('mac-create', ['macaddress'], ['add'], {'interface__device_id__in': device_ids}),
        ]
    if not hardware:
        for field, ids in (placement_read_ids or {}).items():
            specs.append(('placement-read-' + field, [PLACEMENT_MODELS[field]], ['view'], {'id__in': ids}))
    return specs


def run(config, credentials, apply=False):
    from django.contrib.auth import get_user_model
    from django.contrib.contenttypes.models import ContentType
    from django.db import transaction
    from django.utils import timezone
    from dcim.models import Device
    from extras.models import CustomField
    from users.choices import TokenVersionChoices
    from users.models import ObjectPermission, Token

    validate_config(config)
    parsed = {name: parse_token(credentials[name]) for name in ACCOUNTS}
    if len({key for key, _ in parsed.values()}) != len(ACCOUNTS):
        raise ValueError('accounts must use distinct token keys')
    User = get_user_model()
    with transaction.atomic():
        devices = Device.objects.filter(pk__in=config['device_ids'])
        if devices.count() != len(config['device_ids']):
            raise ValueError('one or more configured Devices do not exist')
        device_type = ContentType.objects.get(app_label='dcim', model='device')
        for name in ('hardware_observation', 'kubernetes_lifecycle'):
            field = CustomField.objects.filter(name=name).first()
            if field is not None:
                if (field.type != 'json' or field.required or
                        set(field.object_types.values_list('pk', flat=True)) != {device_type.pk}):
                    raise ValueError('existing custom field has incompatible type, required flag or scope: ' + name)
            elif apply:
                field = CustomField(name=name, type='json', required=False, description=MARKER)
                field.full_clean()
                field.save()
                field.object_types.set([device_type])

        for name in ACCOUNTS:
            email = name + '@node-inventory.invalid'
            user = User.objects.filter(username=name).first()
            if user is not None and (user.has_usable_password() or user.email != email):
                raise ValueError('refusing to adopt an existing unmanaged account: ' + name)
            key, secret = parsed[name]
            existing = Token.objects.filter(key=key).first()
            validate_token_owner(existing, user.pk if user else None, secret)
            specs = permission_specs(config['device_ids'], name == ACCOUNTS[0], config.get('placement_read_ids'))
            permissions = []
            for suffix, models, actions, constraints in specs:
                content_types = [ContentType.objects.get(app_label=model.split('.')[0] if '.' in model else 'dcim',
                                                        model=model.split('.')[-1]) for model in models]
                # Force ORM lookup compilation now, including the MAC GenericRelation reverse query.
                for ct in content_types:
                    str(ct.model_class().objects.filter(**(constraints or {})).query)
                permission_name = name + ' activation ' + suffix
                permission = ObjectPermission.objects.filter(name=permission_name).first()
                if permission is not None:
                    if (permission.description != MARKER or permission.groups.exists() or
                            permission.users.exclude(pk=user.pk if user else None).exists()):
                        raise ValueError('refusing to overwrite an unrelated permission: ' + permission_name)
                if apply:
                    permission = permission or ObjectPermission(name=permission_name)
                    permission.description = MARKER
                    permission.enabled = True
                    permission.actions = actions
                    permission.constraints = constraints
                    permission.full_clean()
                    permission.save()
                    permission.object_types.set(content_types)
                    permissions.append(permission)
            if not apply:
                continue
            user = user or User(username=name, email=email)
            user.is_active = True
            user.is_staff = False
            user.is_superuser = False
            user.set_unusable_password()
            user.full_clean()
            user.save()
            user.groups.clear()
            user.user_permissions.clear()
            user.object_permissions.set(permissions)
            if existing is None:
                existing = Token(user=user, description=MARKER, version=TokenVersionChoices.V2,
                                 key=key, token=secret)
            existing.enabled = True
            existing.write_enabled = True
            existing.expires = None
            existing.allowed_ips = None
            existing.full_clean()
            existing.save()
            now = timezone.now()
            for old in Token.objects.filter(user=user, description=MARKER).exclude(pk=existing.pk):
                if old.expires is None:
                    old.expires = overlap_expiry(old.expires, now)
                    old.save(update_fields=['expires'])
        print(json.dumps({'mode': 'apply' if apply else 'report',
                          'accounts': list(ACCOUNTS), 'device_ids': config['device_ids'],
                          'schema_and_constraints_validated': True}))


def main():
    config = json.loads(Path(os.environ.get('ACTIVATION_CONFIG', '/opt/activation/config.json')).read_text())
    credentials = {name: (Path('/run/activation') / name / 'token').read_text() for name in ACCOUNTS}
    run(config, credentials, apply=os.environ.get('ACTIVATION_APPLY') == 'true')


if __name__ == '__main__':
    main()
