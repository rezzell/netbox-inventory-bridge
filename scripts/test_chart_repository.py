"""Exercise publication history, reproducibility and immutable versions locally."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('build-chart-repository.py')
CHART = Path(__file__).resolve().parents[1] / 'charts/netbox-node-inventory'


class ChartRepositoryTests(unittest.TestCase):
    def test_history_reproducibility_and_overwrite_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chart = root / 'charts/netbox-node-inventory'
            shutil.copytree(CHART, chart)
            def run(*args, success=True):
                result = subprocess.run(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode == 0, success, result.stderr.decode())
                return result
            def commit():
                run('git', 'add', 'charts')
                run('git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
            run('git', 'init', '-b', 'main')
            commit()
            command = ('python3', str(SCRIPT), '--url', 'https://example.invalid/', '--output', str(root / 'dist'))
            run(*command)
            first = (root / 'dist/netbox-node-inventory-0.2.0.tgz').read_bytes()
            run(*command)
            self.assertEqual(first, (root / 'dist/netbox-node-inventory-0.2.0.tgz').read_bytes())
            metadata = chart / 'Chart.yaml'
            metadata.write_text(metadata.read_text().replace('version: 0.2.0', 'version: 0.2.1'))
            commit(); run(*command)
            self.assertTrue((root / 'dist/netbox-node-inventory-0.2.1.tgz').exists())
            self.assertEqual(first, (root / 'dist/netbox-node-inventory-0.2.0.tgz').read_bytes())
            with (chart / 'README.md').open('a') as stream: stream.write('\nChanged same version\n')
            commit()
            result = run(*command, success=False)
            self.assertIn(b'changed without a version bump', result.stderr)
            # Remove only the invalid commit in this disposable fixture so the
            # next assertion reaches the existing-package integrity check.
            run('git', 'reset', '--hard', 'HEAD~1')
            (root / 'dist/netbox-node-inventory-0.2.0.tgz').write_bytes(b'conflicting artifact')
            result = run(*command, success=False)
            self.assertIn(b'Refusing to overwrite existing package', result.stderr)
