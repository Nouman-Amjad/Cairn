# ADR-003: Qwen3-8B AWQ as the local model

**Status:** accepted
**Date:** 2026-07-26

## Context

The local tier exists to serve the roughly 70% of calls that are high-volume
and low-stakes, and to serve *all* calls on restricted data. It has one 24GB
card.

## Decision

Qwen3-8B, AWQ 4-bit, served by vLLM with grammar-constrained decoding
(`guided_json`) for every structured output.

## Alternatives rejected

**Llama 3.1 8B.** Weaker native tool calling and structured output. The
router's task table depends on the local model being reliable at schema-bound
JSON; that is the capability being bought.

**Mistral Small 24B.** Does not fit a single 24GB card with usable KV cache.

**Qwen3-14B AWQ.** Fits, but KV cache drops to roughly 45k tokens, halving
concurrency from about 16 to about 8. The quality gain does not pay for that.

**GPTQ instead of AWQ.** vLLM's AWQ Marlin kernels are faster on Ampere.

**FP8.** The A10G is `sm_86` and has no native FP8.

## Consequence

Model quality caps what the local tier can be trusted with, and the router's
task table encodes that ceiling explicitly: planning and synthesis go to a
frontier model unless the data is restricted, in which case the quality hit is
accepted because the alternative is a data-residency breach.

## Revisit when

A card change. FP8 becomes correct on an L40S or H100, which is the Tier C
conversation.
