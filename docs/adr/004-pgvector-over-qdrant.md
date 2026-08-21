# ADR-004: pgvector over Qdrant

**Status:** accepted
**Date:** 2026-07-26

## Context

Runbook retrieval needs vector search over roughly 5,000 chunks, plus a
growing archive of past trajectories for `find_similar_incidents`.

## Decision

pgvector with HNSW (`m=16, ef_construction=64`, the defaults), in the same
PostgreSQL that holds trajectories.

## Alternatives rejected

**Qdrant.** The better vector database, and rejected anyway. Its advantages
(quantization, sharding, filtered search at 10M+ vectors) address problems this
corpus does not have. What it would add is a second stateful system, a second
backup story, a second failure mode, and a distributed write between "the
runbook row exists" and "its embedding exists".

At 5k chunks, HNSW in Postgres answers in single-digit milliseconds and the
write stays in one ACID transaction.

## Consequence

Retrieval quality is bounded by what pgvector can do. Tuning `m` and
`ef_construction` at this corpus size is superstition, so the knobs stay at
their defaults.

## Revisit when

About 2M vectors, or when filtered recall measurably degrades.
