# cairn-eval

Without this, every other number in the architecture document is an opinion.

## What it measures

Thirty scenarios in `scenarios/`, one YAML file each. **Twenty are solvable.
Ten are unsolvable by design** — the evidence is genuinely absent, and the
only correct answer is an admission.

Seven metrics, each with a launch target:

| Metric | Definition | Target |
|---|---|---|
| `root_cause_top1` | `causal_service` exact match **and** cause equivalence | ≥ 70% |
| `evidence_recall` | fraction of `required_evidence` actually retrieved | ≥ 85% |
| `distractor_resistance` | runs not citing a distractor as the cause | ≥ 90% |
| `abstention_precision` | unsolvable scenarios where the agent abstained | ≥ 80% |
| `tool_efficiency` | mean tool calls per resolution | ≤ 9 |
| `cost_per_resolution` | mean `cost_usd` on solved scenarios | ≤ $0.60 |
| `hallucinated_citation_rate` | answers citing artifacts that do not exist | 0% |

**Abstention precision is the one most teams skip**, and the one that
determines whether anyone trusts the system after month two. An agent that
confidently answers an unsolvable incident is worse than one that says
"I could not determine this".

Two details that make the numbers mean something:

- **Evidence recall is scored against what the tools returned**, not against
  the fixtures. Naming a metric you never fetched is not retrieval.
- **A distractor mentioned while being ruled out does not count.** "No code
  was deployed" is the opposite of blaming the deploy, and an agent that
  correctly excludes a distractor should be rewarded, not penalised.

## Adding a scenario

Copy a file. The format is documented in [`scenarios/_schema.md`](scenarios/_schema.md).

```yaml
id: inc-031
title: Thread pool starvation under a retry storm
archetype: resource_exhaustion
solvable: true
question: why did orders-api stop responding at 04:10?
ground_truth:
  root_cause: A retry storm from cart-service exhausted the request thread pool.
  causal_service: orders-api
  required_evidence:
    - metric: jetty_threads_busy
    - log_pattern: "RejectedExecutionException"
distractors:
  - a coincident GC pause that is a symptom, not a cause
world:
  query_metrics: [...]
  query_logs: [...]
```

Scenarios are validated on load. A `required_evidence` entry that is not
actually present in `world` is a hard error, because a scenario that lies
about its own evidence makes recall meaningless. That check has already
caught one bug in this corpus.

Log fixtures use `occurrences: N` rather than a thousand literal rows, so the
YAML stays reviewable while the tool layer sees the volume a real incident
produces — which is the only way the 4k token cap and the artifact offload
get exercised.

## Running it

```bash
make eval            # no model, no GPU, no API key
```

Four modes, and the distinction matters for how much to believe a number:

| Mode | Model source | Reproducible | What a score means |
|---|---|---|---|
| `heuristic` | scripted keyword rules | yes | **the harness works.** Nothing about agent quality. |
| `replay` | recorded fixtures | yes | agent quality at recording time. Gates CI. |
| `record` | live router, saves responses | no | as `live`, and writes fixtures |
| `live` | the real router | no | honest, slow, costs money |

The committed baseline is heuristic mode and the runner prints a warning
saying so on every run. **Do not quote an accuracy figure from it.**

```bash
export CAIRN_ROUTER_ANTHROPIC_API_KEY=...
export CAIRN_ROUTER_VLLM_URL=http://localhost:8000/v1
make eval-record
git add fixtures/recordings.json
```

## Judging cause equivalence

Everything is deterministic string and set matching **except** whether a
proposed root cause means the same thing as the truth. That one judgement is
delegated to a frontier model with:

- **three votes, majority wins** — a single sample on a borderline case is
  close to a coin flip
- **a rubric that judges mechanism only**, explicitly ignoring wording,
  verbosity and confidence
- **calibration against human labels**, scored as Cohen's kappa

```bash
make eval-calibrate    # needs fixtures/judge-labels.jsonl
```

Below **κ = 0.7** the rubric is broken and gets rewritten. Kappa rather than
raw agreement because on an 80%-equivalent corpus a judge that always says
"equivalent" scores 80% agreement and has learned nothing; kappa scores it at
zero. There is a test for exactly that.

Without a configured judge the harness falls back to term overlap, which is
crude and honest about it. **The judge in this repository is not calibrated** —
nobody has labelled 100 runs.

## Reading a gate failure

The gate fails two independent ways.

**Regression against the baseline:**

```
  - root-cause top-1 80.0% -> 71.0%
  FAIL root-cause accuracy dropped 9.0% (80.0% -> 71.0%), limit is 5%
  - archetypes that got worse: config_change, dependency_outage
```

The per-archetype line is the useful one. A drop concentrated in one
archetype is usually a prompt change that broke a specific reasoning path; a
drop spread evenly is usually a routing or model change.

**Below an absolute target**, even if stable:

```
  FAIL abstention_precision 60.0% below the 80.0% target
```

A gate that only compares to itself will happily ratchet a system down to
zero, one four-point drop at a time.

**Cost**, at +20%:

```
  FAIL cost per resolution rose 34% ($0.4210 -> $0.5640), limit is 20%
```

This is the gate people forget to build. A prompt change that is functionally
fine but triples token consumption will not fail a health check, will not page
anyone, and will show up on the invoice five weeks later.

## Two environments

**In-process** (`make eval`) — the fake world returns fixture data through the
*real* `deliver()` path, so capping, faceting, sensitivity classification and
artifact offload behave exactly as in production. Fast, and what gates CI.

**Seeded stack** (`make eval-stack`) — the same scenarios loaded into a real
Prometheus, a real Loki and a fake ArgoCD, so the tool servers are exercised
against real PromQL and LogQL rather than a dict lookup. Both read the same
YAML, so they cannot drift apart.

## What the harness actually exercises

Everything except the outside world: the loop, the transcript, context
management, tool-result capping, sensitivity classification, artifact offload
and trajectory persistence are all production code paths.

That boundary earns its keep. Running the suite at realistic log volume showed
`evidence_recall` at 75% — because a truncated log search hid every distinct
error message after the cap. The fix was to facet logs by message, which is a
genuine improvement to the tool, found by the eval rather than by an outage.
