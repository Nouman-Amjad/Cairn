"""cairn-mcp-observability: the read-only evidence surface.

Every tool here is read-only and needs no approval. What they all share is a
hard server-side cap on how much they can return: `limit` is bounded, the
response is token-capped by `deliver()`, and anything larger becomes an
artifact handle plus facets.

Tool descriptions are written for the model, not for a human reading docs.
They say when to use the tool and what it costs, because a tool description
is the only documentation the agent will ever read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from cairn_core.artifacts import apply_filter, page
from cairn_core.config import settings
from cairn_core.telemetry import get_logger
from cairn_mcp_kit import build, deliver, error, guarded, not_configured, run, store
from cairn_mcp_observability.backends import (
    BackendError,
    Backends,
    NotConfigured,
    check_ident,
)
from cairn_mcp_observability.timeparse import TimeError, parse_range, resolve_step

log = get_logger(__name__)

INSTRUCTIONS = """
Read-only access to production observability data.

Order of operations that works during an incident: check the deploy timeline
first (cheapest, highest signal), then metrics to establish blast radius and
timing, then logs filtered to that window, then traces for the slow path.

Every tool caps its own output. When a result says `artifact_id`, the full
data is stored and `fetch_artifact` will drill into it — read the facets
before you fetch, they usually answer the question.
""".strip()

server = build("cairn-mcp-observability", INSTRUCTIONS)
_backends: Backends | None = None


def backends() -> Backends:
    global _backends
    if _backends is None:
        _backends = Backends(settings().backends)
    return _backends


def _logql_string(value: str) -> str:
    """Escape a value for a LogQL double-quoted string literal.

    This is the one place model output is spliced into query syntax, so it is
    escaped rather than trusted, and the result is a string literal position
    where the worst case is a filter that matches nothing.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _selector(service: str, namespace: str | None, level: str | None) -> str:
    matchers = [f'app="{_logql_string(check_ident(service, "service"))}"']
    if namespace:
        matchers.append(f'namespace="{_logql_string(check_ident(namespace, "namespace"))}"')
    if level:
        matchers.append(f'level="{_logql_string(check_ident(level, "level"))}"')
    return "{" + ",".join(matchers) + "}"


@server.tool(
    name="query_metrics",
    description=(
        "Run a PromQL query over a time range. Use for rates, latencies, error "
        "ratios and saturation. Returns per-series min/max/mean plus a "
        "downsampled series, not raw scrape points. Cheap: prefer this over "
        "log search when a number would answer the question."
    ),
)
@guarded("query_metrics")
async def query_metrics(
    query: Annotated[
        str,
        Field(
            description='PromQL expression, e.g. sum(rate(http_requests_total{app="checkout-api"}[5m])) by (status)'
        ),
    ],
    start: Annotated[
        str | None, Field(description="RFC3339 or relative like -2h. Default: 1h ago")
    ] = None,
    end: Annotated[str | None, Field(description="RFC3339 or relative. Default: now")] = None,
    step: Annotated[
        str | None, Field(description="Resolution like 30s or 5m. Auto-chosen if omitted")
    ] = None,
) -> dict[str, Any]:
    try:
        start_dt, end_dt = parse_range(start, end)
        rows = await backends().prometheus_range(
            query, start_dt, end_dt, resolve_step(start_dt, end_dt, step)
        )
    except NotConfigured as exc:
        return not_configured("query_metrics", exc.backend)
    except (TimeError, BackendError) as exc:
        return error("query_metrics", str(exc))

    return await deliver(
        rows,
        tool="query_metrics",
        source_kind="metrics",
        facet_fields=["metric"],
        limit=40,
        query=query,
        window={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
    )


@server.tool(
    name="query_logs",
    description=(
        "Search logs for one service over a window. `filter` is a substring "
        "match on the log line. Expensive relative to metrics: narrow the "
        "window first. Results are capped; large matches come back as facets "
        "plus an artifact_id you can drill into with fetch_artifact."
    ),
)
@guarded("query_logs")
async def query_logs(
    service: Annotated[str, Field(description="Service / app label, e.g. checkout-api")],
    filter: Annotated[
        str | None, Field(description="Substring the line must contain, e.g. 'timeout'")
    ] = None,
    namespace: Annotated[str | None, Field(description="Kubernetes namespace")] = None,
    level: Annotated[str | None, Field(description="Log level, e.g. error")] = None,
    start: Annotated[str | None, Field(description="RFC3339 or relative like -30m")] = None,
    end: Annotated[str | None, Field(description="RFC3339 or relative")] = None,
    limit: Annotated[
        int, Field(ge=1, le=500, description="Max lines to scan back (hard cap 500)")
    ] = 200,
    cursor: Annotated[
        str | None, Field(description="Continuation cursor from a previous call")
    ] = None,
) -> dict[str, Any]:
    try:
        start_dt, end_dt = parse_range(start, end, default_window=timedelta(minutes=30))
        logql = _selector(service, namespace, level)
        if filter:
            logql += f' |= "{_logql_string(filter)}"'
        rows = await backends().loki_query(logql, start_dt, end_dt, min(limit, 500))
    except NotConfigured as exc:
        return not_configured("query_logs", exc.backend)
    except (TimeError, BackendError) as exc:
        return error("query_logs", str(exc))

    return await deliver(
        rows,
        tool="query_logs",
        source_kind="logs",
        # `line` first: a distinct-message histogram is what makes a
        # truncated log search useful. Without it the agent sees the
        # first page and is blind to every other error in the window.
        facet_fields=["line", "pod", "level", "status", "container"],
        limit=min(limit, 500),
        cursor=cursor,
        sample_key="line",
        namespace_labels=await _namespace_labels(namespace),
        query=logql,
        window={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
    )


@server.tool(
    name="list_services",
    description=(
        "List services known to the metrics backend, with the namespace they "
        "run in. Use when you need the exact service label and are guessing."
    ),
)
@guarded("list_services")
async def list_services(
    namespace: Annotated[str | None, Field(description="Restrict to one namespace")] = None,
) -> dict[str, Any]:
    try:
        matcher = (
            '{namespace="' + _logql_string(check_ident(namespace, "namespace")) + '"}'
            if namespace
            else None
        )
        names = await backends().label_values("app", matcher)
    except NotConfigured as exc:
        return not_configured("list_services", exc.backend)
    except BackendError as exc:
        return error("list_services", str(exc))

    rows = [{"service": name, "namespace": namespace} for name in sorted(names)]
    return await deliver(
        rows,
        tool="list_services",
        source_kind="metrics",
        facet_fields=["namespace"],
        limit=200,
        persist_artifact=False,
    )


@server.tool(
    name="get_deploy_timeline",
    description=(
        "Deploys for a service in a window, newest first. Run this FIRST for "
        "any 'why did X change at time T' question: a deploy inside the window "
        "is the single highest-prior-probability cause, and this is the "
        "cheapest tool available."
    ),
)
@guarded("get_deploy_timeline")
async def get_deploy_timeline(
    service: Annotated[str, Field(description="Service name")],
    start: Annotated[str | None, Field(description="RFC3339 or relative like -24h")] = None,
    end: Annotated[str | None, Field(description="RFC3339 or relative")] = None,
) -> dict[str, Any]:
    try:
        start_dt, end_dt = parse_range(start, end, default_window=timedelta(hours=24))
    except TimeError as exc:
        return error("get_deploy_timeline", str(exc))

    rows: list[dict[str, Any]] = []
    sources_tried: list[str] = []
    for name, fetch in (
        ("argocd", backends().argocd_history),
        ("github", backends().github_deployments),
    ):
        try:
            rows.extend(await fetch(service))
            sources_tried.append(name)
        except NotConfigured:
            continue
        except BackendError as exc:
            log.warning("deploy_source_failed", source=name, error=str(exc))

    if not sources_tried:
        return not_configured("get_deploy_timeline", "argocd or github")

    in_window = [r for r in rows if _within(r.get("at"), start_dt, end_dt)]
    in_window.sort(key=lambda r: str(r.get("at")), reverse=True)

    return await deliver(
        in_window,
        tool="get_deploy_timeline",
        source_kind="deploys",
        facet_fields=["source", "environment"],
        limit=50,
        persist_artifact=False,
        sources=sources_tried,
        window={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
    )


@server.tool(
    name="get_traces",
    description=(
        "Find slow exemplar traces for a service. Use after metrics show a "
        "latency regression and you need to know which downstream call owns "
        "the time. Returns trace summaries, not full spans."
    ),
)
@guarded("get_traces")
async def get_traces(
    service: Annotated[str, Field(description="Service name")],
    min_duration_ms: Annotated[int, Field(ge=1, description="Only traces slower than this")] = 1000,
    start: Annotated[str | None, Field(description="RFC3339 or relative")] = None,
    end: Annotated[str | None, Field(description="RFC3339 or relative")] = None,
    limit: Annotated[int, Field(ge=1, le=20, description="Max traces (hard cap 20)")] = 10,
) -> dict[str, Any]:
    try:
        start_dt, end_dt = parse_range(start, end)
        rows = await backends().tempo_search(
            service, min_duration_ms, start_dt, end_dt, min(limit, 20)
        )
    except NotConfigured as exc:
        return not_configured("get_traces", exc.backend)
    except (TimeError, BackendError) as exc:
        return error("get_traces", str(exc))

    return await deliver(
        rows,
        tool="get_traces",
        source_kind="traces",
        facet_fields=["root_service", "root_operation"],
        limit=min(limit, 20),
        persist_artifact=False,
    )


@server.tool(
    name="fetch_artifact",
    description=(
        "Drill into a stored result from an earlier tool call. Filter syntax: "
        "`field=value`, `field!=value`, `field~regex`, space-separated (AND). "
        "Check the facets you already have before calling this — they usually "
        "answer the question without spending context on rows."
    ),
)
@guarded("fetch_artifact")
async def fetch_artifact(
    artifact_id: Annotated[str, Field(description="Handle from an earlier result")],
    filter: Annotated[str | None, Field(description="e.g. status=503 pod~7f9")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Field(description="Continuation cursor")] = None,
) -> dict[str, Any]:
    from cairn_core.db import session
    from cairn_core.models import Artifact as ArtifactRow
    from cairn_mcp_kit.identity import current_claims

    async with session() as sess:
        row = await sess.get(ArtifactRow, artifact_id)
        if row is None:
            return error("fetch_artifact", f"unknown artifact {artifact_id}", kind="not_found")
        # An artifact belongs to the investigation that produced it. Without
        # this check, an artifact id leaked into a prompt would read another
        # user's production data.
        claims = current_claims()
        if row.trajectory_id and str(row.trajectory_id) != claims.trajectory_id:
            return error(
                "fetch_artifact",
                "artifact belongs to a different investigation",
                kind="denied",
            )
        s3_key, sensitivity, tool = row.s3_key, row.sensitivity, row.tool

    try:
        rows = await store().get_rows(s3_key)
        matched = apply_filter(rows, filter)
    except ValueError as exc:
        return error("fetch_artifact", str(exc))
    except Exception as exc:
        return error("fetch_artifact", f"artifact unreadable: {exc}")

    shown, next_cursor = page(
        matched,
        limit=limit,
        cursor=cursor,
        max_tokens=settings().mcp.max_response_tokens,
    )
    return {
        "tool": "fetch_artifact",
        "artifact_id": artifact_id,
        "source_tool": tool,
        "sensitivity": sensitivity,
        "total_matched": len(matched),
        "total_stored": len(rows),
        "returned": len(shown),
        "rows": shown,
        "next_cursor": next_cursor,
    }


async def _namespace_labels(namespace: str | None) -> dict[str, str]:
    """Namespace labels drive the restricted classification.

    Read from the Kubernetes API when in-cluster. A namespace whose labels
    cannot be read is treated as restricted: an unknown data classification is
    not the same as a permissive one.
    """
    if not namespace:
        return {}
    cfg = settings().backends
    if not cfg.kube_api_in_cluster:
        return {}
    try:
        return await _read_namespace_labels(namespace)
    except Exception as exc:
        log.warning("namespace_labels_unavailable", namespace=namespace, error=str(exc))
        return {"data-classification": "restricted"}


_ns_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _read_namespace_labels(namespace: str) -> dict[str, str]:
    import time

    import httpx

    cached = _ns_cache.get(namespace)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]

    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - a path
    with open(token_path) as fh:  # noqa: ASYNC230 - tiny local file, cached 5m
        token = fh.read()
    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        verify="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        timeout=3.0,
    ) as client:
        resp = await client.get(
            f"/api/v1/namespaces/{check_ident(namespace, 'namespace')}",
            headers={"authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        labels = resp.json().get("metadata", {}).get("labels", {}) or {}

    _ns_cache[namespace] = (time.monotonic(), labels)
    return labels


def _within(at: Any, start: datetime, end: datetime) -> bool:
    if not at:
        return False
    try:
        parsed = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return start <= parsed <= end


def main() -> None:
    run(server, "cairn-mcp-observability")


if __name__ == "__main__":
    main()
