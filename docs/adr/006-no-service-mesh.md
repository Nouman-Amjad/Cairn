# ADR-006: No service mesh

**Status:** accepted
**Date:** 2026-07-26

## Context

Pod-to-pod encryption, authorization between services, and traffic policy
all need answers. A mesh is the standard one.

## Decision

VPC CNI with encryption enabled for pod-to-pod, NetworkPolicy plus OPA for
authorization, and SPIFFE certificates from cert-manager for the
gateway-to-MCP path.

## Alternatives rejected

**Istio or Linkerd.** Would give mTLS everywhere and richer traffic policy.
Against that: a control plane to run and upgrade, sidecar memory on every pod,
and a debugging surface a small team will lose days to. The requirements are
met without it at a fraction of the operational cost.

## Consequence

No automatic mTLS between arbitrary pods; the gateway-to-MCP path is
configured explicitly rather than acquired for free. A new service-to-service
path is a thing someone must remember to secure.

## Revisit when

Thirty or more services, or a compliance regime that demands mesh-level
attestation.
