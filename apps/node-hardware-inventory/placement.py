"""Opt-in, allowlisted Node metadata to native NetBox placement references."""
from runtime import APIError, InventoryError, owner_check

ENDPOINTS = {'region': 'dcim/regions', 'site': 'dcim/sites', 'location': 'dcim/locations',
             'rack': 'dcim/racks', 'cluster': 'virtualization/clusters'}
DEFAULTS = {'enabled': False, 'dry_run': True, 'fields': {}}


def validate_placement(value, mappings):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise InventoryError('invalid placement configuration')
    settings = {**DEFAULTS, **value}
    if any(type(settings[key]) is not bool for key in ('enabled', 'dry_run')):
        raise InventoryError('invalid placement mode')
    fields = settings['fields']
    if not isinstance(fields, dict) or set(fields) - set(ENDPOINTS):
        raise InventoryError('invalid placement field')
    for rule in fields.values():
        if not isinstance(rule, dict) or set(rule) - {'annotation', 'labels'}:
            raise InventoryError('invalid placement rule')
        if 'annotation' in rule and (not isinstance(rule['annotation'], str) or not rule['annotation'].strip()):
            raise InventoryError('invalid placement annotation')
        labels = rule.get('labels', [])
        if not isinstance(labels, list):
            raise InventoryError('invalid placement labels')
        for label in labels:
            if (not isinstance(label, dict) or set(label) != {'key', 'values'} or
                    not isinstance(label['key'], str) or not label['key'].strip() or
                    not isinstance(label['values'], dict) or
                    any(not isinstance(k, str) or type(v) is not int or v <= 0 for k, v in label['values'].items())):
                raise InventoryError('invalid placement label translation')
    for mapping in mappings.values():
        overrides = mapping.get('placement', {})
        if (not isinstance(overrides, dict) or set(overrides) - set(ENDPOINTS) or
                any(type(v) is not int or v <= 0 for v in overrides.values())):
            raise InventoryError('invalid placement override')
    return settings


def reference_id(value):
    return value.get('id') if isinstance(value, dict) else value


def select(node, mapping, settings):
    """Overrides are intentional; otherwise annotations and translated labels must agree."""
    desired, sources, issues = {}, {}, []
    metadata = node.get('metadata', {})
    annotations, labels = metadata.get('annotations', {}), metadata.get('labels', {})
    overrides = mapping.get('placement', {})
    for field in sorted(set(settings['fields']) | set(overrides)):
        if field in overrides:
            desired[field], sources[field] = overrides[field], ['configuration']
            continue
        rule = settings['fields'][field]
        candidates = []
        annotation = rule.get('annotation')
        if annotation and annotation in annotations:
            raw = annotations[annotation]
            if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
                issues.append({'field': field, 'reason': 'invalid_annotation', 'source': annotation})
            else:
                candidates.append((int(raw), 'annotation:' + annotation))
        for label in rule.get('labels', []):
            key = label['key']
            if key not in labels:
                continue
            translated = label['values'].get(labels[key])
            if translated is None:
                issues.append({'field': field, 'reason': 'unmapped_label', 'source': key})
            else:
                candidates.append((translated, 'label:' + key))
        if len({value for value, _ in candidates}) > 1:
            issues.append({'field': field, 'reason': 'conflicting_sources'})
        elif candidates:
            desired[field] = candidates[0][0]
            sources[field] = [source for _, source in candidates]
    return desired, sources, issues


def plan(node, mapping, settings, device, client):
    desired, sources, issues = select(node, mapping, settings)
    objects = {}
    for field, identity in desired.items():
        try:
            obj = client.get('api/%s/%s/' % (ENDPOINTS[field], identity))
            if obj.get('id') != identity:
                raise InventoryError('placement reference identity mismatch')
            objects[field] = obj
        except APIError as error:
            if error.status not in (403, 404):
                raise
            issues.append({'field': field, 'reason': 'reference_unavailable', 'status': error.status})
    effective = {field: desired.get(field, reference_id(device.get(field)))
                 for field in ('site', 'location', 'rack', 'cluster')}
    # Resolve existing dependent placement too: changing a site must not silently
    # retain a rack/location from a different site. Null never means "clear".
    for field in ('location', 'rack'):
        identity = effective[field]
        if identity and field not in objects and field not in desired:
            objects[field] = client.get('api/%s/%s/' % (ENDPOINTS[field], identity))
        if identity and field in objects and reference_id(objects[field].get('site')) != effective['site']:
            issues.append({'field': field, 'reason': 'site_mismatch'})
    if effective['rack'] and effective['location'] and 'rack' in objects:
        rack_location = reference_id(objects['rack'].get('location'))
        if rack_location and rack_location != effective['location']:
            # A rack may belong to a descendant of the Device location.
            location, seen = rack_location, set()
            while location and location != effective['location'] and location not in seen:
                seen.add(location)
                location = reference_id(client.get('api/dcim/locations/%s/' % location).get('parent'))
            if location != effective['location']:
                issues.append({'field': 'rack', 'reason': 'location_mismatch'})
    if 'region' in desired and 'region' in objects:
        site = objects.get('site')
        if site is None and effective['site']:
            site = client.get('api/dcim/sites/%s/' % effective['site'])
        if not site or reference_id(site.get('region')) != desired['region']:
            issues.append({'field': 'region', 'reason': 'site_region_mismatch'})
    changes = {field: {'from': reference_id(device.get(field)), 'to': identity}
               for field, identity in desired.items()
               if field != 'region' and reference_id(device.get(field)) != identity}
    # Child servers inherit physical placement from their chassis. Never move
    # them independently, or infer chassis bay / U position from node names.
    if 'rack' in changes and device.get('position') is not None:
        issues.append({'field': 'rack', 'reason': 'rack_position_requires_manual_review'})
    if device.get('parent_device') and any(field in changes for field in ('site', 'location', 'rack')):
        issues.append({'field': 'device', 'reason': 'parent_placement_requires_manual_review'})
    return {'desired': desired, 'sources': sources, 'changes': changes, 'issues': issues,
            'status': 'blocked' if issues else ('proposed' if changes else 'unchanged')}


def reconcile_placement(node, mapping, config, name, client, device):
    settings = config['placement']
    if node is None:
        return {'status': 'skipped', 'reason': 'node_absent', 'dry_run': True}
    def validate(current):
        previous = current.get('custom_fields', {}).get('kubernetes_lifecycle')
        owner_check(previous, config, name)
        if previous and previous.get('node_uid') != node['metadata']['uid']:
            raise InventoryError('placement Node UID changed; identity review required')
    validate(device)
    dry_run = config['dry_run'] or settings['dry_run']
    path = 'api/dcim/devices/%s/' % mapping['device_id']
    for attempt in range(3):
        # A fresh ETag and full re-plan on every retry protects curated placement
        # and identity from changes between observation and write.
        if not dry_run:
            device, headers = client.request('GET', path)
            validate(device)
        result = plan(node, mapping, settings, device, client)
        result['dry_run'] = dry_run
        if dry_run or result['issues'] or not result['changes']:
            return result
        etag = headers.get('ETag')
        if not etag:
            raise InventoryError('NetBox ETag required for placement update')
        try:
            client.request('PATCH', path, {field: change['to'] for field, change in result['changes'].items()},
                           {'If-Match': etag})
            result['status'] = 'applied'
            return result
        except APIError as error:
            if error.status != 412 or attempt == 2:
                raise
