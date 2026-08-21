"""Gateway: authentication, limits, and who may read whose investigation."""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest

from cairn_core.config import settings
from cairn_core.db import session
from cairn_core.repo import create_trajectory
from cairn_gateway import app as gw
from cairn_gateway.limits import Verdict

pytestmark = pytest.mark.anyio


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gateway")


@pytest.fixture(autouse=True)
def dev_auth(monkeypatch):
    monkeypatch.setattr(settings().auth, "dev_mode", True)


@pytest.fixture
def allow_all(monkeypatch):
    async def ok(*_a, **_k):
        return Verdict(allowed=True)

    monkeypatch.setattr(gw.limiter, "check_rate", ok)
    monkeypatch.setattr(gw.limiter, "check_budget", ok)


def as_user(sub="alice@example.com", groups="sre"):
    return {"x-cairn-dev-user": sub, "x-cairn-dev-groups": groups}


def dispatch_ok(monkeypatch, capture: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(
            202, json={"trajectory_id": "x", "state": "PLANNING", "prompt_version": "test"}
        )

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gw.httpx, "AsyncClient", Patched)


async def test_query_without_identity_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings().auth, "dev_mode", False)
    resp = await client.post("/v1/queries", json={"query": "why so slow?"})
    assert resp.status_code == 401


async def test_accepted_query_returns_a_stream_url(client, monkeypatch, allow_all):
    calls: list[httpx.Request] = []
    dispatch_ok(monkeypatch, calls)
    resp = await client.post(
        "/v1/queries", json={"query": "why did checkout spike?"}, headers=as_user()
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["stream"].endswith("/events")
    uuid.UUID(body["trajectory_id"])  # a real id, not a placeholder

    # the orchestrator is called with a freshly minted internal token bound to
    # that trajectory, never with the user's IdP token
    from cairn_core.auth import verify_internal

    token = calls[0].headers["authorization"].removeprefix("Bearer ")
    claims = verify_internal(token, settings().auth)
    assert claims.sub == "alice@example.com"
    assert claims.trajectory_id == body["trajectory_id"]
    assert claims.has("tools:write")  # sre


async def test_engineering_group_gets_no_write_scope(client, monkeypatch, allow_all):
    calls: list[httpx.Request] = []
    dispatch_ok(monkeypatch, calls)
    await client.post("/v1/queries", json={"query": "why?"}, headers=as_user(groups="engineering"))

    from cairn_core.auth import verify_internal

    claims = verify_internal(
        calls[0].headers["authorization"].removeprefix("Bearer "), settings().auth
    )
    assert claims.has("tools:read") and not claims.has("tools:write")


async def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    async def limited(*_a, **_k):
        return Verdict(allowed=False, reason="rate limit of 20/min exceeded", retry_after_s=42)

    async def ok(*_a, **_k):
        return Verdict(allowed=True)

    monkeypatch.setattr(gw.limiter, "check_rate", limited)
    monkeypatch.setattr(gw.limiter, "check_budget", ok)

    resp = await client.post("/v1/queries", json={"query": "why?"}, headers=as_user())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "42"


async def test_exhausted_cost_budget_returns_402(client, monkeypatch):
    async def ok(*_a, **_k):
        return Verdict(allowed=True)

    async def broke(*_a, **_k):
        return Verdict(allowed=False, reason="daily cost budget of $25.00 reached")

    monkeypatch.setattr(gw.limiter, "check_rate", ok)
    monkeypatch.setattr(gw.limiter, "check_budget", broke)

    resp = await client.post("/v1/queries", json={"query": "why?"}, headers=as_user())
    assert resp.status_code == 402
    assert "budget" in resp.json()["detail"]


async def test_orchestrator_at_capacity_is_a_503_not_a_500(client, monkeypatch, allow_all):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "at capacity"})

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gw.httpx, "AsyncClient", Patched)
    resp = await client.post("/v1/queries", json={"query": "why?"}, headers=as_user())
    assert resp.status_code == 503


@pytest.mark.requires_db
async def test_you_cannot_read_someone_elses_investigation(client, db, allow_all):
    trajectory_id = uuid.uuid4()
    async with session() as sess:
        await create_trajectory(
            sess,
            user_id="alice@example.com",
            query="secret investigation",
            prompt_version="test",
            trajectory_id=trajectory_id,
        )

    resp = await client.get(
        f"/v1/trajectories/{trajectory_id}", headers=as_user("mallory@example.com")
    )
    # 404, not 403: the existence of another user's trajectory is itself
    # information worth withholding
    assert resp.status_code == 404


@pytest.mark.requires_db
async def test_a_platform_admin_can_read_any_investigation(client, db, monkeypatch, allow_all):
    trajectory_id = uuid.uuid4()
    async with session() as sess:
        await create_trajectory(
            sess,
            user_id="alice@example.com",
            query="q",
            prompt_version="test",
            trajectory_id=trajectory_id,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": str(trajectory_id), "state": "COMPLETE"})

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gw.httpx, "AsyncClient", Patched)
    resp = await client.get(
        f"/v1/trajectories/{trajectory_id}",
        headers=as_user("admin@example.com", groups="platform-admin"),
    )
    assert resp.status_code == 200


async def test_whoami_reports_the_spend_so_far(client, monkeypatch):
    async def spent(*_a, **_k):
        return Decimal("3.25")

    monkeypatch.setattr(gw.limiter, "spent_today", spent)
    resp = await client.get("/v1/me", headers=as_user())
    body = resp.json()
    assert body["sub"] == "alice@example.com"
    assert body["spent_today_usd"] == "3.25"
    assert body["daily_budget_usd"] == settings().max_daily_cost_per_user_usd


async def test_global_circuit_breaker_sheds_load(client, monkeypatch):
    """Per-user caps bound one engineer; the breaker bounds a bad deploy."""

    async def ok(*_a, **_k):
        return Verdict(allowed=True)

    async def tripped(*_a, **_k):
        return Verdict(
            allowed=False,
            reason="global spend $61.00 has passed 150% of the $40.00 daily forecast.",
        )

    monkeypatch.setattr(gw.limiter, "check_rate", ok)
    monkeypatch.setattr(gw.limiter, "check_budget", ok)
    monkeypatch.setattr(gw.limiter, "check_circuit", tripped)

    resp = await client.post("/v1/queries", json={"query": "why?"}, headers=as_user())
    # 503, not 402: this is degraded-and-retryable, not the user's fault.
    assert resp.status_code == 503
    assert "daily forecast" in resp.json()["detail"]


async def test_breaker_is_checked_after_the_per_user_cap(client, monkeypatch, allow_all):
    """A user already over their own cap must not consume breaker checks."""
    calls: list[str] = []

    async def note_circuit(*_a, **_k):
        calls.append("circuit")
        return Verdict(allowed=True)

    async def broke(*_a, **_k):
        return Verdict(allowed=False, reason="daily cost budget of $5.00 reached")

    monkeypatch.setattr(gw.limiter, "check_budget", broke)
    monkeypatch.setattr(gw.limiter, "check_circuit", note_circuit)

    resp = await client.post("/v1/queries", json={"query": "why?"}, headers=as_user())
    assert resp.status_code == 402
    assert calls == []


async def test_spend_is_recorded_globally_as_well_as_per_user(monkeypatch):
    """The breaker can only see what record_spend writes to the global key."""
    from decimal import Decimal

    written: list[tuple[str, float]] = []

    class FakePipe:
        def incrbyfloat(self, key, amount):
            written.append((key, amount))

        def expire(self, *_a):
            pass

        async def execute(self):
            return None

    monkeypatch.setattr(
        gw.limiter, "_redis", type("R", (), {"pipeline": lambda self: FakePipe()})()
    )
    await gw.limiter.record_spend("alice@example.com", Decimal("0.42"))

    keys = [k for k, _ in written]
    assert any("alice@example.com" in k for k in keys)
    assert any("_global" in k for k in keys), "the breaker needs a global counter"
