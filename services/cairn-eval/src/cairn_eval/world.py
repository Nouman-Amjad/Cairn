"""The fake world an evaluated agent investigates.

Tool results are produced through the *real* `deliver()` path from
`cairn-mcp-kit`, so capping, faceting, sensitivity classification and
artifact offload behave exactly as they do in production. Only the backend
HTTP calls are replaced.

That boundary is deliberate. Faking at the tool-result level would score the
prompt while quietly skipping the layer most likely to break the agent —
oversized results and truncation.

The world also records everything it handed over, which is what
`evidence_recall` is scored against: an agent that names a metric it never
fetched has not retrieved it.
"""

from __future__ import annotations

import json
from typing import Any

from cairn_core.domain import ToolResult
from cairn_core.tokens import estimate_json
from cairn_eval.incidents import Incident
from cairn_mcp_kit.results import deliver

FACETS = {
    # `line` is faceted deliberately: with a thousand matching rows the
    # agent sees the first few dozen, and the distinct-message histogram
    # is the only way the rest of the errors are visible at all.
    "query_logs": ["line", "pod", "level"],
    "query_metrics": ["metric"],
    "get_deploy_timeline": ["source"],
    "get_traces": ["root_service"],
    "search_runbooks": ["doc_id"],
    "find_similar_incidents": ["services"],
}
SOURCE_KIND = {
    "query_logs": "logs",
    "query_metrics": "metrics",
    "get_deploy_timeline": "deploys",
    "get_traces": "traces",
    "search_runbooks": "docs",
    "find_similar_incidents": "docs",
}


def _expand(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand `occurrences: N` into N rows.

    Keeps the YAML reviewable while giving the tool layer the volume a real
    incident produces — which is the only way the 4k cap and the artifact
    offload get exercised.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        count = int(row.get("occurrences", 1))
        body = {k: v for k, v in row.items() if k != "occurrences"}
        out.extend(dict(body) for _ in range(max(1, count)))
    return out


class FakeWorld:
    """A ToolBox the agent loop can drive, backed by a scenario fixture."""

    def __init__(self, incident: Incident) -> None:
        self.incident = incident
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: Everything the agent was actually shown, for evidence scoring.
        self.retrieved: list[str] = []
        self.artifacts: set[str] = set()

    def render_catalogue(self, include_writes: bool = True) -> str:
        lines = [
            "- get_deploy_timeline(service, start, end): deploys in a window, "
            "newest first. Cheapest high-signal check for 'why did X change'.",
            "- query_metrics(query, start, end): PromQL over a range; returns "
            "min/max/mean per series.",
            "- query_logs(service, filter, start, end, limit): log search, capped.",
            "- get_traces(service, min_duration_ms): slow exemplar traces.",
            "- search_runbooks(query, services): hybrid runbook search.",
            "- find_similar_incidents(query): past investigations and what they found.",
        ]
        if include_writes:
            lines.append(
                "- rollback_deploy(service, from_sha, to_sha, reason): gated, "
                "needs two human approvals."
            )
        return "\n".join(lines)

    async def connect(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((name, args))

        if name == "rollback_deploy":
            # Write tools in the eval world always come back pending. The
            # suite measures diagnosis, and an eval that could execute a
            # rollback would be an eval with a blast radius.
            return ToolResult(
                tool=name,
                ok=True,
                content={
                    "status": "pending_approval",
                    "approval_id": "eval-approval",
                    "message": "queued for human approval",
                },
                pending_approval_id="eval-approval",
            )

        rows = self.incident.world.get(name)
        if rows is not None and name == "query_logs":
            rows = _expand(rows)
        if rows is None:
            return ToolResult(
                tool=name,
                ok=False,
                error=f"tool {name!r} is not available in this environment",
            )

        payload = await deliver(
            list(rows),
            tool=name,
            source_kind=SOURCE_KIND.get(name, "unknown"),
            facet_fields=FACETS.get(name, []),
            limit=200,
            sample_key="line" if name == "query_logs" else None,
            persist_artifact=False,
        )

        # Record what the agent could see. Rows beyond the cap were not shown
        # and must not count toward evidence recall — that is the whole point
        # of measuring recall against retrieval rather than against fixtures.
        self.retrieved.append(json.dumps(payload, default=str))
        if artifact := payload.get("artifact_id"):
            self.artifacts.add(str(artifact))

        return ToolResult(
            tool=name,
            ok=True,
            content=payload,
            sensitivity=str(payload.get("sensitivity", "public")),
            tokens=estimate_json(payload),
        )

    def tools_used(self) -> set[str]:
        return {name for name, _ in self.calls}

    @property
    def retrieved_text(self) -> str:
        return "\n".join(self.retrieved)
