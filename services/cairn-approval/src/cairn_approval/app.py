"""Approval service HTTP surface.

Three kinds of caller, three trust models:

* `mcp-actions` creates requests, authenticated with the internal JWT.
* Slack posts decisions, authenticated by request signature.
* The UI reads pending requests and can decide, authenticated with the user's
  internal JWT.

The Slack path is the sensitive one: it is a public endpoint that ends in a
production change. Signature, timestamp window, and single-use nonce are all
enforced before the payload is even parsed for meaning.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse

from cairn_approval import service
from cairn_approval.slack import (
    SignatureError,
    SlackClient,
    approval_blocks,
    resolved_blocks,
    verify_signature,
)
from cairn_core.auth import AuthError, bearer, mint_internal, verify_internal
from cairn_core.config import settings
from cairn_core.db import healthcheck, session
from cairn_core.domain import ApprovalState, UserContext
from cairn_core.models import Approval
from cairn_core.telemetry import get_logger, setup, span

log = get_logger(__name__)

APPROVAL_LATENCY = Histogram(
    "cairn_approval_latency_seconds",
    "Time from request to decision",
    buckets=(10, 30, 60, 120, 300, 600, 900),
)
APPROVAL_EXPIRED = Counter("cairn_approval_expired_total", "Approvals that timed out")
APPROVAL_DECISIONS = Counter("cairn_approval_decisions_total", "Approval decisions", ["outcome"])
SELF_APPROVAL_BLOCKED = Counter(
    "cairn_self_approval_blocked_total", "Self-approval attempts refused"
)

slack = SlackClient(settings().approval)
#: Single-use nonces for Slack callbacks, so a signed request cannot be
#: replayed inside the 5-minute signature window.
_seen_nonces: dict[str, float] = {}


class CreateRequest(BaseModel):
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    reason: str = ""
    trajectory_id: str | None = None
    required_approvals: int = Field(default=1, ge=1, le=3)
    ttl_s: int = Field(default=900, ge=60, le=3600)


class DecisionRequest(BaseModel):
    approve: bool
    reason: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    cfg = settings()
    setup(cfg.otel.model_copy(update={"service_name": "cairn-approval"}))
    sweeper = asyncio.create_task(_sweep_expired())
    log.info("approval_started", slack_configured=slack.configured)
    try:
        yield
    finally:
        sweeper.cancel()
        await slack.aclose()


app = FastAPI(title="cairn-approval", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    ok = await healthcheck()
    if not ok:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "slack": slack.configured}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _claims(request: Request):  # type: ignore[no-untyped-def]
    try:
        return verify_internal(bearer(request.headers), settings().auth)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/v1/approvals")
async def create_approval(req: CreateRequest, request: Request) -> dict[str, Any]:
    claims = _claims(request)
    trajectory_id = uuid.UUID(req.trajectory_id) if req.trajectory_id else None

    async with session() as sess:
        approval, created = await service.create(
            sess,
            action=req.action,
            args=req.args,
            key=req.idempotency_key,
            requester=claims.sub,
            requester_email=claims.email,
            trajectory_id=trajectory_id,
            required_approvals=req.required_approvals,
            ttl_s=req.ttl_s,
        )
        payload = _public(approval)
        channel, ts = approval.slack_channel, approval.slack_ts
        blocks = approval_blocks(
            approval_id=str(approval.id),
            action=approval.action,
            args=approval.args,
            requester=approval.requested_by,
            trajectory_id=str(approval.trajectory_id) if approval.trajectory_id else None,
            reason=req.reason or "no reason supplied",
            required_approvals=approval.required_approvals,
            expires_in_s=req.ttl_s,
            ui_base_url=settings().ui_base_url,
        )

    # Posting to Slack happens outside the transaction: a Slack outage must
    # not roll back a durable approval record.
    if created and not ts:
        cfg = settings().approval
        posted_ts = await slack.post(
            cfg.slack_channel, blocks, text=f"Approval needed: {req.action}"
        )
        if posted_ts:
            async with session() as sess:
                row = await sess.get(Approval, uuid.UUID(payload["id"]))
                if row:
                    row.slack_channel, row.slack_ts = cfg.slack_channel, posted_ts
        channel = cfg.slack_channel

    payload["notified"] = bool(channel)
    return payload


@app.get("/v1/approvals/{approval_id}")
async def get_approval(approval_id: uuid.UUID, request: Request) -> dict[str, Any]:
    _claims(request)
    async with session() as sess:
        approval = await sess.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="no such approval")
        return _public(approval)


@app.get("/v1/approvals")
async def list_pending(request: Request) -> dict[str, Any]:
    claims = _claims(request)
    async with session() as sess:
        # A requester never sees their own request in their approval queue.
        rows = await service.pending_for_user(sess, exclude_requester=claims.sub)
        return {"approvals": [_public(r) for r in rows]}


@app.post("/v1/approvals/{approval_id}/decision")
async def decide(approval_id: uuid.UUID, req: DecisionRequest, request: Request) -> dict[str, Any]:
    claims = _claims(request)
    if not claims.has("approvals:grant"):
        raise HTTPException(status_code=403, detail="caller cannot approve actions")
    return await _apply_decision(
        approval_id, actor=claims.sub, approve=req.approve, reason=req.reason
    )


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict[str, Any]:
    cfg = settings().approval
    if not cfg.slack_signing_secret:
        raise HTTPException(status_code=503, detail="slack integration not configured")

    body = await request.body()
    try:
        verify_signature(
            body=body,
            timestamp=request.headers.get("x-slack-request-timestamp", ""),
            signature=request.headers.get("x-slack-signature", ""),
            signing_secret=cfg.slack_signing_secret.get_secret_value(),
            max_age_s=cfg.signature_max_age_s,
        )
    except SignatureError as exc:
        log.warning("slack_signature_rejected", reason=str(exc))
        raise HTTPException(status_code=401, detail="invalid signature") from exc

    payload = _slack_payload(body)
    trigger = payload.get("trigger_id") or payload.get("container", {}).get("message_ts")
    if trigger and not _consume_nonce(str(trigger), cfg.signature_max_age_s):
        raise HTTPException(status_code=409, detail="replayed interaction")

    actions = payload.get("actions") or []
    if not actions:
        return {"ok": True}
    action = actions[0]
    approval_id = uuid.UUID(action["value"])
    actor = payload.get("user", {}).get("id", "unknown")
    approve = action.get("action_id") == "cairn_approve"

    try:
        result = await _apply_decision(approval_id, actor=actor, approve=approve)
    except HTTPException as exc:
        if exc.status_code == 403:
            SELF_APPROVAL_BLOCKED.inc()
            return {
                "response_type": "ephemeral",
                "replace_original": False,
                "text": "You raised this request, so you cannot approve it. "
                "Ask another on-call engineer.",
            }
        raise

    channel = payload.get("channel", {}).get("id")
    ts = payload.get("message", {}).get("ts")
    if channel and ts:
        await slack.update(
            channel,
            ts,
            resolved_blocks(result["action"], result["state"], f"<@{actor}>"),
            text=f"{result['action']} {result['state'].lower()}",
        )
    return {"ok": True}


async def _apply_decision(
    approval_id: uuid.UUID, *, actor: str, approve: bool, reason: str | None = None
) -> dict[str, Any]:
    with span("approval.decide", **{"cairn.approval_id": str(approval_id)}):
        async with session() as sess:
            try:
                approval = await service.decide(
                    sess, approval_id=approval_id, actor=actor, approve=approve, reason=reason
                )
            except service.SelfApproval as exc:
                SELF_APPROVAL_BLOCKED.inc()
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except service.ApprovalError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            APPROVAL_LATENCY.observe((datetime.now(UTC) - approval.created_at).total_seconds())
            APPROVAL_DECISIONS.labels(outcome=approval.state.lower()).inc()
            payload = _public(approval)
            approved = approval.state == ApprovalState.APPROVED.value
            trajectory_id = approval.trajectory_id
            requester = approval.requested_by

        if approved:
            payload = await _execute(approval_id, requester=requester)

        # Either way the trajectory stops waiting: a denial is evidence, not
        # a dead end, and the agent should say so in its answer.
        async with session() as sess:
            await service.notify_resume(sess, trajectory_id)

    return payload


async def _execute(approval_id: uuid.UUID, *, requester: str) -> dict[str, Any]:
    """Hand the action to cairn-mcp-actions and record what happened.

    The approval service never performs a side effect itself. It holds the
    decision; the tool server holds the credentials for the system being
    changed. Splitting those means a compromise of either one alone cannot
    produce an unapproved production change.
    """
    async with session() as sess:
        approval = await service.claim_for_execution(sess, approval_id)
        if approval is None:
            return {"id": str(approval_id), "state": "already_executed"}
        action, args = approval.action, dict(approval.args)
        trajectory_id = approval.trajectory_id

    cfg = settings()
    token = mint_internal(
        UserContext(sub="cairn-approval", email="", groups=["platform-admin"]),
        trajectory_id or approval_id,
        cfg.auth,
    )
    body = {
        "approval_id": str(approval_id),
        "action": action,
        "args": args,
        "requested_by": requester,
    }

    ok: bool = True
    result: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                cfg.mcp.actions_url.replace("/mcp", "") + "/internal/execute",
                json=body,
                headers={"authorization": f"Bearer {token}"},
            )
            result = resp.json() if resp.content else {}
            ok = resp.status_code < 400 and result.get("ok", True)
    except Exception as exc:
        ok, result = False, {"error": str(exc)}
        log.error("action_execution_failed", action=action, error=str(exc))

    async with session() as sess:
        await service.mark_executed(sess, approval_id=approval_id, result=result, ok=ok)
        approval = await sess.get(Approval, approval_id)
        return _public(approval) if approval else {"id": str(approval_id), "state": "unknown"}


async def _sweep_expired() -> None:
    while True:
        try:
            async with session() as sess:
                expired = await service.expire_due(sess)
                for approval in expired:
                    APPROVAL_EXPIRED.inc()
                    await service.notify_resume(sess, approval.trajectory_id)
                    if approval.slack_channel and approval.slack_ts:
                        await slack.update(
                            approval.slack_channel,
                            approval.slack_ts,
                            resolved_blocks(approval.action, "EXPIRED", "nobody in time"),
                            text=f"{approval.action} expired",
                        )
        except Exception as exc:  # keep the sweeper alive across DB blips
            log.warning("expiry_sweep_failed", error=str(exc))
        await asyncio.sleep(30)


def _slack_payload(body: bytes) -> dict[str, Any]:
    from urllib.parse import parse_qs

    parsed = parse_qs(body.decode("utf-8", "replace"))
    raw = parsed.get("payload", ["{}"])[0]
    try:
        payload: dict[str, Any] = json.loads(raw)
        return payload
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="malformed interaction") from exc


def _consume_nonce(nonce: str, ttl_s: int) -> bool:
    import time

    now = time.monotonic()
    for key, seen_at in list(_seen_nonces.items()):
        if now - seen_at > ttl_s:
            del _seen_nonces[key]
    if nonce in _seen_nonces:
        return False
    _seen_nonces[nonce] = now
    return True


def _public(approval: Approval) -> dict[str, Any]:
    return {
        "id": str(approval.id),
        "action": approval.action,
        "args": approval.args,
        "state": approval.state,
        "requested_by": approval.requested_by,
        "required_approvals": approval.required_approvals,
        "approvals": approval.approvals,
        "trajectory_id": str(approval.trajectory_id) if approval.trajectory_id else None,
        "expires_at": approval.expires_at.isoformat(),
        "result": approval.result,
        "denial_reason": approval.denial_reason,
    }
