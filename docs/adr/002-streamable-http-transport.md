# ADR-002: Streamable HTTP as primary transport, stdio also supported

**Status:** accepted
**Date:** 2026-07-26

## Context

MCP servers can speak stdio (a subprocess) or Streamable HTTP (a service).
The tool servers hold database connection pools and need to scale
independently under an HPA.

## Decision

Streamable HTTP is the production transport. Every server also supports
stdio behind `--stdio` / `CAIRN_MCP_STDIO=1`.

## Alternatives rejected

**stdio only.** A subprocess cannot hold a connection pool across requests
and cannot scale independently. Non-starter for the production path.

**HTTP only.** Would have saved about twenty lines and cost the single fastest
debugging loop available: pointing an MCP client at the real tool server and
seeing exactly what the agent sees, including how a result looks after capping
and faceting.

## Consequence

Two code paths to test. The stdio path uses a deliberately read-only dev
identity (`cairn_mcp_kit.identity.dev_claims`) so debugging convenience can
never become a production authorization bypass.

## Revisit when

Never, most likely. The stdio path costs almost nothing and its debugging
value does not diminish.
