"""Router HTTP surface.

One endpoint that matters: POST /v1/complete. The caller says what kind of
task it is, how sensitive the data is, and how much budget is left. The
router decides where the work runs, does it, and returns the answer with the
cost attached.

The caller never names a model. That is the whole point: inference is a
routed resource, not a vendor, and a caller that could pick `claude-sonnet`
directly would eventually pick it for restricted data.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from cairn_core.auth import AuthError, bearer, verify_internal
from cairn_core.config import settings
from cairn_core.domain import LLMCall, LLMRequest, Route, TaskClass
from cairn_core.sensitivity import Sensitivity
from cairn_core.telemetry import get_logger, record_llm_call, setup, span
from cairn_router.configmaps import (
    DEFAULT_POLICY,
    DEFAULT_PRICES,
    ConfigFile,
    PriceBook,
    RoutingPolicy,
)
from cairn_router.providers import CloudProvider, LocalProvider, ProviderError, build_call
from cairn_router.routing import CloudState, LocalState, decide

log = get_logger(__name__)

ROUTE_DECISIONS = Counter(
    "cairn_route_decisions_total", "Routing decisions", ["route", "reason", "task"]
)
LLM_COST = Histogram(
    "cairn_llm_call_cost_usd",
    "Cost per LLM call",
    ["route", "task"],
    buckets=(0.0, 0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
)
LLM_LATENCY = Histogram(
    "cairn_llm_call_latency_seconds",
    "LLM call latency",
    ["route", "task"],
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 45, 90),
)
COLD_START_SPILLS = Counter(
    "cairn_cold_start_spills_total", "Calls sent to cloud while the GPU warms"
)
QUEUE_WAITS = Histogram(
    "cairn_local_queue_wait_seconds",
    "Time spent waiting for the local tier",
    buckets=(1, 5, 15, 30, 60, 120),
)
LOCAL_QUEUE_DEPTH = Gauge("cairn_local_queue_depth", "vLLM requests waiting")
LOCAL_HEALTHY = Gauge("cairn_local_healthy", "1 when the local tier is serving")


class CompleteRequest(BaseModel):
    task_class: TaskClass
    sensitivity: str = "public"
    messages: list[dict[str, str]]
    system: str | None = None
    max_tokens: int = Field(default=2048, le=8192)
    temperature: float = 0.0
    response_schema: dict[str, Any] | None = None
    trajectory_id: str | None = None
    #: Budget state, so the router can enforce the cost cap. The router is the
    #: only component that knows the price, so it is the only one that can.
    spent_usd: Decimal = Decimal("0")
    max_cost_usd: Decimal = Decimal("1.50")
    deadline_ms: int = 120_000


class CompleteResponse(BaseModel):
    text: str
    model: str
    route: str
    route_reason: str
    tokens_in: int
    tokens_out: int
    cached_read_tokens: int
    cost_usd: Decimal
    latency_ms: int


class State:
    policy: ConfigFile[RoutingPolicy]
    prices: ConfigFile[PriceBook]
    local: LocalProvider
    cloud: CloudProvider


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    cfg = settings()
    setup(cfg.otel.model_copy(update={"service_name": "cairn-router"}))

    state.policy = ConfigFile(
        cfg.router.policy_path, RoutingPolicy.parse, DEFAULT_POLICY, "policy.yaml"
    )
    state.prices = ConfigFile(
        cfg.router.prices_path, PriceBook.parse, DEFAULT_PRICES, "prices.yaml"
    )
    state.local = LocalProvider(cfg.router)
    state.cloud = CloudProvider(cfg.router)

    tasks = [
        asyncio.create_task(_poll_local()),
        asyncio.create_task(state.policy.watch()),
        asyncio.create_task(state.prices.watch()),
    ]
    log.info(
        "router_started",
        policy_version=state.policy.value.version,
        price_version=state.prices.value.version,
        cloud_configured=state.cloud.configured,
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await state.local.aclose()


async def _poll_local() -> None:
    while True:
        await state.local.poll()
        LOCAL_QUEUE_DEPTH.set(state.local.queue_depth)
        LOCAL_HEALTHY.set(1 if state.local.healthy else 0)
        await asyncio.sleep(2)


app = FastAPI(title="cairn-router", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    # Ready even with a cold local tier: the router's job includes deciding
    # what to do when local is down, so it is useful while local is down.
    return {
        "status": "ok",
        "local_healthy": state.local.healthy,
        "local_queue_depth": state.local.queue_depth,
        "cloud_configured": state.cloud.configured,
        "policy_version": state.policy.value.version,
    }


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/complete", response_model=CompleteResponse)
async def complete(req: CompleteRequest, request: Request) -> CompleteResponse:
    cfg = settings()
    try:
        claims = verify_internal(bearer(request.headers), cfg.auth)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if req.trajectory_id and claims.trajectory_id != req.trajectory_id:
        # The token is bound to one investigation. A token that could drive
        # any trajectory is a token worth stealing.
        raise HTTPException(status_code=403, detail="token/trajectory mismatch")

    sensitivity = Sensitivity.parse(req.sensitivity)
    call = await _run(req, sensitivity)
    return CompleteResponse(**call.model_dump(mode="python"))


async def _run(req: CompleteRequest, sensitivity: Sensitivity) -> LLMCall:
    policy = state.policy.value
    cfg = settings()
    started = time.monotonic()
    deadline_ms = req.deadline_ms

    while True:
        decision = decide(
            task=req.task_class,
            sensitivity=sensitivity,
            tier=policy.tier_for(req.task_class),
            local=LocalState(
                healthy=state.local.healthy,
                queue_depth=state.local.queue_depth,
                cold_starting=not state.local.healthy,
            ),
            cloud=CloudState(
                configured=state.cloud.configured,
                error_rate_5m=state.cloud.error_rate(cfg.router.cloud_error_rate_window_s),
            ),
            limits=policy.limits,
            spent_usd=req.spent_usd,
            max_cost_usd=req.max_cost_usd,
            deadline_ms=deadline_ms,
        )
        ROUTE_DECISIONS.labels(
            route=decision.route.value,
            reason=decision.reason,
            task=req.task_class.value,
        ).inc()

        if decision.route is Route.FAIL:
            raise HTTPException(status_code=503, detail=decision.reason)

        if decision.route is Route.QUEUE_LOCAL:
            waited = time.monotonic() - started
            if waited * 1000 >= deadline_ms:
                QUEUE_WAITS.observe(waited)
                raise HTTPException(
                    status_code=504,
                    detail=f"timed out waiting for the local tier ({decision.reason})",
                )
            await asyncio.sleep(2)
            deadline_ms = req.deadline_ms - int((time.monotonic() - started) * 1000)
            continue

        QUEUE_WAITS.observe(time.monotonic() - started)
        break

    if "cold_start_spill" in decision.reason:
        COLD_START_SPILLS.inc()

    llm_req = LLMRequest(
        task_class=req.task_class,
        messages=[{"role": m["role"], "content": m["content"]} for m in req.messages],
        system=req.system,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        response_schema=req.response_schema,
        trajectory_id=req.trajectory_id,
    )

    call_started = time.perf_counter()
    with span(
        "llm.call",
        **{"cairn.task_class": req.task_class.value, "cairn.sensitivity": str(sensitivity)},
    ) as sp:
        try:
            if decision.route is Route.LOCAL:
                model = cfg.router.vllm_model
                text, tin, tout, cached = await state.local.complete(llm_req)
            else:
                model = state.cloud.model_for(decision.route)
                text, tin, tout, cached = await state.cloud.complete(llm_req, decision.route)
        except ProviderError as exc:
            log.error("provider_failed", route=decision.route.value, error=str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        latency_ms = int((time.perf_counter() - call_started) * 1000)
        cost = state.prices.value.cost(model, tokens_in=tin, tokens_out=tout, cached_read=cached)
        record_llm_call(
            sp,
            model=model,
            route=decision.route.value,
            route_reason=decision.reason,
            tokens_in=tin,
            tokens_out=tout,
            cached_read_tokens=cached,
            cost_usd=cost,
            task_class=req.task_class.value,
        )

    LLM_COST.labels(route=decision.route.value, task=req.task_class.value).observe(float(cost))
    LLM_LATENCY.labels(route=decision.route.value, task=req.task_class.value).observe(
        latency_ms / 1000
    )

    return build_call(
        text=text,
        model=model,
        route=decision.route,
        reason=decision.reason,
        tokens_in=tin,
        tokens_out=tout,
        cached=cached,
        cost=cost,
        latency_ms=latency_ms,
    )
