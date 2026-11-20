import copy
import io
import json
import pathlib
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from agent import reconcile as hardware_reconcile, ensure
from collector import SCHEMA
from companion import Kubernetes, lifecycle, reconcile as lifecycle_reconcile
from runtime import APIError, HTTPSClient, InventoryError, NoRedirect, update_field, load_config, marker


def config(dry=True):
    return {'cluster_id': 'test-cluster', 'dry_run': dry, 'device_mappings': {'node-a': {'device_id': 42}},
            'netbox_url': 'https://netbox.invalid', 'token_file': '/unreadable-token', 'grace_period_seconds': 300,
            'relist_seconds': 300, 'watch_timeout_seconds': 60}


def snapshot():
    return {'schema_version': SCHEMA, 'node': 'node-a', 'observed_at': '2026-01-01T00:00:00+00:00', 'errors': [],
            'sections': {'system': {'data': {'product_serial': 'fixture-device'}},
                         'memory': {'data': {'cpu_sockets': [{'socket': 'CPU1', 'populated': True, 'manufacturer': 'Vendor', 'model': 'CPU'}],
                                             'dimms': []}}, 'interfaces': {'data': [{'name': 'eth0', 'mac_address': 'aa:bb:cc:dd:ee:ff'}]},
                         'disks': {'data': []}}}


def node(ready='True', uid='uid-1', transition='2026-01-01T00:00:00Z', cordon=False):
    return {'metadata': {'name': 'node-a', 'uid': uid}, 'spec': {'unschedulable': cordon},
            'status': {'conditions': [{'type': 'Ready', 'status': ready, 'lastTransitionTime': transition}]}}


NOW = 1767226200  # 2026-01-01 00:10 UTC


class Response(io.BytesIO):
    def __init__(self, value, headers=None, status=200):
        super().__init__(json.dumps(value).encode())
        self.headers, self.status = headers or {}, status


class TransportTests(unittest.TestCase):
    def test_sigterm_exits_cleanly_during_sleep(self):
        source = pathlib.Path(__file__).resolve().parents[1]
        process = subprocess.Popen(
            [sys.executable, '-u', '-c',
             'from runtime import install_shutdown_handler; import time; '
             'install_shutdown_handler(); print("ready"); time.sleep(60)'],
            cwd=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_tls_verified_redirect_rejected_and_origins_rejected(self):
        client = HTTPSClient('https://netbox.invalid/prefix/', '/unused')
        handler = next(h for h in client.opener.handlers if hasattr(h, '_context'))
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)
        for url in ('http://netbox.invalid/prefix/api/', 'https://other.invalid/prefix/api/', '/api/', '//other.invalid/api/'):
            with self.assertRaises(InventoryError):
                client.url(url)
        with self.assertRaises(APIError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')
        with self.assertRaises(InventoryError):
            HTTPSClient('http://netbox.invalid', '/unused')

    def test_rotation_legacy_and_v2_token_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            token = pathlib.Path(directory) / 'token'
            token.write_text('legacy-fixture')
            opener = Mock()
            opener.open.side_effect = [Response({}), Response({}), urllib.error.URLError('SECRET token body')]
            client = HTTPSClient('https://netbox.invalid', str(token), opener=opener, sleep=lambda _: None)
            client.get('api/')
            token.write_text('nbt_fixture.key')
            client.get('api/')
            calls = opener.open.call_args_list
            self.assertEqual(calls[0].args[0].headers['Authorization'], 'Token legacy-fixture')
            self.assertEqual(calls[1].args[0].headers['Authorization'], 'Bearer nbt_fixture.key')
            opener.open.side_effect = urllib.error.URLError('SECRET token body')
            with self.assertRaises(APIError) as caught:
                client.get('api/')
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertNotIn('nbt_', str(caught.exception))

    def test_no_blind_post_retry_and_get_bounded_backoff(self):
        with tempfile.NamedTemporaryFile(mode='w') as token:
            token.write('fixture'); token.flush()
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError('https://netbox.invalid', 503, 'sensitive', {}, None)
            sleep = Mock()
            client = HTTPSClient('https://netbox.invalid', token.name, opener=opener, sleep=sleep)
            with self.assertRaises(APIError): client.request('POST', 'api/', {})
            self.assertEqual(opener.open.call_count, 1)
            with self.assertRaises(APIError): client.get('api/')
            self.assertEqual(opener.open.call_count, 4)
            self.assertEqual(sleep.call_count, 2)

    def test_cross_origin_pagination_and_incomplete_list(self):
        client = HTTPSClient('https://netbox.invalid', '/unused')
        client.get = Mock(return_value={'results': [], 'count': 1, 'next': 'https://evil.invalid/api/'})
        with self.assertRaises(InventoryError): client.listing('api/')
        self.assertEqual(client.get.call_count, 1)
        client.get.return_value = {'results': [], 'count': 1, 'next': None}
        with self.assertRaises(InventoryError): client.listing('api/')

    def test_json_and_watch_response_byte_bound(self):
        with tempfile.NamedTemporaryFile(mode='w') as token:
            token.write('fixture'); token.flush()
            for lines in (False, True):
                response = Response({})
                response.read = Mock(return_value=b' ' * (16 * 1024 * 1024 + 1))
                opener = Mock()
                opener.open.return_value = response
                client = HTTPSClient('https://netbox.invalid', token.name, opener=opener)
                with self.assertRaisesRegex(InventoryError, 'response too large'):
                    client.request('GET', 'api/', lines=lines)
                response.read.assert_called_once_with(16 * 1024 * 1024 + 1)


class NetBoxFake:
    def __init__(self):
        self.device = {'serial': 'fixture-device', 'custom_fields': {'hardware_observation': None, 'kubernetes_lifecycle': None, 'curated': 'preserve'}}
        self.tables, self.calls = {}, []
        self.fail_conflict_once = False

    def get(self, path):
        return copy.deepcopy(self.device)

    def listing(self, path, filters):
        rows = self.tables.get(path, [])
        def matches(row):
            for key, value in filters.items():
                field = key[:-3] if key.endswith('_id') else key
                actual = row.get(field)
                if isinstance(actual, dict): actual = actual['id']
                if actual != value: return False
            return True
        return [copy.deepcopy(row) for row in rows if matches(row)]

    def request(self, method, path, payload=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(payload), headers))
        if method == 'GET':
            if path.startswith('api/dcim/devices/'):
                return copy.deepcopy(self.device), {'ETag': 'W/"fixture"'}
            endpoint, identity = path.rstrip('/').rsplit('/', 1)
            row = next(row for row in self.tables[endpoint + '/'] if row['id'] == int(identity))
            return copy.deepcopy(row), {'ETag': 'W/"fixture"'}
        if method == 'POST':
            row = {'id': 1 + sum(len(rows) for rows in self.tables.values()), **payload}
            self.tables.setdefault(path, []).append(row)
            if self.fail_conflict_once:
                self.fail_conflict_once = False
                raise APIError(400)
            return copy.deepcopy(row), {}
        if method == 'PATCH':
            self.assert_conditional(headers)
            if 'custom_fields' in payload:
                self.device['custom_fields'].update(payload['custom_fields'])
            else:
                endpoint, identity = path.rstrip('/').rsplit('/', 1)
                row = next(row for row in self.tables[endpoint + '/'] if row['id'] == int(identity))
                row.update(payload)
            return {}, {}
        raise AssertionError(method)

    def assert_conditional(self, headers):
        assert headers and headers['If-Match'] == 'W/"fixture"'


class HardwareTests(unittest.TestCase):
    def test_offline_dryrun_no_client_or_token_even_mapped(self):
        client = Mock()
        report = hardware_reconcile(snapshot(), config(), client)
        self.assertIn('plan', report)
        self.assertFalse(client.mock_calls)
        settings = config(); settings['device_mappings'] = {}
        self.assertIn('skipped', hardware_reconcile(snapshot(), settings, client))
        settings['dry_run'] = False
        with self.assertRaises(InventoryError): hardware_reconcile(snapshot(), settings, client)

    def test_missing_mismatched_and_expected_serial_block_every_write(self):
        for serial in ('', 'different', 'Unknown'):
            client = NetBoxFake(); client.device['serial'] = serial
            with self.assertRaises(InventoryError): hardware_reconcile(snapshot(), config(False), client)
            self.assertFalse(client.calls)
        settings = config(False); settings['device_mappings']['node-a']['expected_serial'] = 'other'
        client = NetBoxFake()
        with self.assertRaises(InventoryError): hardware_reconcile(snapshot(), settings, client)
        self.assertFalse(client.calls)

    def test_explicit_board_serial_works_with_placeholder_or_shared_product(self):
        for product in ('0123456789', 'shared-chassis'):
            with self.subTest(product=product):
                report = snapshot()
                report['sections']['system']['data'].update(product_serial=product, board_serial='fixture-board')
                settings = config(False)
                settings['device_mappings']['node-a'].update(serial_source='board_serial', expected_serial='fixture-board')
                client = NetBoxFake(); client.device['serial'] = 'fixture-board'
                hardware_reconcile(report, settings, client)
                observation = client.device['custom_fields']['hardware_observation']
                self.assertEqual(observation['serial'], 'fixture-board')
                self.assertEqual(observation['serial_source'], 'board_serial')

    def test_default_serial_source_never_falls_back_to_board(self):
        for product in (None, '', 'Unknown', '0123456789'):
            with self.subTest(product=product):
                report = snapshot()
                report['sections']['system']['data'].update(product_serial=product, board_serial='fixture-device')
                client = NetBoxFake()
                with self.assertRaises(InventoryError): hardware_reconcile(report, config(False), client)
                self.assertFalse(client.calls)

    def test_board_identity_missing_or_mismatched_blocks_every_write(self):
        for board, expected, device in ((None, 'fixture-board', 'fixture-board'),
                                        ('0123456789', 'fixture-board', 'fixture-board'),
                                        ('fixture-board', 'fixture-device', 'fixture-board'),
                                        ('fixture-board', '0123456789', 'fixture-board'),
                                        ('fixture-board', 'fixture-board', 'fixture-device')):
            with self.subTest(board=board, expected=expected, device=device):
                report = snapshot(); report['sections']['system']['data']['board_serial'] = board
                settings = config(False)
                settings['device_mappings']['node-a'].update(serial_source='board_serial', expected_serial=expected)
                client = NetBoxFake(); client.device['serial'] = device
                with self.assertRaises(InventoryError): hardware_reconcile(report, settings, client)
                self.assertFalse(client.calls)

    def test_serial_source_configuration(self):
        for source in ('product_serial', 'board_serial', 'bogus', None, 42, []):
            with self.subTest(source=source):
                settings = config(); settings['device_mappings']['node-a']['serial_source'] = source
                with tempfile.NamedTemporaryFile(mode='w') as stream:
                    json.dump(settings, stream); stream.flush()
                    if source in ('product_serial', 'board_serial'):
                        self.assertEqual(load_config(stream.name)['device_mappings']['node-a']['serial_source'], source)
                    else:
                        with self.assertRaises(InventoryError): load_config(stream.name)
                        client = NetBoxFake()
                        with self.assertRaises(InventoryError): hardware_reconcile(snapshot(), settings, client)
                        self.assertFalse(client.calls)
        settings = config()
        with tempfile.NamedTemporaryFile(mode='w') as stream:
            json.dump(settings, stream); stream.flush()
            self.assertNotIn('serial_source', load_config(stream.name)['device_mappings']['node-a'])
        client = NetBoxFake()
        hardware_reconcile(snapshot(), config(False), client)
        self.assertEqual(client.device['custom_fields']['hardware_observation']['serial_source'], 'product_serial')

    def test_idempotence_catalog_race_and_native_payloads(self):
        client = NetBoxFake(); client.fail_conflict_once = True
        settings = config(False)
        hardware_reconcile(snapshot(), settings, client)
        creates = len([call for call in client.calls if call[0] == 'POST'])
        hardware_reconcile(snapshot(), settings, client)
        self.assertEqual(creates, len([call for call in client.calls if call[0] == 'POST']))
        module = client.tables['api/dcim/modules/'][0]
        self.assertIsInstance(module['module_bay'], int)
        self.assertIsInstance(module['module_type'], int)
        self.assertFalse(module['replicate_components'])
        self.assertFalse(module['adopt_components'])
        mac = client.tables['api/dcim/mac-addresses/'][0]
        self.assertEqual(mac['assigned_object_type'], 'dcim.interface')
        self.assertEqual(client.device['custom_fields']['curated'], 'preserve')

    def test_curated_records_and_catalog_attributes_preserved(self):
        client = NetBoxFake()
        client.tables['api/dcim/module-bays/'] = [{'id': 7, 'device': 42, 'name': 'CPU CPU1', 'description': 'curated'}]
        client.tables['api/dcim/interfaces/'] = [{'id': 8, 'device': 42, 'name': 'eth0', 'description': 'curated', 'type': '1000base-t'}]
        hardware_reconcile(snapshot(), config(False), client)
        self.assertNotIn('api/dcim/modules/', client.tables)
        self.assertNotIn('api/dcim/mac-addresses/', client.tables)
        self.assertEqual(client.tables['api/dcim/interfaces/'][0]['description'], 'curated')

    def test_create_conflict_relookup_preserves_foreign_owner(self):
        client = Mock()
        client.listing.side_effect = [[], [{'id': 8, 'description': 'curated'}]]
        client.request.side_effect = APIError(409)
        result = ensure(client, 'interfaces', {'device_id': 42, 'name': 'eth0'},
                        {'device': 42, 'name': 'eth0', 'description': 'ours'}, 'ours')
        self.assertIsNone(result)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args[0], 'POST')

    def test_owned_module_updates_only_observed_fields(self):
        client = NetBoxFake(); settings = config(False)
        hardware_reconcile(snapshot(), settings, client)
        row = client.tables['api/dcim/modules/'][0]
        row.update(serial='old', asset_tag='curated', status='offline')
        hardware_reconcile(snapshot(), settings, client)
        self.assertEqual(row['serial'], '')
        self.assertEqual(row['asset_tag'], 'curated')
        self.assertEqual(row['status'], 'offline')

    def test_cf_etag_retry_preserves_other_owner_and_fails_closed(self):
        client = NetBoxFake(); settings = config(False)
        original = client.request
        fail = [True]
        def request(method, path, payload=None, headers=None):
            if method == 'PATCH' and fail[0]:
                fail[0] = False
                client.device['custom_fields']['kubernetes_lifecycle'] = {'external': 'changed'}
                raise APIError(412)
            return original(method, path, payload, headers)
        client.request = request
        value = {'cluster_id': 'test-cluster', 'node_name': 'node-a'}
        update_field(client, 42, 'hardware_observation', value, settings, 'node-a')
        self.assertEqual(client.device['custom_fields']['kubernetes_lifecycle'], {'external': 'changed'})
        client.device['custom_fields']['hardware_observation'] = {'cluster_id': 'other', 'node_name': 'node-a'}
        with self.assertRaises(InventoryError): update_field(client, 42, 'hardware_observation', value, settings, 'node-a')
        client.request = Mock(return_value=({'custom_fields': {'hardware_observation': None}}, {}))
        with self.assertRaises(InventoryError): update_field(client, 42, 'hardware_observation', value, settings, 'node-a')


class LifecycleTests(unittest.TestCase):
    def test_ready_false_grace_unknown_recovery_and_cordon(self):
        settings = config()
        self.assertEqual(lifecycle(node('False'), None, settings, 'node-a', NOW)['state'], 'not_ready')
        self.assertEqual(lifecycle(node('False'), None, settings, 'node-a', NOW - 500)['state'], 'pending')
        old = lifecycle(node('Unknown'), None, settings, 'node-a', NOW)
        self.assertEqual(old['state'], 'unreachable')
        recovered = lifecycle(node(cordon=True), old, settings, 'node-a', NOW)
        self.assertEqual(recovered['state'], 'ready')
        self.assertTrue(recovered['unschedulable'])

    def test_restart_missing_removes_owned_membership_only(self):
        client = NetBoxFake(); settings = config(False)
        lifecycle_reconcile({'node-a': node()}, settings, client, NOW)
        lifecycle_reconcile({}, settings, client, NOW + 60)
        value = client.device['custom_fields']['kubernetes_lifecycle']
        self.assertEqual(value['state'], 'removed')
        self.assertIsNotNone(value['removed_at'])
        self.assertEqual(client.device['serial'], 'fixture-device')
        self.assertIsNone(client.device['custom_fields']['hardware_observation'])
        self.assertIsNone(lifecycle(None, None, settings, 'node-a', NOW))

    def test_uid_reuse_and_cross_cluster_refused(self):
        previous = lifecycle(node(), None, config(), 'node-a', NOW)
        with self.assertRaises(InventoryError): lifecycle(node(uid='uid-new'), previous, config(), 'node-a', NOW)
        previous['cluster_id'] = 'other'
        with self.assertRaises(InventoryError): lifecycle(None, previous, config(), 'node-a', NOW)

    def test_dryrun_empty_mappings_observes_without_netbox(self):
        settings = config(); settings['device_mappings'] = {}
        client = Mock()
        result = lifecycle_reconcile({'node-a': node('Unknown')}, settings, client, NOW)
        self.assertEqual(result[0]['state'], 'unreachable')
        self.assertFalse(client.mock_calls)

    def test_paginated_list_complete_then_outage_and_410(self):
        transport = Mock()
        other = node(uid='uid-other'); other['metadata']['name'] = 'node-b'
        transport.get.side_effect = [
            {'metadata': {'resourceVersion': '123', 'continue': 'next'}, 'items': [node()]},
            {'metadata': {'resourceVersion': '123'}, 'items': [other]}]
        kube = Kubernetes(config(), transport)
        nodes, version = kube.list_nodes()
        self.assertEqual(set(nodes), {'node-a', 'node-b'})
        self.assertEqual(version, '123')
        self.assertIn('continue=next', transport.get.call_args.args[0])
        transport.get.side_effect = [
            {'metadata': {'resourceVersion': '123', 'continue': 'next'}, 'items': [node()]}, APIError(503)]
        with self.assertRaises(APIError): kube.list_nodes()
        transport.request.return_value = ([{'type': 'ERROR', 'object': {'code': 410}}], {})
        with self.assertRaises(APIError) as caught: kube.watch('123')
        self.assertEqual(caught.exception.status, 410)
        # A stale DELETED event has no direct reconciliation path; complete list wins.
        transport.request.return_value = ([{'type': 'DELETED', 'object': node(uid='old')}], {})
        kube.watch('123')
        current = lifecycle(nodes['node-a'], None, config(), 'node-a', NOW)
        self.assertEqual(current['state'], 'ready')

    def test_incomplete_or_ambiguous_list_never_returns_authority(self):
        transport = Mock(); kube = Kubernetes(config(), transport)
        for page in ({'items': []}, {'metadata': {'resourceVersion': '1'}, 'items': [node(), node()]}, {'metadata': {'resourceVersion': '1'}}):
            transport.get.side_effect = None; transport.get.return_value = page
            with self.assertRaises(InventoryError): kube.list_nodes()

    def test_configuration_duplicate_mapping_rejected(self):
        settings = config(); settings['device_mappings']['node-b'] = {'device_id': 42}
        with tempfile.NamedTemporaryFile(mode='w') as stream:
            json.dump(settings, stream); stream.flush()
            with self.assertRaises(InventoryError): load_config(stream.name)


if __name__ == '__main__':
    unittest.main()
