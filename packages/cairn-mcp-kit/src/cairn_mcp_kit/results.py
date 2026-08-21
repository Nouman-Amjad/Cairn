"""Tool result shaping: the server-side token cap and the artifact offload.

This is the layer that makes the context strategy real. Tools call `deliver()`
with everything they found; `deliver()` decides what fits in the agent's
window, parks the rest in S3, and hands back facets and counts so the agent
knows exactly what it is not seeing.

If a tool can return 40k tokens, the tool is the bug. This function is how
that stops being possible by construction rather than by discipline.
"""

from __future__ import annotations

import json
from typing import Any

from cairn_core.artifacts import ArtifactStore, describe, page
from cairn_core.config import settings
from cairn_core.models import Artifact
from cairn_core.sensitivity import Sensitivity, classify
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

_store: ArtifactStore | None = None


def store() -> ArtifactStore:
    global _store
    if _store is None:
        _store = ArtifactStore(settings().s3)
    return _store


async def deliver(
    rows: list[dict[str, Any]],
    *,
    tool: str,
    source_kind: str,
    facet_fields: list[str],
    limit: int = 100,
    cursor: str | None = None,
    sample_key: str | None = None,
    namespace_labels: dict[str, str] | None = None,
    total_matched: int | None = None,
    persist_artifact: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Shape a result set into something safe to put in a context window."""
    cfg = settings()
    max_tokens = cfg.mcp.max_response_tokens

    # Classify before anything is returned or stored. The level rides with the
    # result so the orchestrator can escalate the trajectory, and the router
    # can refuse to send it anywhere it should not go.
    sample_text = "\n".join(json.dumps(r, default=str) for r in rows[:400])
    level, reasons = classify(
        text=sample_text,
        payload=rows[:50],
        namespace_labels=namespace_labels,
        source_kind=source_kind,
    )

    shown, next_cursor = page(rows, limit=limit, cursor=cursor, max_tokens=max_tokens)
    truncated = next_cursor is not None

    result: dict[str, Any] = {
        "tool": tool,
        "sensitivity": str(level),
        **describe(
            rows,
            facet_fields=facet_fields,
            total_matched=total_matched,
            sample_key=sample_key,
        ),
        # After describe(), so `returned` reports what the agent can actually
        # see rather than what the backend matched.
        "returned": len(shown),
        "rows": shown,
        "next_cursor": next_cursor,
        **extra,
    }
    if level is Sensitivity.RESTRICTED:
        result["sensitivity_reasons"] = reasons[:5]

    # Only park an artifact when there is something the agent could not see.
    # Storing every small result would triple S3 writes for no drill-down.
    if truncated and persist_artifact:
        ref = await store().put(
            rows,
            tool=tool,
            trajectory_id=_trajectory_id(),
            sensitivity=level,
            facet_fields=facet_fields,
        )
        await _register(ref, tool=tool, level=level)
        result["artifact_id"] = ref.artifact_id
        result["note"] = (
            f"Showing {len(shown)} of {len(rows)} rows. "
            f"Use fetch_artifact('{ref.artifact_id}', filter=...) to drill in; "
            "the facets above cover the distribution without fetching."
        )

    return result


def _trajectory_id() -> str | None:
    from cairn_mcp_kit.identity import current_claims

    try:
        tid = current_claims().trajectory_id
    except Exception:
        return None
    return tid if tid and tid != "stdio" else None


async def _register(ref: Any, *, tool: str, level: Sensitivity) -> None:
    import uuid

    from cairn_core.db import session

    tid = _trajectory_id()
    try:
        async with session() as sess:
            sess.add(
                Artifact(
                    id=ref.artifact_id,
                    trajectory_id=uuid.UUID(tid) if tid else None,
                    tool=tool,
                    s3_key=ref.s3_key,
                    content_type="application/x-ndjson",
                    byte_size=ref.byte_size,
                    row_count=ref.row_count,
                    sensitivity=str(level),
                    facets=ref.facets,
                )
            )
    except Exception as exc:
        # The bytes are already in S3 and the handle is already usable for
        # this trajectory. A missing registry row costs retention bookkeeping,
        # not correctness, and is not worth failing an incident query over.
        log.warning("artifact_registration_failed", artifact=ref.artifact_id, error=str(exc))


def error(tool: str, message: str, *, kind: str = "error", **extra: Any) -> dict[str, Any]:
    """A tool failure the model should reason about, not retry blindly."""
    return {"tool": tool, "ok": False, "kind": kind, "error": message, **extra}


def not_configured(tool: str, backend: str) -> dict[str, Any]:
    return error(
        tool,
        f"{backend} is not configured in this environment, so this tool cannot "
        "answer. Do not retry; use another source of evidence.",
        kind="not_configured",
    )
