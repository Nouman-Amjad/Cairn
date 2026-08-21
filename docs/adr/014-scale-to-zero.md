# ADR-014: Scale-to-zero below Tier C, warm pool at Tier C

**Status:** accepted
**Date:** 2026-07-26

## Context

An idle GPU node costs roughly $180/month. A cold start costs 95 seconds on
the first query after idle.

## Decision

KEDA `minReplicaCount: 0` at Tier A and B; `1` at Tier C. The router spills
cold-start traffic to the cloud model when sensitivity permits, and reports an
honest "warming up, about 90s" on the stream when it does not.

## Alternatives rejected

**Always warm.** Correct at Tier C, where sustained load means the node never
scales down anyway. At Tier A and B it is $180/month to remove a delay the
router already routes around.

**Always cold.** Removes the warm-pool cost at Tier C too, and reintroduces a
spill path on a tier whose load never actually goes idle.

## Consequence

Tier B users occasionally see a "warming up" event, so the cold-start spill
logic must exist and be tested. `cairn_cold_start_spills_total` exists to show
whether it is firing more than the 20/day the alert allows.

## Revisit when

Sustained load such that the node never scales down, at which point
`minReplicaCount: 1` costs nothing and removes the spill path entirely.
