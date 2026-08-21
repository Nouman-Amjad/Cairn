"""Orchestrator HTTP surface.

The gateway dispatches here and returns immediately; the loop runs as a
background task on this pod and publishes progress to Redis, which the
gateway streams to the client. That split is what lets a client disconnect
and reconnect without killing the investigation.

Concurrency is bounded by a semaphore, not by the HPA alone. A pod that
accepts a hundred simultaneous investigations will exhaust the router's
budget and the database pool before Kubernetes notices it should scale.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse

from cairn_core.auth import AuthError, bearer, verify_internal
from cairn_core.config import settings
from cairn_core.db import healthcheck, session
from cairn_core.domain import Budget, TrajectoryState
from cairn_core.prompts import load
from cairn_core.repo import claim_lease, create_trajectory, get_trajectory, steps_for
from cairn_core.telemetry import get_logger, setup
from cairn_orchestrator.loop import AgentLoop
from cairn_orchestrator.resume import (
    LEASE_TTL_S,
    POD,
    abandon_expired,
    listen_for_resumes,
    sweep,
)

log = get_logger(__name__)

ACTIVE = Gauge("cairn_active_trajectories", "Trajectories running on this pod")

#: One pod, twelve concurrent investigations. Sized against the database pool
#: (10 connections) and the local tier's `max_num_seqs=16`; beyond this the
#: bottleneck moves somewhere that is harder to see.
MAX_CONCURRENT = 12
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_running: dict[uuid.UUID, asyncio.Task[Any]] = {}
_stop = asyncio.Event()


class DispatchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=4000)
    trajectory_id: str | None = None
    max_cost_usd: Decimal | None = None
    max_iterations: int | None = None
    wall_clock_timeout_s: float | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    cfg = settings()
    setup(cfg.otel.model_copy(update={"service_name": "cairn-orchestrator"}))
    load(cfg.prompt_dir)  # fail fast if the prompt mount is broken

    tasks = [
        asyncio.create_task(listen_for_resumes(_resume, _stop)),
        asyncio.create_task(sweep(_resume, _stop)),
        asyncio.create_task(abandon_expired(_stop)),
    ]
    log.info("orchestrator_started", pod=POD, max_concurrent=MAX_CONCURRENT)
    try:
        yield
    finally:
        _stop.set()
        for task in tasks:
            task.cancel()
        # Let in-flight investigations finish before the pod goes away. A
        # spot reclaim gives two minutes; losing an incident query inside it
        # is not acceptable.
        if _running:
            log.info("draining", in_flight=len(_running))
            await asyncio.wait(list(_running.values()), timeout=90)


app = FastAPI(title="cairn-orchestrator", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "pod": POD, "in_flight": len(_running)}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    if not await healthcheck():
        raise HTTPException(status_code=503, detail="database unavailable")
    # Shed load rather than queue it: a gateway that gets a 503 can retry
    # against another pod, while a request queued behind eleven others just
    # times out slowly.
    if len(_running) >= MAX_CONCURRENT:
        raise HTTPException(status_code=503, detail="at capacity")
    return {"status": "ok", "in_flight": len(_running)}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _claims(request: Request):  # type: ignore[no-untyped-def]
    try:
        return verify_internal(bearer(request.headers), settings().auth)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/v1/dispatch", status_code=202)
async def dispatch(
    req: DispatchRequest, request: Request, background: BackgroundTasks
) -> dict[str, Any]:
    claims = _claims(request)
    token = bearer(request.headers)
    cfg = settings()

    trajectory_id = uuid.UUID(req.trajectory_id) if req.trajectory_id else uuid.uuid4()
    if claims.trajectory_id != str(trajectory_id):
        raise HTTPException(status_code=403, detail="token/trajectory mismatch")

    # Caller-supplied budgets are clamped, never trusted. A caller that can
    # raise its own ceiling does not have one.
    requested = Budget(
        max_cost_usd=req.max_cost_usd or Budget().max_cost_usd,
        max_iterations=req.max_iterations or Budget().max_iterations,
        wall_clock_timeout_s=req.wall_clock_timeout_s or Budget().wall_clock_timeout_s,
    )
    budget = requested.clamp_to(Budget())

    prompts = load(cfg.prompt_dir)
    async with session() as sess:
        await create_trajectory(
            sess,
            user_id=claims.sub,
            query=req.query,
            prompt_version=prompts.version,
            trajectory_id=trajectory_id,
        )
        await claim_lease(sess, trajectory_id, POD, LEASE_TTL_S)

    background.add_task(_run, trajectory_id, req.query, token, budget, False)
    return {
        "trajectory_id": str(trajectory_id),
        "state": TrajectoryState.PLANNING.value,
        "prompt_version": prompts.version,
    }


@app.get("/v1/trajectories/{trajectory_id}")
async def read_trajectory(trajectory_id: uuid.UUID, request: Request) -> dict[str, Any]:
    claims = _claims(request)
    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)
        if trajectory is None:
            raise HTTPException(status_code=404, detail="no such trajectory")
        if trajectory.user_id != claims.sub and "platform-admin" not in claims.groups:
            raise HTTPException(status_code=403, detail="not your trajectory")
        steps = await steps_for(sess, trajectory_id)

    return {
        "id": str(trajectory.id),
        "query": trajectory.query,
        "state": trajectory.state,
        "answer": trajectory.answer,
        "confidence": trajectory.confidence,
        "sensitivity": trajectory.sensitivity,
        "cost_usd": str(trajectory.cost_usd),
        "tokens": {"local": trajectory.tokens_local, "cloud": trajectory.tokens_cloud},
        "prompt_version": trajectory.prompt_version,
        "plan": trajectory.plan,
        "started_at": trajectory.started_at.isoformat(),
        "ended_at": trajectory.ended_at.isoformat() if trajectory.ended_at else None,
        "steps": [
            {
                "seq": step.seq,
                "kind": step.kind,
                "tool": step.tool_name,
                "model": step.model,
                "route": step.route,
                "route_reason": step.route_reason,
                "cost_usd": str(step.cost_usd) if step.cost_usd is not None else None,
                "latency_ms": step.latency_ms,
                "artifact_id": step.artifact_id,
                "sensitivity": step.sensitivity,
                "input": step.input,
                "output": step.output,
                "error": step.error,
                "at": step.created_at.isoformat(),
            }
            for step in steps
        ],
    }


async def _run(
    trajectory_id: uuid.UUID,
    query: str,
    token: str,
    budget: Budget,
    resuming: bool,
) -> None:
    async with _semaphore:
        ACTIVE.inc()
        task = asyncio.current_task()
        if task:
            _running[trajectory_id] = task
        try:
            loop = AgentLoop(trajectory_id=trajectory_id, query=query, token=token, budget=budget)
            result = await loop.run(resuming=resuming)
            log.info(
                "trajectory_finished",
                trajectory_id=str(trajectory_id),
                state=result.state.value,
                cost_usd=str(result.cost_usd),
                iterations=result.iterations,
            )
        finally:
            ACTIVE.dec()
            _running.pop(trajectory_id, None)


async def _resume(trajectory_id: uuid.UUID) -> None:
    """Continue a suspended investigation.

    The token that started it has long expired (5-minute TTL), so a fresh one
    is minted for the original user. The trajectory row is the authority on
    who that was — not anything the caller sends.
    """
    if trajectory_id in _running:
        return

    from cairn_core.auth import mint_internal
    from cairn_core.domain import UserContext

    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)
        if trajectory is None or trajectory.state != TrajectoryState.WAITING_APPROVAL.value:
            return
        if not await claim_lease(sess, trajectory_id, POD, LEASE_TTL_S):
            return  # another pod owns it
        query, user_id = trajectory.query, trajectory.user_id

    token = mint_internal(
        UserContext(sub=user_id, email="", groups=["engineering"]),
        trajectory_id,
        settings().auth,
    )
    await _run(trajectory_id, query, token, Budget(), resuming=True)
