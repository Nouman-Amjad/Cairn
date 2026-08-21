# Operations

## SLOs

| SLO | Target | Window | Error budget |
|---|---|---|---|
| Query availability | 99.5% | 30d | 3.6h |
| Query latency p95 < 45s | 95% | 30d | n/a |
| Write actions correctly gated | 100% | forever | **zero** |
| Approval delivered to Slack < 10s | 99% | 30d | n/a |

The third has no error budget. A single unapproved write is an incident
regardless of rate. The reconciliation job compares the audit log against the
actual mutation log hourly and pages on any discrepancy; the canary analysis
also fails immediately on a non-zero `cairn_unapproved_write_total`.

## What pages, and what to do

### CairnUnapprovedWrite

**Page.** A write executed without a matching approval record.

1. `SELECT * FROM audit_events WHERE event LIKE 'action.%' ORDER BY at DESC LIMIT 20;`
2. Cross-check each against `approvals` by `approval_id`. Any `action.executed`
   without an `approval.granted` for the same id is the incident.
3. Scale `cairn-mcp-actions` to zero. It is the only path to a side effect, so
   this stops the bleeding without touching anything else.
4. Preserve the audit rows — they are immutable, so nothing needs protecting
   from an operator, only from a hasty database restore.

### CairnTrajectoryFailureRate

More than 5% of trajectories reaching FAILED over 15 minutes.

Almost always one of: the router cannot reach either tier, a tool server is
down, or a prompt change broke JSON parsing. Check in that order:

```
kubectl -n cairn logs deploy/cairn-router --tail=100 | grep provider_failed
kubectl -n cairn get pods -l tier=mcp
kubectl -n cairn exec deploy/cairn-router -- curl -s localhost:8000/readyz
```

If the last deploy was a prompt change, roll it back. `prompt_version` on the
trajectory row tells you which bundle produced the failures.

### CairnCostPerQueryHigh

Mean cost per trajectory above $0.90 for an hour.

This is usually the local tier being unavailable, not the agent getting
greedy. Check `cairn_route_decisions_total` by reason: a spike in
`cold_start_spill` or `local_saturated_spill` means GPU capacity, not prompts.
If the split looks normal, compare `cairn_trajectory_iterations` against
baseline — a prompt change that makes the agent explore more shows up here
before it shows up on the invoice.

### CairnLocalShareLow

Local share below 55% for 30 minutes. The router's entire value proposition,
as an alert. Same diagnosis path as above.

### CairnVllmPreempting

vLLM is preempting sequences: the KV cache is undersized for the concurrency
it is being given. Either `max_num_seqs` is too high for `max_model_len`, or
transcripts have grown past what compaction is keeping them to. Check
`cairn_compactions_total` — if it is flat while iteration counts rise, the
high-water mark is not being reached and should come down.

### CairnApprovalsExpiring

More than three approvals timed out in a day. Nobody is watching the channel.
This is a process problem, not a systems one, but it silently converts
investigations into ABANDONED, so it is worth chasing.

## Routine operations

**Retune the local/cloud split.** Edit `cairn-deploy/routing/policy.yaml`, open
a PR. ArgoCD applies it and the router hot-reloads within seconds. No deploy,
no restart. A malformed file is rejected and the previous version stays live —
check for `configmap_rejected` in the router logs if a change appears not to
land.

**Vendor price change.** Same, in `routing/prices.yaml`.

**Change who may roll back what.** Edit `cairn-deploy/policy/data.json`
ownership map, and add a test to `policy/cairn_test.rego`. The bundle is
rebuilt by CI and OPA picks it up within 30 seconds.

**Rotate the internal JWT key.** Write the new value to
`cairn/prod/internal-jwt-current` and move the old one to `...-previous`.
Both are accepted during the overlap; drop `previous` after 10 minutes (twice
the token TTL).

**Investigate a specific trajectory.** The UI at `/trajectories/{id}` shows
every step, which model ran it, why the router chose that model, what it cost
and what the tool returned. Everything there comes from `trajectory_steps`,
which is append-only.

**Replay an investigation.** The full transcript is in Postgres regardless of
what was compacted out of the context window. Compaction is a context decision,
never data loss.

## Deployment

No human has `kubectl apply` in production. Git is the only path:

- **App code** — merge to `main`, CI builds and signs, CD pins staging, a human
  opens a PR against `values/prod.yaml` to promote.
- **Prompts** — a PR against `cairn-deploy/prompts/`. The eval gate runs on it
  like any other change, which is the point of versioning them as files.
- **Infrastructure** — a PR against `cairn-infra`, applied by a human after
  reading the plan.

The canary gates on error rate, p95 latency, **and mean cost per trajectory**.
That last one catches the failure mode nobody instruments: a change that is
functionally fine but triples token consumption. It will not page anyone and
it will not fail a health check; it will show up on the invoice five weeks
later.

## Data retention

| Data | Retention | Why |
|---|---|---|
| Trajectories and steps | 180 days | eval corpus and postmortem reference |
| S3 artifacts | 30 days, Glacier from day 7 | bulk of storage, rarely reread after a week |
| Approvals and audit log | 7 years, WORM | compliance; immutable by design |
| Prompts and completions in logs | never written | `--disable-log-requests`, structlog redaction |

`DELETE /v1/users/{id}/data` cascades trajectories and steps. The audit log is
exempt, deliberately and with legal sign-off.
