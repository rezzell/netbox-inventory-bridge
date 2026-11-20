#!/usr/bin/env python3
"""Kubernetes membership observations; never changes physical Device status."""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse

from placement import reconcile_placement
from runtime import APIError, HTTPSClient, InventoryError, install_shutdown_handler, load_config, owner_check, update_field

TOKEN = '/var/run/secrets/kubernetes.io/serviceaccount/token'
CA = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'


def timestamp(now):
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()


def parsed_time(value, fallback):
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError, AttributeError):
        return fallback


def lifecycle(node, previous, config, name, now):
    """Pure lifecycle projection. None means no authoritative owned membership yet."""
    owner_check(previous, config, name)
    if node is None:
        if not previous:
            return None
        value = dict(previous)
        if value.get('state') != 'removed':
            value.update(state='removed', removed_at=timestamp(now), last_transition=timestamp(now))
        return value
    metadata = node.get('metadata', {})
    uid = metadata.get('uid')
    if not uid or metadata.get('name') != name:
        raise InventoryError('ambiguous Kubernetes node identity')
    if previous and previous.get('node_uid') != uid:
        raise InventoryError('Node UID changed; explicit identity review required')
    conditions = node.get('status', {}).get('conditions', [])
    ready_conditions = [condition for condition in conditions if condition.get('type') == 'Ready']
    if len(ready_conditions) > 1:
        raise InventoryError('ambiguous Kubernetes Ready condition')
    condition = ready_conditions[0] if ready_conditions else {}
    ready = condition.get('status', 'Unknown')
    if ready not in ('True', 'False', 'Unknown'):
        ready = 'Unknown'
    transition = condition.get('lastTransitionTime')
    started = parsed_time(transition, now)
    # Missing timestamps use durable previous transition for grace across restarts.
    if not transition and previous and previous.get('ready') == ready:
        started = parsed_time(previous.get('last_transition'), now)
    if ready == 'True':
        state = 'ready'
    elif ready == 'Unknown':
        state = 'unreachable'
    else:
        state = 'not_ready' if now - started >= config['grace_period_seconds'] else 'pending'
    value = {'cluster_id': config['cluster_id'], 'node_name': name, 'node_uid': uid, 'ready': ready,
             'unschedulable': bool(node.get('spec', {}).get('unschedulable', False)), 'state': state,
             'last_transition': timestamp(started), 'removed_at': None}
    return value


class Kubernetes:
    def __init__(self, config, client=None):
        host = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        port = os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')
        self.client = client or HTTPSClient('https://' + host + ':' + port, TOKEN, CA, bearer=True)
        self.config = config

    def list_nodes(self):
        rows, continuation, seen, resource_version = [], '', set(), None
        while True:
            query = {'limit': 500}
            if continuation:
                query['continue'] = continuation
            page = self.client.get('api/v1/nodes?' + urllib.parse.urlencode(query))
            if not isinstance(page, dict) or not isinstance(page.get('items'), list):
                raise InventoryError('incomplete Kubernetes LIST')
            meta = page.get('metadata', {})
            version = meta.get('resourceVersion')
            if not version or resource_version not in (None, version):
                raise InventoryError('inconsistent Kubernetes LIST')
            resource_version = version
            rows.extend(page['items'])
            continuation = meta.get('continue', '')
            if not continuation:
                break
            if continuation in seen:
                raise InventoryError('Kubernetes pagination cycle')
            seen.add(continuation)
        names = [node.get('metadata', {}).get('name') for node in rows]
        uids = [node.get('metadata', {}).get('uid') for node in rows]
        if None in names or None in uids or len(set(names)) != len(names) or len(set(uids)) != len(uids):
            raise InventoryError('ambiguous Kubernetes LIST identity')
        return {node['metadata']['name']: node for node in rows}, resource_version

    def watch(self, version):
        # Watch events only invalidate the list. Never trust raw DELETED payloads:
        # a stale UID/name event cannot remove current authoritative membership.
        seconds = min(self.config['watch_timeout_seconds'], self.config['relist_seconds'])
        query = {'watch': 'true', 'resourceVersion': version, 'timeoutSeconds': seconds, 'allowWatchBookmarks': 'true'}
        events, _ = self.client.request('GET', 'api/v1/nodes?' + urllib.parse.urlencode(query),
                                        lines=True, timeout=seconds + 5)
        for event in events:
            if event.get('type') == 'ERROR':
                raise APIError(event.get('object', {}).get('code', 0))
        return events


def reconcile(nodes, config, client=None, now=None):
    now = time.time() if now is None else now
    results = []
    for name, node in nodes.items():
        if name not in config['device_mappings']:
            value = lifecycle(node, None, config, name, now)
            results.append({'node_name': name, 'state': value['state'], 'unschedulable': value['unschedulable'], 'skipped': 'unmapped'})
    placement_enabled = config.get('placement', {}).get('enabled', False)
    if not config['dry_run'] or placement_enabled:
        client = client or HTTPSClient(config['netbox_url'], config['token_file'])
    for name, mapping in config['device_mappings'].items():
        device = None
        if config['dry_run'] and not placement_enabled:
            value = lifecycle(nodes.get(name), None, config, name, now)
        else:
            path = 'api/dcim/devices/%s/' % mapping['device_id']
            device = client.get(path)
            value = lifecycle(nodes.get(name), device.get('custom_fields', {}).get('kubernetes_lifecycle'), config, name, now)
        if value is None:
            continue
        if not config['dry_run']:
            def validate(device, name=name, value=value):
                previous = device.get('custom_fields', {}).get('kubernetes_lifecycle')
                owner_check(previous, config, name)
                if previous and previous.get('node_uid') != value['node_uid']:
                    raise InventoryError('lifecycle UID changed during update')
            update_field(client, mapping['device_id'], 'kubernetes_lifecycle', value, config, name, validate)
        result = {'device_id': mapping['device_id'], 'dry_run': config['dry_run'], 'lifecycle': value}
        if placement_enabled:
            try:
                result['placement'] = reconcile_placement(nodes.get(name), mapping, config, name, client, device)
            except (InventoryError, OSError, ValueError, KeyError, TypeError):
                # Placement lookup failures must not interrupt lifecycle or other nodes.
                result['placement'] = {'status': 'blocked', 'reason': 'placement_lookup_or_update_failed'}
        results.append(result)
    return results


def main():
    install_shutdown_handler()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        kube = Kubernetes(config)
    except (InventoryError, OSError, ValueError):
        print('invalid configuration or Kubernetes CA', file=sys.stderr)
        return 1
    failures = 0
    while True:
        try:
            # Do not reconcile until every list page has succeeded. Restart recovery
            # reads durable NetBox membership only after this authoritative snapshot.
            nodes, version = kube.list_nodes()
            print(json.dumps({'observations': reconcile(nodes, config)}, sort_keys=True), flush=True)
            failures = 0
            if args.once:
                return 0
            kube.watch(version)
        except (InventoryError, OSError, ValueError, KeyError, TypeError):
            print(json.dumps({'error': 'lifecycle observation failed; preserving membership until complete relist'}), flush=True)
            failures += 1
            if args.once:
                return 1
            time.sleep(min(60, 2 ** min(failures, 6)))


if __name__ == '__main__':
    sys.exit(main())
