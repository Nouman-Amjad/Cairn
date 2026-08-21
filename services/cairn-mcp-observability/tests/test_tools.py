"""Observability tools: shaping, capping, and the injection guards."""

from __future__ import annotations

import json

import httpx
import pytest

from cairn_core.config import S3Settings, settings
from cairn_mcp_kit import identity, results
from cairn_mcp_observability import server as srv
from cairn_mcp_observability.backends import BackendError, Backends, check_ident

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def dev_identity(monkeypatch):
    """Authenticated read-only caller, policy engine disabled (as in dev)."""
    from cairn_mcp_kit import guard

    identity.set_claims(identity.dev_claims())
    monkeypatch.setattr(guard, "_policy", None)
    monkeypatch.setattr(results, "_store", None)
    monkeypatch.setattr(
        results,
        "store",
        lambda: __import__("cairn_core.artifacts", fromlist=["ArtifactStore"]).ArtifactStore(
            S3Settings(bucket=":memory:")
        ),
    )
    yield
    identity.set_claims(None)


def wire(monkeypatch, handler, **cfg):
    backends = Backends()
    for key, value in cfg.items():
        setattr(backends.cfg, key, value)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://backend")
    backends._clients = dict.fromkeys(("prom", "loki", "tempo", "argocd", "github"), client)
    monkeypatch.setattr(srv, "_backends", backends)
    return backends


def prom_matrix(n_points=10):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "http_p95", "app": "checkout-api"},
                    "values": [[1700000000 + i * 60, str(0.2 + i * 0.1)] for i in range(n_points)],
                }
            ],
        },
    }


def loki_streams(n_lines, line="connection timeout"):
    return {
        "data": {
            "result": [
                {
                    "stream": {"app": "checkout-api", "pod": "checkout-api-7f9", "level": "error"},
                    "values": [
                        [str(1700000000000000000 + i), f"{line} seq={i}"] for i in range(n_lines)
                    ],
                }
            ]
        }
    }


async def test_query_metrics_summarises_series(monkeypatch):
    wire(
        monkeypatch, lambda r: httpx.Response(200, json=prom_matrix()), prometheus_url="http://prom"
    )
    out = await srv.query_metrics(query="http_p95", start="-1h")
    assert out["total_matched"] == 1
    row = out["rows"][0]
    assert row["metric"] == "http_p95"
    assert row["min"] == 0.2 and row["max"] == pytest.approx(1.1)
    assert len(row["series"]) == 10
    assert out["sensitivity"] == "public"


async def test_metrics_backend_error_is_a_result_not_an_exception(monkeypatch):
    wire(
        monkeypatch,
        lambda r: httpx.Response(422, text="parse error at char 3"),
        prometheus_url="http://prom",
    )
    out = await srv.query_metrics(query="not(valid")
    assert out["ok"] is False
    assert "parse error" in out["error"]


async def test_unconfigured_backend_tells_the_model_not_to_retry(monkeypatch):
    wire(monkeypatch, lambda r: httpx.Response(200, json={}), prometheus_url=None)
    out = await srv.query_metrics(query="up")
    assert out["kind"] == "not_configured"
    assert "Do not retry" in out["error"]


async def test_query_logs_caps_output_and_offloads_the_rest(monkeypatch):
    wire(
        monkeypatch,
        lambda r: httpx.Response(200, json=loki_streams(500)),
        loki_url="http://loki",
        kube_api_in_cluster=False,
    )
    out = await srv.query_logs(service="checkout-api", filter="timeout", limit=500)

    assert out["total_matched"] == 500
    assert out["returned"] < 500, "the 4k token cap must bite before the row limit"
    assert out["artifact_id"].startswith("art_")
    assert out["facets"]["pod"]["checkout-api-7f9"] == 500
    assert "1" in out["next_cursor"]
    # the whole point: what came back is small
    from cairn_core.tokens import estimate_json

    assert estimate_json(out["rows"]) <= settings().mcp.max_response_tokens * 1.1


async def test_logs_from_a_service_are_internal_not_public(monkeypatch):
    wire(
        monkeypatch,
        lambda r: httpx.Response(200, json=loki_streams(3)),
        loki_url="http://loki",
        kube_api_in_cluster=False,
    )
    out = await srv.query_logs(service="checkout-api")
    assert out["sensitivity"] == "internal"


async def test_pii_in_a_log_line_makes_the_result_restricted(monkeypatch):
    wire(
        monkeypatch,
        lambda r: httpx.Response(
            200, json=loki_streams(3, line="charge failed for alice@example.com")
        ),
        loki_url="http://loki",
        kube_api_in_cluster=False,
    )
    out = await srv.query_logs(service="checkout-api")
    assert out["sensitivity"] == "restricted"
    assert any("email" in reason for reason in out["sensitivity_reasons"])


async def test_service_name_cannot_break_out_of_the_log_selector(monkeypatch):
    wire(monkeypatch, lambda r: httpx.Response(200, json=loki_streams(1)), loki_url="http://loki")
    out = await srv.query_logs(service='checkout"} |= "secret')
    assert out["ok"] is False
    assert "not a valid identifier" in out["error"]


async def test_filter_text_is_escaped_into_the_query(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("query", ""))
        return httpx.Response(200, json=loki_streams(1))

    wire(monkeypatch, handler, loki_url="http://loki", kube_api_in_cluster=False)
    await srv.query_logs(service="checkout-api", filter='oops" | drop_everything')

    query = seen[0]
    assert query.startswith('{app="checkout-api"}')
    # exactly two unescaped quotes belong to the filter literal itself
    assert '\\"' in query
    assert "drop_everything" in query  # kept as text, defanged as syntax


async def test_deploy_timeline_filters_to_the_window(monkeypatch):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    payload = {
        "status": {
            "history": [
                {
                    "deployedAt": (now - timedelta(minutes=20)).isoformat(),
                    "revision": "a" * 40,
                    "id": 9,
                },
                {
                    "deployedAt": (now - timedelta(days=30)).isoformat(),
                    "revision": "b" * 40,
                    "id": 8,
                },
            ]
        }
    }
    wire(
        monkeypatch,
        lambda r: httpx.Response(200, json=payload),
        argocd_url="http://argocd",
    )
    srv.backends().cfg.argocd_token = type("S", (), {"get_secret_value": lambda self: "t"})()

    out = await srv.get_deploy_timeline(service="checkout-api", start="-24h")
    assert out["total_matched"] == 1
    assert out["rows"][0]["revision"] == "a" * 12


async def test_denied_when_the_caller_lacks_the_scope(monkeypatch):
    from cairn_core.auth import InternalClaims

    identity.set_claims(
        InternalClaims(
            sub="nobody",
            email="n@x",
            groups=(),
            team=None,
            trajectory_id="stdio",
            scopes=frozenset(),
            jti="j",
        )
    )
    out = await srv.query_metrics(query="up")
    assert out["kind"] == "denied"
    assert "Do not retry" in out["error"]


def test_identifier_validation_rejects_injection_shapes():
    for bad in ['a"}', "a b", "a\nb", "-leading", "x" * 300, "a{b}"]:
        with pytest.raises(BackendError):
            check_ident(bad, "service")
    for good in ["checkout-api", "checkout_api.v2", "a1"]:
        assert check_ident(good, "service") == good


def test_tool_descriptions_tell_the_model_when_to_use_them():
    """A tool description is the only documentation the agent reads."""
    import asyncio

    tools = asyncio.run(srv.server.list_tools())
    assert {t.name for t in tools} == {
        "query_metrics",
        "query_logs",
        "list_services",
        "get_deploy_timeline",
        "get_traces",
        "fetch_artifact",
    }
    for tool in tools:
        assert tool.description and len(tool.description) > 80, tool.name
        assert json.dumps(tool.inputSchema)  # schema must serialise
