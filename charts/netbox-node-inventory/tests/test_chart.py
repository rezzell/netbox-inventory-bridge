#!/usr/bin/env python3
# Modified for portable extraction: installation-specific examples use fictional identities.
"""Structural Helm render checks. Requires Helm and system Ruby (YAML stdlib)."""
import itertools
import json
from pathlib import Path
import subprocess
import unittest

CHART = Path(__file__).resolve().parents[1]


def render(*settings, release='inventory', namespace='inventory-test', valid=True, kube_version='1.34.0'):
    command = ['helm', 'template', release, str(CHART), '--namespace', namespace,
               '--set', 'clusterId=example-cluster', '--kube-version', kube_version]
    for setting in settings:
        command += ['--set', setting]
    result = subprocess.run(command, text=True, capture_output=True)
    if not valid:
        if result.returncode == 0:
            raise AssertionError('invalid configuration rendered successfully')
        return result.stderr
    if result.returncode:
        raise AssertionError(result.stderr)
    parsed = subprocess.run(
        ['ruby', '-ryaml', '-rjson', '-e',
         'puts JSON.generate(YAML.load_stream(STDIN.read).compact)'],
        input=result.stdout, text=True, capture_output=True, check=True)
    return json.loads(parsed.stdout)


def one(documents, kind, component=None):
    matches = [item for item in documents if item['kind'] == kind and
               (component is None or item['metadata']['labels'].get(
                   'app.kubernetes.io/component') == component)]
    if len(matches) != 1:
        raise AssertionError(f'expected one {kind}/{component}: {matches}')
    return matches[0]


class ChartTests(unittest.TestCase):
    def test_agent_requires_stable_recursive_read_only_mounts(self):
        for version in ['1.29.0', '1.32.0', '1.33.0-rc.1']:
            with self.subTest(version=version):
                self.assertIn('agent requires Kubernetes >=1.33.0',
                              render(kube_version=version, valid=False))
                render('agent.enabled=false', kube_version=version)
                render('agent.enabled=false', 'companion.enabled=false', kube_version=version)
        for version in ['1.33.0', '1.34.0']:
            render(kube_version=version)

    def test_independent_enablement_matrix(self):
        for agent, companion, openshift in itertools.product([False, True], repeat=3):
            with self.subTest(agent=agent, companion=companion, openshift=openshift):
                documents = render(f'agent.enabled={str(agent).lower()}',
                                   f'companion.enabled={str(companion).lower()}',
                                   f'openshift.enabled={str(openshift).lower()}')
                kinds = [doc['kind'] for doc in documents]
                self.assertEqual(kinds.count('DaemonSet'), int(agent))
                self.assertEqual(kinds.count('Deployment'), int(companion))
                self.assertEqual(kinds.count('SecurityContextConstraints'), int(agent and openshift))
                self.assertEqual(kinds.count('ServiceAccount'), int(agent) + int(companion))
                self.assertEqual(kinds.count('ConfigMap'), int(agent) + int(companion))
                self.assertEqual(kinds.count('ClusterRoleBinding'), int(companion))
                self.assertEqual(kinds.count('RoleBinding'), int(agent and openshift))
                self.assertEqual(kinds.count('ClusterRole'), int(companion) + int(agent and openshift))
                self.assertNotIn('Namespace', kinds)
                for item in documents:
                    if not agent:
                        self.assertNotEqual(item['metadata']['labels'].get('app.kubernetes.io/component'), 'agent')
                    if not companion:
                        self.assertNotEqual(item['metadata']['labels'].get('app.kubernetes.io/component'), 'companion')

    def test_confinement_and_runtime_contract(self):
        documents = render('openshift.enabled=true')
        for component, kind in [('agent', 'DaemonSet'), ('companion', 'Deployment')]:
            workload = one(documents, kind)
            template = workload['spec']['template']
            pod = template['spec']
            container = pod['containers'][0]
            self.assertEqual(pod['automountServiceAccountToken'], component == 'companion')
            for flag in ['hostNetwork', 'hostPID', 'hostIPC']:
                self.assertIs(pod[flag], False)
            self.assertEqual(pod['securityContext']['seccompProfile'], {'type': 'RuntimeDefault'})
            self.assertEqual(pod['nodeSelector']['kubernetes.io/os'], 'linux')
            self.assertEqual(pod['nodeSelector']['kubernetes.io/arch'], 'amd64')
            self.assertEqual(container['securityContext'], {
                'privileged': False, 'allowPrivilegeEscalation': False,
                'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}})
            self.assertEqual(container['command'], ['python3', f'/opt/inventory/{component}.py'])
            self.assertEqual(container['args'], ['--config', '/etc/inventory/config.json'])
            self.assertEqual(workload['spec']['selector']['matchLabels'], {
                k: template['metadata']['labels'][k]
                for k in ['app.kubernetes.io/instance', 'app.kubernetes.io/component']})
            configmap = one(documents, 'ConfigMap', component)
            config = json.loads(configmap['data']['config.json'])
            self.assertTrue(config['dry_run'])
            self.assertEqual(config['netbox_url'], 'https://netbox.example.com')
            self.assertEqual(config['token_file'], '/run/netbox/token')
            self.assertEqual(config['device_mappings'], {})
            self.assertEqual(config['cluster_id'], 'example-cluster')
            self.assertEqual(config['interval_seconds'], 3600)
            self.assertIn('checksum/config', template['metadata']['annotations'])
            self.assertFalse(any('secret' in volume for volume in pod['volumes']))
            if component == 'agent':
                self.assertEqual(pod['securityContext']['runAsUser'], 0)
                self.assertEqual(config['host_root'], '/host')
                hosts = {v['name']: v['hostPath'] for v in pod['volumes'] if 'hostPath' in v}
                self.assertEqual({v['path'] for v in hosts.values()},
                                 {'/sys', '/proc/cpuinfo', '/proc/meminfo', '/etc/os-release'})
                mounts = {m['name']: m for m in container['volumeMounts']}
                self.assertTrue(all(mounts[name]['readOnly'] for name in hosts))
                self.assertEqual(mounts['host-sys']['recursiveReadOnly'], 'Enabled')
                self.assertEqual(mounts['host-sys']['mountPropagation'], 'None')
                node_env = next(e for e in container['env'] if e['name'] == 'NODE_NAME')
                self.assertEqual(node_env['valueFrom']['fieldRef']['fieldPath'], 'spec.nodeName')
            else:
                self.assertTrue(pod['securityContext']['runAsNonRoot'])
                self.assertNotIn('runAsUser', pod['securityContext'])
                self.assertFalse(any('hostPath' in v for v in pod['volumes']))
                self.assertEqual(workload['spec']['replicas'], 1)
                self.assertEqual(workload['spec']['strategy'], {'type': 'Recreate'})
                self.assertNotIn('openshift.io/required-scc', template['metadata']['annotations'])
        rules = one(documents, 'ClusterRole', 'companion')['rules']
        self.assertEqual(rules, [{'apiGroups': [''], 'resources': ['nodes'], 'verbs': ['get', 'list', 'watch']}])
        scc = one(documents, 'SecurityContextConstraints')
        for flag in ['allowPrivilegedContainer', 'allowPrivilegeEscalation', 'allowHostIPC',
                     'allowHostNetwork', 'allowHostPID', 'allowHostPorts']:
            self.assertIs(scc[flag], False)
        self.assertTrue(scc['allowHostDirVolumePlugin'])
        self.assertEqual(scc['requiredDropCapabilities'], ['ALL'])
        self.assertTrue(scc['readOnlyRootFilesystem'])
        self.assertEqual(scc['seLinuxContext'], {'type': 'MustRunAs'})
        self.assertEqual(scc['fsGroup'], {'type': 'MustRunAs'})
        self.assertEqual(scc['seccompProfiles'], ['runtime/default'])
        self.assertEqual(scc['users'], [])
        self.assertEqual(scc['groups'], [])
        binding = one(documents, 'RoleBinding')
        self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount',
                         'name': one(documents, 'ServiceAccount', 'agent')['metadata']['name'],
                         'namespace': 'inventory-test'}])
        role = one(documents, 'ClusterRole', 'agent')
        self.assertEqual(role['rules'], [{'apiGroups': ['security.openshift.io'],
                         'resources': ['securitycontextconstraints'],
                         'resourceNames': [scc['metadata']['name']], 'verbs': ['use']}])

    def test_independent_credentials_images_and_source(self):
        documents = render('agent.credentials.existingSecret=hardware-token',
                           'agent.credentials.key=hardware', 'companion.credentials.existingSecret=kube-token',
                           'agent.sourceConfigMap=local-python', 'agent.image.repository=local/agent',
                           'agent.image.digest=sha256:' + 'a' * 64,
                           'companion.image.repository=local/companion', 'companion.image.tag=test',
                           'agent.nodeSelector.kubernetes\\.io/os=windows',
                           'agent.nodeSelector.kubernetes\\.io/arch=arm64')
        agent = one(documents, 'DaemonSet')['spec']['template']['spec']
        companion = one(documents, 'Deployment')['spec']['template']['spec']
        self.assertEqual(agent['containers'][0]['image'], 'local/agent@sha256:' + 'a' * 64)
        self.assertEqual(companion['containers'][0]['image'], 'local/companion:test')
        self.assertEqual(agent['nodeSelector']['kubernetes.io/os'], 'linux')
        self.assertEqual(agent['nodeSelector']['kubernetes.io/arch'], 'arm64')
        self.assertIn({'name': 'source', 'configMap': {'name': 'local-python'}}, agent['volumes'])
        self.assertNotIn('source', [v['name'] for v in companion['volumes']])
        for pod, name, key in [(agent, 'hardware-token', 'hardware'), (companion, 'kube-token', 'token')]:
            secret = next(v['secret'] for v in pod['volumes'] if 'secret' in v)
            self.assertEqual(secret, {'secretName': name, 'items': [{'key': key, 'path': 'token'}]})
            mount = next(m for m in pod['containers'][0]['volumeMounts'] if m['name'] == 'netbox-token')
            self.assertEqual(mount, {'name': 'netbox-token', 'mountPath': '/run/netbox', 'readOnly': True})

    def test_explicit_serial_source_is_passed_to_both_runtimes(self):
        for source in ['product_serial', 'board_serial']:
            documents = render('deviceMappings.node.device_id=42',
                               'deviceMappings.node.serial_source=' + source)
            for component in ['agent', 'companion']:
                config = json.loads(one(documents, 'ConfigMap', component)['data']['config.json'])
                self.assertEqual(config['device_mappings']['node'],
                                 {'device_id': 42, 'serial_source': source})
        render('deviceMappings.node.device_id=42', 'deviceMappings.node.serial_source=bogus', valid=False)

    def test_invalid_configuration(self):
        for setting in ['netbox.url=http://netbox.example', 'intervalSeconds=0', 'jitterSeconds=-1',
                        'gracePeriodSeconds=0', 'relistSeconds=0', 'watchTimeoutSeconds=0',
                        'deviceMappings.node.device_id=0', 'deviceMappings.node.extra=true',
                        'agent.credentials.token=forbidden', 'companion.credentials.token=forbidden',
                        'agent.token=forbidden', 'netbox.token=forbidden', 'clusterId=',
                        'agent.image.digest=sha256:bad', 'companion.credentials.key=']:
            with self.subTest(setting=setting):
                render(setting, valid=False)
        for component in ['agent', 'companion']:
            self.assertIn('apply mode requires', render(f'{component}.dryRun=false', valid=False))
            render(f'{component}.dryRun=false', f'{component}.credentials.existingSecret=token', valid=False)
            render(f'{component}.dryRun=false', 'deviceMappings.node.device_id=1', valid=False)
            documents = render(f'{component}.dryRun=false',
                               f'{component}.credentials.existingSecret=token',
                               'deviceMappings.node.device_id=1')
            self.assertFalse(json.loads(one(documents, 'ConfigMap', component)['data']['config.json'])['dry_run'])
        render('companion.enabled=false', 'clusterId=', valid=False)
        render('agent.enabled=false', 'clusterId=', valid=False)
        render('agent.enabled=false', 'companion.enabled=false', 'clusterId=')

    def test_optional_placement_is_companion_only_and_requires_read_credentials(self):
        defaults = render()
        agent = json.loads(one(defaults, 'ConfigMap', 'agent')['data']['config.json'])
        companion = json.loads(one(defaults, 'ConfigMap', 'companion')['data']['config.json'])
        self.assertNotIn('placement', agent)
        self.assertEqual(companion['placement'], {'enabled': False, 'dry_run': True, 'fields': {}})
        self.assertIn('placement requires', render('companion.placement.enabled=true', valid=False))
        docs = render('companion.placement.enabled=true', 'companion.credentials.existingSecret=token',
                      'deviceMappings.node.device_id=42', 'deviceMappings.node.placement.site=1',
                      'companion.placement.fields.site.annotation=netbox.example.com/site-id',
                      'companion.placement.fields.location.labels[0].key=topology.kubernetes.io/zone',
                      'companion.placement.fields.location.labels[0].values.zone-a=2')
        cfg = json.loads(one(docs, 'ConfigMap', 'companion')['data']['config.json'])
        self.assertEqual(cfg['device_mappings']['node']['placement'], {'site': 1})
        self.assertTrue(cfg['placement']['dry_run'])
        self.assertEqual(cfg['placement']['fields']['location']['labels'][0]['values'], {'zone-a': 2})
        self.assertEqual(one(docs, 'ClusterRole', 'companion')['rules'],
                         one(defaults, 'ClusterRole', 'companion')['rules'])
        for setting in ['companion.placement.fields.serial.annotation=x',
                        'companion.placement.dryRun=bad', 'deviceMappings.node.placement.rack=0',
                        'companion.placement.fields.site.labels[0].values.a=0']:
            render(setting, valid=False)

    def test_names_and_config_rollout(self):
        rendered = [render('openshift.enabled=true', release='r' * 53, namespace=namespace)
                    for namespace in ['first-namespace', 'second-namespace']]
        names = [{doc['metadata']['name'] for doc in docs} for docs in rendered]
        self.assertFalse(names[0] & names[1])
        self.assertTrue(all(len(name) <= 63 for group in names for name in group))
        before = render()
        after = render('intervalSeconds=900')
        for kind in ['DaemonSet', 'Deployment']:
            def checksum(documents):
                return one(documents, kind)['spec']['template']['metadata']['annotations']['checksum/config']
            self.assertNotEqual(checksum(before), checksum(after))


if __name__ == '__main__':
    unittest.main()
