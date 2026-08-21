# Architecture Decision Records

Fourteen decisions, each with the alternative that was rejected and the
consequence that was accepted. A decision record without a consequence is a
press release.

| # | Decision | Consequence we accepted |
|---|---|---|
| [001](001-bespoke-orchestrator.md) | Bespoke orchestrator, not LangGraph or Temporal | We own the durability bugs |
| [002](002-streamable-http-transport.md) | Streamable HTTP primary, stdio also | Two code paths to test |
| [003](003-qwen3-8b-awq.md) | Qwen3-8B AWQ as the local model | Model quality caps what the local tier can be trusted with |
| [004](004-pgvector-over-qdrant.md) | pgvector, not Qdrant | Revisit at ~2M vectors |
| [005](005-a10g-gpu.md) | A10G (g5.xlarge), not L4 or L40S | Locked to sm_86, so no FP8 path without a card change |
| [006](006-no-service-mesh.md) | No service mesh | No automatic mTLS between arbitrary pods |
| [007](007-python-server-side.md) | Python server-side, TypeScript for UI only | asyncio-heavy orchestrator; CPU work goes to a thread pool |
| [008](008-aws-eks.md) | AWS EKS, eu-west-1 | Lock-in via IRSA, RDS and Secrets Manager |
| [009](009-karpenter.md) | Karpenter, not Cluster Autoscaler | Another controller to keep current; must be tuned against KEDA |
| [010](010-pending-approval-is-a-result.md) | `PENDING_APPROVAL` is a tool result, not an exception | Every tool schema must express the pending state |
| [011](011-deterministic-sensitivity.md) | Deterministic sensitivity classification | False positives degrade answer quality — the correct direction to fail |
| [012](012-router-holds-the-key.md) | The router is the only holder of the API key | One more hop, one more thing that can be down |
| [013](013-three-repos.md) | Three repos, not a monorepo | Cross-cutting changes need coordinated PRs |
| [014](014-scale-to-zero.md) | Scale-to-zero below Tier C, warm pool at Tier C | Tier B users see occasional "warming up"; spill logic must exist |

## Deviations in this implementation

**ADR-013 is not honoured here.** `cairn-deploy/` and `cairn-infra/` are
vendored as directories so the whole system can be reviewed in one place. The
ADR's reasoning still holds and the split is a `git filter-repo` away; see
[013](013-three-repos.md) for what that costs.
