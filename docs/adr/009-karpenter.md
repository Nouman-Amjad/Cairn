# ADR-009: Karpenter over Cluster Autoscaler

**Status:** accepted
**Date:** 2026-07-26

## Context

GPU nodes take about 90 seconds to become useful. Every second of
provisioning latency lands on an engineer waiting during an incident.

## Decision

Karpenter, with a GPU NodePool limited to three GPUs and spot-first
capacity.

## Alternatives rejected

**Cluster Autoscaler.** Provisions in about three minutes against
Karpenter's roughly 45 seconds, and its consolidation does not really bin-pack.
With a 90-second cold start already in the path, that two-minute delta is the
whole user experience.

## Consequence

Another controller to keep current. Its `consolidateAfter` must be tuned
against the KEDA cooldown or the two autoscalers thrash. Both are set to 10
minutes, and that coupling is documented in both places.

## Revisit when

Karpenter stops being maintained, or the GPU cold start is removed by a
permanent warm pool at every tier.
