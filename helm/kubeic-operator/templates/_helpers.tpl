{{/*
Expand the name of the chart.
*/}}
{{- define "kubeic-operator.name" -}}
{{- default "kubeic-operator" .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "kubeic-operator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := include "kubeic-operator.name" . }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "kubeic-operator.labels" -}}
app.kubernetes.io/name: {{ include "kubeic-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- with .Values.additionalLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Operator labels
*/}}
{{- define "kubeic-operator.operatorLabels" -}}
{{ include "kubeic-operator.labels" . }}
app.kubernetes.io/component: operator
{{- end }}

{{/*
Operator selector labels (stable — must not change between releases)
*/}}
{{- define "kubeic-operator.operatorSelectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeic-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: operator
{{- end }}

{{/*
Operator image
*/}}
{{- define "kubeic-operator.operatorImage" -}}
{{- $img := .Values.operator.image }}
{{- printf "%s:%s" $img.repository $img.tag }}
{{- end }}

{{/*
Checker image
*/}}
{{- define "kubeic-operator.checkerImage" -}}
{{- $img := .Values.checker.image }}
{{- printf "%s:%s" $img.repository $img.tag }}
{{- end }}
