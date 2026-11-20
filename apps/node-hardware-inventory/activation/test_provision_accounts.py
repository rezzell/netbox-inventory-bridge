# Modified for portable extraction: installation-specific examples use fictional identities.
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from provision_accounts import (MARKER, overlap_expiry, parse_token, permission_specs,
                                validate_config, validate_token_owner)


class ActivationTests(unittest.TestCase):
    def test_explicit_device_scope_required(self):
        for ids in ([], [True], [0], [-1], [1, 1], ['1'], None):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_config({'cluster_id': 'example-cluster', 'device_ids': ids})
        self.assertEqual(validate_config({'cluster_id': 'example-cluster', 'device_ids': [1, 2]})['device_ids'], [1, 2])
        with self.assertRaises(ValueError):
            validate_config({'cluster_id': '', 'device_ids': [1]})

    def test_optional_placement_reads_are_scoped_and_companion_only(self):
        config = {'cluster_id': 'example-cluster', 'device_ids': [1], 'placement_read_ids': {'site': [1], 'cluster': [34]}}
        validate_config(config)
        reads = permission_specs([1], False, config['placement_read_ids'])[1:]
        self.assertEqual(reads, [('placement-read-site', ['site'], ['view'], {'id__in': [1]}),
                                ('placement-read-cluster', ['virtualization.cluster'], ['view'], {'id__in': [34]})])
        self.assertFalse(any('placement' in name for name, *_ in permission_specs([1], True, config['placement_read_ids'])))
        for scope in [{'site': [True]}, {'site': []}, {'site': [1, 1]}, {'device': [1]}]:
            with self.assertRaises(ValueError):
                validate_config({**config, 'placement_read_ids': scope})

    def test_bare_v2_only(self):
        credential = 'nbt_' + 'a' * 12 + '.' + 'B' * 40
        self.assertEqual(parse_token(credential + '\n'), ('a' * 12, 'B' * 40))
        for invalid in ('Bearer ' + credential, 'Token ' + 'a' * 40, 'a' * 40, credential + ' extra'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_token(invalid)

    def test_key_cannot_be_reassigned_or_repurposed(self):
        existing = SimpleNamespace(user_id=7, description=MARKER, validate=lambda value: value == 'correct')
        validate_token_owner(existing, 7, 'correct')
        for owner, secret in ((8, 'correct'), (None, 'correct'), (7, 'incorrect')):
            with self.assertRaises(ValueError):
                validate_token_owner(existing, owner, secret)
        existing.description = 'unrelated'
        with self.assertRaises(ValueError):
            validate_token_owner(existing, 7, 'correct')

    def test_overlap_never_extends_an_existing_deadline(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        self.assertEqual(overlap_expiry(None, now), now + timedelta(hours=1))
        for expiry in (now - timedelta(hours=1), now + timedelta(minutes=30)):
            self.assertEqual(overlap_expiry(expiry, now), expiry)

    def test_runtime_permissions_have_no_delete_or_device_create(self):
        companion = permission_specs([3, 4], False)
        self.assertEqual(companion, [('devices', ['device'], ['view', 'change'], {'id__in': [3, 4]})])
        hardware = permission_specs([3, 4], True)
        for _, models, actions, constraints in hardware:
            self.assertNotIn('delete', actions)
            if 'device' in models:
                self.assertNotIn('add', actions)
                self.assertEqual(constraints, {'id__in': [3, 4]})
        by_name = {spec[0]: spec for spec in hardware}
        self.assertEqual(by_name['modules-change'][3]['description__startswith'], 'node-inventory:')
        self.assertEqual(by_name['mac-create'][3], {'interface__device_id__in': [3, 4]})


if __name__ == '__main__':
    unittest.main()
