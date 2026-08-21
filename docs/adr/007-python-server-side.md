# ADR-007: Python server-side, TypeScript for the UI only

**Status:** accepted
**Date:** 2026-07-26

## Context

Every server-side component talks to at least one of: the MCP SDK, the vLLM
client, the observability client libraries. All three are Python-first.

## Decision

Python 3.12 for all nine services. TypeScript for the Next.js UI, where the
same argument runs the other way.

## Alternatives rejected

**Go for the hot-path services.** Faster and better at concurrency, and it
would mean reimplementing or FFI-ing the MCP SDK. A second backend language
also costs a second CI pipeline, a second set of base images, and a second
dependency-audit surface.

## Consequence

Python's concurrency story means the orchestrator is asyncio-heavy and any
CPU-bound work must go to a thread pool. The token estimator is deliberately
arithmetic rather than a real tokenizer partly for this reason.

## Revisit when

A measured throughput problem in a specific service, and then rewrite that
one service rather than the stack.
