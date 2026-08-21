"""cairn-gateway: the only door into the system.

Responsibilities, in order of importance:

1. Verify the caller's identity against the IdP, once, here.
2. Mint the short-lived internal token everything downstream re-verifies.
3. Enforce rate limits and the per-user daily cost budget.
4. Stream progress to the client over SSE.

It holds no model credentials and makes no tool calls. If this service is
compromised, the attacker gets to impersonate users — which is bad — but not
to bypass a single tool-level authorization check, because those are
evaluated against the token's claims at the tool server.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.responses import PlainTextResponse

from cairn_core.auth import AuthError, bearer, mint_internal, verify_oidc
from cairn_core.config import settings
from cairn_core.db import healthcheck, session
from cairn_core.domain import UserContext
from cairn_core.repo import get_trajectory
from cairn_core.telemetry import bind, get_logger, setup
from cairn_gateway.limits import Limiter
from cairn_orchestrator.events import Event, EventBus, EventType

log = get_logger(__name__)

QUERIES = Counter("cairn_queries_total", "Queries accepted", ["outcome"])
STREAMS = Counter("cairn_sse_streams_total", "SSE streams opened")
CIRCUIT_OPEN = Gauge("cairn_cost_circuit_open", "1 while the global cost breaker is tripped")

limiter = Limiter(settings().redis)
bus = EventBus(settings().redis)


class QueryRequest(BaseModel):
    query: str = Field(min_length=3, max_length=4000)
    max_cost_usd: Decimal | None = None
    max_iterations: int | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    cfg = settings()
    setup(cfg.otel.model_copy(update={"service_name": "cairn-gateway"}))
    if cfg.is_prod and cfg.auth.dev_mode:
        # Refuse to start rather than run a production gateway that trusts a
        # header. This check is the whole reason dev mode is survivable.
        raise RuntimeError("CAIRN_AUTH_DEV_MODE cannot be enabled in prod")
    log.info("gateway_started", env=cfg.env, dev_mode=cfg.auth.dev_mode)
    try:
        yield
    finally:
        await limiter.aclose()
        await bus.aclose()


app = FastAPI(title="cairn-gateway", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings().ui_base_url],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["authorization", "content-type", "last-event-id"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    db_ok, redis_ok = await asyncio.gather(healthcheck(), limiter.healthy())
    if not db_ok:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "redis": redis_ok}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def authenticate(request: Request) -> UserContext:
    cfg = settings()
    if cfg.auth.dev_mode:
        user = request.headers.get("x-cairn-dev-user", "dev@localhost")
        groups = (request.headers.get("x-cairn-dev-groups") or "engineering").split(",")
        return UserContext(sub=user, email=user, groups=[g.strip() for g in groups])
    try:
        return verify_oidc(bearer(request.headers), cfg.auth)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/v1/queries", status_code=202)
async def create_query(req: QueryRequest, request: Request) -> dict[str, Any]:
    cfg = settings()
    user = await authenticate(request)
    bind(user=user.sub)

    rate = await limiter.check_rate(user.sub, cfg.rate_limit_per_minute)
    if not rate.allowed:
        QUERIES.labels(outcome="rate_limited").inc()
        raise HTTPException(
            status_code=429, detail=rate.reason, headers={"retry-after": str(rate.retry_after_s)}
        )

    budget = await limiter.check_budget(user.sub, cfg.max_daily_cost_per_user_usd)
    if not budget.allowed:
        QUERIES.labels(outcome="over_budget").inc()
        raise HTTPException(status_code=402, detail=budget.reason)

    # Global breaker. Per-user caps bound one enthusiastic engineer; they do
    # not bound a change that makes every query cost ten times what it should.
    circuit = await limiter.check_circuit(cfg.daily_cost_forecast_usd, cfg.cost_breaker_trip_ratio)
    if not circuit.allowed:
        QUERIES.labels(outcome="circuit_open").inc()
        CIRCUIT_OPEN.set(1)
        # Degraded, not down: local-only still answers, so this is a 503 the
        # caller can retry rather than a hard refusal.
        raise HTTPException(status_code=503, detail=circuit.reason)
    CIRCUIT_OPEN.set(0)

    trajectory_id = uuid.uuid4()
    token = mint_internal(user, trajectory_id, cfg.auth)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_orchestrator_url()}/v1/dispatch",
                json={
                    "query": req.query,
                    "trajectory_id": str(trajectory_id),
                    "max_cost_usd": str(req.max_cost_usd) if req.max_cost_usd else None,
                    "max_iterations": req.max_iterations,
                },
                headers={"authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        QUERIES.labels(outcome="dispatch_failed").inc()
        raise HTTPException(status_code=502, detail=f"orchestrator unreachable: {exc}") from exc

    if resp.status_code == 503:
        QUERIES.labels(outcome="at_capacity").inc()
        raise HTTPException(status_code=503, detail="all orchestrators are busy; retry shortly")
    if resp.status_code >= 400:
        QUERIES.labels(outcome="dispatch_failed").inc()
        raise HTTPException(status_code=502, detail=resp.text[:300])

    QUERIES.labels(outcome="accepted").inc()
    # Downstream fields first: the gateway minted the trajectory id and bound
    # the token to it, so an echoed value must never overwrite it.
    return {
        **resp.json(),
        "trajectory_id": str(trajectory_id),
        "stream": f"/v1/queries/{trajectory_id}/events",
    }


@app.get("/v1/queries/{trajectory_id}/events")
async def stream(trajectory_id: uuid.UUID, request: Request) -> EventSourceResponse:
    user = await authenticate(request)
    await _assert_owner(trajectory_id, user)
    STREAMS.inc()

    # Replay before subscribing. A client that reconnects mid-investigation —
    # laptop lid, flaky wifi, tab restore — gets the steps it missed instead
    # of a stream that resumes in the middle of a sentence.
    last_seen = int(request.headers.get("last-event-id") or -1)

    async def publisher() -> AsyncIterator[dict[str, str]]:
        seen = last_seen
        for event in await bus.replay(str(trajectory_id), after_seq=seen):
            seen = event.seq
            yield _sse(event)
            if event.type is EventType.DONE:
                return

        async for event in bus.subscribe(str(trajectory_id)):
            if await request.is_disconnected():
                return
            if event.seq <= seen:
                continue  # already replayed
            seen = event.seq
            yield _sse(event)
            if event.type is EventType.DONE:
                return

    return EventSourceResponse(publisher(), ping=15)


@app.get("/v1/trajectories/{trajectory_id}")
async def read_trajectory(trajectory_id: uuid.UUID, request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    await _assert_owner(trajectory_id, user)
    token = mint_internal(user, trajectory_id, settings().auth)
    return await _proxy("GET", f"{_orchestrator_url()}/v1/trajectories/{trajectory_id}", token)


@app.get("/v1/approvals")
async def list_approvals(request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    token = mint_internal(user, uuid.uuid4(), settings().auth)
    return await _proxy("GET", f"{settings().approval.url}/v1/approvals", token)


@app.post("/v1/approvals/{approval_id}/decision")
async def decide(approval_id: uuid.UUID, request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    body = await request.json()
    token = mint_internal(user, uuid.uuid4(), settings().auth)
    return await _proxy(
        "POST",
        f"{settings().approval.url}/v1/approvals/{approval_id}/decision",
        token,
        json=body,
    )


@app.get("/v1/me")
async def whoami(request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    cfg = settings()
    spent = await limiter.spent_today(user.sub)
    return {
        "sub": user.sub,
        "email": user.email,
        "groups": user.groups,
        "spent_today_usd": str(spent),
        "daily_budget_usd": cfg.max_daily_cost_per_user_usd,
    }


async def _assert_owner(trajectory_id: uuid.UUID, user: UserContext) -> None:
    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)
    if trajectory is None:
        raise HTTPException(status_code=404, detail="no such trajectory")
    if trajectory.user_id != user.sub and "platform-admin" not in user.groups:
        # 404, not 403: whether someone else's investigation exists is itself
        # information.
        raise HTTPException(status_code=404, detail="no such trajectory")


async def _proxy(method: str, url: str, token: str, json: Any = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(
                method, url, json=json, headers={"authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=_detail(resp))
    body: dict[str, Any] = resp.json()
    return body


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text[:300]))
    except ValueError:
        return resp.text[:300]


def _sse(event: Event) -> dict[str, str]:
    return {"event": str(event.type), "id": str(event.seq), "data": event.to_json()}


def _orchestrator_url() -> str:
    import os

    return os.environ.get("CAIRN_ORCHESTRATOR_URL", "http://cairn-orchestrator:8000")
