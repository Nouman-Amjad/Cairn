# ADR-012: The router is the only holder of the model API key

**Status:** accepted
**Date:** 2026-07-26

## Context

Something must hold the frontier model's API key. The orchestrator is the
component with model-controlled behaviour.

## Decision

The key lives in `cairn-router` and nowhere else, scoped by IRSA to that one
role. The router has a fixed, schema-validated interface and no model-controlled
input.

## Alternatives rejected

**Orchestrator holds the key and calls the API directly.** One less hop, and
it puts the credential in the one process an attacker can influence through a
poisoned log line.

## Consequence

Every cloud call takes an extra hop, about 3ms, and there is one more service
that can be down. In exchange, a prompt injection that fully compromises the
orchestrator cannot exfiltrate a credential it was never able to read.

## Revisit when

Never. The hop is cheap and the property is not replaceable.
