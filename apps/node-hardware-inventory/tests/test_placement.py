# Modified for portable extraction: installation-specific examples use fictional identities.
"""Placement trust boundaries, preservation, hierarchy and conditional writes."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion import reconcile
from placement import plan, reconcile_placement, validate_placement
from runtime import APIError, InventoryError, load_config

NODE = {'metadata': {'name': 'node-a', 'uid': 'uid-a', 'labels': {'topology.kubernetes.io/zone': 'zone-a'},
                     'annotations': {'netbox.example.com/site-id': '1'}}}
DEVICE = {'id': 42, 'site': {'id': 2}, 'location': None, 'rack': None, 'cluster': None,
          'custom_fields': {'kubernetes_lifecycle': {'cluster_id': 'test', 'node_name': 'node-a', 'node_uid': 'uid-a'}}}
SETTINGS = {'enabled': True, 'dry_run': True, 'fields': {
    'site': {'annotation': 'netbox.example.com/site-id'},
    'location': {'labels': [{'key': 'topology.kubernetes.io/zone', 'values': {'zone-a': 3}}]}}}
CONFIG = {'cluster_id': 'test', 'dry_run': True, 'placement': SETTINGS, 'device_mappings': {'node-a': {'device_id': 42}},
          'grace_period_seconds': 300}


class Client:
    def __init__(self):
        self.calls = []
        self.device = copy.deepcopy(DEVICE)
        self.objects = {'api/dcim/sites/1/': {'id': 1, 'region': {'id': 7}},
                        'api/dcim/locations/3/': {'id': 3, 'site': {'id': 1}},
                        'api/dcim/racks/4/': {'id': 4, 'site': {'id': 1}, 'location': {'id': 3}},
                        'api/dcim/regions/7/': {'id': 7}}
        self.race = None

    def get(self, path):
        self.calls.append(('GET', path))
        if path == 'api/dcim/devices/42/':
            return copy.deepcopy(self.device)
        if path not in self.objects:
            raise APIError(404)
        return copy.deepcopy(self.objects[path])

    def request(self, method, path, payload=None, headers=None):
        if method == 'GET':
            return self.get(path), {'ETag': 'fixture-etag'}
        self.calls.append((method, path, payload, headers))
        if self.race:
            self.race(self)
            self.race = None
            raise APIError(412)
        for field, value in payload.items():
            if field == 'custom_fields':
                self.device[field].update(value)
            else:
                self.device[field] = {'id': value}
        return self.device, {}


class PlacementTests(unittest.TestCase):
    def run_plan(self, node=None, mapping=None, settings=None, client=None, device=None):
        return plan(node or NODE, mapping or {'device_id': 42}, settings or SETTINGS,
                    device or DEVICE, client or Client())

    def test_annotation_and_zone_translation(self):
        result = self.run_plan()
        self.assertEqual(result['desired'], {'site': 1, 'location': 3})
        self.assertEqual(result['changes'], {'site': {'from': 2, 'to': 1}, 'location': {'from': None, 'to': 3}})
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['sources']['location'], ['label:topology.kubernetes.io/zone'])

    def test_missing_metadata_preserves_existing_placement(self):
        result = self.run_plan(node={'metadata': {}})
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['changes'], {})

    def test_unmapped_invalid_and_conflicting_sources_block(self):
        for mutate, reason in [
            (lambda n, s: n['metadata']['labels'].update({'topology.kubernetes.io/zone': 'unknown'}), 'unmapped_label'),
            (lambda n, s: n['metadata']['annotations'].update({'netbox.example.com/site-id': '-1'}), 'invalid_annotation'),
            (lambda n, s: s['fields']['site'].update({'labels': [{'key': 'topology.kubernetes.io/zone', 'values': {'zone-a': 2}}]}), 'conflicting_sources')]:
            n, s = copy.deepcopy(NODE), copy.deepcopy(SETTINGS)
            mutate(n, s)
            result = self.run_plan(node=n, settings=s)
            self.assertEqual(result['status'], 'blocked')
            self.assertIn(reason, [i['reason'] for i in result['issues']])

    def test_configuration_override_is_explicit_and_highest_priority(self):
        result = self.run_plan(mapping={'device_id': 42, 'placement': {'site': 1}})
        self.assertEqual(result['sources']['site'], ['configuration'])

    def test_reference_missing_or_forbidden_never_writes(self):
        for status in [403, 404]:
            client = Client()
            original = client.get
            def get(path):
                if path == 'api/dcim/sites/1/':
                    raise APIError(status)
                return original(path)
            client.get = get
            result = self.run_plan(client=client)
            self.assertEqual(result['status'], 'blocked')
            self.assertIn('reference_unavailable', [i['reason'] for i in result['issues']])

    def test_retained_location_and_rack_cannot_cross_sites(self):
        device = copy.deepcopy(DEVICE)
        device['rack'] = {'id': 4}
        client = Client()
        client.objects['api/dcim/racks/4/']['site'] = {'id': 2}
        result = self.run_plan(device=device, client=client)
        self.assertIn('site_mismatch', [i['reason'] for i in result['issues']])

    def test_region_is_a_constraint_and_never_a_device_patch(self):
        settings = copy.deepcopy(SETTINGS)
        mapping = {'device_id': 42, 'placement': {'region': 7}}
        result = self.run_plan(mapping=mapping, settings=settings)
        self.assertNotIn('region', result['changes'])
        self.assertEqual(result['issues'], [])
        client = Client()
        client.objects['api/dcim/sites/1/']['region'] = {'id': 8}
        self.assertEqual(self.run_plan(mapping=mapping, client=client)['status'], 'blocked')

    def test_parent_placement_requires_review(self):
        device = {**DEVICE, 'parent_device': {'id': 9}}
        result = self.run_plan(device=device)
        self.assertEqual(result['status'], 'blocked')

    def test_existing_rack_position_is_not_reused_in_another_rack(self):
        device = {**DEVICE, 'position': 10}
        result = self.run_plan(mapping={'device_id': 42, 'placement': {'rack': 4}}, device=device)
        self.assertIn('rack_position_requires_manual_review', [i['reason'] for i in result['issues']])

    def test_rack_location_ancestor_and_mismatch(self):
        client = Client()
        client.objects['api/dcim/racks/4/']['location'] = {'id': 5}
        client.objects['api/dcim/locations/5/'] = {'id': 5, 'site': {'id': 1}, 'parent': {'id': 3}}
        mapping = {'device_id': 42, 'placement': {'rack': 4}}
        self.assertEqual(self.run_plan(client=client, mapping=mapping)['issues'], [])
        client.objects['api/dcim/locations/5/']['parent'] = None
        self.assertIn('location_mismatch', [i['reason'] for i in self.run_plan(client=client, mapping=mapping)['issues']])

    def test_missing_etag_blocks_apply(self):
        client = Client()
        original = client.request
        client.request = lambda method, *args, **kwargs: (original(method, *args, **kwargs)[0], {})
        config = copy.deepcopy(CONFIG)
        config['dry_run'] = config['placement']['dry_run'] = False
        with self.assertRaises(InventoryError):
            reconcile_placement(NODE, {'device_id': 42}, config, 'node-a', client, DEVICE)
        self.assertTrue(all(c[0] == 'GET' for c in client.calls))

    def test_lookup_failure_is_reported_and_other_nodes_continue(self):
        config = copy.deepcopy(CONFIG)
        config['device_mappings']['node-b'] = {'device_id': 43}
        second = copy.deepcopy(NODE)
        second['metadata'].update(name='node-b', uid='uid-b')
        client = Client()
        original = client.get
        def get(path):
            if path == 'api/dcim/devices/43/':
                device = copy.deepcopy(DEVICE)
                device['custom_fields']['kubernetes_lifecycle'].update(node_name='node-b', node_uid='uid-b')
                return device
            if path == 'api/dcim/sites/1/':
                raise APIError(503)
            return original(path)
        client.get = get
        results = reconcile({'node-a': NODE, 'node-b': second}, config, client, now=0)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r['placement']['status'] == 'blocked' for r in results))

    def test_removed_node_never_moves(self):
        client = Client()
        result = reconcile_placement(None, {'device_id': 42}, CONFIG, 'node-a', client, DEVICE)
        self.assertEqual(result['reason'], 'node_absent')
        self.assertEqual(client.calls, [])

    def test_independent_dry_run_and_minimal_conditional_patch(self):
        for global_dry, placement_dry in [(True, True), (True, False), (False, True), (False, False)]:
            config = copy.deepcopy(CONFIG)
            config['dry_run'] = global_dry
            config['placement']['dry_run'] = placement_dry
            client = Client()
            result = reconcile_placement(NODE, {'device_id': 42}, config, 'node-a', client, DEVICE)
            patches = [c for c in client.calls if c[0] == 'PATCH']
            self.assertEqual(len(patches), int(not (global_dry or placement_dry)))
            if patches:
                self.assertEqual(patches[0][2:], ({'site': 1, 'location': 3}, {'If-Match': 'fixture-etag'}))
                self.assertEqual(result['status'], 'applied')

    def test_lifecycle_writes_continue_while_placement_is_dry_run(self):
        config = copy.deepcopy(CONFIG)
        config['dry_run'] = False
        client = Client()
        result = reconcile({'node-a': NODE}, config, client, now=0)
        patches = [c[2] for c in client.calls if c[0] == 'PATCH']
        self.assertEqual(len(patches), 1)
        self.assertEqual(set(patches[0]), {'custom_fields'})
        self.assertEqual(set(patches[0]['custom_fields']), {'kubernetes_lifecycle'})
        self.assertEqual(result[0]['placement']['status'], 'proposed')
        self.assertTrue(result[0]['placement']['dry_run'])

    def test_identity_and_concurrent_identity_change_refuse_write(self):
        config = copy.deepcopy(CONFIG)
        config['dry_run'] = config['placement']['dry_run'] = False
        client = Client()
        def replace_uid(c):
            c.device['custom_fields']['kubernetes_lifecycle']['node_uid'] = 'replaced'
        client.race = replace_uid
        with self.assertRaises(InventoryError):
            reconcile_placement(NODE, {'device_id': 42}, config, 'node-a', client, DEVICE)
        self.assertEqual(len([c for c in client.calls if c[0] == 'PATCH']), 1)

    def test_conflict_replans_and_idempotence(self):
        config = copy.deepcopy(CONFIG)
        config['dry_run'] = config['placement']['dry_run'] = False
        client = Client()
        def concurrent(c):
            c.device['site'] = {'id': 1}
            c.device['location'] = {'id': 3}
        client.race = concurrent
        result = reconcile_placement(NODE, {'device_id': 42}, config, 'node-a', client, DEVICE)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(len([c for c in client.calls if c[0] == 'PATCH']), 1)

    def test_companion_dry_run_uses_get_only_and_validates_durable_uid(self):
        client = Client()
        result = reconcile({'node-a': NODE}, CONFIG, client, now=0)
        self.assertEqual(result[0]['placement']['status'], 'proposed')
        self.assertTrue(all(c[0] == 'GET' for c in client.calls))
        client.device['custom_fields']['kubernetes_lifecycle']['node_uid'] = 'other'
        with self.assertRaises(InventoryError):
            reconcile({'node-a': NODE}, CONFIG, client, now=0)

    def test_configuration_validation(self):
        for value in [{'enabled': 'false'}, {'dry_run': 0}, {'fields': {'serial': {}}},
                      {'fields': {'site': {'labels': [{'key': 'x', 'values': {'a': True}}]}}},
                      {'fields': {'rack': {'annotation': ''}}}, {'unknown': True}]:
            with self.subTest(value=value), self.assertRaises(InventoryError):
                validate_placement(value, {})
        with self.assertRaises(InventoryError):
            validate_placement({}, {'n': {'placement': {'site': None}}})
        with tempfile.NamedTemporaryFile(mode='w+') as stream:
            json.dump({'cluster_id': 'test'}, stream)
            stream.flush()
            self.assertEqual(load_config(stream.name)['placement'], {'enabled': False, 'dry_run': True, 'fields': {}})


if __name__ == '__main__':
    unittest.main()
