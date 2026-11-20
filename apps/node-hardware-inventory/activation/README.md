# Optional administrator bootstrap

`provision_accounts.py` is a reusable NetBox Django-shell utility, separate from both runtime images and the Helm release. Installation manifests, secret-manager resources and account configuration are intentionally excluded. No recurring provisioning controller is included.

The utility validates two optional Device JSON fields, creates dedicated API-only accounts, imports distinct projected v2 credentials, and assigns scoped object permissions. Existing unmanaged accounts, permissions and token keys are refused. Existing managed account groups and direct permissions are replaced on apply. Older managed tokens without expiry receive a one-hour overlap. Review these administrative effects before use; runtime credentials must not have these privileges.

Supply an installation-owned JSON config through `ACTIVATION_CONFIG` (default `/opt/activation/config.json`):

```json
{
  "cluster_id": "example-cluster",
  "device_ids": [101, 102],
  "placement_read_ids": {"site": [201], "location": [301]}
}
```

All IDs above are fictional; replace them with reviewed existing objects. Optional placement view grants support `region`, `site`, `location`, `rack` and `cluster`, for the companion only. Include retained references and ancestor Locations needed for hierarchy validation. Hardware permissions include shared Manufacturer/ModuleType creation and scoped component access; Device grants are object-level, while runtime code enforces field ownership.

Inside the installed NetBox environment, provide bare v2 credential files at `/run/activation/netbox-hardware-agent/token` and `/run/activation/netbox-lifecycle-companion/token`. The utility reads them without printing token values. Using NetBox's configured Django shell, execute the script with `runpy.run_path('/path/to/provision_accounts.py', run_name='__main__')`. Leave `ACTIVATION_APPLY` unset for validation/report mode; only the exact value `true` enables changes. Report mode still requires Django/database access and projected credentials, and does not prove that apply will succeed.

Do not put real credentials or installation configuration in this repository. Unit tests cover validation and permission specifications without Django; database-backed bootstrap execution requires a disposable NetBox integration environment and is not established by those tests.
