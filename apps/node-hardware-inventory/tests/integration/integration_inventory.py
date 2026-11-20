"""Real NetBox serializers/permissions/database; deliberately no HTTP transport."""
import copy
import os
from urllib.parse import urlencode
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site, Interface, Module, ModuleBay, ModuleType, MACAddress
from extras.models import CustomField
from users.models import ObjectPermission
from rest_framework.test import APIClient
from agent import reconcile as hardware_reconcile
from companion import reconcile as lifecycle_reconcile
from provision_accounts import permission_specs
from runtime import APIError, update_field
from test_runtime import config, snapshot, node, NOW


class Adapter:
    def __init__(self, user):
        self.client = APIClient()
        self.client.force_authenticate(user=user)
        self.writes = []

    def request(self, method, path, payload=None, headers=None):
        kwargs = {'HTTP_' + key.upper().replace('-', '_'): value for key, value in (headers or {}).items()}
        response = getattr(self.client, method.lower())('/' + path.lstrip('/'), data=payload, format='json', **kwargs)
        if response.status_code >= 300: raise APIError(response.status_code)
        if method != 'GET': self.writes.append((method, path, copy.deepcopy(payload)))
        return response.json(), dict(response.items())

    def get(self, path): return self.request('GET', path)[0]

    def listing(self, path, filters=None):
        page = self.get(path + ('?' + urlencode(filters) if filters else ''))
        if page.get('next'): raise AssertionError('fixture unexpectedly paginated')
        return page['results']


class InventoryIntegration(TransactionTestCase):
    def setUp(self):
        self.site = Site.objects.create(name='Fixture site', slug='fixture-site')
        self.other_site = Site.objects.create(name='Other site', slug='other-site')
        manufacturer = Manufacturer.objects.create(name='Fixture maker', slug='fixture-maker')
        dtype = DeviceType.objects.create(manufacturer=manufacturer, model='Fixture server', slug='fixture-server')
        role = DeviceRole.objects.create(name='Fixture role', slug='fixture-role')
        ct = ContentType.objects.get_for_model(Device)
        for name in ('hardware_observation', 'kubernetes_lifecycle', 'curated'):
            field = CustomField.objects.create(name=name, type='json')
            field.object_types.add(ct)
        self.device = Device.objects.create(name='fixture-node', serial='fixture-device', site=self.site,
                                           device_type=dtype, role=role, custom_field_data={'curated': 'preserve'})
        self.outside = Device.objects.create(name='outside-scope', serial='outside', site=self.site, device_type=dtype, role=role)
        self.hardware = self.account('fixture-hardware', True)
        self.lifecycle = self.account('fixture-lifecycle', False)
        self.settings = config(False)
        self.settings['device_mappings']['node-a']['device_id'] = self.device.pk
        self.path = 'api/dcim/devices/%s/' % self.device.pk

    def account(self, name, hardware):
        user = get_user_model().objects.create_user(username=name)
        for suffix, models, actions, constraints in permission_specs([self.device.pk], hardware, {'site': [self.other_site.pk]}):
            permission = ObjectPermission.objects.create(name=name + suffix, actions=actions, constraints=constraints)
            permission.users.add(user)
            permission.object_types.set([ContentType.objects.get(app_label=model.split('.')[0] if '.' in model else 'dcim', model=model.split('.')[-1]) for model in models])
        return Adapter(user)

    def test_real_runtime_payloads_replay_scopes_and_defaults(self):
        hardware_reconcile(snapshot(), self.settings, self.hardware)
        counts = [model.objects.count() for model in (ModuleBay, ModuleType, Module, Interface, MACAddress)]
        self.assertEqual(counts, [1, 1, 1, 1, 1])
        hardware_reconcile(snapshot(), self.settings, self.hardware)
        self.assertEqual(counts, [model.objects.count() for model in (ModuleBay, ModuleType, Module, Interface, MACAddress)])
        lifecycle_reconcile({'node-a': node()}, self.settings, self.lifecycle, NOW)
        fields = self.hardware.get(self.path)['custom_fields']
        self.assertEqual(fields['curated'], 'preserve')
        self.assertEqual(fields['kubernetes_lifecycle']['node_uid'], 'uid-1')
        self.assertEqual(fields['hardware_observation']['serial'], 'fixture-device')
        self.assertEqual(len(self.hardware.listing('api/dcim/mac-addresses/', {'assigned_object_id': Interface.objects.get().pk})), 1)
        for client in (self.hardware, self.lifecycle):
            with self.assertRaises(APIError): client.get('api/dcim/devices/%s/' % self.outside.pk)
            with self.assertRaises(APIError): client.request('PATCH', 'api/dcim/devices/%s/' % self.outside.pk, {'comments': 'refused'})
        with self.assertRaises(APIError): self.lifecycle.request('POST', 'api/dcim/interfaces/', {'device': self.device.pk, 'name': 'forbidden', 'type': 'other'})
        dry = copy.deepcopy(self.settings); dry['dry_run'] = True
        before = len(self.hardware.writes)
        hardware_reconcile(snapshot(), dry, self.hardware)
        lifecycle_reconcile({'node-a': node()}, dry, self.hardware, NOW)
        self.assertEqual(before, len(self.hardware.writes))
        if os.environ.get('INVENTORY_INJECT_FAILURE') == '1': self.fail('intentional cleanup verification')

    def test_stale_etag_and_retry_preserve_both_fields(self):
        _, headers = self.hardware.request('GET', self.path)
        self.assertIn('ETag', headers)
        value = {'cluster_id': 'test-cluster', 'node_name': 'node-a', 'node_uid': 'uid-1'}
        update_field(self.lifecycle, self.device.pk, 'kubernetes_lifecycle', value, self.settings, 'node-a')
        with self.assertRaises(APIError) as caught:
            self.hardware.request('PATCH', self.path, {'custom_fields': {'hardware_observation': value}}, {'If-Match': headers['ETag']})
        self.assertEqual(caught.exception.status, 412)
        self.assertIsNone(self.hardware.get(self.path)['custom_fields']['hardware_observation'])
        original = self.hardware.request
        raced = []
        def request(method, path, payload=None, headers=None):
            if method == 'PATCH' and not raced:
                raced.append(True)
                update_field(self.lifecycle, self.device.pk, 'kubernetes_lifecycle', dict(value, state='ready'), self.settings, 'node-a')
            return original(method, path, payload, headers)
        self.hardware.request = request
        update_field(self.hardware, self.device.pk, 'hardware_observation', value, self.settings, 'node-a')
        fields = self.hardware.get(self.path)['custom_fields']
        self.assertEqual(fields['kubernetes_lifecycle']['state'], 'ready')
        self.assertEqual(fields['hardware_observation'], value)

    def test_placement_independent_dryrun_and_apply(self):
        settings = copy.deepcopy(self.settings)
        settings['placement'] = {'enabled': True, 'dry_run': True, 'fields': {}}
        settings['device_mappings']['node-a']['placement'] = {'site': self.other_site.pk}
        lifecycle_reconcile({'node-a': node()}, settings, self.lifecycle, NOW)
        self.device.refresh_from_db(); self.assertEqual(self.device.site_id, self.site.pk)
        settings['placement']['dry_run'] = False
        result = lifecycle_reconcile({'node-a': node()}, settings, self.lifecycle, NOW)
        self.assertEqual(result[0]['placement']['status'], 'applied')
        self.device.refresh_from_db(); self.assertEqual(self.device.site_id, self.other_site.pk)

    def test_global_mac_lookup_preserves_curated_foreign_assignment(self):
        interface = Interface.objects.create(device=self.outside, name='curated-port', type='other')
        address = MACAddress.objects.create(mac_address='AA:BB:CC:DD:EE:FF', assigned_object=interface, description='curated')
        hardware_reconcile(snapshot(), self.settings, self.hardware)
        address.refresh_from_db()
        self.assertEqual(address.assigned_object_id, interface.pk)
        self.assertEqual(address.description, 'curated')
        self.assertEqual(MACAddress.objects.count(), 1)
        self.assertEqual(self.hardware.listing('api/dcim/mac-addresses/', {'mac_address': '00:00:00:00:00:01'}), [])
        self.assertEqual(len(self.hardware.listing('api/dcim/mac-addresses/', {'mac_address': 'aa:bb:cc:dd:ee:ff'})), 1)
        with self.assertRaises(APIError) as caught:
            self.hardware.request('POST', 'api/dcim/module-bays/', {'device': self.outside.pk, 'name': 'forbidden-bay'})
        self.assertIn(caught.exception.status, (403, 404))
