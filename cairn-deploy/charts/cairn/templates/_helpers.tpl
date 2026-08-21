{{/*
Shared helpers. Every service is rendered from the same template, so a
security default fixed here is fixed everywhere.
*/}}

{{- define "cairn.name" -}}
cairn-{{ . }}
{{- end -}}

{{- define "cairn.labels" -}}
app.kubernetes.io/name: {{ include "cairn.name" .name }}
app.kubernetes.io/part-of: cairn
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ .root.Values.global.image.tag | quote }}
{{- if .svc.tier }}
tier: {{ .svc.tier }}
{{- end }}
{{- end -}}

{{- define "cairn.image" -}}
{{ .root.Values.global.image.registry }}/{{ .svc.image }}:{{ .root.Values.global.image.tag }}
{{- end -}}

{{/*
Environment shared by every Python service. Secrets arrive as env vars from
External Secrets; nothing here contains a credential.
*/}}
{{- define "cairn.env" -}}
- name: CAIRN_ENV
  value: {{ .root.Values.global.env | quote }}
- name: CAIRN_PORT
  value: "8000"
- name: CAIRN_UI_BASE_URL
  value: {{ .root.Values.global.uiBaseUrl | quote }}
- name: CAIRN_PROMPT_DIR
  value: /etc/cairn/prompts
- name: CAIRN_OTEL_ENDPOINT
  value: {{ .root.Values.global.otelEndpoint | quote }}
- name: CAIRN_OTEL_LOG_LEVEL
  value: {{ .root.Values.global.logLevel | quote }}
- name: CAIRN_DB_DSN
  valueFrom:
    secretKeyRef:
      name: cairn-database
      key: dsn
- name: CAIRN_DB_POOL_SIZE
  value: {{ .root.Values.database.poolSize | quote }}
- name: CAIRN_REDIS_URL
  value: "redis://{{ .root.Values.redis.host }}:{{ .root.Values.redis.port }}/0"
- name: CAIRN_S3_BUCKET
  value: {{ .root.Values.s3.bucket | quote }}
- name: CAIRN_S3_REGION
  value: {{ .root.Values.global.region | quote }}
{{- if .root.Values.s3.kmsKeyId }}
- name: CAIRN_S3_KMS_KEY_ID
  value: {{ .root.Values.s3.kmsKeyId | quote }}
{{- end }}
- name: CAIRN_AUTH_OIDC_ISSUER
  value: {{ .root.Values.auth.oidcIssuer | quote }}
- name: CAIRN_AUTH_OIDC_AUDIENCE
  value: {{ .root.Values.auth.oidcAudience | quote }}
- name: CAIRN_AUTH_DEV_MODE
  value: {{ .root.Values.auth.devMode | quote }}
- name: CAIRN_AUTH_INTERNAL_JWT_KEY
  valueFrom:
    secretKeyRef:
      name: cairn-internal-jwt
      key: current
- name: CAIRN_AUTH_INTERNAL_JWT_KEY_PREVIOUS
  valueFrom:
    secretKeyRef:
      name: cairn-internal-jwt
      key: previous
      optional: true
- name: CAIRN_POLICY_ENABLED
  value: {{ .root.Values.policy.enabled | quote }}
- name: CAIRN_APPROVAL_URL
  value: http://cairn-approval:8000
- name: CAIRN_ROUTER_URL
  value: http://cairn-router:8000
- name: CAIRN_ORCHESTRATOR_URL
  value: http://cairn-orchestrator:8000
- name: CAIRN_MCP_OBSERVABILITY_URL
  value: http://cairn-mcp-observability:8000/mcp
- name: CAIRN_MCP_RUNBOOKS_URL
  value: http://cairn-mcp-runbooks:8000/mcp
- name: CAIRN_MCP_ACTIONS_URL
  value: http://cairn-mcp-actions:8000/mcp
- name: CAIRN_EMBED_URL
  value: http://cairn-embeddings:8080
- name: CAIRN_EMBED_RERANKER_URL
  value: http://cairn-reranker:8080
- name: CAIRN_MAX_DAILY_COST_PER_USER_USD
  value: {{ .root.Values.budgets.maxDailyCostPerUserUsd | quote }}
- name: CAIRN_RATE_LIMIT_PER_MINUTE
  value: {{ .root.Values.budgets.rateLimitPerMinute | quote }}
- name: HOSTNAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
{{- end -}}

{{- define "cairn.backendEnv" -}}
- name: CAIRN_BACKEND_PROMETHEUS_URL
  value: {{ .Values.backends.prometheusUrl | quote }}
- name: CAIRN_BACKEND_LOKI_URL
  value: {{ .Values.backends.lokiUrl | quote }}
- name: CAIRN_BACKEND_TEMPO_URL
  value: {{ .Values.backends.tempoUrl | quote }}
- name: CAIRN_BACKEND_ARGOCD_URL
  value: {{ .Values.backends.argocdUrl | quote }}
- name: CAIRN_BACKEND_JIRA_URL
  value: {{ .Values.backends.jiraUrl | quote }}
- name: CAIRN_BACKEND_JIRA_PROJECT
  value: {{ .Values.backends.jiraProject | quote }}
- name: CAIRN_BACKEND_GITHUB_ORG
  value: {{ .Values.backends.githubOrg | quote }}
{{- end -}}

{{/*
Pod-level security context. Kyverno rejects anything that does not match, so
this is belt and braces on purpose.
*/}}
{{- define "cairn.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "cairn.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
