{{/*
Expand the name of the chart.
*/}}
{{- define "corpbot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Truncated to 63 chars for the k8s DNS name spec.
*/}}
{{- define "corpbot.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label value (name + version).
*/}}
{{- define "corpbot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "corpbot.labels" -}}
helm.sh/chart: {{ include "corpbot.chart" . }}
{{ include "corpbot.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: corpbot
{{- end -}}

{{/*
Selector labels (immutable subset used by Deployment selectors / Service selectors).
*/}}
{{- define "corpbot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "corpbot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: nanobot
{{- end -}}

{{/*
Name of the nanobot ServiceAccount.
*/}}
{{- define "corpbot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "corpbot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The namespace boxy provisions sandboxes in (and where nanobot's SA needs boxy.dev RBAC).
Mirrors boxy's own `global.sandboxNamespace | default .Release.Namespace`. When the boxy
subchart is enabled we share the release namespace by default (single-namespace install).
*/}}
{{- define "corpbot.boxyNamespace" -}}
{{- $boxy := .Values.boxy | default dict -}}
{{- $global := $boxy.global | default dict -}}
{{- default .Release.Namespace $global.sandboxNamespace -}}
{{- end -}}

{{/*
Name of the secret holding nanobot's env secrets (Slack + LLM key, optionally boxy token).
Either the chart-created secret or a pre-existing one supplied by the operator.
*/}}
{{- define "corpbot.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "corpbot.fullname" .) -}}
{{- end -}}
{{- end -}}
