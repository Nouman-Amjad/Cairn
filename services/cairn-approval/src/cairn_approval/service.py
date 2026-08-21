"""The approval state machine.

This is the reason Cairn is deployable inside a real company, so it is worth
being explicit about the properties it guarantees:

* **No unapproved write.** Execution is reachable only from `APPROVED`, and
  only through `execute()`.
* **No double execution.** A unique index on the idempotency key, plus a
  conditional state transition, means two clicks or two pods produce one
  side effect.
* **No self-approval.** `approver != requester` is enforced here, in the
  service, not in the UI. A UI check is a suggestion.
* **Nothing waits forever.** Every request has a TTL, and expiry is a state,
  not a timeout that silently drops the work.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cairn_core.domain import ApprovalState
from cairn_core.models import Approval
from cairn_core.repo import audit
from cairn_core.telemetry import get_logger

log = get_logger(__name__)


class ApprovalError(RuntimeError):
    pass


class SelfApproval(ApprovalError):
    pass


def idempotency_key(action: str, args: dict[str, Any], *, salt: str = "") -> str:
    """Deterministic over the action and its semantically meaningful args.

    Which args count is the tool's decision (see `mcp-actions`), because
    "the same action" means different things for `create_ticket` (same
    trajectory + title) and `rollback_deploy` (same service + sha pair).
    """
    material = json.dumps({"action": action, "args": args, "salt": salt}, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


async def create(
    sess: AsyncSession,
    *,
    action: str,
    args: dict[str, Any],
    key: str,
    requester: str,
    requester_email: str | None,
    trajectory_id: uuid.UUID | None,
    required_approvals: int = 1,
    ttl_s: int = 900,
) -> tuple[Approval, bool]:
    """Create or return the existing request. Returns (approval, created)."""
    now = datetime.now(UTC)
    stmt = (
        insert(Approval)
        .values(
            id=uuid.uuid4(),
            trajectory_id=trajectory_id,
            action=action,
            args=args,
            idempotency_key=key,
            state=ApprovalState.PENDING.value,
            requested_by=requester,
            requested_by_email=requester_email,
            required_approvals=required_approvals,
            approvals=[],
            expires_at=now + timedelta(seconds=ttl_s),
        )
        .on_conflict_do_nothing(index_elements=[Approval.idempotency_key])
        .returning(Approval.id)
    )
    created_id = await sess.scalar(stmt)

    if created_id is None:
        existing = await sess.scalar(select(Approval).where(Approval.idempotency_key == key))
        if existing is None:  # pragma: no cover - only under a concurrent delete
            raise ApprovalError("approval vanished during creation")
        return existing, False

    approval = await sess.get(Approval, created_id)
    assert approval is not None
    await audit(
        sess,
        actor=requester,
        event="approval.requested",
        subject=action,
        trajectory_id=trajectory_id,
        approval_id=approval.id,
        args=args,
        required_approvals=required_approvals,
    )
    return approval, True


async def decide(
    sess: AsyncSession,
    *,
    approval_id: uuid.UUID,
    actor: str,
    approve: bool,
    reason: str | None = None,
) -> Approval:
    approval = await sess.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise ApprovalError("no such approval")

    if approval.state != ApprovalState.PENDING.value:
        # Already decided. Idempotent by design: a double-click on a Slack
        # button is normal, not an error worth surfacing to a human.
        return approval

    if approval.expires_at <= datetime.now(UTC):
        approval.state = ApprovalState.EXPIRED.value
        await audit(
            sess,
            actor="system",
            event="approval.expired",
            subject=approval.action,
            approval_id=approval.id,
            trajectory_id=approval.trajectory_id,
        )
        return approval

    if actor == approval.requested_by:
        # The one check that cannot live in the UI.
        await audit(
            sess,
            actor=actor,
            event="approval.self_approval_blocked",
            subject=approval.action,
            approval_id=approval.id,
            trajectory_id=approval.trajectory_id,
        )
        raise SelfApproval("the requester cannot approve their own action")

    if not approve:
        approval.state = ApprovalState.DENIED.value
        approval.decided_by = actor
        approval.decided_at = datetime.now(UTC)
        approval.denial_reason = reason
        await audit(
            sess,
            actor=actor,
            event="approval.denied",
            subject=approval.action,
            approval_id=approval.id,
            trajectory_id=approval.trajectory_id,
            reason=reason,
        )
        return approval

    granted = list(approval.approvals or [])
    if any(g["actor"] == actor for g in granted):
        return approval  # one human, one vote
    granted.append({"actor": actor, "at": datetime.now(UTC).isoformat()})
    approval.approvals = granted

    if len(granted) >= approval.required_approvals:
        approval.state = ApprovalState.APPROVED.value
        approval.decided_by = actor
        approval.decided_at = datetime.now(UTC)

    await audit(
        sess,
        actor=actor,
        event="approval.granted",
        subject=approval.action,
        approval_id=approval.id,
        trajectory_id=approval.trajectory_id,
        have=len(granted),
        need=approval.required_approvals,
    )
    return approval


async def mark_executed(
    sess: AsyncSession,
    *,
    approval_id: uuid.UUID,
    result: dict[str, Any],
    ok: bool,
) -> bool:
    """Move APPROVED -> EXECUTED (or FAILED), exactly once.

    The conditional WHERE is the second half of the no-double-execution
    guarantee: two racing executors, one row update, one winner.
    """
    outcome = ApprovalState.EXECUTED if ok else ApprovalState.FAILED
    changed = cast(
        CursorResult[Any],
        await sess.execute(
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.state == ApprovalState.APPROVED.value,
            )
            .values(state=outcome.value, result=result)
        ),
    )
    if not changed.rowcount:
        return False

    approval = await sess.get(Approval, approval_id)
    await audit(
        sess,
        actor="cairn-approval",
        event=f"action.{'executed' if ok else 'failed'}",
        subject=approval.action if approval else None,
        approval_id=approval_id,
        trajectory_id=approval.trajectory_id if approval else None,
        result=result,
    )
    return True


async def claim_for_execution(sess: AsyncSession, approval_id: uuid.UUID) -> Approval | None:
    """Take an APPROVED row with a row lock so only one worker executes it."""
    approval = await sess.get(Approval, approval_id, with_for_update=True)
    if approval is None or approval.state != ApprovalState.APPROVED.value:
        return None
    return approval


async def expire_due(sess: AsyncSession, limit: int = 100) -> list[Approval]:
    """Sweep expired requests. Returns the rows that changed so their
    trajectories can be told to stop waiting."""
    rows = list(
        (
            await sess.execute(
                select(Approval)
                .where(
                    Approval.state == ApprovalState.PENDING.value,
                    Approval.expires_at <= datetime.now(UTC),
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for approval in rows:
        approval.state = ApprovalState.EXPIRED.value
        await audit(
            sess,
            actor="system",
            event="approval.expired",
            subject=approval.action,
            approval_id=approval.id,
            trajectory_id=approval.trajectory_id,
        )
    return rows


async def notify_resume(sess: AsyncSession, trajectory_id: uuid.UUID | None) -> None:
    """Wake the orchestrator pod holding this trajectory.

    LISTEN/NOTIFY rather than polling: the loop suspends with no timer, and a
    resume arrives in milliseconds. If the NOTIFY is lost (pod restart, for
    instance), the 30-second resume sweeper picks the trajectory up anyway —
    the notification is an optimisation, not the mechanism.
    """
    if trajectory_id is None:
        return
    await sess.execute(
        text("SELECT pg_notify('cairn_resume', :payload)"),
        {"payload": str(trajectory_id)},
    )


async def pending_for_user(
    sess: AsyncSession, *, exclude_requester: str | None = None, limit: int = 50
) -> list[Approval]:
    query = select(Approval).where(
        Approval.state == ApprovalState.PENDING.value,
        Approval.expires_at > datetime.now(UTC),
    )
    if exclude_requester:
        query = query.where(Approval.requested_by != exclude_requester)
    return list((await sess.execute(query.order_by(Approval.created_at).limit(limit))).scalars())
