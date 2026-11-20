# Disposable NetBox integration

Run separately from offline discovery; Docker or Podman with Compose and an already running local Unix socket engine is required. The runner never starts an engine or changes its context. Remote endpoints are refused. No credentials, host inventory, Kubernetes config or container socket are mounted into services.

```sh
python3 apps/node-hardware-inventory/tests/integration/run.py --check
python3 apps/node-hardware-inventory/tests/integration/run.py
# Expected exit status 1, with Project cleanup completed:
python3 apps/node-hardware-inventory/tests/integration/run.py --inject-failure
```

The Compose file pins official NetBox `v4.6.5-5.0.2`, PostgreSQL `18-alpine` and Valkey `9.1-alpine` by digest. Each invocation creates a unique internal network without host ports, temporary PostgreSQL storage, and finally removes only that project's services, volumes and network. The printed recovery command handles an interrupted host process. Startup is bounded to 180 seconds and Django tests to 600 seconds. Missing prerequisites return exit status 2 (`NOT RUN`). An unavailable local engine is not a passing integration test.

NetBox's Django test runner creates and destroys its own database. Synthetic fixtures and separate users use the existing activation permission specifications. The adapter calls real NetBox API views with DRF APIClient and `force_authenticate`: this covers serializers, ORM constraints, object permissions, filters, weak ETag conditional updates, replay, curated preservation and placement apply/dry-run. It does **not** cover HTTPS, sockets, token parsing/rotation, simultaneous PostgreSQL lock races, Kubernetes API access, full bootstrap execution or cluster admission. Offline event-coordinated tests cover simultaneous client behavior.

Verified locally on 2026-11-20: four integration tests passed against the pinned images, with successful project cleanup. A second run deliberately failed a fixture assertion and also cleaned its project. Image/API compatibility claims are limited to this fixture and these exact pins.
