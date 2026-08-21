# ADR-008: AWS EKS, eu-west-1

**Status:** accepted
**Date:** 2026-07-26

## Context

The system needs GPU instances with deep spot capacity, managed Postgres with
pgvector, an object store, and a workload-identity story, in one place, at a
price a small team can defend.

## Decision

AWS EKS in eu-west-1.

## Alternatives rejected

**GCP GKE + L4.** Autopilot GPU support is good and `g2-standard-4` is
competitive. Genuinely close, and rejected on familiarity rather than merit.

**Hetzner + RunPod.** About 60% cheaper and the *correct* answer for a demo
deployment. Rejected for production because there is no managed Postgres worth
using and GPU capacity is not contractual.

**On-prem.** One engineer cannot also run a datacenter.

## Consequence

Lock-in through IRSA, RDS and Secrets Manager. The Terraform modules are
structured so the Postgres and secrets layers are the swappable ones, because
those are where the lock-in actually bites.

## Revisit when

A pricing change large enough to matter, or a second region for availability
rather than cost.
