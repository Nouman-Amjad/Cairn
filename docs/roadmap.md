# Roadmap

Fourteen weeks, seven phases, each with an outcome someone outside the project
can see.

| Phase | Weeks | Deliverable | Demonstrable outcome |
|---|---|---|---|
| 0. Spike | 1 | vLLM + Qwen3-8B AWQ on one g5.xlarge; run the §7.4 benchmark | The performance table is measured, not estimated. If tok/s is >20% under, the model or card choice changes now, not in week six. |
| 1. Vertical slice | 2-3 | mcp-observability (2 tools) + the loop + a cloud model, docker-compose, seeded Loki/Prometheus | One real question answered end to end, locally |
| 2. Eval harness | 4 | 30 scenarios, judge calibration, scoring CLI | A number to improve. Everything after this is measured. |
| 3. Local + router | 5-6 | vLLM in the loop, router, sensitivity classifier, cost accounting | "70% local, $0.09/query" measured against the eval suite |
| 4. Full tool surface | 7-8 | Runbooks server with hybrid search, actions server, approval state machine, Slack | The approval gate demo. This is the part that impresses people who ship. |
| 5. Platform | 9-11 | Terraform, EKS, Karpenter, ArgoCD, KEDA, External Secrets, CI/CD with the eval gate | `terraform apply` from nothing to running |
| 6. Observability + security | 12-13 | OTel, Langfuse, dashboards, SLOs, NetworkPolicies, OPA bundles, Kyverno | A trace from question to answer with cost attached at every hop |
| 7. Hardening | 14 | Chaos: kill the GPU node mid-query, expire approvals, saturate the local tier, inject a poisoned runbook | Documented behaviour under each failure |

## Risks, and the spike that kills each

| Risk | Spike |
|---|---|
| 8B is not good enough at tool calling to be worth the tier | Phase 0: 50 tool calls against the real schemas, measure arg validity |
| Root-cause accuracy plateaus below 70% | Phase 2 exists to find this out in week 4, not week 12 |
| Cold-start spill makes Tier B feel broken | Phase 3: measure spill rate at realistic arrival intervals |
| Spot GPU reclaims are frequent enough to matter | Phase 0: run a node for a week in the target AZ, count interruptions |
| Approval latency makes the write path useless | Phase 4: instrument p50 from Slack post to click |

## The two phases that must not be skipped

**Phase 0** is where the hardware assumptions either hold or the whole cost
model rewrites itself. Every performance number in `docs/inference.md` is
arithmetic from memory bandwidth. They are good arithmetic and they are not
measurements.

**Phase 2** is where the project acquires the ability to tell whether it is
getting better. Every week it is deferred is a week of changes nobody can
evaluate.

The instinct will be to build the platform first, because platform work is
legible and satisfying. Resist it. A beautifully deployed agent that cannot be
measured is a demo with a Terraform module attached.

## Where this implementation actually is

Built out of order, deliberately: the whole system exists in code so the
design can be reviewed as a whole, but the two phases that generate *evidence*
have not been run.

| Phase | State |
|---|---|
| 0. Spike | **Not run.** No GPU has executed this code. `docs/inference.md` has the benchmark command and a falsification threshold. |
| 1. Vertical slice | Built. Runs against fixtures; `services/cairn-eval/stack/` seeds real Prometheus and Loki. |
| 2. Eval harness | Built and running. 30 scenarios, 7 metrics, gate enforced. The judge is **not calibrated** — `cairn_eval/llm_judge.py` has the calibration harness and nobody has labelled 100 runs. |
| 3. Local + router | Built. The routing decision has 157 tests. The 70%/$0.09 claim is **unmeasured**. |
| 4. Full tool surface | Built. The approval gate has 16 tests against real Postgres. |
| 5. Platform | Written, **never applied**. `terraform validate` runs in CI; no AWS account has seen it. |
| 6. Observability + security | Built. Traces, metrics, SLO alerts, NetworkPolicies, OPA bundle (12 passing policy tests), Kyverno. |
| 7. Hardening | **Not done.** No chaos testing has been run. |

The honest summary: phases 1, 2, 4 and 6 are real and tested. Phase 3 is real
but its central claim is unverified. Phases 0, 5 and 7 are the ones that need
hardware, an AWS account, and a willingness to break things — none of which a
code review can substitute for.
