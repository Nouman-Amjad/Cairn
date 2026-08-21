"""The agent loop's behaviour, against a real database and fake models."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fakes import FakeBus, FakeRouter, FakeToolBox, logs_result, pending_result

from cairn_core.db import session
from cairn_core.domain import Budget, StepKind, TaskClass, ToolResult, TrajectoryState
from cairn_core.repo import create_trajectory, get_trajectory, steps_for
from cairn_orchestrator.loop import AgentLoop

pytestmark = [pytest.mark.anyio, pytest.mark.requires_db]

QUERY = "why did checkout latency spike at 3am?"

PLAN = {
    "steps": [
        {"goal": "check deploys", "tools": ["get_deploy_timeline"], "why": "cheapest signal"},
        {"goal": "check logs", "tools": ["query_logs"], "why": "confirm blast radius"},
    ],
    "hypotheses": ["a deploy at 03:02 caused it"],
}
ANSWER = {
    "root_cause": "The 03:02 deploy of checkout-api exhausted the connection pool.",
    "confidence": 0.82,
    "evidence": [{"step": 1, "fact": "503s began 4 minutes after the deploy"}],
    "unknowns": [],
    "recommended_actions": ["roll back to abc123"],
}
ACCEPT = {"verdict": "accept", "reasons": []}
REJECT = {
    "verdict": "reject",
    "reasons": ["correlation presented as causation"],
    "missing_evidence": ["pool metrics"],
}


async def new_trajectory() -> uuid.UUID:
    trajectory_id = uuid.uuid4()
    async with session() as sess:
        await create_trajectory(
            sess,
            user_id="alice",
            query=QUERY,
            prompt_version="test",
            trajectory_id=trajectory_id,
        )
    return trajectory_id


def build(trajectory_id, script, tools=None, budget=None) -> tuple[AgentLoop, FakeRouter, FakeBus]:
    router, bus = FakeRouter(script), FakeBus()
    loop = AgentLoop(
        trajectory_id=trajectory_id,
        query=QUERY,
        token="t",
        budget=budget or Budget(),
        bus=bus,  # type: ignore[arg-type]
        router=router,  # type: ignore[arg-type]
        tools=tools or FakeToolBox({"query_logs": logs_result()}),  # type: ignore[arg-type]
    )
    return loop, router, bus


async def test_happy_path_reaches_complete(db):
    trajectory_id = await new_trajectory()
    loop, router, bus = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {"service": "checkout-api"}},
                {"done": True},
            ],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
    )
    result = await loop.run()

    assert result.state is TrajectoryState.COMPLETE
    assert "connection pool" in result.answer
    assert result.confidence == pytest.approx(0.82)

    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)
        steps = await steps_for(sess, trajectory_id)

    assert trajectory.state == TrajectoryState.COMPLETE
    assert trajectory.plan == PLAN
    assert trajectory.ended_at is not None
    kinds = [s.kind for s in steps]
    assert StepKind.PLAN.value in kinds
    assert StepKind.TOOL_CALL.value in kinds
    assert StepKind.TOOL_RESULT.value in kinds
    assert StepKind.CRITIQUE.value in kinds
    # every step is committed with a monotonic sequence, which is what makes
    # the trajectory replayable
    assert [s.seq for s in steps] == sorted(s.seq for s in steps)


async def test_cost_and_tokens_are_attributed(db):
    trajectory_id = await new_trajectory()
    loop, router, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [{"done": True}],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
    )
    await loop.run()

    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)

    # two cloud calls (plan + synthesis) at $0.01 each
    assert Decimal(trajectory.cost_usd) == Decimal("0.02")
    assert trajectory.tokens_local > 0 and trajectory.tokens_cloud > 0


async def test_planning_and_synthesis_are_the_only_cloud_tasks(db):
    trajectory_id = await new_trajectory()
    loop, router, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"done": True},
            ],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
    )
    await loop.run()

    tasks = router.tasks()
    assert TaskClass.PLANNING in tasks and TaskClass.SYNTHESIS in tasks
    assert TaskClass.CRITIQUE in tasks
    # the loop asks for structured output on every decision it parses
    for call in router.calls:
        if call["task"] in (TaskClass.PLANNING, TaskClass.SYNTHESIS, TaskClass.CRITIQUE):
            assert call["schema"] is not None


async def test_restricted_tool_output_escalates_the_trajectory(db):
    trajectory_id = await new_trajectory()
    tools = FakeToolBox({"query_logs": logs_result(sensitivity="restricted")})
    loop, router, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"done": True},
            ],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
        tools=tools,
    )
    await loop.run()

    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)
    assert trajectory.sensitivity == "restricted"

    # and every model call after that point carries the raised level, so the
    # router can refuse to send it to a vendor
    after = [c for c in router.calls if c["task"] in (TaskClass.SYNTHESIS, TaskClass.CRITIQUE)]
    assert after and all(c["sensitivity"] == "restricted" for c in after)


async def test_pending_approval_suspends_rather_than_failing(db):
    trajectory_id = await new_trajectory()
    tools = FakeToolBox({"rollback_deploy": pending_result()})
    loop, _, bus = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [{"tool": "rollback_deploy", "args": {}}],
        },
        tools=tools,
    )
    result = await loop.run()

    assert result.state is TrajectoryState.WAITING_APPROVAL
    async with session() as sess:
        trajectory = await get_trajectory(sess, trajectory_id)
    assert trajectory.state == TrajectoryState.WAITING_APPROVAL
    assert (
        str(trajectory.waiting_approval_id)
        == "b" * 8 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 12
    )
    assert "approval" in bus.types()
    # the tool was called exactly once: a pending approval must not be retried
    assert [name for name, _ in tools.calls] == ["rollback_deploy"]


async def test_a_suspended_trajectory_resumes_with_its_evidence(db):
    trajectory_id = await new_trajectory()
    tools = FakeToolBox({"query_logs": logs_result(), "rollback_deploy": pending_result()})
    first, _, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"tool": "rollback_deploy", "args": {}},
            ],
        },
        tools=tools,
    )
    assert (await first.run()).state is TrajectoryState.WAITING_APPROVAL

    # a different pod picks it up after the human approves
    second, router, _ = build(
        trajectory_id,
        {
            TaskClass.ARG_EXTRACTION: [{"done": True}],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
        tools=tools,
    )
    result = await second.run(resuming=True)

    assert result.state is TrajectoryState.COMPLETE
    # the resumed loop rebuilt the earlier evidence from Postgres, not memory
    assert len(second.transcript.evidence) >= 2
    rendered = second.transcript.render_evidence()
    assert "1,247 matches" in rendered
    assert TaskClass.PLANNING not in router.tasks(), "resume must not re-plan"


async def test_budget_exhaustion_gives_a_partial_answer_not_a_failure(db):
    trajectory_id = await new_trajectory()
    budget = Budget(max_iterations=2)
    loop, _, bus = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"tool": "query_logs", "args": {}},
                {"tool": "query_logs", "args": {}},
            ],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
        budget=budget,
    )
    result = await loop.run()

    assert result.state is TrajectoryState.PARTIAL
    assert result.reason == "max_iterations"
    assert "Stopped early" in result.answer
    assert any("budget exhausted" in str(e.data) for e in bus.of("notice"))


async def test_cost_cap_stops_the_loop(db):
    trajectory_id = await new_trajectory()
    loop, router, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [{"tool": "query_logs", "args": {}}] * 10,
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
        budget=Budget(max_cost_usd=Decimal("0.05")),
    )
    # model the spill case: local work is being served by the cheap cloud
    # model, so every call costs money and the cap is reachable mid-loop
    router.cost_per_call = Decimal("0.02")
    router.charge_all = True
    result = await loop.run()

    assert result.state is TrajectoryState.PARTIAL
    assert result.reason == "max_cost_usd"
    assert result.cost_usd >= Decimal("0.05")


async def test_critic_rejection_lowers_confidence_and_says_so(db):
    trajectory_id = await new_trajectory()
    loop, _, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [{"done": True}],
            TaskClass.SYNTHESIS: [ANSWER, ANSWER],
            TaskClass.CRITIQUE: [REJECT, REJECT],
        },
    )
    result = await loop.run()

    assert result.state is TrajectoryState.PARTIAL, "twice-rejected must not read as COMPLETE"
    assert result.confidence <= 0.4
    assert "reviewer was not satisfied" in result.answer
    assert "pool metrics" in result.answer, "the critic's gap should reach the reader"


async def test_critic_rejection_then_acceptance_completes(db):
    trajectory_id = await new_trajectory()
    loop, router, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [{"done": True}],
            TaskClass.SYNTHESIS: [ANSWER, ANSWER],
            TaskClass.CRITIQUE: [REJECT, ACCEPT],
        },
    )
    result = await loop.run()
    assert result.state is TrajectoryState.COMPLETE
    assert router.tasks().count(TaskClass.SYNTHESIS) == 2, "one revision, not more"


async def test_unparseable_plan_fails_after_two_attempts(db):
    trajectory_id = await new_trajectory()
    loop, router, bus = build(
        trajectory_id,
        {TaskClass.PLANNING: ["I'm afraid I can't do that", "nor that"]},
    )
    result = await loop.run()

    assert result.state is TrajectoryState.FAILED
    assert router.tasks().count(TaskClass.PLANNING) == 2
    assert bus.of("error")


async def test_a_failing_tool_does_not_kill_the_investigation(db):
    trajectory_id = await new_trajectory()
    tools = FakeToolBox({})  # every call returns "no such tool"
    loop, _, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"done": True},
            ],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
        tools=tools,
    )
    result = await loop.run()
    assert result.state is TrajectoryState.COMPLETE

    async with session() as sess:
        steps = await steps_for(sess, trajectory_id)
    errors = [s for s in steps if s.error]
    assert errors, "the tool failure should be recorded as evidence"


async def test_oversized_tool_output_is_summarised_locally(db):
    trajectory_id = await new_trajectory()
    huge = ToolResult(
        tool="query_logs",
        ok=True,
        content={"rows": [{"line": "x" * 200} for _ in range(500)], "total_matched": 500},
        sensitivity="internal",
        tokens=40_000,
    )
    loop, router, _ = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"done": True},
            ],
            TaskClass.SUMMARIZATION: ["503s from one pod between 03:02 and 03:31"],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
        tools=FakeToolBox({"query_logs": huge}),
    )
    await loop.run()

    assert TaskClass.SUMMARIZATION in router.tasks()
    rendered = loop.transcript.render_evidence()
    assert "503s from one pod" in rendered
    assert "x" * 200 not in rendered, "raw payload must not reach the window"


async def test_events_narrate_the_investigation(db):
    trajectory_id = await new_trajectory()
    loop, _, bus = build(
        trajectory_id,
        {
            TaskClass.PLANNING: [PLAN],
            TaskClass.ARG_EXTRACTION: [
                {"tool": "query_logs", "args": {}},
                {"done": True},
            ],
            TaskClass.SYNTHESIS: [ANSWER],
            TaskClass.CRITIQUE: [ACCEPT],
        },
    )
    await loop.run()

    types = bus.types()
    assert types[0] == "state"
    assert "plan" in types and "step" in types and "answer" in types
    assert types[-1] == "done"
    assert [e.seq for e in bus.events] == list(range(1, len(bus.events) + 1))
