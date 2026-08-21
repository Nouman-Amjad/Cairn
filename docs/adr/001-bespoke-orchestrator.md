# ADR-001: Bespoke orchestrator over LangGraph or Temporal

**Status:** accepted
**Date:** 2026-07-26

## Context

The agent loop must suspend on a human approval that may take fifteen minutes,
resume on a different pod, route each model call by cost and data sensitivity,
and keep sensitivity monotonic for the life of a trajectory. Those are the
three places this system is unusual.

## Decision

A bespoke state machine persisted to Postgres. Every transition is a
committed row; the resume path is a `LISTEN/NOTIFY` fast path plus a 30-second
sweeper that reclaims trajectories whose lease expired.

Implemented in `services/cairn-orchestrator/src/cairn_orchestrator/loop.py`.

## Alternatives rejected

**LangGraph.** The loop is about 400 lines of actual logic, and the
framework's abstractions leak precisely where this system is unusual. Wrapping
approval suspension in a graph framework means fighting it at exactly the
moment correctness matters most.

**Temporal.** Genuinely the right primitive for durable human-in-the-loop
workflows, and the honest answer if the team were larger. Rejected only
because it adds four services and a datastore for one engineer to operate. We
get the same durability guarantee from Postgres at the cost of writing the
resume-sweeper ourselves, which is about 120 lines.

## Consequence

We own the durability bugs. A missed NOTIFY, a lease that does not expire,
or a transition that is not conditional becomes a stuck investigation that
nobody notices. The sweeper and the conditional `UPDATE ... WHERE state = ?`
are load-bearing, and both have tests that would fail loudly if removed.

## Revisit when

Three or more engineers, or when the workflow count exceeds about five.
At that point Temporal's operational cost is amortised across enough workflows
to be worth it.
