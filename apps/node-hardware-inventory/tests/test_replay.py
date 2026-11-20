"""Ambiguous creation and fresh-caller recovery after every native boundary."""
import copy
import unittest
from unittest.mock import Mock
from test_runtime import NetBoxFake, config, snapshot
from agent import ensure, reconcile
from runtime import APIError, InventoryError


class ReplayTests(unittest.TestCase):
    def test_post_ambiguity_has_one_post_and_one_relookup(self):
        for status in (0, 400, 409, 429, 503):
            for outcome in ('uncommitted', 'owned', 'foreign', 'multiple', 'catalog'):
                with self.subTest(status=status, outcome=outcome):
                    row = {'id': 1, 'description': 'foreign' if outcome == 'foreign' else 'ours'}
                    matches = [] if outcome == 'uncommitted' else [row, row] if outcome == 'multiple' else [row]
                    client = Mock()
                    client.listing.side_effect = [[], matches]
                    client.request.side_effect = APIError(status)
                    args = (client, 'interfaces', {'name': 'eth0'}, {'name': 'eth0'}, 'ours')
                    if outcome in ('uncommitted', 'multiple'):
                        with self.assertRaises(InventoryError): ensure(*args)
                    else:
                        self.assertEqual(ensure(*args, catalog=outcome == 'catalog'), None if outcome == 'foreign' else row)
                    self.assertEqual(client.request.call_count, 1)
                    self.assertEqual(client.request.call_args.args[0], 'POST')
                    self.assertEqual(client.listing.call_count, 2)

    def test_interrupted_committed_writes_converge_without_duplicates(self):
        for endpoint in ('module-bays', 'module-types', 'modules', 'interfaces', 'mac-addresses', 'devices'):
            with self.subTest(endpoint=endpoint):
                server = NetBoxFake()
                original = server.request
                interrupted = []
                def request(method, path, payload=None, headers=None):
                    result = original(method, path, payload, headers)
                    if method in ('POST', 'PATCH') and ('/' + endpoint + '/') in path and not interrupted:
                        interrupted.append(True)
                        # Non-retryable transport status leaves the committed state visible to replay.
                        raise APIError(418)
                    return result
                server.request = request
                with self.assertRaises(APIError): reconcile(snapshot(), config(False), server)
                self.assertTrue(interrupted)
                committed = copy.deepcopy(server.tables)
                # A fresh caller/client view retains only durable server state.
                fresh = NetBoxFake()
                fresh.tables, fresh.device = server.tables, server.device
                reconcile(snapshot(), config(False), fresh)
                counts = {path: len(rows) for path, rows in fresh.tables.items()}
                state = copy.deepcopy((fresh.tables, fresh.device))
                reconcile(snapshot(), config(False), fresh)
                self.assertEqual(state, (fresh.tables, fresh.device))
                self.assertTrue(all(count == 1 for count in counts.values()))
                for path, rows in committed.items():
                    self.assertEqual(rows, fresh.tables[path])
                self.assertEqual(fresh.device['custom_fields']['curated'], 'preserve')
                self.assertFalse(any(call[0] in ('PATCH', 'DELETE') for call in fresh.calls) if endpoint == 'devices' else
                                 any(call[0] == 'DELETE' for call in fresh.calls))

    def test_lost_patch_response_is_not_blindly_retried(self):
        server = NetBoxFake()
        original = server.request
        def request(method, path, payload=None, headers=None):
            result = original(method, path, payload, headers)
            if method == 'PATCH': raise APIError(0)
            return result
        server.request = request
        with self.assertRaises(APIError): reconcile(snapshot(), config(False), server)
        self.assertEqual(sum(call[0] == 'PATCH' for call in server.calls), 1)
        server.request = original
        reconcile(snapshot(), config(False), server)
        self.assertEqual(sum(call[0] == 'PATCH' for call in server.calls), 1)
