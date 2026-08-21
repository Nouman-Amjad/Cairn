"""The routing table's load-bearing properties, as tests.

If a change makes one of these fail, the change is wrong. Deleting a test
here is a decision about data residency, not about test maintenance.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cairn_core.domain import CHEAP_ONLY, Route, TaskClass
from cairn_core.sensitivity import Sensitivity
from cairn_router.configmaps import DEFAULT_POLICY, RoutingPolicy
from cairn_router.routing import CloudState, Limits, LocalState, Tier, decide

POLICY = RoutingPolicy.parse(DEFAULT_POLICY)
LIMITS = Limits()
CLOUD_OK = CloudState(configured=True)

LOCAL_STATES = {
    "healthy": LocalState(healthy=True, queue_depth=0),
    "busy": LocalState(healthy=True, queue_depth=39),
    "saturated": LocalState(healthy=True, queue_depth=200),
    "dead": LocalState(healthy=False, queue_depth=0),
    "cold": LocalState(healthy=False, queue_depth=0, cold_starting=True),
}


def route(
    task: TaskClass,
    sensitivity: Sensitivity = Sensitivity.PUBLIC,
    local: str = "healthy",
    cloud: CloudState = CLOUD_OK,
    spent: str = "0",
    deadline_ms: int = 120_000,
    tier: str | None = None,
):
    return decide(
        task=task,
        sensitivity=sensitivity,
        tier=tier or POLICY.tier_for(task),
        local=LOCAL_STATES[local],
        cloud=cloud,
        limits=LIMITS,
        spent_usd=Decimal(spent),
        max_cost_usd=Decimal("1.50"),
        deadline_ms=deadline_ms,
    )


@pytest.mark.parametrize("task", list(TaskClass))
@pytest.mark.parametrize("local", list(LOCAL_STATES))
@pytest.mark.parametrize("spent", ["0", "1.49"])
@pytest.mark.parametrize("deadline_ms", [500, 120_000])
def test_restricted_never_reaches_cloud(task, local, spent, deadline_ms):
    decision = route(
        task,
        Sensitivity.RESTRICTED,
        local=local,
        spent=spent,
        deadline_ms=deadline_ms,
    )
    assert not decision.is_cloud, decision
    assert decision.route in (Route.LOCAL, Route.QUEUE_LOCAL, Route.FAIL)


@pytest.mark.parametrize("task", list(TaskClass))
def test_every_task_is_routable_when_everything_is_healthy(task):
    assert route(task).route in (Route.LOCAL, Route.CLOUD_SONNET)


def test_planning_and_synthesis_go_to_the_strong_model():
    assert route(TaskClass.PLANNING).route is Route.CLOUD_SONNET
    assert route(TaskClass.SYNTHESIS).route is Route.CLOUD_SONNET


def test_high_volume_tasks_stay_local():
    for task in (TaskClass.SUMMARIZATION, TaskClass.COMPACTION, TaskClass.CRITIQUE):
        assert route(task).route is Route.LOCAL


def test_saturated_local_spills_only_cheap_tasks():
    for task in TaskClass:
        decision = route(task, local="saturated")
        if decision.route is Route.CLOUD_HAIKU:
            assert task in CHEAP_ONLY, f"{task} must not spill to the cheap model"


def test_log_summarization_never_spills_even_when_cheap():
    # It is in CHEAP_ONLY, but the policy pins it local_forced because raw log
    # lines are the PII risk regardless of how the classifier scored them.
    assert POLICY.tier_for(TaskClass.SUMMARIZATION) == Tier.LOCAL_FORCED
    assert route(TaskClass.SUMMARIZATION, local="saturated").route is Route.QUEUE_LOCAL
    assert route(TaskClass.SUMMARIZATION, local="dead").route is Route.QUEUE_LOCAL


def test_cost_backoff_moves_cloud_work_local():
    assert route(TaskClass.SYNTHESIS, spent="0.5").route is Route.CLOUD_SONNET
    assert route(TaskClass.SYNTHESIS, spent="1.21").route is Route.LOCAL


def test_cloud_errors_trip_to_local():
    sick = CloudState(configured=True, error_rate_5m=0.4)
    assert route(TaskClass.SYNTHESIS, cloud=sick).route is Route.LOCAL


def test_no_cloud_key_means_local_only():
    unset = CloudState(configured=False)
    for task in TaskClass:
        assert not route(task, cloud=unset).is_cloud


def test_cold_start_spill_is_labelled():
    decision = route(TaskClass.COMPACTION, local="cold")
    assert decision.route is Route.CLOUD_HAIKU
    assert "cold_start_spill" in decision.reason


def test_restricted_with_no_time_fails_honestly():
    decision = route(TaskClass.SYNTHESIS, Sensitivity.RESTRICTED, local="dead", deadline_ms=1_000)
    assert decision.route is Route.FAIL
    assert "restricted" in decision.reason


def test_queue_boundary_is_exclusive():
    # queue_depth 39 < 40 still serves; 40 does not.
    assert route(TaskClass.CRITIQUE, local="busy").route is Route.LOCAL
    at_limit = LocalState(healthy=True, queue_depth=LIMITS.local_queue_depth)
    decision = decide(
        task=TaskClass.CRITIQUE,
        sensitivity=Sensitivity.PUBLIC,
        tier=Tier.LOCAL,
        local=at_limit,
        cloud=CLOUD_OK,
        limits=LIMITS,
        spent_usd=Decimal("0"),
        max_cost_usd=Decimal("1.50"),
        deadline_ms=120_000,
    )
    assert decision.route is not Route.LOCAL
