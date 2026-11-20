"""Deterministically ordered writers, with no timing-based race assumptions."""
import copy
import threading
import unittest
from test_runtime import config, snapshot, node, NOW
from support import VersionedNetBox
from agent import serial_validator
from companion import reconcile as lifecycle_reconcile
from placement import reconcile_placement
from runtime import APIError, InventoryError, update_field


class ConcurrencyTests(unittest.TestCase):
    def race(self, winner, placement=False):
        server = VersionedNetBox()
        settings = config(False)
        settings['placement'] = {'enabled': True, 'dry_run': False, 'fields': {}}
        reads = threading.Barrier(2, timeout=5)
        committed = threading.Event()
        failures = []
        values = {key: {'cluster_id': 'test-cluster', 'node_name': 'node-a', 'node_uid': 'uid-1'}
                  for key in ('hardware_observation', 'kubernetes_lifecycle')}

        class View:
            def __init__(self, name): self.name, self.first = name, True
            def get(self, path): return server.get(path)
            def request(self, method, path, payload=None, headers=None):
                if method == 'GET' and self.first:
                    self.first = False
                    result = server.request(method, path)
                    reads.wait()
                    return result
                if method == 'PATCH' and self.name != winner:
                    if not committed.wait(5): raise AssertionError('winner failed to commit')
                result = server.request(method, path, payload, headers)
                if method == 'PATCH' and self.name == winner: committed.set()
                return result

        def run(name):
            try:
                view = View(name)
                if name == 'placement':
                    reconcile_placement(node(), {'device_id': 42, 'placement': {'site': 1}}, settings,
                                        'node-a', view, copy.deepcopy(server.device))
                else:
                    update_field(view, 42, name, values[name], settings, 'node-a')
            except BaseException as error:
                failures.append(error)
                committed.set()
                reads.abort()

        server.objects['api/dcim/sites/1/'] = {'id': 1}
        names = ['placement', 'kubernetes_lifecycle'] if placement else list(values)
        threads = [threading.Thread(target=run, args=(name,), daemon=True) for name in names]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        patches = [call for call in server.attempted if call[0] == 'PATCH']
        self.assertEqual(len(patches), 3)
        self.assertEqual(len(server.committed), 2)
        self.assertEqual(server.version, 3)
        self.assertEqual(patches[0][3], patches[1][3])
        self.assertNotEqual(patches[1][3], patches[2][3])
        self.assertEqual(server.device['custom_fields']['curated'], 'preserve')
        for name in names:
            if name == 'placement':
                self.assertEqual(server.device['site'], {'id': 1})
                self.assertIn({'site': 1}, server.committed)
            else: self.assertEqual(server.device['custom_fields'][name], values[name])

    def test_both_hardware_lifecycle_winner_orders(self):
        for winner in ('hardware_observation', 'kubernetes_lifecycle'):
            with self.subTest(winner=winner): self.race(winner)

    def test_placement_coexists_with_lifecycle_in_both_orders(self):
        for winner in ('placement', 'kubernetes_lifecycle'):
            with self.subTest(winner=winner): self.race(winner, True)

    def test_retry_bounds_missing_etag_and_revalidation(self):
        for conflict in ('repeat', 'serial', 'owner', 'missing-etag', 'uid'):
            with self.subTest(conflict=conflict):
                server = VersionedNetBox()
                original = server.request
                attempts = []
                def request(method, path, payload=None, headers=None):
                    if method == 'PATCH':
                        attempts.append(payload)
                        if conflict == 'serial': server.device['serial'] = 'changed'
                        if conflict == 'owner': server.device['custom_fields']['hardware_observation'] = {'cluster_id': 'foreign'}
                        if conflict == 'uid': server.device['custom_fields']['kubernetes_lifecycle'] = {
                            'cluster_id': 'test-cluster', 'node_name': 'node-a', 'node_uid': 'changed'}
                        raise APIError(412)
                    result, response_headers = original(method, path, payload, headers)
                    return result, {} if conflict == 'missing-etag' else response_headers
                server.request = request
                with self.assertRaises(InventoryError):
                    if conflict == 'uid': lifecycle_reconcile({'node-a': node()}, config(False), server, NOW)
                    else:
                        _, validate = serial_validator(snapshot(), config(False)['device_mappings']['node-a'])
                        update_field(server, 42, 'hardware_observation', {'cluster_id': 'test-cluster', 'node_name': 'node-a'},
                                     config(False), 'node-a', validate)
                self.assertEqual(len(attempts), 3 if conflict == 'repeat' else 0 if conflict == 'missing-etag' else 1)
                self.assertEqual(server.committed, [])

    def test_reads_are_detached_and_stale_patch_does_not_advance_version(self):
        server = VersionedNetBox()
        value, headers = server.request('GET', 'api/dcim/devices/42/')
        value['custom_fields']['curated'] = 'changed'
        self.assertEqual(server.device['custom_fields']['curated'], 'preserve')
        server.request('PATCH', 'api/dcim/devices/42/', {'site': 1}, {'If-Match': headers['ETag']})
        with self.assertRaises(APIError):
            server.request('PATCH', 'api/dcim/devices/42/', {'site': 3}, {'If-Match': headers['ETag']})
        self.assertEqual(server.version, 2)

    def test_module_retries_are_bounded_and_recheck_ownership(self):
        from agent import ensure
        from unittest.mock import Mock
        for change in (False, True):
            with self.subTest(ownership_changes=change):
                client = Mock()
                row = {'id': 8, 'description': 'ours', 'serial': 'old', 'module_type': 1}
                client.listing.return_value = [row]
                attempts = []
                def request(method, path, payload=None, headers=None):
                    if method == 'GET': return dict(row, description='foreign' if change and attempts else 'ours'), {'ETag': 'v1'}
                    attempts.append(payload)
                    self.assertEqual(payload, {'serial': 'new'})
                    raise APIError(412)
                client.request.side_effect = request
                with self.assertRaises(InventoryError):
                    ensure(client, 'modules', {}, {'serial': 'new', 'asset_tag': 'ignore'}, 'ours')
                self.assertEqual(len(attempts), 1 if change else 3)

    def test_placement_repeated_conflicts_and_uid_change_fail_closed(self):
        for change in (False, True):
            with self.subTest(uid_changes=change):
                server = VersionedNetBox()
                server.objects['api/dcim/sites/1/'] = {'id': 1}
                settings = config(False)
                settings['placement'] = {'enabled': True, 'dry_run': False, 'fields': {}}
                original = server.request
                attempts = []
                def request(method, path, payload=None, headers=None):
                    if method == 'PATCH':
                        attempts.append(payload)
                        if change:
                            server.device['custom_fields']['kubernetes_lifecycle'] = {
                                'cluster_id': 'test-cluster', 'node_name': 'node-a', 'node_uid': 'reused'}
                        raise APIError(412)
                    return original(method, path, payload, headers)
                server.request = request
                with self.assertRaises(InventoryError):
                    reconcile_placement(node(), {'device_id': 42, 'placement': {'site': 1}}, settings,
                                        'node-a', server, copy.deepcopy(server.device))
                self.assertEqual(attempts, [{'site': 1}] * (1 if change else 3))
