"""Resuming suspended and orphaned trajectories.

Two mechanisms, on purpose:

* **LISTEN/NOTIFY** — the fast path. The approval service notifies on
  `cairn_resume` the moment a human decides, and the waiting investigation
  continues in milliseconds.
* **The sweeper** — the correct path. Every 30 seconds it looks for live
  trajectories whose lease has expired, which covers the notification being
  lost, the pod holding the loop being evicted, and the node disappearing.

The notification is an optimisation. The sweeper is the guarantee. Building
only the first is how you get an agent that works in the demo and hangs in
production.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import Counter
from sqlalchemy import text

from cairn_core.db import engine, session
from cairn_core.domain import TrajectoryState
from cairn_core.repo import claim_lease, force_state, resumable
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

RESUMED = Counter("cairn_trajectories_resumed_total", "Resumed trajectories", ["trigger"])
ORPHANED = Counter("cairn_trajectories_orphaned_total", "Trajectories found with a dead lease")

POD = os.environ.get("HOSTNAME", "local")
LEASE_TTL_S = 300
SWEEP_INTERVAL_S = 30

Resumer = Callable[[uuid.UUID], Awaitable[None]]

_background: set[asyncio.Task[None]] = set()


async def _resume_once(resume: Resumer, trajectory_id: uuid.UUID) -> None:
    await resume(trajectory_id)


async def listen_for_resumes(resume: Resumer, stop: asyncio.Event) -> None:
    """LISTEN on cairn_resume, using a raw asyncpg connection.

    Deliberately outside the SQLAlchemy pool: a LISTEN connection is held open
    for the life of the pod, and parking a pooled connection there starves
    everything else.
    """
    while not stop.is_set():
        raw = None
        try:
            raw = await engine().raw_connection()
            conn: Any = raw.driver_connection
            if conn is None:  # pragma: no cover - driver contract
                raise RuntimeError("no asyncpg connection behind this pool")

            async def on_notify(_c: Any, _pid: int, _channel: str, payload: str) -> None:
                try:
                    trajectory_id = uuid.UUID(payload)
                except ValueError:
                    return
                RESUMED.labels(trigger="notify").inc()
                log.info("resume_notified", trajectory_id=payload)
                # Strong reference: without one, the garbage collector can
                # drop the task mid-await and the resume silently stops.
                task = asyncio.create_task(_resume_once(resume, trajectory_id))
                _background.add(task)
                task.add_done_callback(_background.discard)

            await conn.add_listener("cairn_resume", on_notify)
            log.info("listening_for_resumes")
            await stop.wait()
            await conn.remove_listener("cairn_resume", on_notify)
        except Exception as exc:
            log.warning("resume_listener_failed", error=str(exc))
            await asyncio.sleep(5)
        finally:
            if raw is not None:
                with contextlib.suppress(Exception):
                    raw.close()


async def sweep(resume: Resumer, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with session() as sess:
                candidates = await resumable(sess)

            for trajectory in candidates:
                async with session() as sess:
                    if not await claim_lease(sess, trajectory.id, POD, LEASE_TTL_S):
                        continue  # another pod got there first

                ORPHANED.inc()
                if trajectory.state == TrajectoryState.WAITING_APPROVAL.value:
                    # Still legitimately waiting on a human. Reclaiming the
                    # lease is enough; do not restart the loop.
                    log.info("lease_reclaimed", trajectory_id=str(trajectory.id))
                    continue

                RESUMED.labels(trigger="sweeper").inc()
                log.info("resuming_orphan", trajectory_id=str(trajectory.id))
                await resume(trajectory.id)
        except Exception as exc:
            log.warning("resume_sweep_failed", error=str(exc))

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_S)


async def abandon_expired(stop: asyncio.Event) -> None:
    """Trajectories whose approval expired have nothing left to wait for."""
    while not stop.is_set():
        try:
            async with session() as sess:
                rows = await sess.execute(
                    _EXPIRED_SQL, {"state": TrajectoryState.WAITING_APPROVAL.value}
                )
                for (trajectory_id,) in rows:
                    log.info("abandoning_trajectory", trajectory_id=str(trajectory_id))
                    await force_state(
                        sess,
                        trajectory_id,
                        TrajectoryState.ABANDONED,
                        answer="The action this investigation was waiting on was "
                        "not approved in time.",
                    )
        except Exception as exc:
            log.warning("abandon_sweep_failed", error=str(exc))

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_S * 2)


#: Only EXPIRED counts as abandonment. A DENIED action is evidence — the
#: agent resumes, records the refusal, and says so in its answer — so denial
#: goes down the normal resume path, not this one.
_EXPIRED_SQL = text(
    """
    SELECT t.id
    FROM trajectories t
    JOIN approvals a ON a.id = t.waiting_approval_id
    WHERE t.state = :state
      AND a.state = 'EXPIRED'
      AND a.expires_at < now() - interval '1 minute'
    LIMIT 50
    """
)
