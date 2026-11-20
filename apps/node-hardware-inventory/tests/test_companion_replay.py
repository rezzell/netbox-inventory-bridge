"""Run the actual main loop across invalidation, failed relist and restart."""
import contextlib
import io
import unittest
from unittest.mock import Mock, patch
from test_runtime import config, node, NOW, NetBoxFake
import companion
from runtime import APIError, InventoryError


class StopLoop(BaseException): pass


class CompanionReplayTests(unittest.TestCase):
    def invoke(self, kube, server, once=False, now=NOW):
        real = companion.reconcile
        observed = []
        def reconcile(nodes, settings):
            observed.append(set(nodes))
            return real(nodes, settings, server, now)
        with patch.object(companion, 'install_shutdown_handler'), patch.object(companion, 'load_config', return_value=config(False)), \
             patch.object(companion, 'Kubernetes', return_value=kube), patch.object(companion, 'reconcile', side_effect=reconcile), \
             patch.object(companion.time, 'sleep'), patch('sys.argv', ['companion', '--config', 'unused'] + (['--once'] if once else [])), \
             contextlib.redirect_stdout(io.StringIO()):
            if once: return companion.main(), observed
            with self.assertRaises(StopLoop): companion.main()
        return observed

    def test_full_loop_only_complete_list_can_remove_and_restart_preserves_timestamp(self):
        transport = Mock()
        transport.get.side_effect = [
            {'metadata': {'resourceVersion': '1'}, 'items': [node()]},
            {'metadata': {'resourceVersion': '2', 'continue': 'page2'}, 'items': []}, APIError(503),
            {'metadata': {'resourceVersion': '3'}, 'items': [node()]},
            {'metadata': {'resourceVersion': '4'}, 'items': []}, StopLoop()]
        transport.request.side_effect = [([{'type': 'DELETED', 'object': node(uid='stale')}], {}),
                                         ([{'type': 'ERROR', 'object': {'code': 410}}], {}), ([], {})]
        server = NetBoxFake()
        observations = self.invoke(companion.Kubernetes(config(False), transport), server)
        self.assertEqual(observations, [{'node-a'}, {'node-a'}, set()])
        patches = [call[2]['custom_fields']['kubernetes_lifecycle'] for call in server.calls if call[0] == 'PATCH']
        self.assertEqual([value['state'] for value in patches], ['ready', 'removed'])
        removed_at = server.device['custom_fields']['kubernetes_lifecycle']['removed_at']
        kube = Mock(); kube.list_nodes.return_value = ({}, '5')
        result, observations = self.invoke(kube, server, once=True, now=NOW + 3600)
        self.assertEqual(result, 0)
        self.assertEqual(server.device['custom_fields']['kubernetes_lifecycle']['removed_at'], removed_at)
        self.assertEqual(len([call for call in server.calls if call[0] == 'PATCH']), 2)
        kube.list_nodes.return_value = ({'node-a': node(uid='reused')}, '6')
        result, _ = self.invoke(kube, server, once=True, now=NOW + 3600)
        self.assertEqual(result, 1)
        self.assertEqual(server.device['custom_fields']['kubernetes_lifecycle']['state'], 'removed')
