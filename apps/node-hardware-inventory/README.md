# Node inventory runtimes

Modified for portable extraction: fictional defaults and examples; installation configuration and operational evidence omitted.

This opt-in application has two independently deployable Python standard-library runtimes and one Helm chart at `charts/netbox-node-inventory`. Both default to dry-run. The earlier `collector.py` snapshot interface and `mapper.py` offline proposal interface remain available.

- `agent.py`: a periodic hardware DaemonSet. It reads narrowly mounted Linux host files, resolves an explicit Device mapping, verifies the observed serial, and reconciles native CPU/DIMM modules and physical interfaces/MAC observations directly with NetBox.
- `companion.py`: a single-replica Kubernetes lifecycle Deployment. It has no host mounts and uses only Kubernetes Nodes get/list/watch permissions. It records Kubernetes readiness and membership separately from physical hardware.
- `runtime.py`: shared explicit identity, configuration, HTTPS transport and conditional custom-field updates. There are no additional Python dependencies or subprocess collectors.

Neither runtime creates Devices, guesses a Device from a hostname, changes native Device status, or deletes Devices/components. Native placement changes require the companion’s separate opt-in placement capability. Disk modules remain deferred until physical slots are reliable. The default endpoint is `https://netbox.example.com`; other HTTPS endpoints are supported.

## Configuration and execution

Use `config.example.json` as a starting point. Each component gets its own JSON config, allowing either to be enabled alone. The chart mounts it at `/etc/inventory/config.json`.

| Key | Default / meaning |
| --- | --- |
| `cluster_id` | Required stable ownership identifier |
| `netbox_url` | `https://netbox.example.com`, verified HTTPS only |
| `dry_run` | `true`; hardware dry-run is completely offline |
| `device_mappings` | `{}`; explicit node FQDN to `{"device_id": 123, "serial_source": "product_serial", "expected_serial": "reviewed value"}`; serial source defaults to `product_serial`, expected serial is optional |
| `token_file` | `/run/netbox/token`; read afresh for every NetBox HTTP request |
| `host_root` | `/host`, hardware agent only |
| `interval_seconds` / `jitter_seconds` | `3600` / `60`, hardware refresh and random delay |
| `grace_period_seconds` | `300`, Ready=False grace |
| `relist_seconds` / `watch_timeout_seconds` | `300` / `60`; watch sessions are bounded by the smaller value |

Device IDs must be positive integers and unique across the configured node mappings. Configuration contains no tokens. The projected Secret file must contain the **bare** legacy token or the complete bare v2 `nbt_<key>.<token>` value, without an `Authorization`, `Bearer` or `Token` prefix. A trailing newline is stripped; embedded whitespace is rejected. v2 uses Bearer authentication; legacy tokens use Token authentication. The hardware agent never needs a Kubernetes API token. The companion uses the standard in-cluster service account token and CA paths.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s apps/node-hardware-inventory/tests -v
NODE_NAME=node-a python3 apps/node-hardware-inventory/agent.py \
  --config apps/node-hardware-inventory/config.example.json --once
python3 apps/node-hardware-inventory/companion.py --config /etc/inventory/config.json --once
# Existing snapshot/report interfaces:
python3 apps/node-hardware-inventory/collector.py --host-root / --node-name node-a --once > /tmp/snapshot.json
python3 apps/node-hardware-inventory/mapper.py /tmp/snapshot.json --device-id 123 > /tmp/plan.json
```

The companion command requires in-cluster Kubernetes API access even during dry-run. With placement disabled, it makes no NetBox requests in dry-run. Enabling placement requires authenticated NetBox reads even in dry-run, to validate references and durable Node identity. With empty mappings, both modes report unmapped observations and skip registration. A mapped hardware dry-run emits the existing pure mapper plan and does not read credentials or contact NetBox; its symbolic references describe intended resolution, not completed writes. `--once` exits nonzero on a reconciliation failure. Periodic hardware failures use bounded backoff and jitter instead of deliberately restarting the container. Companion API failures preserve membership until a successful complete relist.

## NetBox provisioning and ownership

An administrator must pre-provision **two optional JSON custom fields** on `dcim.device`: `hardware_observation` and `kubernetes_lifecycle`. `custom-fields.json` contains the corresponding REST create definitions for `/api/extras/custom-fields/`; review against existing fields before applying with separate administrator credentials. The runtimes require the fields to appear in the Device response and never create or modify schema. Do not grant their tokens custom-field administration permissions.

The separate [opt-in activation bootstrap](activation/README.md) provisions these fields and dedicated scoped accounts using the installed NetBox Django configuration. It is a manually invoked administrator utility, separate from the Helm release. No CronJob, secret-manager wiring or installation configuration is included. Device-scoped NetBox permissions contain access to the reviewed Devices; field ownership is enforced by the runtimes because NetBox object permissions do not restrict individual fields.

Review Device IDs and Device serials in NetBox before enabling `dry_run: false`. Hardware writes require the selected observed serial to match the existing mapped Device serial. Each mapping accepts only `serial_source: product_serial` (default) or `serial_source: board_serial`. Use board identity only after verifying the board serial uniquely identifies the physical node, such as a child server whose product serial identifies a shared chassis. There is no automatic fallback between sources. If `expected_serial` is supplied, it must also match the selected source. Missing serials and firmware placeholders, including `0123456789`, fail closed. Companion identity uses the explicit mapping, cluster owner and durable Kubernetes Node UID. Duplicate Device mappings, cross-cluster ownership and Node UID replacement are refused. A node recreated with a new UID needs an operator identity review and deliberate reset of its lifecycle field before the new UID can be adopted.

Hardware owns only `hardware_observation` on the Device: `cluster_id`, `node_name`, `serial`, `serial_source`, `last_seen`, `summary`, collector `errors`, and `hardware_hash`. The companion owns only `kubernetes_lifecycle`: `cluster_id`, `node_name`, `node_uid`, `ready`, `unschedulable`, `state`, `last_transition`, `removed_at`. Existing nonempty custom fields with a different cluster/node owner are refused. Unrelated fields are preserved.

NetBox 4.6.5 [custom-field deserialization merges supplied keys into the existing instance](https://github.com/netbox-community/netbox/blob/v4.6.5/netbox/extras/api/customfields.py). Because two simultaneous stale merges could still lose updates, every Device PATCH sends only the component's own key **and** the ETag from a fresh detail GET in `If-Match`. Missing ETags fail closed; HTTP 412 triggers at most three fresh reads with renewed ownership and serial/UID checks. NetBox 4.6.5 [rechecks conditional updates under a row lock](https://github.com/netbox-community/netbox/blob/v4.6.5/netbox/netbox/api/viewsets/__init__.py). This is required for independent agents updating the same Device safely.

Native reconciliation resolves the pure mapper references to numeric IDs for ModuleBay, ModuleType, Module, Interface and MACAddress payloads, following the NetBox 4.6.5 [Device/Module/MAC serializers](https://github.com/netbox-community/netbox/blob/v4.6.5/netbox/dcim/api/serializers_/devices.py), [component serializers](https://github.com/netbox-community/netbox/blob/v4.6.5/netbox/dcim/api/serializers_/device_components.py) and [type serializers](https://github.com/netbox-community/netbox/blob/v4.6.5/netbox/dcim/api/serializers_/devicetypes.py). Manufacturers and ModuleTypes are shared catalog references: exact existing matches are reused without editing attributes. Creation uses uniqueness-conflict/ambiguous-timeout relookup, never a blind POST retry. Ambiguous matches fail closed.

Created native components carry a deterministic cluster/node ownership marker in `description`. Matching unowned components are skipped, including their children and MAC assignments. Only an owned Module's observed serial/module type may be updated, using conditional PATCH; curated status, asset tags, comments and other attributes remain unchanged. Module creation explicitly disables component replication and adoption, so existing catalog templates cannot unexpectedly populate or adopt components. Interfaces use conservative type `other`. MACs are current sysfs observations; permanent MAC identity is not established. Existing MAC records are never reassigned. There are no automatic deletions or stale-component pruning.

## Lifecycle semantics

Ready=True yields `ready`; Ready=False yields `pending` until its condition transition has aged beyond the grace period, then `not_ready`. Ready=Unknown or a missing Ready condition yields `unreachable`. None of these states implies physical power state. Cordon is recorded independently as `unschedulable`.

A mapped node UID/ownership review failure aborts the current ordered reconciliation cycle. Earlier mappings may already have updated; later mappings wait until operator review resolves the failure. This release does not isolate such failures per node.

Startup and every watch completion perform an authoritative complete paginated LIST before reconciliation. Watch events only invalidate that snapshot; raw DELETED payloads never remove membership directly. This protects current Nodes from stale delete events and name reuse. Expired resource versions (410) cause relist/reconnect. List failures, inconsistent resource versions, duplicate identity, incomplete pages and API outages never become removal observations.

A mapped Node absent from a successful complete LIST is marked `removed` only when its previously owned lifecycle JSON already exists in NetBox. That durable state recovers missed removals across companion restarts. The physical Device and all components remain. This release requires one companion replica with Recreate strategy; it has no leader election.

## Images and rollout

Build from the application directory, preserving the pinned UBI Python base digest:

```sh
podman build -f Containerfile.agent -t node-inventory-agent .
podman build -f Containerfile.companion -t node-inventory-companion .
```

`Containerfile.agent` uses UID 0 solely for host DMI DAC access. `Containerfile.companion` uses UID 1001; its source is readable by OpenShift's allocated UID, and neither image needs writable image paths. Both disable Python bytecode writes. The generic `Containerfile` remains the legacy snapshot-only image. Build/publication and live NetBox apply must be verified separately; passing fake API tests does not establish live writer readiness.

Both periodic runtimes explicitly handle SIGTERM and exit cleanly, including during interval sleep or API requests, so normal Kubernetes termination does not wait for the grace period to expire. Interrupted reconciliation may leave a partial set of native observations; the next run resolves existing objects before continuing.

Use the Helm chart for periodic runtimes, reviewing its independently optional components, image references, mappings and security controls. For an unpublished-source experiment, the chart supports a source ConfigMap containing every application `.py` file at `/opt/inventory`. Start with dry-run and empty mappings; set reviewed mappings and provision fields before considering an apply rollout. Roll back by disabling either chart component or uninstalling the release. Previously written observations/components remain in NetBox for operator review; rollback never deletes inventory.

## Confinement and sensitive observations

The custom OpenShift SCC permits UID 0 and hostPath access, retains the namespace's normal `container_t` SELinux/MCS context, drops all capabilities, and forbids privileged mode, escalation, host networking, host PID/IPC and writable container root filesystems. Narrow mounts are `/sys`, `/proc/cpuinfo`, `/proc/meminfo`, `/etc/os-release`. `/sys` requires `recursiveReadOnly: Enabled` with `mountPropagation: None`; unsupported runtimes must reject the pod. The chart requires Kubernetes 1.33 or later when the agent is enabled, avoiding older API servers that could discard the recursive-read-only field. Companion-only deployment has no such version gate. The SCC itself does not enforce a path allowlist or read-only mounts: review the workload manifests and actual runtime mount state. Do not grant unrelated workloads this SCC.

Serials, UUIDs and MACs are sensitive operational inventory. Collector snapshots and mapped dry-run plans include them; control pod log access and retention. Tokens, authorization headers and raw upstream error bodies are never logged. HTTPS uses CA verification, rejects redirects and cross-origin pagination, and has finite request timeouts and bounded GET retries. NetBox and Kubernetes access use distinct projected tokens. Hardware dry-run does not need the NetBox Secret. Companion placement requires authenticated reads even in dry-run.

## Verification and limits

The standard-library suite covers existing SMBIOS/mapper fixtures plus native reconciliation idempotence, catalog-create races, curated preservation, serial/mapping refusals, own-field conditional retries, rotation/auth formatting, TLS/redirect/origin protections, bounded retries, lifecycle grace/recovery/cordon/removal/restart recovery/UID replacement, incomplete lists and watch 410. These tests use synthetic fixtures; they do not establish compatibility with a live NetBox or Kubernetes installation.

The SMBIOS parser supports processor type 4 and memory-device type 17 fields only. Malformed or unusual firmware yields collection errors. Disks behind RAID, USB and unusual transports need further validation; no physical disk bays, SMART, firmware or PSU inventory is implemented. PCI BDF and kernel disk names are observations rather than stable asset identifiers. IP addresses and permanent MAC discovery are omitted; SSH/Redfish enrichment remains deferred. Permission failures are recorded rather than automatically broadening privileges.

## Optional placement mappings

The companion can map allowlisted Node labels and annotations to existing native Device `site`, `location`, `rack` and `cluster` references. `region` is a constraint on the selected or retained Site's Region, not a writable Device field; it never changes a shared Site. A Kubernetes region/zone can instead map directly to a Site or Location through configured translations. All targets are positive NetBox IDs; no names are guessed, and no catalogue objects are created.

Helm values use `companion.placement.enabled` (default `false`), `companion.placement.dryRun` (default `true`), and `companion.placement.fields`. The corresponding runtime JSON is `placement: {enabled, dry_run, fields}`. For example:

```yaml
companion:
  placement:
    enabled: true
    dryRun: true
    fields:
      site:
        annotation: netbox.example.com/site-id
        labels:
          - key: topology.kubernetes.io/region
            values:
              us-east-1: 10
      location:
        annotation: netbox.example.com/location-id
        labels:
          - key: topology.kubernetes.io/zone
            values:
              us-east-1a: 20
      rack:
        annotation: netbox.example.com/rack-id
```

`deviceMappings.<node>.placement` accepts explicit per-node ID overrides for those fields and `region`. Overrides intentionally supersede metadata for that field. Otherwise, configured annotations contain a decimal ID and all present configured sources must agree. Conflicting sources, invalid IDs, present labels without translations, unavailable references or inconsistent Site/Location/Rack relationships block the entire placement update for that Device. Absent keys preserve existing placement. Unknown keys outside the configured allowlist are ignored; arbitrary labels/annotations are never copied into NetBox or logs. Results include selected IDs, source keys, proposed `from`/`to` changes and issues.

Both global companion dry-run and placement dry-run must be false to PATCH placement. Keeping placement dry-run true allows normal lifecycle writes to continue. Placement errors are reported per Device without stopping lifecycle or later placement checks. Removed Nodes never move an asset. Placement writes recheck owner/Node UID, require ETags, and re-plan after HTTP 412. Only changed placement fields are sent; custom fields, status, identity, rack face/position and other curated attributes are untouched.

Chassis/device-bay installation and moving a racked Device with a U position require manual review and are outside this capability. Child Devices already installed in a parent chassis cannot be independently relocated. Selecting a rack never infers a bay or U position.

Kubernetes access remains Nodes `get/list/watch`; no Node patch permission is added. Enabling metadata-driven placement trusts writers of the configured Node metadata, so choose administrator-managed keys and explicit translations. The scoped NetBox account must also have `view` on referenced Regions/Sites/Locations/Racks/Clusters and any retained placement or ancestor Location needed for consistency checks. IDs that exist but are outside its read scope fail closed. Existing Device change grants are object-level, not field-level; application code enforces the placement dry-run boundary.
