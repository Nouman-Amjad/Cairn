"""Scoring: the seven metrics from the architecture document, §12.2.

| Metric                    | Definition                                          | Target |
|---------------------------|-----------------------------------------------------|--------|
| root_cause_top1           | causal_service exact + cause equivalence            | ≥ 70%  |
| evidence_recall           | fraction of required_evidence actually retrieved     | ≥ 85%  |
| distractor_resistance     | runs not citing a distractor as the cause            | ≥ 90%  |
| abstention_precision      | unsolvable scenarios returning PARTIAL               | ≥ 80%  |
| tool_efficiency           | mean tool calls per resolution                       | ≤ 9    |
| cost_per_resolution       | mean cost_usd on solved scenarios                    | ≤ $0.60|
| hallucinated_citation_rate| cited artifacts that do not exist                    | 0%     |

Abstention precision is the one most teams skip and the one that determines
whether anyone trusts the system after month two.

Everything here is deterministic string and set matching, except cause
equivalence, which is delegated to `llm_judge` when it is configured. A
deterministic metric you trust beats a clever one you do not.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from cairn_core.domain import TrajectoryState
from cairn_eval.incidents import Incident

#: Launch targets. The gate reads these; a run below any of them is a fail
#: on its own terms, independent of how it compares to the baseline.
TARGETS: dict[str, float] = {
    "root_cause_top1": 0.70,
    "evidence_recall": 0.85,
    "distractor_resistance": 0.90,
    "abstention_precision": 0.80,
    "tool_efficiency": 9.0,  # upper bound
    "cost_per_resolution": 0.60,  # upper bound
    "hallucinated_citation_rate": 0.0,  # upper bound
}
UPPER_BOUND_METRICS = frozenset(
    {"tool_efficiency", "cost_per_resolution", "hallucinated_citation_rate"}
)


class CauseJudge(Protocol):
    """Decides whether a proposed cause means the same thing as the truth."""

    def equivalent(self, *, question: str, truth: str, proposed: str) -> bool: ...


@dataclass(slots=True)
class Score:
    incident_id: str
    archetype: str
    solvable: bool
    state: str

    # --- the seven, per incident ------------------------------------------
    root_cause_correct: bool
    evidence_recall: float
    cited_distractor: bool
    abstained: bool
    tool_calls: int
    cost_usd: Decimal
    hallucinated_citations: int

    confidence: float = 0.0
    iterations: int = 0
    duration_s: float = 0.0
    service_matched: bool = False
    cause_equivalent: bool = False
    missing_evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cost_usd"] = str(self.cost_usd)
        return payload


#: Ordinary inflections a correct answer might use. Anything looser matches
#: "no" inside "notification-worker".
_SUFFIXES = r"(?:s|es|ed|ing|ment|ments)?"

_ABSTENTION_MARKERS = (
    "not determin",
    "could not determine",
    "cannot determine",
    "no evidence",
    "insufficient evidence",
    "unable to identify",
    "not enough",
)


def contains(haystack: str, needle: str) -> bool:
    needle = needle.lower().strip()
    if not needle:
        return False
    if " " in needle or not needle.isalnum():
        return needle in haystack
    return re.search(rf"\b{re.escape(needle)}{_SUFFIXES}\b", haystack) is not None


class KeywordJudge:
    """Deterministic fallback for cause equivalence.

    Overlap of significant terms. Crude, and honest about it: the document
    calls for an LLM judge here, and `llm_judge.py` provides one. This exists
    so the harness runs with no model at all, and so there is a floor when the
    judge is unavailable.
    """

    STOPWORDS = frozenset(
        """the a an and or of to in on at for with by from is was were be been
        that this it its as after before during not no there their they which
        because caused cause causing due had has have when while into over""".split()
    )

    def __init__(self, threshold: float = 0.34) -> None:
        self.threshold = threshold

    def _terms(self, text: str) -> set[str]:
        words = re.findall(r"[a-z0-9_][a-z0-9_.-]{2,}", text.lower())
        return {w for w in words if w not in self.STOPWORDS}

    def equivalent(self, *, question: str, truth: str, proposed: str) -> bool:
        truth_terms = self._terms(truth)
        if not truth_terms:
            return False
        overlap = truth_terms & self._terms(proposed)
        return len(overlap) / len(truth_terms) >= self.threshold


def _abstained(text: str, state: TrajectoryState) -> bool:
    """An abstention is a PARTIAL state or an answer that says so in words.

    Both count: the state machine reaches PARTIAL for budget reasons too, and
    an agent that reaches COMPLETE while saying "I could not determine this"
    has still done the right thing.
    """
    return state is TrajectoryState.PARTIAL or any(m in text for m in _ABSTENTION_MARKERS)


def _evidence_found(incident: Incident, retrieved: str) -> tuple[float, list[str]]:
    """Fraction of required evidence that actually appeared in tool output.

    Scored against what the tools returned, not against what the answer
    claims. An agent that names a metric it never fetched has not retrieved it.
    """
    required = incident.truth.required_evidence
    if not required:
        return 1.0, []
    missing = [e.value for e in required if e.value.lower() not in retrieved]
    return (len(required) - len(missing)) / len(required), missing


def _count_hallucinated_citations(text: str, real_artifacts: set[str]) -> int:
    """Artifact handles cited in the answer that were never produced.

    Target is zero, and it is a hard zero: an incident answer citing evidence
    that does not exist is worse than one citing nothing.
    """
    cited = set(re.findall(r"\bart_[0-9a-f]{8,}\b", text))
    return len(cited - real_artifacts)


def judge(
    incident: Incident,
    *,
    answer: str | None,
    state: TrajectoryState,
    confidence: float,
    cost_usd: Decimal,
    iterations: int,
    tool_calls: int,
    retrieved_text: str = "",
    real_artifacts: set[str] | None = None,
    duration_s: float = 0.0,
    cause_judge: CauseJudge | None = None,
) -> Score:
    text = (answer or "").lower()
    retrieved = retrieved_text.lower()
    cause_judge = cause_judge or KeywordJudge()
    notes: list[str] = []

    abstained = _abstained(text, state)
    recall, missing = _evidence_found(incident, retrieved)

    # A distractor cited as *the cause* is the failure. Mentioning one while
    # explicitly ruling it out is good practice, so the check is scoped to the
    # part of the answer that states a conclusion.
    conclusion = text.split("**evidence**")[0] if "**evidence**" in text else text[:600]
    cited_distractor = any(_distractor_cited(conclusion, d) for d in incident.distractors)

    if incident.solvable:
        service = (incident.truth.causal_service or "").lower()
        service_matched = bool(service) and contains(text, service)
        equivalent = cause_judge.equivalent(
            question=incident.question,
            truth=incident.truth.root_cause,
            proposed=answer or "",
        )
        # Top-1 requires both: naming the right service with the wrong
        # mechanism is not a root cause, and the right mechanism attributed to
        # the wrong service sends someone to the wrong dashboard.
        correct = service_matched and equivalent and not cited_distractor
        if service_matched and not equivalent:
            notes.append("named the right service with a non-equivalent cause")
        if equivalent and not service_matched:
            notes.append("described the right mechanism without naming the service")
    else:
        # On an unsolvable scenario the only correct answer is an admission.
        service_matched = False
        equivalent = False
        correct = abstained
        if not abstained:
            notes.append("answered confidently on an unsolvable scenario")

    return Score(
        incident_id=incident.id,
        archetype=incident.archetype,
        solvable=incident.solvable,
        state=state.value,
        root_cause_correct=correct,
        evidence_recall=round(recall, 3),
        cited_distractor=cited_distractor,
        abstained=abstained,
        tool_calls=tool_calls,
        cost_usd=cost_usd,
        hallucinated_citations=_count_hallucinated_citations(text, real_artifacts or set()),
        confidence=confidence,
        iterations=iterations,
        duration_s=round(duration_s, 2),
        service_matched=service_matched,
        cause_equivalent=equivalent,
        missing_evidence=missing,
        notes=notes,
    )


#: Clauses containing one of these are ruling something *out*. "no code was
#: deployed" must not count as blaming the deploy — that is the opposite of
#: what it says, and an agent that correctly excludes a distractor should be
#: rewarded rather than penalised.
_NEGATIONS = (
    "no code",
    "not caused",
    "rather than",
    "ruled out",
    # "unrelated" is deliberately absent: it appears inside the distractors'
    # own wording ("unrelated frontend deploy"), so treating it as a negation
    # would silently stop detecting the very thing being measured.
    "was not",
    "were not",
    "is not",
    "did not",
    "no deploy",
    "without any",
    "nothing in",
)


def _affirmative_clauses(text: str) -> str:
    """The parts of a conclusion that assert something, negations dropped."""
    clauses = re.split(r"[;,.]| but | although | however ", text)
    return " ".join(c for c in clauses if not any(n in c for n in _NEGATIONS))


def _distractor_cited(conclusion: str, distractor: str) -> bool:
    """Did the conclusion lean on a distractor?

    Matched on the distinctive terms of the distractor rather than the whole
    phrase, because the agent will never quote it verbatim, and only within
    clauses that actually assert something.
    """
    terms = [
        t
        for t in re.findall(r"[a-z0-9-]{4,}", distractor.lower())
        if t not in KeywordJudge.STOPWORDS
    ]
    if len(terms) < 2:
        return False
    affirmative = _affirmative_clauses(conclusion)
    hits = sum(1 for t in terms if t in affirmative)
    return hits >= max(2, len(terms) // 2)


@dataclass(slots=True)
class Summary:
    n: int
    n_solvable: int
    n_unsolvable: int

    root_cause_top1: float
    evidence_recall: float
    distractor_resistance: float
    abstention_precision: float
    tool_efficiency: float
    cost_per_resolution: float
    hallucinated_citation_rate: float

    mean_iterations: float = 0.0
    failures: list[str] = field(default_factory=list)
    by_archetype: dict[str, float] = field(default_factory=dict)
    below_target: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mean(values: list[float], default: float = 0.0) -> float:
    return round(sum(values) / len(values), 4) if values else default


def summarise(scores: list[Score]) -> Summary:
    if not scores:
        return Summary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    solvable = [s for s in scores if s.solvable]
    unsolvable = [s for s in scores if not s.solvable]
    resolved = [s for s in solvable if s.root_cause_correct]

    by_archetype: dict[str, list[bool]] = {}
    for score in scores:
        by_archetype.setdefault(score.archetype, []).append(score.root_cause_correct)

    summary = Summary(
        n=len(scores),
        n_solvable=len(solvable),
        n_unsolvable=len(unsolvable),
        root_cause_top1=_mean([float(s.root_cause_correct) for s in solvable]),
        evidence_recall=_mean([s.evidence_recall for s in solvable], 1.0),
        distractor_resistance=_mean([float(not s.cited_distractor) for s in scores], 1.0),
        abstention_precision=_mean([float(s.abstained) for s in unsolvable], 1.0),
        tool_efficiency=_mean([float(s.tool_calls) for s in resolved or solvable]),
        # Cost is measured on solved scenarios: a run that gave up early is
        # cheap, and averaging it in flatters the number.
        cost_per_resolution=_mean([float(s.cost_usd) for s in resolved or solvable]),
        hallucinated_citation_rate=_mean([float(bool(s.hallucinated_citations)) for s in scores]),
        mean_iterations=_mean([float(s.iterations) for s in scores]),
        failures=[s.incident_id for s in scores if not s.root_cause_correct],
        by_archetype={name: round(sum(r) / len(r), 3) for name, r in sorted(by_archetype.items())},
    )
    summary.below_target = below_target(summary)
    return summary


def below_target(summary: Summary) -> list[str]:
    """Which launch targets this run misses, in the doc's own terms."""
    misses = []
    values = summary.to_dict()
    for metric, target in TARGETS.items():
        value = values[metric]
        if metric in UPPER_BOUND_METRICS:
            if value > target:
                misses.append(f"{metric} {value} > {target}")
        elif value < target:
            misses.append(f"{metric} {value:.1%} < {target:.0%}")
    return misses


def _self_check() -> None:
    from cairn_eval.incidents import build_suite

    suite = build_suite()
    solvable = next(i for i in suite if i.solvable and i.id == "inc-001")
    unsolvable = next(i for i in suite if not i.solvable)

    retrieved = "checkout_db_pool_wait_seconds TimeoutError: QueuePool limit 9f2c1ab4de11"

    good = judge(
        solvable,
        answer="The 03:02 deploy of checkout-api saturated the DB connection pool; "
        "requests queued behind the 30s acquire timeout.",
        state=TrajectoryState.COMPLETE,
        confidence=0.85,
        cost_usd=Decimal("0.42"),
        iterations=4,
        tool_calls=6,
        retrieved_text=retrieved,
    )
    assert good.root_cause_correct, good.notes
    assert good.evidence_recall == 1.0
    assert not good.cited_distractor

    # right service, wrong mechanism -> not a root cause
    wrong_cause = judge(
        solvable,
        answer="checkout-api was slow because of a network partition.",
        state=TrajectoryState.COMPLETE,
        confidence=0.9,
        cost_usd=Decimal("0.4"),
        iterations=3,
        tool_calls=5,
        retrieved_text=retrieved,
    )
    assert not wrong_cause.root_cause_correct
    assert wrong_cause.service_matched and not wrong_cause.cause_equivalent

    # blaming a distractor fails even if the words look right
    distracted = judge(
        solvable,
        answer="The unrelated frontend deploy at 02:45 caused the checkout-api pool "
        "to saturate with connection timeout errors.",
        state=TrajectoryState.COMPLETE,
        confidence=0.8,
        cost_usd=Decimal("0.4"),
        iterations=3,
        tool_calls=5,
        retrieved_text=retrieved,
    )
    assert distracted.cited_distractor, "the frontend deploy is a distractor"
    assert not distracted.root_cause_correct

    # evidence recall counts what was retrieved, not what was claimed
    thin = judge(
        solvable,
        answer="The 03:02 deploy saturated the checkout-api connection pool.",
        state=TrajectoryState.COMPLETE,
        confidence=0.8,
        cost_usd=Decimal("0.1"),
        iterations=2,
        tool_calls=2,
        retrieved_text="checkout_db_pool_wait_seconds",
    )
    assert thin.evidence_recall < 0.5, thin.evidence_recall
    assert "TimeoutError: QueuePool limit" in thin.missing_evidence

    # unsolvable: abstaining is correct, answering confidently is not
    abstained = judge(
        unsolvable,
        answer="I could not determine a cause; the logs for that window have aged out.",
        state=TrajectoryState.PARTIAL,
        confidence=0.2,
        cost_usd=Decimal("0.08"),
        iterations=2,
        tool_calls=3,
    )
    assert abstained.root_cause_correct and abstained.abstained

    invented = judge(
        unsolvable,
        answer="The root cause is a deploy at 03:02.",
        state=TrajectoryState.COMPLETE,
        confidence=0.85,
        cost_usd=Decimal("0.3"),
        iterations=3,
        tool_calls=4,
    )
    assert not invented.root_cause_correct
    assert "answered confidently" in invented.notes[0]

    # hallucinated citations
    fake = judge(
        solvable,
        answer="See artifact art_0123456789abcdef for the pool metrics.",
        state=TrajectoryState.COMPLETE,
        confidence=0.5,
        cost_usd=Decimal("0.2"),
        iterations=2,
        tool_calls=3,
        real_artifacts=set(),
    )
    assert fake.hallucinated_citations == 1

    summary = summarise([good, wrong_cause, distracted, thin, abstained, invented, fake])
    assert summary.n == 7
    assert summary.n_unsolvable == 2
    assert summary.abstention_precision == 0.5
    assert summary.hallucinated_citation_rate > 0
    assert any("hallucinated" in m for m in summary.below_target)

    perfect = summarise([good, abstained])
    assert perfect.below_target == [], perfect.below_target
    print("judge self-check ok")


if __name__ == "__main__":
    _self_check()
