#!/usr/bin/env python3
"""Periodic hardware reconciliation with an explicit, serial-verified Device mapping."""
import argparse
import hashlib
import json
import os
import random
import re
import sys
import time

from collector import Collector
from mapper import hardware_value, plan
from runtime import APIError, HTTPSClient, InventoryError, install_shutdown_handler, load_config, marker, owner_check, update_field

ENDPOINTS = {'ModuleBay': 'module-bays', 'ModuleType': 'module-types', 'Module': 'modules',
             'Interface': 'interfaces', 'MACAddress': 'mac-addresses'}


def reference_id(value):
    return value.get('id') if isinstance(value, dict) else value


def slug(value):
    readable = re.sub('[^a-z0-9]+', '-', value.lower()).strip('-')[:70] or 'hardware'
    return readable + '-' + hashlib.sha256(value.encode()).hexdigest()[:12]


def ensure(client, endpoint, filters, fields, owner, catalog=False):
    path = 'api/dcim/' + endpoint + '/'
    rows = client.listing(path, filters)
    if not rows:
        try:
            return client.request('POST', path, fields)[0]
        except APIError as error:
            # A timeout or uniqueness conflict may mean the create succeeded elsewhere.
            # Re-query once, never blindly retry POST.
            if error.status not in (0, 400, 409, 429, 500, 502, 503, 504):
                raise
            rows = client.listing(path, filters)
            if not rows:
                raise error
    if len(rows) != 1:
        raise InventoryError('ambiguous existing component/catalog match')
    row = rows[0]
    if not catalog and row.get('description') != owner:
        return None  # Curated records are preserved, including children/assignment.
    # Catalog records are shared references, never modified. Component metadata is
    # likewise left untouched; only native observed module identity may be refreshed.
    if not catalog and endpoint == 'modules':
        updates = {key: value for key, value in fields.items()
                   if key in ('serial', 'module_type') and reference_id(row.get(key)) != value}
        if updates:
            for attempt in range(3):
                current, headers = client.request('GET', path + str(row['id']) + '/')
                if current.get('description') != owner or not headers.get('ETag'):
                    raise InventoryError('component ownership or ETag changed')
                updates = {key: value for key, value in updates.items()
                           if reference_id(current.get(key)) != value}
                if not updates:
                    break
                try:
                    client.request('PATCH', path + str(row['id']) + '/', updates, {'If-Match': headers['ETag']})
                    break
                except APIError as error:
                    if error.status != 412 or attempt == 2:
                        raise
    return row


def serial_validator(snapshot, mapping):
    source = mapping.get('serial_source', 'product_serial')
    if source not in ('product_serial', 'board_serial'):
        raise InventoryError('invalid serial_source')
    observed = hardware_value(snapshot['sections']['system']['data'].get(source))
    if not observed:
        raise InventoryError('usable observed Device serial required')
    expected = mapping.get('expected_serial')
    if expected is not None and hardware_value(expected) != observed:
        raise InventoryError('expected Device serial mismatch')

    def validate(device):
        if hardware_value(device.get('serial')) != observed:
            raise InventoryError('mapped Device serial mismatch or missing')
    return observed, validate


def reconcile(snapshot, config, client=None):
    node = snapshot['node']
    mapping = config['device_mappings'].get(node)
    if not mapping:
        if config['dry_run']:
            return {'node_name': node, 'dry_run': True, 'skipped': 'no explicit Device mapping'}
        raise InventoryError('explicit node Device mapping required')
    observed, validate = serial_validator(snapshot, mapping)
    proposal = plan(snapshot, mapping['device_id'])
    summary = {'cpu_sockets': len([r for r in snapshot['sections']['memory']['data']['cpu_sockets'] if r.get('populated')]),
               'dimms': len([r for r in snapshot['sections']['memory']['data']['dimms'] if r.get('populated')]),
               'disks': len(snapshot['sections']['disks']['data']), 'interfaces': len(snapshot['sections']['interfaces']['data'])}
    result = {'node_name': node, 'device_id': mapping['device_id'], 'dry_run': config['dry_run'],
              'summary': summary, 'proposed_operations': len(proposal['operations']), 'skipped': len(proposal['skipped'])}
    if config['dry_run']:
        # Offline plan keeps mapper compatibility, avoids token reads and network entirely.
        result['plan'] = proposal
        return result
    client = client or HTTPSClient(config['netbox_url'], config['token_file'])
    device = client.get('api/dcim/devices/%s/' % mapping['device_id'])
    validate(device)
    owner_check(device.get('custom_fields', {}).get('hardware_observation'), config, node)
    if 'hardware_observation' not in device.get('custom_fields', {}):
        raise InventoryError('required hardware_observation JSON custom field missing')
    owner, resolved = marker(config, node), {}
    operations = proposal['operations']
    for kind in ('ModuleBay', 'ModuleType', 'Module', 'Interface', 'MACAddress'):
        for operation in (op for op in operations if op['model'] == kind):
            fields = dict(operation['fields'])
            if kind == 'ModuleType':
                manufacturer = fields.pop('manufacturer_reference')
                catalog = ensure(client, 'manufacturers', {'name': manufacturer},
                                 {'name': manufacturer, 'slug': slug(manufacturer)}, owner, True)
                fields['manufacturer'] = catalog['id']
                filters = {'manufacturer_id': catalog['id'], 'model': fields['model']}
            elif kind == 'Module':
                bay = resolved.get(fields.pop('module_bay_reference'))
                module_type = resolved.get(fields.pop('module_type_reference'))
                if not bay or not module_type:
                    continue
                fields.update(module_bay=bay['id'], module_type=module_type['id'], status='active',
                              replicate_components=False, adopt_components=False)
                filters = {'device_id': fields['device'], 'module_bay_id': bay['id']}
            elif kind == 'MACAddress':
                interface = resolved.get(fields.pop('assigned_object_reference'))
                fields.pop('observation_note', None)
                if not interface:
                    continue
                fields.update(assigned_object_type='dcim.interface', assigned_object_id=interface['id'])
                # MAC identity is global; never reassign an existing curated address.
                filters = {'mac_address': fields['mac_address']}
            else:
                filters = {'device_id': fields['device'], 'name': fields['name']}
            fields['description'] = owner
            resolved[operation['key']] = ensure(client, ENDPOINTS[kind], filters, fields, owner, kind == 'ModuleType')
    hardware_hash = hashlib.sha256(json.dumps(snapshot['sections'], sort_keys=True).encode()).hexdigest()
    observation = {'cluster_id': config['cluster_id'], 'node_name': node, 'serial': observed,
                   'serial_source': mapping.get('serial_source', 'product_serial'),
                   'last_seen': snapshot['observed_at'], 'summary': summary, 'errors': snapshot['errors'], 'hardware_hash': hardware_hash}
    update_field(client, mapping['device_id'], 'hardware_observation', observation, config, node, validate)
    return result


def main():
    install_shutdown_handler()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        node = os.environ.get('NODE_NAME')
        if not node:
            raise InventoryError('NODE_NAME is required')
    except InventoryError as error:
        print(str(error), file=sys.stderr)
        return 1
    failures = 0
    while True:
        try:
            snapshot = Collector(config['host_root']).collect(node)
            result = reconcile(snapshot, config)
            print(json.dumps(result, sort_keys=True), flush=True)
            failures = 0
        except (InventoryError, OSError, ValueError, KeyError, TypeError):
            print(json.dumps({'node_name': node, 'error': 'hardware reconciliation failed; check mapping, schema and API access'}), flush=True)
            failures += 1
            if args.once:
                return 1
        if args.once:
            return 0
        delay = min(300, 5 * 2 ** min(failures - 1, 6)) if failures else config['interval_seconds']
        time.sleep(delay + random.uniform(0, config['jitter_seconds']))


if __name__ == '__main__':
    sys.exit(main())
