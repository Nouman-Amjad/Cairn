"""HTTP surface: auth binding, cost attribution, and the 503 that says no."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from cairn_core.auth import mint_internal
from cairn_core.config import settings
from cairn_core.domain import UserContext
from cairn_router import app as router_app
from cairn_router.configmaps import (
    DEFAULT_POLICY,
    DEFAULT_PRICES,
    PriceBook,
    RoutingPolicy,
)

TRAJ = "11111111-1111-1111-1111-111111111111"


class FakeLocal:
    def __init__(self, healthy: bool = True, queue_depth: int = 0) -> None:
        self.healthy = healthy
        self.queue_depth = queue_depth
        self.calls = 0

    async def complete(self, req):
        self.calls += 1
        return "local answer", 1200, 300, 0


class FakeCloud:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    def error_rate(self, _window):
        return 0.0

    def model_for(self, route):
        return "claude-sonnet-4-5"

    async def complete(self, req, route):
        self.calls += 1
        return "cloud answer", 4000, 500, 1000


class Holder:
    def __init__(self, value):
        self.value = value


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(
        router_app.state, "policy", Holder(RoutingPolicy.parse(DEFAULT_POLICY)), raising=False
    )
    monkeypatch.setattr(
        router_app.state, "prices", Holder(PriceBook.parse(DEFAULT_PRICES)), raising=False
    )
    local, cloud = FakeLocal(), FakeCloud()
    monkeypatch.setattr(router_app.state, "local", local, raising=False)
    monkeypatch.setattr(router_app.state, "cloud", cloud, raising=False)
    return local, cloud


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=router_app.app)
    return httpx.AsyncClient(transport=transport, base_url="http://router")


def token(groups=("sre",), trajectory_id=TRAJ):
    user = UserContext(sub="u1", email="a@b.com", groups=list(groups), team="checkout")
    return mint_internal(user, trajectory_id, settings().auth)


def body(**kw):
    payload = {
        "task_class": "synthesis",
        "sensitivity": "public",
        "messages": [{"role": "user", "content": "why did checkout spike?"}],
        "trajectory_id": TRAJ,
    }
    payload.update(kw)
    return payload


async def test_rejects_missing_token(client):
    resp = await client.post("/v1/complete", json=body())
    assert resp.status_code == 401


async def test_rejects_token_bound_to_another_trajectory(client, wired):
    resp = await client.post(
        "/v1/complete",
        json=body(),
        headers={
            "authorization": f"Bearer {token(trajectory_id='22222222-2222-2222-2222-222222222222')}"
        },
    )
    assert resp.status_code == 403


async def test_public_synthesis_goes_to_cloud_and_is_priced(client, wired):
    local, cloud = wired
    resp = await client.post(
        "/v1/complete", json=body(), headers={"authorization": f"Bearer {token()}"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert cloud.calls == 1 and local.calls == 0
    assert data["route"] == "cloud_sonnet"
    # 3000 billable input + 1000 cached + 500 output on Sonnet pricing
    expected = (
        Decimal("3000") * Decimal("3")
        + Decimal("500") * Decimal("15")
        + Decimal("1000") * Decimal("0.30")
    ) / Decimal("1000000")
    assert Decimal(str(data["cost_usd"])) == expected


async def test_restricted_synthesis_stays_local(client, wired):
    local, cloud = wired
    resp = await client.post(
        "/v1/complete",
        json=body(sensitivity="restricted"),
        headers={"authorization": f"Bearer {token()}"},
    )
    assert resp.status_code == 200
    assert resp.json()["route"] == "local"
    assert cloud.calls == 0, "restricted work must not reach the cloud provider"
    assert local.calls == 1


async def test_restricted_with_dead_local_and_no_time_gives_503(client, wired, monkeypatch):
    local, cloud = wired
    local.healthy = False
    resp = await client.post(
        "/v1/complete",
        json=body(sensitivity="restricted", deadline_ms=500),
        headers={"authorization": f"Bearer {token()}"},
    )
    assert resp.status_code == 503
    assert "restricted" in resp.json()["detail"]
    assert cloud.calls == 0


async def test_cost_cap_pushes_work_local(client, wired):
    local, cloud = wired
    resp = await client.post(
        "/v1/complete",
        json=body(spent_usd="1.30", max_cost_usd="1.50"),
        headers={"authorization": f"Bearer {token()}"},
    )
    assert resp.json()["route_reason"].endswith("cost_backoff")
    assert cloud.calls == 0
