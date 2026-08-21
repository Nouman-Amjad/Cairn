"""Persistence operations shared by the orchestrator, gateway and approval
service.

Two rules govern everything here:

1. A step is committed before the next one starts. That is what makes a
   trajectory replayable and what lets a pod die mid-loop without losing the
   investigation.
2. State transitions are checked against the state machine and applied with a
   conditional UPDATE, so two pods racing to advance the same trajectory
   cannot both win.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningUpdate  # noqa: F401 - typing only

from cairn_core.domain import (
    InvalidTransition,
    StepKind,
    TrajectoryState,
    check_transition,
)
from cairn_core.models import AuditEvent, Trajectory, TrajectoryStep
from cairn_core.sensitivity import Sensitivity, escalate


async def create_trajectory(
    sess: AsyncSession,
    *,
    user_id: str,
    query: str,
    prompt_version: str,
    trajectory_id: uuid.UUID | None = None,
) -> Trajectory:
    traj = Trajectory(
        id=trajectory_id or uuid.uuid4(),
        user_id=user_id,
        query=query,
        state=TrajectoryState.PLANNING,
        prompt_version=prompt_version,
    )
    sess.add(traj)
    await sess.flush()
    return traj


async def get_trajectory(sess: AsyncSession, trajectory_id: uuid.UUID) -> Trajectory | None:
    return await sess.get(Trajectory, trajectory_id)


async def next_seq(sess: AsyncSession, trajectory_id: uuid.UUID) -> int:
    result = await sess.execute(
        select(func.coalesce(func.max(TrajectoryStep.seq), -1) + 1).where(
            TrajectoryStep.trajectory_id == trajectory_id
        )
    )
    return int(result.scalar_one())


async def append_step(
    sess: AsyncSession,
    trajectory_id: uuid.UUID,
    *,
    kind: StepKind,
    tool_name: str | None = None,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    artifact_id: str | None = None,
    sensitivity: Sensitivity = Sensitivity.PUBLIC,
    model: str | None = None,
    route: str | None = None,
    route_reason: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
    error: dict[str, Any] | None = None,
) -> TrajectoryStep:
    step = TrajectoryStep(
        trajectory_id=trajectory_id,
        seq=await next_seq(sess, trajectory_id),
        kind=kind.value,
        tool_name=tool_name,
        input=input,
        output=output,
        artifact_id=artifact_id,
        sensitivity=str(sensitivity),
        model=model,
        route=route,
        route_reason=route_reason,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        error=error,
    )
    sess.add(step)
    await sess.flush()
    return step


async def accrue_cost(
    sess: AsyncSession,
    trajectory_id: uuid.UUID,
    *,
    tokens: int,
    cost_usd: Decimal,
    local: bool,
) -> None:
    """Cost is accumulated on the trajectory row, not derived from steps at
    read time. The router's per-query cap has to be checkable in one read."""
    column = Trajectory.tokens_local if local else Trajectory.tokens_cloud
    await sess.execute(
        update(Trajectory)
        .where(Trajectory.id == trajectory_id)
        .values(
            {
                column: column + tokens,
                Trajectory.cost_usd: Trajectory.cost_usd + cost_usd,
            }
        )
    )


async def escalate_sensitivity(
    sess: AsyncSession, trajectory_id: uuid.UUID, level: Sensitivity
) -> None:
    """Monotonic: the conditional WHERE means a concurrent public result can
    never write the level back down."""
    if level is Sensitivity.PUBLIC:
        return
    order = {"public": 0, "internal": 1, "restricted": 2}
    current = await sess.scalar(
        select(Trajectory.sensitivity).where(Trajectory.id == trajectory_id)
    )
    if current is not None and order[str(level)] > order[current]:
        await sess.execute(
            update(Trajectory)
            .where(Trajectory.id == trajectory_id)
            .values(sensitivity=str(escalate(Sensitivity.parse(current), level)))
        )


async def transition(
    sess: AsyncSession,
    trajectory_id: uuid.UUID,
    *,
    frm: TrajectoryState,
    to: TrajectoryState,
    **fields: Any,
) -> bool:
    """Conditional state change. Returns False if another worker got there
    first, which is a normal race, not an error."""
    check_transition(frm, to)
    values: dict[str, Any] = {"state": to.value, **fields}
    if to.is_terminal:
        values["ended_at"] = datetime.now(UTC)
        values["lease_owner"] = None
        values["lease_expires_at"] = None
    result = cast(
        CursorResult[Any],
        await sess.execute(
            update(Trajectory)
            .where(Trajectory.id == trajectory_id, Trajectory.state == frm.value)
            .values(**values)
        ),
    )
    return bool(result.rowcount)


async def force_state(
    sess: AsyncSession, trajectory_id: uuid.UUID, to: TrajectoryState, **fields: Any
) -> None:
    """Bypass the transition table. Only the crash-recovery sweeper uses this,
    and only to move an abandoned lease to FAILED."""
    values: dict[str, Any] = {"state": to.value, **fields}
    if to.is_terminal:
        values["ended_at"] = datetime.now(UTC)
    await sess.execute(update(Trajectory).where(Trajectory.id == trajectory_id).values(**values))


async def claim_lease(
    sess: AsyncSession, trajectory_id: uuid.UUID, owner: str, ttl_s: int = 300
) -> bool:
    """Take ownership of a trajectory. Only one pod may drive a loop at a
    time; the lease expiring is how a crashed pod hands work back."""
    now = datetime.now(UTC)
    result = cast(
        CursorResult[Any],
        await sess.execute(
            update(Trajectory)
            .where(
                Trajectory.id == trajectory_id,
                (Trajectory.lease_owner.is_(None)) | (Trajectory.lease_expires_at < now),
            )
            .values(lease_owner=owner, lease_expires_at=now + timedelta(seconds=ttl_s))
        ),
    )
    return bool(result.rowcount)


async def renew_lease(
    sess: AsyncSession, trajectory_id: uuid.UUID, owner: str, ttl_s: int = 300
) -> bool:
    result = cast(
        CursorResult[Any],
        await sess.execute(
            update(Trajectory)
            .where(Trajectory.id == trajectory_id, Trajectory.lease_owner == owner)
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=ttl_s))
        ),
    )
    return bool(result.rowcount)


async def resumable(sess: AsyncSession, limit: int = 20) -> list[Trajectory]:
    """Rows the sweeper should pick up: live states with a dead lease.

    This is the query the partial index on `state` exists for.
    """
    now = datetime.now(UTC)
    result = await sess.execute(
        select(Trajectory)
        .where(
            Trajectory.state.in_(
                [TrajectoryState.EXECUTING.value, TrajectoryState.WAITING_APPROVAL.value]
            ),
            (Trajectory.lease_owner.is_(None)) | (Trajectory.lease_expires_at < now),
        )
        .order_by(Trajectory.started_at)
        .limit(limit)
    )
    return list(result.scalars())


async def steps_for(sess: AsyncSession, trajectory_id: uuid.UUID) -> list[TrajectoryStep]:
    result = await sess.execute(
        select(TrajectoryStep)
        .where(TrajectoryStep.trajectory_id == trajectory_id)
        .order_by(TrajectoryStep.seq)
    )
    return list(result.scalars())


async def audit(
    sess: AsyncSession,
    *,
    actor: str,
    event: str,
    subject: str | None = None,
    trajectory_id: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
    **detail: Any,
) -> None:
    sess.add(
        AuditEvent(
            actor=actor,
            event=event,
            subject=subject,
            trajectory_id=trajectory_id,
            approval_id=approval_id,
            detail=detail,
        )
    )


__all__ = [
    "InvalidTransition",
    "accrue_cost",
    "append_step",
    "audit",
    "claim_lease",
    "create_trajectory",
    "escalate_sensitivity",
    "force_state",
    "get_trajectory",
    "next_seq",
    "renew_lease",
    "resumable",
    "steps_for",
    "transition",
]
