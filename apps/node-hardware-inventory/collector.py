#!/usr/bin/env python3
"""Read-only Linux host inventory. No network access or API credentials."""
import argparse
import datetime
import json
import pathlib
import re
import struct
import time

SCHEMA = 'node-hardware-inventory/v1alpha1'


def smbios_records(data):
    """Walk SMBIOS formatted structures and their double-NUL string sets."""
    offset = 0
    while offset + 4 <= len(data):
        kind, length, handle = struct.unpack_from('<BBH', data, offset)
        if length < 4 or offset + length > len(data):
            raise ValueError('truncated SMBIOS formatted structure')
        end = data.find(b'\0\0', offset + length)
        if end < 0:
            raise ValueError('unterminated SMBIOS string set')
        strings = data[offset + length:end].split(b'\0')
        yield kind, handle, data[offset:offset + length], [s.decode('utf-8', 'replace').strip() for s in strings]
        offset = end + 2
        if kind == 127:
            return
    if offset != len(data):
        raise ValueError("truncated SMBIOS structure header")


def parse_smbios(data):
    sockets, dimms = [], []
    for kind, handle, raw, strings in smbios_records(data):
        def string(index):
            pos = raw[index] if index < len(raw) else 0
            return strings[pos - 1] if 0 < pos <= len(strings) else None
        if kind == 4 and len(raw) >= 26:
            # Status bit 6 distinguishes an installed processor from an empty socket.
            def count(offset, extended_offset):
                value = raw[offset] if offset < len(raw) else 0
                if value == 255 and len(raw) >= extended_offset + 2:
                    value = int.from_bytes(raw[extended_offset:extended_offset + 2], 'little')
                return value if value not in (0, 255) else None
            sockets.append({'handle': handle, 'socket': string(4), 'manufacturer': string(7),
                            'model': string(16), 'populated': bool(raw[24] & 0x40),
                            'serial': string(32), 'part_number': string(34),
                            'core_count': count(35, 42), 'thread_count': count(37, 46)})
        if kind == 17 and len(raw) >= 21:
            size = int.from_bytes(raw[12:14], 'little')
            size_bytes = None
            if size == 0:
                size_bytes = 0
            elif size == 0x7fff and len(raw) >= 32:
                size_bytes = (int.from_bytes(raw[28:32], 'little') & 0x7fffffff) * 1024 ** 2
            elif size != 0xffff and size != 0x7fff:
                size_bytes = (size & 0x7fff) * (1024 if size & 0x8000 else 1024 ** 2)
            dimms.append({'handle': handle, 'locator': string(16), 'bank': string(17),
                          'size_bytes': size_bytes, 'populated': size != 0,
                          'manufacturer': string(23), 'serial': string(24), 'part_number': string(26)})
    return {'cpu_sockets': sockets, 'dimms': dimms}


class Collector:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.errors = []

    def read(self, path, binary=False):
        try:
            target = self.root / path.lstrip('/')
            return target.read_bytes() if binary else target.read_text().strip()
        except (OSError, UnicodeError) as error:
            self.errors.append({'path': path, 'error': type(error).__name__})
            return None

    def optional(self, path):
        target = self.root / path.lstrip('/')
        try:
            return target.read_text().strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            self.errors.append({'path': path, 'error': type(error).__name__})
            return None

    def entries(self, path):
        try:
            return sorted((self.root / path.lstrip("/")).iterdir())
        except OSError as error:
            self.errors.append({"path": path, "error": type(error).__name__})
            return []

    def section(self, paths, data, unsupported=None):
        result = {'provenance': paths, 'data': data}
        if unsupported:
            result['unsupported'] = unsupported
        return result

    def collect(self, node_name):
        dmi = {key: self.read('/sys/class/dmi/id/' + key) for key in
               ('sys_vendor', 'product_name', 'product_serial', 'product_uuid', 'board_name', 'board_serial')}
        table = self.read('/sys/firmware/dmi/tables/DMI', binary=True)
        memory = {'cpu_sockets': [], 'dimms': []}
        if table is not None:
            try:
                memory = parse_smbios(table)
            except ValueError as error:
                self.errors.append({'path': '/sys/firmware/dmi/tables/DMI', 'error': str(error)})
        cpuinfo = self.read('/proc/cpuinfo') or ''
        cpu_rows = [dict(re.findall(r'^([^:\n]+)\s*:\s*(.*)$', block, re.M)) for block in cpuinfo.split('\n\n') if block.strip()]
        cpu_rows = [{k.strip(): v for k, v in row.items()} for row in cpu_rows]
        cpu = {'logical_processors': len(cpu_rows) if cpuinfo else None, 'models': sorted({r['model name'] for r in cpu_rows if 'model name' in r}),
               'physical_package_ids': sorted({r['physical id'] for r in cpu_rows if 'physical id' in r})}
        meminfo = self.read('/proc/meminfo') or ''
        match = re.search(r'^MemTotal:\s+(\d+) kB$', meminfo, re.M)
        memory['host_mem_total_bytes'] = int(match[1]) * 1024 if match else None
        disks = []
        for path in self.entries('/sys/class/block'):
            name = path.name
            if re.match(r'^(loop|rbd|nbd|dm-|ram|zram|md)', name) or (path / 'partition').exists() or not (path / 'device').exists():
                continue
            prefix = '/sys/class/block/' + name
            serial = self.optional(prefix + '/device/serial')
            wwid = self.optional(prefix + '/wwid') or self.optional(prefix + '/device/wwid')
            size = self.read(prefix + '/size')
            disks.append({'kernel_name': name, 'serial': serial, 'wwid': wwid,
                          'identity': ('wwid:' + wwid) if wwid else ('serial:' + serial if serial else None),
                          'model': self.optional(prefix + '/device/model'),
                          'size_bytes': int(size) * 512 if size and size.isdigit() else None,
                          'physical_slot': None})
        nics = []
        for path in self.entries('/sys/class/net'):
            if not (path / 'device').exists():
                continue
            prefix = '/sys/class/net/' + path.name
            nics.append({'name': path.name, 'mac_address': self.read(prefix + '/address'),
                         'device_path': str(path.resolve()).split('/sys/')[-1],
                         'physical_port_name': self.optional(prefix + '/phys_port_name'),
                         'permanent_mac_address': None})
        pci = []
        for path in self.entries('/sys/bus/pci/devices'):
            prefix = '/sys/bus/pci/devices/' + path.name
            pci.append(dict({'address': path.name}, **{key: self.read(prefix + '/' + key) for key in
                            ('vendor', 'device', 'subsystem_vendor', 'subsystem_device', 'class')}))
        os_release = self.read('/etc/os-release') or ''
        os_data = dict(re.findall(r'^([A-Z_]+)=(.*)$', os_release, re.M))
        os_data = {k: v.strip('"') for k, v in os_data.items()}
        return {'schema_version': SCHEMA, 'node': node_name,
                'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'sections': {'system': self.section(['/sys/class/dmi/id'], dmi, ['firmware versions and PSU inventory not collected']),
                             'cpu': self.section(['/proc/cpuinfo (host)'], cpu),
                             'memory': self.section(['/sys/firmware/dmi/tables/DMI (SMBIOS types 4,17)', '/proc/meminfo (host)'], memory),
                             'disks': self.section(['/sys/class/block'], disks, ['physical slots and SMART health not collected']),
                             'interfaces': self.section(['/sys/class/net'], nics, ['IP addresses and permanent MAC require a host-network probe; omitted']),
                             'pci': self.section(['/sys/bus/pci/devices'], pci),
                             'os': self.section(['/etc/os-release (host)'], os_data)},
                'errors': self.errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-root', default='/host')
    parser.add_argument('--node-name', required=True)
    parser.add_argument('--once', action='store_true', help='exit after writing one JSON snapshot')
    args = parser.parse_args()
    print(json.dumps(Collector(args.host_root).collect(args.node_name), sort_keys=True), flush=True)
    if not args.once:
        while True:
            time.sleep(3600)


if __name__ == '__main__':
    main()
