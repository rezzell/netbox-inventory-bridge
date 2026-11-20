# NetBox Inventory Bridge

An opt-in hardware agent and Kubernetes lifecycle companion for an existing HTTPS NetBox. Both use Python's standard library and default to dry-run. The companion also supports independently enabled placement reconciliation against existing catalogue objects.

- [Runtime configuration, ownership and limitations](apps/node-hardware-inventory/README.md)
- [Helm chart and deployment controls](charts/netbox-node-inventory/README.md)
- [Optional administrator bootstrap](apps/node-hardware-inventory/activation/README.md)

The agent collects Linux hardware through bounded read-only host mounts and verifies an explicit node-to-Device mapping and serial before writes. The companion watches Nodes with get/list/watch permissions and validates durable cluster ownership and Node UID. Conditional updates require ETags; unrelated curated fields are preserved. Devices are never automatically created or deleted.

## Local verification

Run from the repository root. Python 3 is required; chart tests also require Helm and Ruby's standard YAML/JSON libraries. Application and bootstrap unit tests use synthetic data and fake APIs; no live credentials or services are needed.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s apps/node-hardware-inventory/tests -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s apps/node-hardware-inventory/activation -p 'test_*.py' -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s charts/netbox-node-inventory/tests -q
helm lint charts/netbox-node-inventory --set clusterId=example-cluster
```

Build containers with `apps/node-hardware-inventory` as the build context. The chart's `registry.example.com` images and `https://netbox.example.com` endpoint are placeholders: supply your own image repositories and endpoint before installation. Empty mappings and dry-run defaults are deliberate.

## Extraction boundary

This repository retains the portable implementation, current placement code and tests, pinned container definitions, chart, custom-field definitions and reusable administrator bootstrap. Source implementation and installation files were left intact. GitOps configuration, deployment overlays, credential references, actual topology assignments and operational evidence are excluded.

The inherited [Apache 2.0 license](LICENSE) is preserved. Portable extraction changed endpoint/image defaults and test examples to fictional identities, removed installation evidence from documentation, and added standalone repository/bootstrap guidance. In `config.example.json`, the endpoint was changed to `https://netbox.example.com`; the runtime and mapper use the same generic default. Ownership markers and bootstrap account names retain their existing values for compatibility.

Placement catalogue creation, centralized writes, batching and asynchronous transport are future capabilities. No broker dependency is required. Local unit checks alone do not establish live NetBox compatibility, container builds or cluster admission; see the isolated verification and its limits below.

## Disposable integration verification

The [local NetBox harness](apps/node-hardware-inventory/tests/integration/README.md) runs real NetBox serializers, scoped permissions and conditional updates against ephemeral PostgreSQL with pinned official images. It requires an already running local engine and is separate from offline tests:

```sh
python3 apps/node-hardware-inventory/tests/integration/run.py --check
python3 apps/node-hardware-inventory/tests/integration/run.py
python3 -m unittest discover -s scripts -p 'test_*.py' -q
```

Deterministic tests exercise both hardware/lifecycle race orders, placement coexistence, bounded retries, ambiguous creation, interruption after native writes and full companion relist/restart recovery. Local integration passed against NetBox 4.6.5; the Django adapter excludes HTTPS/token transport and real parallel database locking. Both pinned Containerfiles also built locally for Linux amd64 and passed isolated `--help` smoke checks. Images have not been published; host collection and cluster admission remain separate verification.

## Helm repository

The default-branch workflow validates charts and publishes the Helm repository to GitHub Pages. After a successful publication:

```sh
helm repo add inventory-bridge https://rezzell.github.io/netbox-inventory-bridge/
helm repo update inventory-bridge
helm pull inventory-bridge/netbox-node-inventory --version 0.2.0
```

Every packaged chart change requires a new `Chart.yaml` version. Publication rebuilds historical versions from the full first-parent Git history and rejects changed content under an existing version. Package archives are normalized for reproducible bytes. Preserve main's history when maintaining releases; rewriting it can remove historical packages. Pull requests run read-only validation; Pages deployment runs only on main pushes or manual dispatch on main, with deployment permissions confined to its publishing job. Chart packages contain deployment templates and defaults; container images must be supplied separately.
