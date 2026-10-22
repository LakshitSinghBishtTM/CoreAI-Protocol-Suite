{{- define "caips.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "caips.fullname" -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "caips.labels" -}}
app.kubernetes.io/name: {{ include "caips.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
{{- define "caips.selector" -}}
app.kubernetes.io/name: {{ include "caips.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "caips.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}{{ default (include "caips.fullname" .) .Values.serviceAccount.name }}{{ else }}{{ default "default" .Values.serviceAccount.name }}{{ end -}}
{{- end -}}
{{- define "caips.secretName" -}}
{{- if .Values.existingSecret }}{{ .Values.existingSecret }}{{ else if .Values.secret.create }}{{ include "caips.fullname" . }}{{ end -}}
{{- end -}}
