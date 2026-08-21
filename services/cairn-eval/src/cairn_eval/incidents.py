"""The scenario corpus.

Scenarios are YAML files in `scenarios/`, not Python. That boundary matters:
an SRE who has never opened this package should be able to add an incident by
copying a file, and the corpus should be reviewable as data in a diff.

Ten of the thirty are unsolvable by design. An agent that answers those
confidently is worse than one that abstains, and `abstention_precision` is
the metric that says so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

#: src/cairn_eval/incidents.py -> cairn_eval -> src -> cairn-eval/scenarios.
#: Overridable so a team can point the harness at its own corpus without
#: forking the package.
SCENARIO_DIR = Path(
    os.environ.get("CAIRN_EVAL_SCENARIOS") or Path(__file__).resolve().parents[2] / "scenarios"
)


@dataclass(slots=True)
class Evidence:
    """One thing a competent investigation must actually retrieve.

    Exactly one of the three kinds is set. `kind` is what the scorer matches
    on, so a metric requirement is satisfied by that metric appearing in a
    tool result — not by the answer happening to mention its name.
    """

    kind: str  # metric | log_pattern | event
    value: str

    @classmethod
    def parse(cls, raw: dict[str, str]) -> Evidence:
        for kind in ("metric", "log_pattern", "event"):
            if kind in raw:
                return cls(kind=kind, value=str(raw[kind]))
        raise ValueError(f"evidence entry has no metric/log_pattern/event: {raw}")

    def to_dict(self) -> dict[str, str]:
        return {self.kind: self.value}


@dataclass(slots=True)
class GroundTruth:
    root_cause: str
    causal_service: str | None
    contributing_deploy: str | None = None
    required_evidence: list[Evidence] = field(default_factory=list)


@dataclass(slots=True)
class Incident:
    id: str
    title: str
    archetype: str
    solvable: bool
    question: str
    truth: GroundTruth
    distractors: list[str] = field(default_factory=list)
    world: dict[str, Any] = field(default_factory=dict)

    @property
    def service(self) -> str:
        return self.truth.causal_service or "unknown"

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Incident:
        truth = raw["ground_truth"]
        return cls(
            id=str(raw["id"]),
            title=str(raw["title"]),
            archetype=str(raw.get("archetype", "unknown")),
            solvable=bool(raw.get("solvable", True)),
            question=str(raw["question"]).strip(),
            truth=GroundTruth(
                root_cause=str(truth["root_cause"]).strip(),
                causal_service=truth.get("causal_service"),
                contributing_deploy=truth.get("contributing_deploy"),
                required_evidence=[
                    Evidence.parse(e) for e in (truth.get("required_evidence") or [])
                ],
            ),
            distractors=[str(d) for d in (raw.get("distractors") or [])],
            world=raw.get("world") or {},
        )


class ScenarioError(ValueError):
    pass


def _validate(incident: Incident) -> None:
    """A scenario that lies about its own evidence makes `evidence_recall`
    meaningless, so this runs on load rather than in a test nobody runs."""
    if not incident.solvable and incident.truth.required_evidence:
        raise ScenarioError(f"{incident.id}: unsolvable scenarios must not require evidence")

    serialized = yaml.safe_dump(incident.world).lower()
    for evidence in incident.truth.required_evidence:
        if evidence.value.lower() not in serialized:
            raise ScenarioError(
                f"{incident.id}: required evidence {evidence.value!r} is not present "
                "anywhere in `world`; the agent could not retrieve it if it tried"
            )


@lru_cache(maxsize=4)
def build_suite(count: int | None = None, directory: str | None = None) -> tuple[Incident, ...]:
    """Load scenarios in id order. Deterministic: a CI failure is about the
    agent changing, never about the sample changing."""
    path = Path(directory) if directory else SCENARIO_DIR
    files = sorted(p for p in path.glob("*.yaml"))
    if not files:
        raise ScenarioError(f"no scenarios found in {path}")

    incidents: list[Incident] = []
    for file in files:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
        incident = Incident.parse(raw)
        _validate(incident)
        incidents.append(incident)

    if count is not None:
        incidents = incidents[:count]
    return tuple(incidents)


def archetypes(suite: tuple[Incident, ...] | None = None) -> set[str]:
    return {i.archetype for i in (suite or build_suite())}


def _self_check() -> None:
    suite = build_suite()
    assert len(suite) == 30, len(suite)
    assert len({i.id for i in suite}) == 30

    solvable = [i for i in suite if i.solvable]
    unsolvable = [i for i in suite if not i.solvable]
    assert len(solvable) == 20, len(solvable)
    # The number the architecture document is specific about, and the reason
    # the abstention metric can mean anything at all.
    assert len(unsolvable) == 10, len(unsolvable)

    for incident in solvable:
        assert incident.truth.causal_service, f"{incident.id} has no causal service"
        assert incident.truth.required_evidence, f"{incident.id} requires no evidence"
        assert incident.distractors, f"{incident.id} has no distractors"

    for incident in unsolvable:
        assert not incident.truth.required_evidence
        assert incident.truth.root_cause.startswith("Not determinable")

    assert [i.id for i in build_suite()] == [i.id for i in suite]
    assert len(archetypes(suite)) >= 6

    print(f"incidents self-check ok ({len(solvable)} solvable, {len(unsolvable)} unsolvable)")


if __name__ == "__main__":
    _self_check()
