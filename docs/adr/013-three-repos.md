# ADR-013: Three repos (infra, deploy, app), not a monorepo

**Status:** accepted
**Date:** 2026-07-26

## Context

Terraform changes quarterly. Prompts change daily. They have nothing in
common except the system they describe.

## Decision

`cairn` (application), `cairn-deploy` (Helm, ArgoCD, OPA, prompts),
`cairn-infra` (Terraform), each with its own lifecycle.

## Alternatives rejected

**A monorepo.** Simpler to navigate and to review. It also means every prompt
tweak triggers a `terraform plan` against the VPC, which is how a team ends up
with a five-minute feedback loop and a cluster nobody wants to touch.

## Consequence

A change spanning all three needs coordinated PRs, and the ArgoCD image tag
is the coupling point between them.

**This implementation does not honour the ADR:** `cairn-deploy/` and
`cairn-infra/` are vendored as directories so the system can be reviewed in one
place. Splitting them is `git filter-repo` and a CI change. The reasoning above
still applies the moment more than one person is committing.

## Revisit when

Immediately, if more than one person is working on the system.
