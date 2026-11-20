{{/* Include namespace and a hash to keep cluster-scoped names unique after truncation. */}}
{{- define "inventory.name" -}}
{{- $identity := printf "%s-%s-netbox-node-inventory" .Release.Namespace .Release.Name -}}
{{- printf "%s-%s" ($identity | trunc 43 | trimSuffix "-") ($identity | sha256sum | trunc 8) -}}
{{- end -}}
{{- define "inventory.labels" -}}
app.kubernetes.io/name: netbox-node-inventory
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}
{{- define "inventory.config" -}}
{{- $root := .root -}}
{{- $component := index $root.Values .component -}}
{{- $config := dict "netbox_url" $root.Values.netbox.url "cluster_id" $root.Values.clusterId "dry_run" $component.dryRun "device_mappings" $root.Values.deviceMappings "token_file" "/run/netbox/token" "interval_seconds" $root.Values.intervalSeconds "jitter_seconds" $root.Values.jitterSeconds "grace_period_seconds" $root.Values.gracePeriodSeconds "relist_seconds" $root.Values.relistSeconds "watch_timeout_seconds" $root.Values.watchTimeoutSeconds -}}
{{- if eq .component "agent" -}}{{- $_ := set $config "host_root" "/host" -}}{{- end -}}
{{- if eq .component "companion" -}}
{{- $_ := set $config "placement" (dict "enabled" $component.placement.enabled "dry_run" $component.placement.dryRun "fields" $component.placement.fields) -}}
{{- end -}}
{{- $config | toPrettyJson -}}
{{- end -}}
