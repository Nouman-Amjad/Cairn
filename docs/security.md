# Security

## The one-line version

Prompt injection is not solved here. It is contained. The design assumes the
model will eventually be tricked, and places the security boundary at the tool
server, where a deterministic policy engine — not a language model — makes the
decision. An agent that gets injected loses the argument with OPA.

## Threat model

| # | Threat | Mitigation | Where | Residual |
|---|---|---|---|---|
| T1 | Prompt injection in a log line or runbook causes an unwanted tool call | Tool authz evaluated against the *user's* token at the MCP server; write tools additionally gated by human approval; results fenced as `<untrusted_data>` with angle brackets transliterated so the fence cannot be closed early | `cairn_mcp_kit/guard.py`, `cairn_core/prompts.py:fence` | Injection can still waste budget and skew a read-only answer. Accepted; the critic catches some. |
| T2 | Tool-call privilege escalation via crafted arguments | Schema validation at the MCP boundary; identifiers checked against a strict pattern before reaching a query; LogQL string literals escaped; no interpolation into PromQL | `cairn_mcp_observability/backends.py:check_ident`, `server.py:_logql_string` | Low |
| T3 | Data exfiltration to the cloud model | Deterministic sensitivity classification at the tool-result boundary; restricted trajectories never route to cloud; the router is the only pod with egress to the model API | `cairn_core/sensitivity.py`, `cairn_router/routing.py`, `templates/networkpolicy.yaml` | Classifier false negatives. Presidio plus namespace labels; sampled monitoring. |
| T4 | Poisoned runbook injects instructions | Ingest strips imperative-to-assistant patterns; every chunk records the committing author; content is fenced as data | `cairn_mcp_runbooks/ingest.py:defang` | Medium. A malicious insider with docs write access is a real gap; the audit trail is the compensating control. |
| T5 | Supply chain | Pinned lockfiles, SBOM per image, cosign signature verified at admission, Dependabot | `.github/workflows/ci.yml`, `templates/kyverno.yaml` | Low |
| T6 | Approval bypass or replay | Slack signature verification with a 5-minute window, single-use nonces, `approver != requester` enforced server-side, unique index on the idempotency key | `cairn_approval/slack.py`, `service.py` | Low |
| T7 | Stolen user token drives the agent | 5-minute internal JWT TTL, `trajectory_id` bound into the token and checked by every service, per-user daily cost budget | `cairn_core/auth.py`, `cairn_gateway/limits.py` | Medium |
| T8 | Cost-based denial of wallet | Per-user daily budget in Redis (fails closed), per-trajectory cap enforced in the router, GPU node ceiling in Terraform | `cairn_gateway/limits.py`, `cairn_router/app.py`, `modules/karpenter` | Low |

## The controls worth calling out

**The orchestrator has no internet access.** Not restricted — absent. The
NetworkPolicy has no `0.0.0.0/0` rule, and CI fails the build if one appears.
The component with model-controlled behaviour can reach the router, the tool
servers, Postgres and DNS. Every external call is brokered by a service with a
fixed, schema-validated interface. This is the highest-leverage control in the
design.

**The API key lives in the router.** Not the orchestrator, not the tool
servers. IRSA scopes the secret to one role. If the orchestrator is
compromised it cannot exfiltrate a credential it was never able to read. The
router is smaller, dumber, and has no model-controlled input — which is
precisely why the key lives there.

**Authorization is re-evaluated per call, at the tool server.** The most common
MCP mistake is authorising at the gateway and treating tool servers as
trusted. Every MCP call carries a 5-minute internal JWT that the tool server
verifies independently, then re-checks against OPA for that tool with those
arguments. The orchestrator's opinion about what it may do is never consulted.

**The audit log cannot be rewritten.** A database trigger rejects UPDATE and
DELETE on `audit_events`, and the S3 archive uses Object Lock in COMPLIANCE
mode for seven years. There are tests for both. The user-data deletion path
deliberately exempts the audit log; that is a documented legal-basis decision,
not an oversight.

**No service mesh.** Istio or Linkerd would give mTLS and richer traffic
policy, against a control plane to run, sidecar memory on every pod, and a
debugging surface a small team will lose days to. Instead: VPC CNI encryption
for pod-to-pod, NetworkPolicy plus OPA for authz, IMDSv2 with hop limit 1 so a
compromised pod cannot reach node credentials. Revisit at 30+ services, or a
compliance regime that demands mesh-level attestation.

## Sensitivity classification

Three levels, assigned deterministically at the tool-result boundary. A model
never decides what is sensitive.

- **public** — service names, deploy SHAs, aggregate metrics
- **internal** — log lines and trace payloads from internal services
- **restricted** — anything matching a PII pattern, anything from a namespace
  labelled `data-classification: restricted`, and anything from a namespace
  whose labels cannot be read (unknown is not permissive)

Once a restricted artifact enters a trajectory, that trajectory is restricted
for the rest of its life. Sensitivity is monotonic and never downgrades.

One tuning note that matters more than it looks: the credit-card pattern
requires both a Luhn check and a plausible issuer prefix. Without them,
floating-point noise like `0.9000000000000001` reads as a card number, every
metric result classifies restricted, the router sends 100% of traffic local,
and a "safe" default quietly deletes the product. There is a test for exactly
this case.

## Key rotation

Internal JWT keys rotate every 90 days with a dual-key overlap window:
`verify_internal` accepts the current key and the previous one, so a rotation
does not invalidate the tokens of investigations already in flight. The
minimum key length is enforced at boot, not warned about per request — a
short HMAC key from Secrets Manager fails startup rather than silently
downgrading every signature.
