# ADR-010: PENDING_APPROVAL is a tool result, not an exception

**Status:** accepted
**Date:** 2026-07-26

## Context

A write tool cannot return its result immediately; a human has to decide
first, and that may take fifteen minutes.

## Decision

Write tools return a successful result with `status: pending_approval` and an
approval id. The loop transitions to `WAITING_APPROVAL` and suspends with no
timer and no polling.

## Alternatives rejected

**Model it as an error.** This is the obvious implementation and it is
actively harmful: an error teaches the agent to retry, so it files the same
request again, spams the approver, and eventually gets an action approved
through persistence rather than merit.

## Consequence

Every write tool's schema must express the pending state, and the loop must
be genuinely suspendable and resumable, which is what forces the durable state
machine in ADR-001. The tool description tells the model explicitly not to
retry, and there is a test asserting the tool was called exactly once.

## Revisit when

Never. This one is load-bearing.
