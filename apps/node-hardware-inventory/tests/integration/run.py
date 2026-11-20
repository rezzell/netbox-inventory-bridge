#!/usr/bin/env python3
"""Local-only disposable integration runner; never starts or changes an engine."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


def run(argv, timeout=30, capture=False, env=None):
    return subprocess.run(argv, timeout=timeout, check=True, text=True, env=env,
                          stdout=subprocess.PIPE if capture else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('docker', 'podman'), default='docker')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--inject-failure', action='store_true', help='exercise assertion-failure cleanup')
    args = parser.parse_args()
    provider = shutil.which(args.provider)
    if not provider:
        print('NOT RUN: local Compose provider unavailable', file=sys.stderr)
        return 2
    try:
        if args.provider == 'docker':
            endpoint = os.environ.get('DOCKER_HOST')
            if not endpoint:
                context = run([provider, 'context', 'inspect'], capture=True)
                endpoint = json.loads(context.stdout)[0]['Endpoints']['docker']['Host']
        else:
            endpoint = os.environ.get('CONTAINER_HOST')
            if not endpoint:
                connections = json.loads(run([provider, 'system', 'connection', 'list', '--format', 'json'], capture=True).stdout)
                endpoint = next(row['URI'] for row in connections if row.get('Default'))
        if not endpoint or not endpoint.startswith('unix://'):
            raise ValueError('explicit local Unix engine endpoint required; remote connections refused')
        env = dict(os.environ)
        env.pop('DOCKER_CONTEXT', None)
        env.pop('CONTAINER_CONNECTION', None)
        env['DOCKER_HOST'] = endpoint
        env['CONTAINER_HOST'] = endpoint
        run([provider, 'info'], capture=True, env=env)
        run([provider, 'compose', 'version'], capture=True, env=env)
    except (subprocess.SubprocessError, ValueError, KeyError, StopIteration) as error:
        print('NOT RUN: local engine/Compose preflight failed (%s)' % type(error).__name__, file=sys.stderr)
        return 2
    project = 'inventory-test-' + uuid.uuid4().hex[:12]
    command = [provider, 'compose', '-f', str(Path(__file__).with_name('compose.yaml')), '-p', project]
    run(command + ['config', '--quiet'], env=env)
    if args.check:
        print('Preflight passed; no services started')
        return 0
    print('Disposable project: ' + project, flush=True)
    print('Hard-kill recovery: ' + ' '.join(command + ['down', '--volumes', '--remove-orphans']), flush=True)
    env['INVENTORY_INJECT_FAILURE'] = '1' if args.inject_failure else '0'
    result = 1
    try:
        run(command + ['up', '-d', '--wait', '--wait-timeout', '150', 'postgres', 'valkey'], timeout=180, env=env)
        run(command + ['run', '--rm', '--no-deps', 'runner'], timeout=600, env=env)
        result = 0
    except (subprocess.SubprocessError, OSError) as error:
        print('Integration failed (%s)' % type(error).__name__, file=sys.stderr)
    finally:
        try:
            run(command + ['down', '--volumes', '--remove-orphans', '--timeout', '10'], timeout=60, env=env)
            print('Project cleanup completed', flush=True)
        except (subprocess.SubprocessError, OSError):
            print('Project cleanup failed; use recovery command above', file=sys.stderr)
            result = 1
    return result


if __name__ == '__main__':
    sys.exit(main())
