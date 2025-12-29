{{/*
Expand the name of the chart.
*/}}
{{- define "jellyfin-db-sync.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "jellyfin-db-sync.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "jellyfin-db-sync.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "jellyfin-db-sync.labels" -}}
helm.sh/chart: {{ include "jellyfin-db-sync.chart" . }}
{{ include "jellyfin-db-sync.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "jellyfin-db-sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "jellyfin-db-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "jellyfin-db-sync.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "jellyfin-db-sync.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return the proper image name
*/}}
{{- define "jellyfin-db-sync.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}

{{/*
Return the config secret name
*/}}
{{- define "jellyfin-db-sync.secretName" -}}
{{- if .Values.config.existingSecret }}
{{- .Values.config.existingSecret }}
{{- else }}
{{- include "jellyfin-db-sync.fullname" . }}
{{- end }}
{{- end }}

{{/*
Return the PVC name
*/}}
{{- define "jellyfin-db-sync.pvcName" -}}
{{- if .Values.persistence.existingClaim }}
{{- .Values.persistence.existingClaim }}
{{- else }}
{{- include "jellyfin-db-sync.fullname" . }}
{{- end }}
{{- end }}

{{/*
Check if any server needs a chart-managed secret (has apiKey but no existingSecret)
*/}}
{{- define "jellyfin-db-sync.needsSecret" -}}
{{- $needsSecret := false }}
{{- range .Values.config.servers }}
{{- if and .apiKey (not .existingSecret) }}
{{- $needsSecret = true }}
{{- end }}
{{- end }}
{{- $needsSecret }}
{{- end }}

{{/*
Get environment variable name for server API key
*/}}
{{- define "jellyfin-db-sync.serverEnvName" -}}
{{- printf "%s_API_KEY" (. | upper | replace "-" "_") }}
{{- end }}
