# Cairn

Agentic incident-analysis copilot: an agent orchestrator, a cost-and-sensitivity-aware
model router, MCP tool servers for observability and runbooks, an approval-gated write
path, a Postgres+pgvector data layer, and a scenario-based evaluation harness.

[Source on GitHub](https://github.com/Nouman-Amjad/Cairn) ·
[npm package](https://www.npmjs.com/package/@nouman-amjad/cairn)

## Documentation

- [Solution architecture](architecture.md) — the section-to-code map and what was
  and wasn't implemented from the original design doc.
- [Security model](security.md)
- [Cost model](cost.md)
- [Inference](inference.md) — the local model serving path.
- [Operations](operations.md)
- [Roadmap](roadmap.md) — what's left, stated plainly.
- [Architecture decision records](adr/README.md) — the fourteen decisions and why.

## The npm CLI

The companion package installs into any project for platform analysis, a live
local dashboard, log tailing, and log chat, without touching the Python platform:

```bash
npx @nouman-amjad/cairn analyze
npx @nouman-amjad/cairn dashboard
```

See the [npm package README](https://github.com/Nouman-Amjad/Cairn/tree/main/npm) for
the full command reference.
