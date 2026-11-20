#!/usr/bin/env python3
# Modified for portable extraction: installation-specific examples use fictional identities.
"""Pure report-only mapping. Never reads credentials, calls APIs or mutates input."""
import argparse
import json
import sys

from collector import SCHEMA

NETBOX_URL = 'https://netbox.example.com'


def hardware_value(value):
    """Firmware placeholders are missing observations, not catalog identities."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    placeholders = {'not specified', 'to be filled by o.e.m.', 'to be filled by oem',
                    '0123456789', 'default string', 'unknown', 'none', 'n/a', 'not available', 'not provided'}
    return value if value and value.casefold() not in placeholders else None


def plan(snapshot, device_id, existing=None):
    if snapshot.get('schema_version') != SCHEMA:
        raise ValueError('unsupported snapshot schema')
    if not isinstance(device_id, int) or isinstance(device_id, bool) or device_id <= 0:
        raise ValueError('an explicit positive NetBox Device ID is required')
    # existing is a reviewed export keyed by proposal key, not arbitrary matching by hostname.
    existing = existing or {}
    operations, skipped = [], []

    def propose(model, key, fields):
        if key in existing:
            skipped.append({'key': key, 'reason': 'exists; preserve all curated fields'})
        else:
            operations.append({'action': 'propose_create', 'model': model, 'key': key, 'fields': fields})

    sections = snapshot['sections']
    memory = sections['memory']['data']
    for category, records, locator_key in (('cpu', memory['cpu_sockets'], 'socket'), ('dimm', memory['dimms'], 'locator')):
        # Duplicate or missing locators are ambiguous; handles are diagnostic, not durable IDs.
        locators = [hardware_value(record.get(locator_key)) for record in records]
        for record in records:
            locator = hardware_value(record.get(locator_key))
            if not record.get('populated'):
                continue
            if not locator or locators.count(locator) != 1:
                skipped.append({'model': 'Module', 'reason': category + ' has missing/ambiguous physical locator'})
                continue
            bay = f'device:{device_id}/{category}:{locator}'
            propose('ModuleBay', bay, {'device': device_id, 'name': category.upper() + ' ' + locator})
            model = hardware_value(record.get('part_number')) or hardware_value(record.get('model'))
            manufacturer = hardware_value(record.get('manufacturer'))
            if not model or not manufacturer:
                skipped.append({'key': bay, 'reason': 'module manufacturer/model unavailable'})
                continue
            module_type = f'module-type:{manufacturer}/{model}'
            propose('ModuleType', module_type, {'manufacturer_reference': manufacturer, 'model': model})
            propose('Module', bay + '/module', {'device': device_id, 'module_bay_reference': bay,
                    'module_type_reference': module_type, 'serial': hardware_value(record.get('serial')) or ''})
    for nic in sections['interfaces']['data']:
        key = f'device:{device_id}/interface:{nic["name"]}'
        propose('Interface', key, {'device': device_id, 'name': nic['name'], 'type': 'other'})
        mac = nic.get('mac_address')
        if mac and mac != '00:00:00:00:00:00':
            propose('MACAddress', key + '/mac:' + mac.lower(),
                    {'mac_address': mac.lower(), 'assigned_object_reference': key,
                     'observation_note': 'current sysfs address; permanent address not established'})
    for disk in sections['disks']['data']:
        skipped.append({'model': 'Module', 'identity': disk.get('identity'),
                        'reason': 'disk physical bay unknown; kernel name is not a physical slot'})
    skipped.append({'model': 'IPAddress', 'reason': 'host network IP data not collected'})
    # Deduplicate shared ModuleType proposals while preserving deterministic order.
    operations = list({op['key']: op for op in operations}.values())
    return {'schema_version': 'netbox-hardware-plan/v1alpha1', 'target': NETBOX_URL,
            'report_only': True, 'device_mapping': {'node': snapshot['node'], 'device_id': device_id},
            'device': {'model': 'Device', 'action': 'reference_only', 'id': device_id},
            'operations': sorted(operations, key=lambda op: op['key']),
            'skipped': sorted(skipped, key=lambda item: json.dumps(item, sort_keys=True)),
            'custom_fields': {'status': 'not proposed; CPU/RAM totals require explicitly defined custom fields'},
            'limitations': ['symbolic references require reviewed NetBox IDs before any future writer',
                            'manufacturer references require reviewed existing manufacturer IDs',
                            'interface names and observed MACs may change; reconcile before creation',
                            'existing keys must be supplied from a reviewed export to suppress proposals']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot')
    parser.add_argument('--device-id', type=int, required=True)
    parser.add_argument('--existing', help='reviewed JSON object keyed by proposal key')
    args = parser.parse_args()
    try:
        with open(args.snapshot) as stream:
            snapshot = json.load(stream)
        existing = None
        if args.existing:
            with open(args.existing) as stream:
                existing = json.load(stream)
        print(json.dumps(plan(snapshot, args.device_id, existing), indent=2, sort_keys=True))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
