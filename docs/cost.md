# Cost model

Everything here derives from one measured workload profile. If the profile is
wrong, the money is wrong, so it is stated explicitly rather than buried.

## Workload assumptions

Per query, 9 LLM calls:

| | Calls | Input tokens | Output tokens |
|---|---|---|---|
| Local (classify, 4x summarize, compact, critique) | 7 | 36,200 | 2,550 |
| Cloud (plan, synthesize) | 2 | 12,000 (8,000 cacheable) | 1,800 |

Cloud cost per query, at $3/M in, $15/M out, $0.30/M cache read:

```
  8,000 cached read  x $0.30/M  =  $0.0024
  4,000 fresh input  x $3.00/M  =  $0.0120
  1,800 output       x $15.00/M =  $0.0270
                                   --------
                                   $0.0414 per query
```

**Prompt caching is doing real work there.** Without it, input cost triples to
$0.036 and the per-query total rises 58%. The system prompt plus tool
definitions are byte-identical on every call, which is exactly the shape
caching is built for. This is free money and most implementations leave it on
the table — see `--enable-prefix-caching` locally and the `cache_control`
block in `cairn_router/providers.py` for the cloud side.

## Tier A: demo and portfolio

Scale-to-zero. GPU runs about 4 hours/day.

| Line | Monthly |
|---|---|
| EKS control plane | $73 |
| 2x t3.small spot (system) | $10 |
| GPU: g5.xlarge spot, 120 hr @ $0.40 | $48 |
| NAT instance (t4g.nano, not NAT Gateway) | $3 |
| ALB | $16 |
| Postgres: CloudNativePG in-cluster + 20GB EBS | $2 |
| ECR (15 GiB) + S3 | $3 |
| Cloud LLM (~300 queries) | $12 |
| Data transfer | $5 |
| **Total** | **$172** |

The EKS control plane is 42% of this bill and buys nothing a demo needs.

**The honest Tier A answer is $70/month.** A single Hetzner CCX23 running k3s
($32), a RunPod A10 on demand at $0.22/hr for 120 hours ($26), Cloudflare
Tunnel instead of an ALB ($0), and $12 of cloud LLM. It runs the identical
Helm charts. If the point is to demonstrate the architecture rather than to
survive a regional outage, this is the correct deployment, and paying AWS
2.5x for a demo is a failure of judgment rather than a display of rigour.

## Tier B: team, 25 users, 400 queries/day (12,000/month)

| Line | Detail | Monthly |
|---|---|---|
| EKS control plane | | $73 |
| System nodes | 2x m6a.large on-demand | $126 |
| App nodes | 3x m6a.large spot | $66 |
| GPU | g5.xlarge, ~330 hr, blended $0.55 | $182 |
| RDS | db.t4g.small Multi-AZ + 50GB | $53 |
| ElastiCache | cache.t4g.micro | $12 |
| NAT Gateway | $32 + 80GB processing | $36 |
| ALB | | $18 |
| Langfuse | 1x m6a.large spot + ClickHouse EBS | $30 |
| Grafana Cloud | free tier | $0 |
| S3 + ECR | | $8 |
| Cloud LLM | 12,000 x $0.0414 | $497 |
| Data transfer + backups | | $25 |
| **Total** | | **$1,126** |

Per query: **$0.094 all-in**, of which $0.041 is the model.

## Tier C: org, 200 users, 4,000 queries/day (120,000/month)

| Line | Detail | Monthly |
|---|---|---|
| EKS control plane | | $73 |
| System nodes | 2x m6a.large on-demand | $126 |
| App nodes | 6x m6a.xlarge spot | $263 |
| GPU | avg 1.8 replicas 24/7, 1,314 hr @ $0.55 | $723 |
| RDS writer | db.r6g.large Multi-AZ + 200GB | $377 |
| RDS read replica | db.r6g.large | $177 |
| ElastiCache | 2x cache.t4g.small | $47 |
| NAT Gateway | $32 + 400GB | $50 |
| ALB | | $25 |
| Langfuse | 2x m6a.xlarge + 500GB | $128 |
| Grafana Cloud Pro | | $150 |
| S3 + ECR | | $40 |
| Cloud LLM | 120,000 x $0.0414 | $4,968 |
| Data transfer + backups | | $120 |
| **Total** | | **$7,267** |

Per query: **$0.061 all-in**. The infrastructure amortizes; the model does not.

## Does the local tier actually pay for itself?

This deserves an honest answer rather than the flattering one.

The flattering comparison routes everything to the frontier model:

```
All-Sonnet:   $0.188/query  ->  Tier C: $22,562/mo
Cairn:        $0.041 cloud + GPU  ->  Tier C: $5,691/mo
"Saving":     $16,871/mo
```

**That number is marketing.** Nobody would run log summarization on Sonnet.
The honest counterfactual routes cheap work to a cheap cloud model at $1/M in,
$5/M out:

```
Cheap tasks on Haiku:  36,200 x $1/M + 2,550 x $5/M  =  $0.049/query
All-cloud total:       $0.0414 + $0.049              =  $0.090/query
```

| | All-cloud | Cairn (cloud + GPU) | Saving |
|---|---|---|---|
| Tier B | $1,085 | $679 | $406/mo |
| Tier C | $10,848 | $5,691 | $5,157/mo |

**Break-even is about 125 queries/day.** Below that, the GPU costs more than
it saves and you should delete it.

At Tier B the local tier saves $406/month and costs a GPU node group, a KEDA
config, a cold-start path, a spill policy, and a class of 3am incident that
does not otherwise exist. That is a genuinely marginal trade, and anyone who
tells you otherwise is selling something.

**The local tier's real justification at Tier B is not cost.** It is that
restricted data cannot leave the VPC. If that constraint were removed, the
correct Tier B architecture is all-cloud with a cheap/strong split, and it
would be simpler and roughly break even. At Tier C the economics turn
independently and the GPU is justified on money alone.

State the constraint that actually drives the design. An architecture
justified by the wrong reason survives until someone checks the arithmetic.

## Cost controls

| Control | Mechanism | Where |
|---|---|---|
| Per-user daily cap | Redis counter, gateway enforces, $5/user/day | `cairn_gateway/limits.py:check_budget` |
| Per-trajectory cap | Router hard-stops at $1.50, transitions to PARTIAL | `cairn_core/domain.py:Budget` |
| Global circuit breaker | 150% of daily forecast disables the cloud tier, local only, and pages | `cairn_gateway/limits.py:check_circuit` |
| Canary cost gate | Rollout fails if mean cost/trajectory rises >20% | `charts/cairn/templates/rollout.yaml` |
| Eval cost gate | Merge blocked if cost per resolution rises >20% | `cairn_eval/gate.py` |
| Spot everywhere non-critical | About 60% off compute | `modules/karpenter` |
| S3 lifecycle | Glacier at 7 days, delete at 30 | `modules/s3` |
| VPC endpoints | Removes ECR/S3 pulls from NAT metering | `modules/vpc` |
| Prompt caching | 58% off cloud input | `cairn_router/providers.py` |

The circuit breaker is worth a note. Per-user caps bound one enthusiastic
engineer; they do not bound a change that makes *every* query cost ten times
what it should, because every user stays individually under their cap while
the total goes somewhere nobody authorised. When it trips, cloud inference is
disabled and queries keep working on the local tier, where the marginal cost
is zero. Degraded, not down.

## What the money is actually sensitive to

| Lever | Effect at Tier C | Difficulty |
|---|---|---|
| Cut output tokens 30% (terser synthesis prompt) | −$1,490/mo | One prompt change |
| Raise local share 70% -> 85% | −$1,060/mo | Router policy tuning + eval validation |
| Prompt caching (already in) | −$2,880/mo | Done |
| Drop the read replica | −$177/mo | Trivial, mild risk |
| Move to Hetzner + RunPod | −$1,400/mo | Loses managed Postgres and IAM |
| Buy a bigger GPU | +$800/mo | Solves nothing at 8B |

**Output tokens are the single most expensive thing in this system**, at 5x
the price of input and un-cacheable. The highest-leverage cost optimisation
available is telling the model to be less verbose. It is also the least
glamorous, which is why it is usually the last thing anyone tries.

## Measuring it rather than believing it

Every LLM call records `tokens_in`, `tokens_out`, `cached_read_tokens` and a
computed `cost_usd` on its trajectory step, labelled by route and task class.
`cairn_trajectory_cost_usd` is the histogram; the cost-and-routing dashboard
is reviewed weekly.

That is what makes the router's value provable rather than assumed. "70%
local" is a claim. "$0.058 per query against a $0.090 all-cloud counterfactual,
measured over 12,000 queries" is an argument.

**None of the numbers on this page have been measured by this implementation.**
The accounting that would measure them is built and tested; the traffic to
populate it does not exist yet.
