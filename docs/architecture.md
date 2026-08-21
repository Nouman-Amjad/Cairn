# Cairn: Solution Architecture

**Status:** approved for implementation. This repository is the implementation.

> **Note on this file.** The approved architecture document is the source of
> truth for the design. The copy handed to implementation was truncated
> partway through §12 (Evaluation harness); §12.2 onward and §13 (the cost
> model) were not received. Everything below §11 was therefore implemented
> from the requirements stated elsewhere in the document — §2.5 (quality
> attributes), §9.7 (the CI eval gate thresholds) and the cost-per-query
> targets — rather than from §12–13 directly. **Reconcile
> `services/cairn-eval` and the cost dashboard against the full document
> before treating either as final.**

## Section-to-code map

| Section | Implementation |
|---|---|
| §3.3 Service inventory | `services/`, `packages/` |
| §3.4 Diagnostic query flow | `cairn_orchestrator/loop.py` |
| §3.5 Write flow with approval gate | `cairn_mcp_actions/server.py`, `cairn_approval/service.py` |
| §4.1 The loop as a state machine | `cairn_core/domain.py:TRANSITIONS`, `cairn_orchestrator/loop.py` |
| §4.2 Budgets | `cairn_core/domain.py:Budget` |
| §4.3 Context management | `cairn_mcp_kit/results.py`, `cairn_core/artifacts.py`, `cairn_orchestrator/context.py` |
| §4.4 Trajectory schema | `cairn_core/models.py`, `migrations/versions/0001_initial.py` |
| §4.5 Prompt versioning | `cairn_core/prompts.py`, `cairn-deploy/prompts/` |
| §5.2 Tool inventory | `cairn_mcp_observability/server.py`, `cairn_mcp_runbooks/server.py`, `cairn_mcp_actions/server.py` |
| §5.3 Transport (HTTP + stdio) | `cairn_mcp_kit/server.py` |
| §5.4 Authorization | `cairn_core/auth.py`, `cairn_core/policy.py`, `cairn_mcp_kit/guard.py`, `cairn-deploy/policy/` |
| §6.1–6.3 Routing and fallback | `cairn_router/routing.py` |
| §6.2 Sensitivity classification | `cairn_core/sensitivity.py` |
| §6.4 Policy as data | `cairn_router/configmaps.py`, `cairn-deploy/routing/` |
| §6.5 Cost accounting | `cairn_router/configmaps.py:PriceBook`, `cairn_core/repo.py:accrue_cost` |
| §7 Local LLM deployment | `cairn-deploy/charts/cairn/templates/vllm.yaml`, `docs/inference.md` |
| §8 Data layer | `cairn_core/models.py`, `cairn_mcp_runbooks/search.py`, `chunking.py` |
| §9 Deployment | `cairn-deploy/`, `cairn-infra/`, `.github/workflows/` |
| §10 Security | `docs/security.md` |
| §11 Observability | `cairn_core/telemetry.py`, `templates/prometheusrules.yaml` |
| §12 Evaluation harness | `services/cairn-eval/` (see the note above) |

## Deviations from the document

Three, all deliberate, all cheap to reverse.

**1. A `cairn-mcp-kit` package that the document does not name.** Identity,
the OPA guard and result capping are identical across three tool servers.
Three copies is three places to forget a control; the shared package is where
`deliver()` enforces the 4k token cap for all of them.

**2. `pyproject`-level workspace instead of three separate repositories.**
`cairn-deploy` and `cairn-infra` are vendored here as directories so the whole
system can be reviewed in one place. Splitting them is a `git filter-repo`
away, and the document's reasoning for splitting them still holds — the
lifecycles genuinely differ.

**3. Redis pub/sub between orchestrator and gateway.** The document shows SSE
flowing orchestrator → gateway → client without saying how it crosses pods.
The loop runs on an orchestrator pod, the SSE connection lives on a gateway
pod, and neither knows the other's identity. Events go through Redis with a
15-minute replay list, so a client that reconnects mid-investigation gets what
it missed instead of a stream resuming mid-sentence.

## Things the document asserts that this implementation cannot yet confirm

- **"94% of resolved incidents finish in ≤9 iterations."** The `max_iterations`
  budget of 12 is taken on faith from that figure. Nothing here has measured
  it; the eval suite records `mean_iterations` so it can be.
- **The vLLM performance table.** Arithmetic, not measurement. See
  `docs/inference.md` for the benchmark command and the falsification
  threshold.
- **"$0.58 per query against a $1.91 all-cloud counterfactual."** The
  accounting to produce this number is implemented (`cairn_trajectory_cost_usd`
  labelled by route and task class); the number itself awaits real traffic.
