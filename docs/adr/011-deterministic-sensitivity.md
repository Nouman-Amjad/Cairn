# ADR-011: Deterministic sensitivity classification

**Status:** accepted
**Date:** 2026-07-26

## Context

Log and metric payloads may contain customer PII, and classified content must
not reach a third-party model API. Something has to decide what is sensitive.

## Decision

Namespace labels, plus regex, plus an optional Presidio pass, evaluated at
the tool-result boundary before the payload can reach the router. Sensitivity
is monotonic within a trajectory and never downgrades.

## Alternatives rejected

**Ask the model.** A model can be talked out of its decision, and the whole
threat model assumes the model will eventually be tricked. A classifier an
attacker can argue with is not a control.

## Consequence

False positives push work to the local tier and degrade answer quality. That
is the correct direction to fail, but only if the false-positive rate stays
sane. The credit-card rule requires both a Luhn check and a valid issuer
prefix; without them, floating-point noise like `0.9000000000000001` reads as a
card number, every metric result classifies restricted, and the router sends
100% of traffic local. There is a test for exactly that.

## Revisit when

Measured false-negative rate on sampled restricted payloads exceeding what
the compliance owner will accept.
