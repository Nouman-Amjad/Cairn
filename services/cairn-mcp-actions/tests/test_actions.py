"""Write tools: gated, idempotent, and honest about being queued."""

from __future__ import annotations

import httpx
import pytest

from cairn_core.auth import InternalClaims
from cairn_mcp_actions import server as srv
from cairn_mcp_actions.executors import EXECUTORS
from cairn_mcp_kit import identity

pytestmark = pytest.mark.anyio

TRAJ = "11111111-1111-1111-1111-111111111111"


def claims(scopes=("tools:read", "tools:write"), sub="alice"):
    return InternalClaims(
        sub=sub,
        email=f"{sub}@example.com",
        groups=("sre",),
        team="checkout",
        trajectory_id=TRAJ,
        scopes=frozenset(scopes),
        jti="j",
    )


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    from cairn_mcp_kit import guard

    identity.set_claims(claims(), raw_token="user-token")
    monkeypatch.setattr(guard, "_policy", None)
    yield
    identity.set_claims(None)


@pytest.fixture
def captured(monkeypatch):
    """Stand in for the approval service and record what was filed."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        calls.append({"body": body, "auth": request.headers.get("authorization")})
        return httpx.Response(
            200,
            json={
                "id": "a" * 32,
                "state": "PENDING",
                "expires_at": "2026-07-26T00:15:00+00:00",
                "required_approvals": body["required_approvals"],
            },
        )

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)
    return calls


async def test_write_tool_queues_and_says_so(captured):
    out = await srv.create_ticket(
        title="checkout 503s after deploy",
        description="evidence…",
        reason="503 rate rose 4 minutes after the 03:02 deploy",
    )
    assert out["ok"] is True
    assert out["status"] == "pending_approval"
    assert out["approval_id"] == "a" * 32
    # the model must be told not to retry, in the result it actually reads
    assert "do not request this action again" in out["message"].lower()


async def test_the_users_own_token_is_forwarded(captured):
    await srv.create_ticket(title="t", description="d", reason="r")
    assert captured[0]["auth"] == "Bearer user-token", (
        "filing as a service account would defeat approver != requester"
    )


async def test_rollback_needs_two_approvers(captured):
    await srv.rollback_deploy(
        service="checkout-api", from_sha="def456", to_sha="abc123", reason="evidence"
    )
    assert captured[0]["body"]["required_approvals"] == 2


async def test_other_actions_need_one_approver(captured):
    await srv.scale_deployment(service="checkout-api", replicas=6, reason="saturation")
    await srv.silence_alert(alert="HighLatency", duration_minutes=30, reason="known")
    assert [c["body"]["required_approvals"] for c in captured] == [1, 1]


async def test_identical_requests_share_an_idempotency_key(captured):
    args = dict(service="checkout-api", from_sha="def456", to_sha="abc123", reason="x")
    await srv.rollback_deploy(**args)
    await srv.rollback_deploy(**dict(args, reason="reworded justification"))
    keys = [c["body"]["idempotency_key"] for c in captured]
    assert keys[0] == keys[1], "rewording the reason must not queue a second rollback"


async def test_different_target_is_a_different_request(captured):
    await srv.rollback_deploy(
        service="checkout-api", from_sha="def456", to_sha="abc123", reason="x"
    )
    await srv.rollback_deploy(
        service="checkout-api", from_sha="def456", to_sha="999999", reason="x"
    )
    keys = [c["body"]["idempotency_key"] for c in captured]
    assert keys[0] != keys[1]


async def test_reader_cannot_request_a_write(captured):
    identity.set_claims(claims(scopes=("tools:read",)), raw_token="t")
    out = await srv.rollback_deploy(
        service="checkout-api", from_sha="a" * 7, to_sha="b" * 7, reason="x"
    )
    assert out["kind"] == "denied"
    assert not captured, "nothing may be filed for an unauthorised caller"


async def test_approval_service_outage_is_reported_not_retried(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(boom)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)

    out = await srv.create_ticket(title="t", description="d", reason="r")
    assert out["kind"] == "unavailable"
    assert "has NOT been queued" in out["error"]


async def test_execute_endpoint_rejects_a_non_approver_token():
    from starlette.requests import Request

    from cairn_core.auth import mint_internal
    from cairn_core.config import settings
    from cairn_core.domain import UserContext

    token = mint_internal(
        UserContext(sub="alice", email="a@b", groups=["sre"]), TRAJ, settings().auth
    )

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/internal/execute",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }

    async def receive():
        return {"type": "http.request", "body": b"{}"}

    resp = await srv.execute(Request(scope, receive))
    assert resp.status_code == 403


def test_every_tool_has_an_executor_and_vice_versa():
    """A tool with no executor queues an approval that can never run; an
    executor with no tool is a side effect nothing gates."""
    import asyncio

    tools = {t.name for t in asyncio.run(srv.server.list_tools())}
    assert tools == set(EXECUTORS)
