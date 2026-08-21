"""Thin clients for the observability backends.

Cairn owns none of these systems. It is a read-mostly consumer, and every
call here carries a timeout and a result cap, because the backend it is
querying is the same one the on-call engineer is staring at.

No query is ever assembled by string interpolation of model output into
another query's syntax. PromQL and LogQL arrive as opaque parameter values,
and the pieces Cairn does construct (label matchers) are built from validated
identifiers.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from cairn_core.config import BackendSettings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

#: Kubernetes-ish identifier. Anything Cairn splices into a label matcher
#: must match this, so a service name cannot carry `"} or vector(1) #`.
IDENT = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,126}[a-zA-Z0-9])?$")


class BackendError(RuntimeError):
    pass


class NotConfigured(BackendError):
    def __init__(self, backend: str) -> None:
        super().__init__(f"{backend} is not configured")
        self.backend = backend


def check_ident(value: str, what: str) -> str:
    if not IDENT.match(value):
        raise BackendError(
            f"{what} {value!r} is not a valid identifier; expected letters, "
            "digits, dots, dashes or underscores"
        )
    return value


class Backends:
    def __init__(self, cfg: BackendSettings | None = None) -> None:
        self.cfg = cfg or BackendSettings()
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _client(
        self, name: str, base_url: str, headers: dict[str, str] | None = None
    ) -> httpx.AsyncClient:
        if name not in self._clients:
            self._clients[name] = httpx.AsyncClient(
                base_url=base_url,
                timeout=self.cfg.query_timeout_s,
                headers=headers or {},
            )
        return self._clients[name]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    # ---------------------------------------------------------------- metrics
    async def prometheus_range(
        self, query: str, start: datetime, end: datetime, step: str
    ) -> list[dict[str, Any]]:
        if not self.cfg.prometheus_url:
            raise NotConfigured("prometheus")
        client = self._client("prom", self.cfg.prometheus_url)
        resp = await client.get(
            "/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
        )
        return _prom_series(_prom_body(resp))

    async def prometheus_instant(self, query: str, at: datetime) -> list[dict[str, Any]]:
        if not self.cfg.prometheus_url:
            raise NotConfigured("prometheus")
        client = self._client("prom", self.cfg.prometheus_url)
        resp = await client.get("/api/v1/query", params={"query": query, "time": at.timestamp()})
        return _prom_series(_prom_body(resp))

    async def label_values(self, label: str, matcher: str | None = None) -> list[str]:
        if not self.cfg.prometheus_url:
            raise NotConfigured("prometheus")
        client = self._client("prom", self.cfg.prometheus_url)
        params: dict[str, Any] = {}
        if matcher:
            params["match[]"] = matcher
        resp = await client.get(
            f"/api/v1/label/{check_ident(label, 'label')}/values", params=params
        )
        body = _prom_body(resp)
        return [str(v) for v in body.get("data", [])]

    # ------------------------------------------------------------------- logs
    async def loki_query(
        self,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int,
        direction: str = "backward",
    ) -> list[dict[str, Any]]:
        if not self.cfg.loki_url:
            raise NotConfigured("loki")
        client = self._client("loki", self.cfg.loki_url)
        resp = await client.get(
            "/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit,
                "direction": direction,
            },
        )
        if resp.status_code >= 400:
            raise BackendError(f"loki: {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        rows: list[dict[str, Any]] = []
        for stream in body.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                rows.append(
                    {
                        "ts": datetime.fromtimestamp(int(ts_ns) / 1e9).isoformat(),
                        "line": line,
                        **{k: v for k, v in labels.items() if k in _KEPT_LABELS},
                    }
                )
        rows.sort(key=lambda r: r["ts"])
        return rows

    # ----------------------------------------------------------------- traces
    async def tempo_search(
        self, service: str, min_duration_ms: int, start: datetime, end: datetime, limit: int
    ) -> list[dict[str, Any]]:
        if not self.cfg.tempo_url:
            raise NotConfigured("tempo")
        client = self._client("tempo", self.cfg.tempo_url)
        resp = await client.get(
            "/api/search",
            params={
                "tags": f"service.name={check_ident(service, 'service')}",
                "minDuration": f"{min_duration_ms}ms",
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "limit": limit,
            },
        )
        if resp.status_code >= 400:
            raise BackendError(f"tempo: {resp.status_code} {resp.text[:300]}")
        return [
            {
                "trace_id": t.get("traceID"),
                "root_service": t.get("rootServiceName"),
                "root_operation": t.get("rootTraceName"),
                "duration_ms": t.get("durationMs"),
                "started_at": t.get("startTimeUnixNano"),
            }
            for t in resp.json().get("traces", [])
        ]

    # ---------------------------------------------------------------- deploys
    async def argocd_history(self, service: str) -> list[dict[str, Any]]:
        if not (self.cfg.argocd_url and self.cfg.argocd_token):
            raise NotConfigured("argocd")
        client = self._client(
            "argocd",
            self.cfg.argocd_url,
            {"authorization": f"Bearer {self.cfg.argocd_token.get_secret_value()}"},
        )
        resp = await client.get(f"/api/v1/applications/{check_ident(service, 'service')}")
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise BackendError(f"argocd: {resp.status_code} {resp.text[:300]}")
        history = resp.json().get("status", {}).get("history", []) or []
        return [
            {
                "at": item.get("deployedAt"),
                "revision": (item.get("revision") or "")[:12],
                "source": "argocd",
                "service": service,
                "id": item.get("id"),
            }
            for item in history
        ]

    async def github_deployments(self, service: str) -> list[dict[str, Any]]:
        if not self.cfg.github_token:
            raise NotConfigured("github")
        client = self._client(
            "github",
            self.cfg.github_api_url,
            {
                "authorization": f"Bearer {self.cfg.github_token.get_secret_value()}",
                "accept": "application/vnd.github+json",
            },
        )
        repo = f"{self.cfg.github_org}/{check_ident(service, 'service')}"
        resp = await client.get(f"/repos/{repo}/deployments", params={"per_page": 30})
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise BackendError(f"github: {resp.status_code} {resp.text[:300]}")
        return [
            {
                "at": item.get("created_at"),
                "revision": (item.get("sha") or "")[:12],
                "source": "github",
                "service": service,
                "environment": item.get("environment"),
                "creator": (item.get("creator") or {}).get("login"),
            }
            for item in resp.json()
        ]


_KEPT_LABELS = frozenset(
    {"app", "pod", "container", "namespace", "level", "service_name", "status"}
)


def _prom_body(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise BackendError(f"prometheus: {resp.status_code} {resp.text[:300]}")
    body: dict[str, Any] = resp.json()
    if body.get("status") != "success":
        raise BackendError(f"prometheus: {body.get('error', 'query failed')}")
    return body


def _prom_series(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Prometheus' result shapes into rows.

    Both `matrix` and `vector` collapse to the same row shape so the tool
    contract does not change depending on which query the model wrote.
    """
    data = body.get("data", {})
    out: list[dict[str, Any]] = []
    for series in data.get("result", []):
        labels = series.get("metric", {})
        name = labels.get("__name__", "")
        pretty = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "__name__")
        if "values" in series:
            points = [(float(t), _num(v)) for t, v in series["values"]]
        else:
            t, v = series.get("value", (0, "0"))
            points = [(float(t), _num(v))]
        values = [p[1] for p in points if p[1] is not None]
        out.append(
            {
                "metric": name,
                "labels": pretty,
                "points": len(points),
                "first": points[0][1] if points else None,
                "last": points[-1][1] if points else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": round(sum(values) / len(values), 4) if values else None,
                # The raw series is what makes a spike visible. Downsampled to
                # 60 points: enough shape to reason about, small enough to sit
                # in a context window alongside twenty other results.
                "series": _downsample(points, 60),
            }
        )
    return out


def _num(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # drop NaN


def _downsample(points: list[tuple[float, float | None]], target: int) -> list[list[Any]]:
    if len(points) <= target:
        return [[round(t), v] for t, v in points]
    stride = len(points) / target
    return [[round(points[int(i * stride)][0]), points[int(i * stride)][1]] for i in range(target)]
