# NetBox node inventory

Modified for portable extraction: fictional endpoints and image repositories; installation evidence omitted.

This chart deploys two independent components against an existing remote HTTPS NetBox:

- **Agent:** a DaemonSet that reads bounded, read-only host hardware inventory and owns hardware fields in NetBox.
- **Companion:** one Deployment that watches Kubernetes nodes and owns Kubernetes status fields in NetBox.

Both components default to dry run. Neither deploys NetBox or depends on the other component being enabled. The chart creates no Namespace; use Helm's `--create-namespace`. Runtime behavior, field ownership, custom-field provisioning, mapping rules and container builds are documented in [the application README](../../apps/node-hardware-inventory/README.md).

## Install

```sh
helm upgrade --install node-inventory ./charts/netbox-node-inventory \
  --namespace node-hardware-inventory --create-namespace \
  --set clusterId=example-cluster \
  --set netbox.url=https://netbox.example.com \
  --set openshift.enabled=true
```

Enable `openshift.enabled` only on OpenShift. It creates an agent-specific SCC and grants only the agent service account in the release namespace permission to use it. Installing this option requires permission to create the SCC and associated RBAC. On other Kubernetes distributions, admission policy must independently permit the bounded hostPath agent. A restrictive default policy may reject it.

The agent requires Kubernetes 1.33 or later for stable recursive read-only mounts; chart rendering rejects older Kubernetes versions when the agent is enabled. Companion-only deployments remain allowed on older versions. The node container runtime must also support recursive read-only mounts; the version check does not replace runtime verification.

The container definitions target Linux; other architectures require separate build and runtime verification. Both components default to `kubernetes.io/arch: amd64`; operators supplying a suitable image may explicitly change that selector. Linux selection is always enforced. Configure `agent.nodeSelector.kubernetes.io/hostname` for a one-node canary, then remove that hostname selector to deploy to all matching nodes. The agent tolerates master/control-plane `NoSchedule` taints by default. Each component has independent `nodeSelector`, `tolerations` and `resources` settings.

```yaml
agent:
  enabled: true
  nodeSelector:
    kubernetes.io/arch: amd64
    kubernetes.io/hostname: worker.example.com
companion:
  enabled: false
```

For companion-only deployment, set `agent.enabled: false`. This creates no agent service account, host mounts or SCC, even with `openshift.enabled: true`. For agent-only deployment, set `companion.enabled: false`; all companion RBAC and resources disappear. Disabling both produces an empty manifest.

## Credentials and apply mode

Create separate existing Secrets in the release namespace through your secret manager, such as External Secrets Operator. This chart accepts only Secret references; token contents do not belong in values. Each component mounts its own selected key at `/run/netbox/token`, without a `subPath`, so Kubernetes can propagate Secret rotation. The default Secret key is `token`. No token volume is created when the reference is empty.

Keep dry run enabled while verifying mappings, provisioned fields, ownership and the emitted proposed changes. To apply, configure a positive NetBox device ID for each exact node name and independently enable writes for the intended component:

```yaml
netbox:
  url: https://netbox.example.com
clusterId: example-cluster
deviceMappings:
  worker.example.com:
    device_id: 123
    serial_source: product_serial
    expected_serial: EXPECTED-SERIAL
agent:
  dryRun: false
  credentials:
    existingSecret: netbox-hardware-agent
    key: token
companion:
  dryRun: true
  credentials:
    existingSecret: netbox-kubernetes-companion
    key: token
```

`expected_serial` is optional. `serial_source` defaults to `product_serial`; the only alternative is an explicitly reviewed `board_serial`, which must match the mapped Device serial. The hardware agent never falls back between sources. Each enabled component in apply mode requires both a Secret reference and nonempty mappings. The runtime performs additional validation before writing. Either enabled component requires an explicitly configured, nonempty `clusterId`. Its default is empty so an installation cannot accidentally reuse a sample cluster identity; set it to your actual cluster identity. Plaintext token fields and unknown credential fields fail schema validation. Credentials should have only the NetBox permissions required by the respective component. The two Secrets may contain different tokens and scopes.

The separate [activation bootstrap](../../apps/node-hardware-inventory/activation/README.md) can provision dedicated accounts, bounded object permissions and JSON fields for operator-supplied mappings. It remains opt-in and outside this chart. NetBox object permissions constrain Device access; runtime code enforces individual field ownership.

## Images and local source override

Each component has independent `image.repository`, `image.tag`, `image.digest` and `image.pullPolicy`. Defaults are `registry.example.com/netbox-hardware-agent:0.2.0` and `registry.example.com/netbox-kubernetes-companion:0.2.0`; publishing these images is a separate release step. Override with images built from the application sources for a local deployment. A nonempty SHA-256 digest takes precedence over the tag. Top-level `imagePullSecrets` accepts a list of `{name: existing-pull-secret}` references.

The default image repositories are placeholders. Build images from these sources and override both repositories before installation; no published image is assumed.

The chart runs `python3 /opt/inventory/agent.py --config /etc/inventory/config.json` and the analogous `companion.py` command. Regular images must contain the application Python files under `/opt/inventory`. The chart does not duplicate application source.

For a local canary before image publication, create a ConfigMap containing **all** application Python files and set the relevant `sourceConfigMap`. This mounts that external ConfigMap at `/opt/inventory`; it does not create or update it. Use a Python image compatible with the application and the component's user security settings. Recreate pods after changing external source content; chart checksums cover chart-generated configuration, not external ConfigMaps or Secrets. Source and credential references can be configured independently:

```yaml
agent:
  sourceConfigMap: inventory-python-source
  image:
    repository: image-registry.openshift-image-registry.svc:5000/node-hardware-inventory/agent
    tag: canary
companion:
  sourceConfigMap: ""
  image:
    repository: image-registry.openshift-image-registry.svc:5000/node-hardware-inventory/companion
    tag: canary
```

## Timing and isolation

Each component receives a separate JSON ConfigMap. The top-level timing values are `intervalSeconds: 3600`, `jitterSeconds: 60`, `gracePeriodSeconds: 300`, `relistSeconds: 300` and `watchTimeoutSeconds: 60`. Durations must be positive except jitter, which may be zero. Configuration checksums trigger a rollout when generated configuration changes. The companion uses one replica and `Recreate` strategy to avoid overlapping writers during upgrades.

The agent runs as UID 0 to read root-owned DMI files, with no capabilities, no privilege escalation, a read-only root filesystem and RuntimeDefault seccomp. It has no Kubernetes API token or node API permissions. It reads only `/sys`, `/proc/cpuinfo`, `/proc/meminfo` and `/etc/os-release` through host mounts. The `/sys` mount requires `recursiveReadOnly: Enabled` and `mountPropagation: None`; verify runtime support and actual mount state before broader deployment. An unsupported runtime must reject the pod rather than leave nested mounts writable. Neither component uses host PID, IPC or networking.

The agent SCC retains namespace SELinux and filesystem-group assignment (`MustRunAs`) and requires all capabilities dropped. **SCC does not enforce a hostPath allowlist or read-only hostPath mounts.** Those restrictions are workload manifest controls. Granting this SCC to unrelated service accounts would allow broader host access. The companion has no host mounts or elevated SCC grant, runs as non-root without a fixed UID so OpenShift can allocate one, and receives only `get`, `list` and `watch` permissions for nodes. It uses its own service account's in-cluster CA and rotating token.

## Verify

```sh
helm lint ./charts/netbox-node-inventory --set clusterId=example-cluster
python3 charts/netbox-node-inventory/tests/test_chart.py
helm template node-inventory ./charts/netbox-node-inventory \
  --namespace node-hardware-inventory --set clusterId=example-cluster --set openshift.enabled=true
```

Structural tests require Helm, Python 3 and system Ruby's YAML/JSON libraries; no packages are installed. They cover all enablement/OpenShift combinations, confinement, RBAC scope, image and Secret independence, config rollouts, naming and invalid configurations. Template tests do not establish admission, runtime mount state or NetBox correctness; validate these on the target cluster in dry run before enabling writes.
