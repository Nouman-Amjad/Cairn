"""Tests for the eval harness itself.

These assert that the *measurement* works — that scenarios load, that
incidents run through the real loop, that each of the seven metrics
discriminates, and that the gate blocks a regression. They deliberately
assert nothing about agent quality, which is what the suite itself is for.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from cairn_core.domain import Budget, TrajectoryState
from cairn_eval.gate import compare
from cairn_eval.incidents import ScenarioError, build_suite
from cairn_eval.judge import TARGETS, judge, summarise
from cairn_eval.llm_judge import cohens_kappa
from cairn_eval.runner import run_incident, run_suite

pytestmark = pytest.mark.anyio


def solvable() -> Any:
    return next(i for i in build_suite() if i.solvable)


def unsolvable() -> Any:
    return next(i for i in build_suite() if not i.solvable)


# --------------------------------------------------------------- corpus


def test_the_corpus_is_twenty_solvable_and_ten_not():
    suite = build_suite()
    assert len(suite) == 30
    assert sum(1 for i in suite if not i.solvable) == 10, (
        "abstention precision is only measurable against unsolvable scenarios"
    )


def test_every_required_evidence_entry_is_actually_retrievable():
    """A scenario that lies about its own evidence makes recall meaningless."""
    import yaml

    for incident in build_suite():
        world = yaml.safe_dump(incident.world).lower()
        for evidence in incident.truth.required_evidence:
            assert evidence.value.lower() in world, (
                f"{incident.id}: {evidence.value!r} is not in the fixtures"
            )


def test_a_scenario_that_lies_is_rejected_on_load(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        """
id: inc-bad
title: requires evidence it does not have
archetype: test
solvable: true
question: why?
ground_truth:
  root_cause: something
  causal_service: checkout-api
  required_evidence:
    - metric: a_metric_that_is_nowhere
world:
  query_metrics: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError, match="not present"):
        build_suite(directory=str(tmp_path))


def test_unsolvable_scenarios_carry_no_solvable_path():
    for incident in build_suite():
        if not incident.solvable:
            assert not incident.truth.required_evidence
            assert incident.truth.root_cause.startswith("Not determinable")


# ----------------------------------------------------------------- loop


@pytest.mark.requires_db
async def test_an_incident_runs_through_the_real_loop(db):
    score = await run_incident(solvable(), mode="heuristic", budget=Budget())
    assert score.state in (TrajectoryState.COMPLETE.value, TrajectoryState.PARTIAL.value)
    assert score.iterations > 0, "the loop must actually have executed"
    assert score.tool_calls > 0, "the agent must have called tools"


@pytest.mark.requires_db
async def test_the_suite_is_deterministic(db):
    first = await run_suite(count=6, mode="heuristic", concurrency=3)
    second = await run_suite(count=6, mode="heuristic", concurrency=3)
    assert [s.incident_id for s in first.scores] == [s.incident_id for s in second.scores]
    assert first.summary.root_cause_top1 == second.summary.root_cause_top1


@pytest.mark.requires_db
async def test_tool_results_are_capped_before_reaching_the_agent(db):
    """The fake world goes through the real deliver() path, so the 4k token
    cap applies here exactly as it does in production."""
    from cairn_core.tokens import estimate_json
    from cairn_eval.world import FakeWorld, _expand

    # Pick by expanded volume: fixtures store `occurrences` rather than a
    # thousand literal rows, so raw length says nothing about what the tool
    # actually has to return.
    incident = max(build_suite(), key=lambda i: len(_expand(i.world.get("query_logs") or [])))
    world = FakeWorld(incident)
    result = await world.call("query_logs", {"service": incident.service})

    assert result.content["total_matched"] > 500, "need a fixture large enough to cap"
    assert result.content["returned"] < result.content["total_matched"]
    assert result.content["next_cursor"] is not None
    assert estimate_json(result.content["rows"]) <= 4_400
    assert result.content["facets"]["pod"]


@pytest.mark.requires_db
async def test_write_tools_never_execute_in_the_eval_world(db):
    from cairn_eval.world import FakeWorld

    result = await FakeWorld(solvable()).call("rollback_deploy", {"service": "checkout-api"})
    assert result.pending_approval_id is not None
    assert result.content["status"] == "pending_approval"


# --------------------------------------------------------------- metrics


def test_evidence_recall_is_scored_against_retrieval_not_fixtures():
    """An agent that names a metric it never fetched has not retrieved it."""
    thin = judge(
        solvable(),
        answer="something happened",
        state=TrajectoryState.COMPLETE,
        confidence=0.5,
        cost_usd=Decimal("0.1"),
        iterations=1,
        tool_calls=1,
        retrieved_text="",
    )
    assert thin.evidence_recall == 0.0
    assert thin.missing_evidence


def test_confidently_wrong_fails_root_cause():
    score = judge(
        solvable(),
        answer="A dns network partition was responsible.",
        state=TrajectoryState.COMPLETE,
        confidence=0.95,
        cost_usd=Decimal("0.1"),
        iterations=3,
        tool_calls=4,
    )
    assert not score.root_cause_correct


def test_abstaining_on_an_unsolvable_scenario_is_correct():
    incident = unsolvable()
    good = judge(
        incident,
        answer="I could not determine a cause from the available evidence.",
        state=TrajectoryState.PARTIAL,
        confidence=0.2,
        cost_usd=Decimal("0.05"),
        iterations=2,
        tool_calls=3,
    )
    invented = judge(
        incident,
        answer="The root cause is a deploy at 03:02.",
        state=TrajectoryState.COMPLETE,
        confidence=0.9,
        cost_usd=Decimal("0.2"),
        iterations=3,
        tool_calls=4,
    )
    assert good.root_cause_correct and good.abstained
    assert not invented.root_cause_correct


def test_hallucinated_citations_are_counted():
    fake = judge(
        solvable(),
        answer="See art_0123456789abcdef.",
        state=TrajectoryState.COMPLETE,
        confidence=0.5,
        cost_usd=Decimal("0.1"),
        iterations=2,
        tool_calls=2,
        real_artifacts=set(),
    )
    honest = judge(
        solvable(),
        answer="See art_0123456789abcdef.",
        state=TrajectoryState.COMPLETE,
        confidence=0.5,
        cost_usd=Decimal("0.1"),
        iterations=2,
        tool_calls=2,
        real_artifacts={"art_0123456789abcdef"},
    )
    assert fake.hallucinated_citations == 1
    assert honest.hallucinated_citations == 0


def test_ruling_a_distractor_out_is_not_citing_it():
    """'no code was deployed' must not read as blaming the deploy."""
    incident = next(
        i for i in build_suite() if i.solvable and any("deploy" in d for d in i.distractors)
    )
    ruled_out = judge(
        incident,
        answer="A config reload caused this; no code was deployed in the window.",
        state=TrajectoryState.COMPLETE,
        confidence=0.8,
        cost_usd=Decimal("0.1"),
        iterations=2,
        tool_calls=3,
    )
    assert not ruled_out.cited_distractor


def test_summary_reports_all_seven_metrics():
    scores = [
        judge(
            solvable(),
            answer="x",
            state=TrajectoryState.COMPLETE,
            confidence=0.5,
            cost_usd=Decimal("0.1"),
            iterations=2,
            tool_calls=3,
        )
    ]
    summary = summarise(scores).to_dict()
    for metric in TARGETS:
        assert metric in summary, metric


# ------------------------------------------------------------------ gate


def _summary(**overrides: Any) -> dict[str, Any]:
    base = {
        "n": 30,
        "n_solvable": 20,
        "n_unsolvable": 10,
        "root_cause_top1": 0.80,
        "evidence_recall": 0.95,
        "distractor_resistance": 1.0,
        "abstention_precision": 1.0,
        "tool_efficiency": 4.0,
        "cost_per_resolution": 0.40,
        "hallucinated_citation_rate": 0.0,
        "by_archetype": {},
    }
    base.update(overrides)
    return {"mode": "replay", "summary": base}


def test_the_gate_blocks_an_accuracy_regression():
    assert not compare(_summary(root_cause_top1=0.70), _summary()).passed


def test_the_gate_blocks_a_cost_regression():
    assert not compare(_summary(cost_per_resolution=0.60), _summary()).passed


def test_the_gate_blocks_a_run_below_absolute_target():
    """Stable against yesterday but below target is still not shippable."""
    weak = _summary(root_cause_top1=0.55)
    assert not compare(weak, weak).passed


def test_the_gate_blocks_a_truncated_suite():
    assert not compare(_summary(n=5), _summary()).passed
    assert not compare(_summary(n_unsolvable=0), _summary()).passed


def test_kappa_catches_a_judge_that_always_agrees():
    """The reason calibration uses kappa and not raw agreement."""
    human = [True] * 8 + [False] * 2
    lazy = [True] * 10
    assert abs(cohens_kappa(human, lazy)) < 0.01
    assert cohens_kappa(human, human) == 1.0
