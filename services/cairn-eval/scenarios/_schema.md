# Scenario file format

One YAML file per incident. Data, not code — an SRE who has never opened the
Python should be able to add a scenario by copying a file and editing it.

```yaml
id: inc-014                       # unique, stable; referenced in eval output
title: Connection pool exhaustion after replica downscale
archetype: resource_exhaustion    # groups scenarios for per-archetype scoring
solvable: true                    # false => the evidence is genuinely absent

question: |                       # what the engineer types
  why did checkout-api start timing out at 03:00?

ground_truth:
  root_cause: >                   # prose; judged for equivalence, not string match
    checkout-api pool max=20; RDS replica scaled 3->1 at 02:58, connection
    wait exceeded the 30s timeout
  causal_service: checkout-api    # exact match required for root-cause top-1
  contributing_deploy: null       # sha, or null when no deploy is involved
  required_evidence:              # each entry must be retrievable from `world`
    - metric: checkout_db_pool_wait_seconds
    - log_pattern: "TimeoutError: QueuePool limit"
    - event: rds_replica_scale_down

distractors:                      # present in the data, not the cause
  - unrelated frontend deploy at 02:45
  - correlated but non-causal CPU spike on payments-api

world:                            # what the fake backends serve, keyed by tool
  get_deploy_timeline: [...]
  query_metrics: [...]
  query_logs: [...]
  search_runbooks: [...]
```

## Rules

**`solvable: false` scenarios must contain no path to a confident answer.**
Ten of the thirty are like this. An agent that answers them confidently is
worse than one that returns PARTIAL, and `abstention_precision` is the metric
that says so. If you can solve one of these by reading the fixtures, it is
mis-labelled — fix the fixture, not the label.

**Every `required_evidence` entry must actually be present in `world`.** The
`evidence_recall` metric is meaningless otherwise, and there is a test that
enforces it.

**Distractors must be genuinely plausible.** A distractor nobody would fall
for measures nothing. The good ones are correlated in time, in a neighbouring
service, or a deploy that landed near the window but touched nothing relevant.
