"""The safety properties of the approval gate, against a real database.

These are the tests that would have to be deleted for Cairn to produce an
unapproved production change, which is the point of writing them this way.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text, update

from cairn_approval import service
from cairn_core.db import session
from cairn_core.domain import ApprovalState
from cairn_core.models import Approval, AuditEvent

pytestmark = [pytest.mark.anyio, pytest.mark.requires_db]

ARGS = {"service": "checkout-api", "from_sha": "abc123", "to_sha": "def456"}


async def make(
    *, action="rollback_deploy", requester="alice", required=1, ttl=900, args=None, key=None
):
    args = args if args is not None else ARGS
    async with session() as sess:
        approval, created = await service.create(
            sess,
            action=action,
            args=args,
            key=key or service.idempotency_key(action, args),
            requester=requester,
            requester_email=f"{requester}@example.com",
            trajectory_id=None,
            required_approvals=required,
            ttl_s=ttl,
        )
        return approval.id, created


async def state_of(approval_id) -> str:
    async with session() as sess:
        approval = await sess.get(Approval, approval_id)
        return approval.state


async def test_a_request_starts_pending(db):
    approval_id, created = await make()
    assert created
    assert await state_of(approval_id) == ApprovalState.PENDING


async def test_identical_requests_collapse_to_one(db):
    first, created_first = await make()
    second, created_second = await make()
    assert created_first and not created_second
    assert first == second, "the same action must not queue twice"

    async with session() as sess:
        count = await sess.scalar(select(func.count()).select_from(Approval))
    assert count == 1


async def test_different_arguments_are_different_requests(db):
    first, _ = await make()
    other_args = dict(ARGS, to_sha="99999")
    second, created = await make(
        args=other_args, key=service.idempotency_key("rollback_deploy", other_args)
    )
    assert created and first != second


async def test_requester_cannot_approve_their_own_action(db):
    approval_id, _ = await make(requester="alice")
    async with session() as sess:
        with pytest.raises(service.SelfApproval):
            await service.decide(sess, approval_id=approval_id, actor="alice", approve=True)

    assert await state_of(approval_id) == ApprovalState.PENDING

    async with session() as sess:
        blocked = await sess.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event == "approval.self_approval_blocked")
        )
    assert blocked == 1, "a blocked self-approval must be auditable"


async def test_someone_else_can_approve(db):
    approval_id, _ = await make(requester="alice")
    async with session() as sess:
        approval = await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)
        assert approval.state == ApprovalState.APPROVED
        assert approval.decided_by == "bob"


async def test_two_approver_action_needs_two_distinct_humans(db):
    approval_id, _ = await make(requester="alice", required=2)

    async with session() as sess:
        approval = await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)
    assert approval.state == ApprovalState.PENDING, "one approval is not two"

    # the same person clicking twice must not satisfy the second approval
    async with session() as sess:
        approval = await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)
    assert approval.state == ApprovalState.PENDING
    assert len(approval.approvals) == 1

    async with session() as sess:
        approval = await service.decide(sess, approval_id=approval_id, actor="carol", approve=True)
    assert approval.state == ApprovalState.APPROVED


async def test_denial_is_terminal_and_recorded(db):
    approval_id, _ = await make()
    async with session() as sess:
        approval = await service.decide(
            sess, approval_id=approval_id, actor="bob", approve=False, reason="wrong sha"
        )
    assert approval.state == ApprovalState.DENIED
    assert approval.denial_reason == "wrong sha"

    # a later approval attempt cannot resurrect it
    async with session() as sess:
        approval = await service.decide(sess, approval_id=approval_id, actor="carol", approve=True)
    assert approval.state == ApprovalState.DENIED


async def test_expired_request_cannot_be_approved(db):
    approval_id, _ = await make(ttl=60)
    async with session() as sess:
        await sess.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(expires_at=text("now() - interval '1 minute'"))
        )

    async with session() as sess:
        approval = await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)
    assert approval.state == ApprovalState.EXPIRED


async def test_expiry_sweep_marks_and_reports(db):
    approval_id, _ = await make(ttl=60)
    async with session() as sess:
        await sess.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(expires_at=text("now() - interval '1 minute'"))
        )
    async with session() as sess:
        expired = await service.expire_due(sess)
    assert [a.id for a in expired] == [approval_id]
    assert await state_of(approval_id) == ApprovalState.EXPIRED


async def test_execution_happens_exactly_once(db):
    approval_id, _ = await make()
    async with session() as sess:
        await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)

    async with session() as sess:
        first = await service.mark_executed(
            sess, approval_id=approval_id, result={"issue": "OPS-1"}, ok=True
        )
    async with session() as sess:
        second = await service.mark_executed(
            sess, approval_id=approval_id, result={"issue": "OPS-2"}, ok=True
        )

    assert first is True and second is False, "an action must execute once"
    async with session() as sess:
        approval = await sess.get(Approval, approval_id)
    assert approval.state == ApprovalState.EXECUTED
    assert approval.result == {"issue": "OPS-1"}


async def test_execution_is_unreachable_without_approval(db):
    approval_id, _ = await make()
    async with session() as sess:
        claimed = await service.claim_for_execution(sess, approval_id)
    assert claimed is None, "a PENDING approval must not be executable"

    async with session() as sess:
        executed = await service.mark_executed(sess, approval_id=approval_id, result={}, ok=True)
    assert executed is False
    assert await state_of(approval_id) == ApprovalState.PENDING


async def test_concurrent_approvals_produce_one_execution(db):
    """Two pods, one row, one side effect."""
    approval_id, _ = await make()
    async with session() as sess:
        await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)

    async def attempt(tag: str) -> bool:
        async with session() as sess:
            return await service.mark_executed(
                sess, approval_id=approval_id, result={"by": tag}, ok=True
            )

    results = await asyncio.gather(attempt("pod-a"), attempt("pod-b"))
    assert sum(results) == 1, results


async def test_audit_log_cannot_be_rewritten(db):
    approval_id, _ = await make()
    async with session() as sess:
        with pytest.raises(Exception, match="append-only"):
            await sess.execute(
                text("UPDATE audit_events SET actor = 'nobody' WHERE approval_id = :a"),
                {"a": str(approval_id)},
            )

    async with session() as sess:
        with pytest.raises(Exception, match="append-only"):
            await sess.execute(text("DELETE FROM audit_events"))


async def test_every_decision_leaves_an_audit_trail(db):
    approval_id, _ = await make(requester="alice")
    async with session() as sess:
        await service.decide(sess, approval_id=approval_id, actor="bob", approve=True)

    async with session() as sess:
        events = list(
            (
                await sess.execute(
                    select(AuditEvent.event).where(AuditEvent.approval_id == approval_id)
                )
            ).scalars()
        )
    assert "approval.requested" in events
    assert "approval.granted" in events


async def test_pending_queue_hides_your_own_requests(db):
    await make(requester="alice", args={"service": "a"}, key="k1")
    await make(requester="bob", args={"service": "b"}, key="k2")

    async with session() as sess:
        for_alice = await service.pending_for_user(sess, exclude_requester="alice")
    assert [a.requested_by for a in for_alice] == ["bob"]


def test_idempotency_key_is_stable_and_argument_sensitive():
    key = service.idempotency_key("rollback_deploy", ARGS)
    assert key == service.idempotency_key("rollback_deploy", dict(reversed(list(ARGS.items()))))
    assert key != service.idempotency_key("rollback_deploy", dict(ARGS, to_sha="x"))
    assert key != service.idempotency_key("scale_deployment", ARGS)
    assert key != service.idempotency_key("rollback_deploy", ARGS, salt=str(uuid.uuid4()))
