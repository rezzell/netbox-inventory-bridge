import copy
import pathlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from collector import Collector, parse_smbios
from mapper import plan


def record(kind, length, values, strings):
    raw = bytearray(length)
    struct.pack_into('<BBH', raw, 0, kind, length, kind)
    for offset, value in values.items():
        raw[offset] = value
    return bytes(raw) + b'\0'.join(s.encode() for s in strings) + b'\0\0'


DMI = (record(4, 38, {4: 1, 7: 2, 16: 3, 24: 0x41, 32: 4, 34: 5, 35: 12, 37: 24},
              ['CPU1', 'Intel', 'Xeon', 'cpu-serial', 'cpu-part']) +
       record(17, 32, {12: 0, 13: 0x20, 16: 1, 17: 2, 23: 3, 24: 4, 26: 5},
              ['DIMM_A1', 'BANK0', 'Vendor', 'dimm-serial', 'dimm-part']) +
       record(127, 4, {}, []))


class InventoryTests(unittest.TestCase):
    def test_dmi_socket_and_dimm(self):
        data = parse_smbios(DMI)
        self.assertEqual(data['cpu_sockets'][0]['socket'], 'CPU1')
        self.assertEqual(data['cpu_sockets'][0]['core_count'], 12)
        self.assertEqual(data['cpu_sockets'][0]['thread_count'], 24)
        self.assertEqual(data['dimms'][0]['size_bytes'], 8 * 1024 ** 3)
        self.assertEqual(data['dimms'][0]['serial'], 'dimm-serial')
        with self.assertRaises(ValueError):
            parse_smbios(DMI[:-3])

    def test_extended_and_empty_memory(self):
        raw = bytearray(record(17, 32, {12: 0xff, 13: 0x7f, 16: 1}, ['A']))
        struct.pack_into('<I', raw, 28, 65536)
        self.assertEqual(parse_smbios(bytes(raw))['dimms'][0]['size_bytes'], 64 * 1024 ** 3)
        empty = record(17, 21, {16: 1}, ['B'])
        self.assertFalse(parse_smbios(empty)['dimms'][0]['populated'])

    def fixture(self, root):
        def write(name, value):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value if isinstance(value, bytes) else value.encode())
        write('sys/firmware/dmi/tables/DMI', DMI)
        write('proc/cpuinfo', 'processor : 0\nphysical id : 0\nmodel name : Xeon\n\nprocessor : 1\nphysical id : 0\nmodel name : Xeon\n')
        write('proc/meminfo', 'MemTotal:       1234 kB\n')
        for name in ['sda', 'loop0', 'rbd0', 'nbd0', 'dm-0', 'sda1']:
            write('sys/class/block/' + name + '/size', '100')
            write('sys/class/block/' + name + '/device/serial', 'stable-serial')
        write('sys/class/block/sda1/partition', '1')
        write('sys/class/net/eno1/device/vendor', '0x1234')
        write('sys/class/net/eno1/address', 'aa:bb:cc:dd:ee:ff')
        write('sys/class/net/veth0/address', '11:22:33:44:55:66')

    def test_partial_access_and_virtual_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.fixture(root)
            snapshot = Collector(root).collect('node-a')
        self.assertEqual([d['kernel_name'] for d in snapshot['sections']['disks']['data']], ['sda'])
        self.assertEqual([n['name'] for n in snapshot['sections']['interfaces']['data']], ['eno1'])
        self.assertEqual(snapshot['sections']['cpu']['data']['logical_processors'], 2)
        self.assertTrue(snapshot['errors'])  # Missing DMI ID files and os-release are reported.
        self.assertEqual(snapshot['sections']['memory']['data']['host_mem_total_bytes'], 1234 * 1024)

    def test_denied_directories_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pathlib.Path, 'iterdir', side_effect=PermissionError('denied')):
                snapshot = Collector(directory).collect('node-a')
        errors = {item['path']: item['error'] for item in snapshot['errors']}
        for directory in ('/sys/class/block', '/sys/class/net', '/sys/bus/pci/devices'):
            self.assertEqual(errors[directory], 'PermissionError')

    def test_optional_attributes_report_denied_access(self):
        collector = Collector('/host')
        with patch.object(pathlib.Path, 'read_text', side_effect=PermissionError('denied')):
            self.assertIsNone(collector.optional('/sys/class/block/sda/device/serial'))
        self.assertEqual(collector.errors, [{'path': '/sys/class/block/sda/device/serial',
                                             'error': 'PermissionError'}])
        with patch.object(pathlib.Path, 'read_text', side_effect=FileNotFoundError()):
            self.assertIsNone(collector.optional('/sys/class/block/sda/device/serial'))
        self.assertEqual(len(collector.errors), 1)

    def test_56_gib_fixture_and_record_boundaries(self):
        records = b''.join(record(17, 21, {12: 0, 13: size, 16: 1}, ['DIMM' + str(index)])
                           for index, size in enumerate((0x40, 0x40, 0x40, 0x20)))
        self.assertEqual(sum(item['size_bytes'] for item in parse_smbios(records)['dimms']), 56 * 1024 ** 3)
        for malformed in (b'\x11\x02\x00\x00', b'\x11\x20\x00\x00', b'\x11\x04\x00\x00x'):
            with self.assertRaises(ValueError):
                parse_smbios(malformed)

    def test_mapper_is_deterministic_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.fixture(root)
            snapshot = Collector(root).collect('node-a')
        original = copy.deepcopy(snapshot)
        first = plan(snapshot, 42)
        snapshot['observed_at'] = 'different'
        self.assertEqual(first, plan(snapshot, 42))
        key = first['operations'][0]['key']
        curated = {key: {'description': 'curated'}}
        result = plan(snapshot, 42, curated)
        self.assertNotIn(key, [op['key'] for op in result['operations']])
        self.assertEqual(curated[key]['description'], 'curated')
        self.assertEqual(original['sections'], snapshot['sections'])
        self.assertTrue(all(op['action'] == 'propose_create' for op in result['operations']))
        with self.assertRaises(ValueError):
            plan(snapshot, None)

    def test_mapper_rejects_firmware_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.fixture(root)
            snapshot = Collector(root).collect('node-a')
        cpu = snapshot['sections']['memory']['data']['cpu_sockets'][0]
        cpu.update(part_number='Not Specified', serial='To Be Filled By O.E.M.')
        dimm = snapshot['sections']['memory']['data']['dimms'][0]
        dimm.update(part_number='Default string', manufacturer='Unknown')
        result = plan(snapshot, 42)
        types = [op['fields'] for op in result['operations'] if op['model'] == 'ModuleType']
        self.assertEqual(types, [{'manufacturer_reference': 'Intel', 'model': 'Xeon'}])
        modules = [op['fields'] for op in result['operations'] if op['model'] == 'Module']
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]['serial'], '')


if __name__ == '__main__':
    unittest.main()
