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

{{/*
Validated checker mode. Use this rather than .Values.checker.mode directly, so a
typo fails the install instead of silently falling back at runtime — the operator
treats an unrecognised mode as perNamespace, which would quietly deploy hundreds
of checkers to someone who asked for one.
*/}}
{{- define "kubeic-operator.checkerMode" -}}
{{- $mode := .Values.checker.mode | default "perNamespace" -}}
{{- if not (has $mode (list "perNamespace" "central")) -}}
{{- fail (printf "checker.mode must be \"perNamespace\" or \"central\", got %q" $mode) -}}
{{- end -}}
{{- $mode -}}
{{- end }}

{{/*
Central checker name. Distinct from the per-namespace checker's fixed
"kubeic-checker" names on purpose: the operator deletes those by name in every
namespace it drains, and the operator namespace is drained like any other when
mode flips to central.
*/}}
{{- define "kubeic-operator.centralCheckerName" -}}
{{- printf "%s-checker" (include "kubeic-operator.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Central checker selector labels (stable — must not change between releases).
component: checker is what the existing checker ServiceMonitor selects on, so the
central checker is scraped by it without a ServiceMonitor of its own.
*/}}
{{- define "kubeic-operator.centralCheckerSelectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeic-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: checker
{{- end }}

{{/*
Central checker labels
*/}}
{{- define "kubeic-operator.centralCheckerLabels" -}}
{{ include "kubeic-operator.labels" . }}
app.kubernetes.io/component: checker
{{- end }}
