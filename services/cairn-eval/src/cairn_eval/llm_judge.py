"""LLM-as-judge for cause equivalence, and the calibration that keeps it honest.

Used for exactly one thing: deciding whether a proposed root cause means the
same as the ground truth. Everything else in `judge.py` is deterministic
string and set matching, because a deterministic metric you trust beats a
clever one you do not.

Two controls make this a measurement rather than a vibe:

* **Three votes, majority wins.** A single sample from a frontier model on a
  borderline case is close to a coin flip. Three at temperature 0.0 with the
  rubric restated is not.
* **Calibration against human labels.** Agreement is measured as Cohen's
  kappa against ~100 human-labelled runs. Below κ=0.7 the rubric is broken and
  gets rewritten — the judge does not get to quietly drift into being a
  different metric than it was last quarter.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn_core.domain import TaskClass
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

VOTES = 3
KAPPA_FLOOR = 0.70

RUBRIC = """\
You are grading whether a proposed root cause is EQUIVALENT to the known true
root cause of a production incident.

Question the engineer asked:
{question}

TRUE root cause:
{truth}

PROPOSED root cause:
{proposed}

Answer "equivalent" if the proposed cause identifies the same underlying
mechanism, even if worded differently, at different length, or with different
supporting detail.

Answer "different" if it:
- names a different mechanism, even in the same service
- names a symptom rather than the cause (e.g. "requests timed out" when the
  cause is pool exhaustion)
- is so vague it would not tell an engineer what to fix
- attributes the cause to the wrong service or component

Wording, verbosity and confidence are irrelevant. Judge the mechanism only.

Return JSON: {{"verdict": "equivalent" | "different", "why": "<one sentence>"}}
"""


@dataclass(slots=True)
class Verdict:
    equivalent: bool
    votes: list[bool]
    reasons: list[str]

    @property
    def unanimous(self) -> bool:
        return len(set(self.votes)) <= 1


class LLMCauseJudge:
    """Cause equivalence by majority vote of a frontier model.

    Falls back to the deterministic `KeywordJudge` if the router is
    unreachable, and says so. A judge that hard-fails takes the whole eval
    with it, which is the wrong trade for a metric.
    """

    def __init__(self, router: Any, fallback: Any | None = None) -> None:
        self.router = router
        self.fallback = fallback
        self.calls = 0
        self.fallbacks = 0
        self._cache: dict[tuple[str, str], Verdict] = {}

    def equivalent(self, *, question: str, truth: str, proposed: str) -> bool:
        return self.verdict(question=question, truth=truth, proposed=proposed).equivalent

    def verdict(self, *, question: str, truth: str, proposed: str) -> Verdict:
        key = (truth, proposed.strip())
        if key in self._cache:
            return self._cache[key]

        import anyio

        try:
            result = anyio.from_thread.run(self._vote, question, truth, proposed)
        except RuntimeError:
            result = anyio.run(self._vote, question, truth, proposed)
        except Exception as exc:
            log.warning("llm_judge_unavailable", error=str(exc))
            self.fallbacks += 1
            fallback = bool(
                self.fallback
                and self.fallback.equivalent(question=question, truth=truth, proposed=proposed)
            )
            result = Verdict(fallback, [fallback], ["deterministic fallback"])

        self._cache[key] = result
        return result

    async def _vote(self, question: str, truth: str, proposed: str) -> Verdict:
        prompt = RUBRIC.format(question=question, truth=truth, proposed=proposed or "(no answer)")
        votes: list[bool] = []
        reasons: list[str] = []

        for _ in range(VOTES):
            call = await self.router.complete(
                task_class=TaskClass.CRITIQUE,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.0,
                response_schema={
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string", "enum": ["equivalent", "different"]},
                        "why": {"type": "string"},
                    },
                    "required": ["verdict"],
                    "additionalProperties": False,
                },
            )
            self.calls += 1
            parsed = _parse(call.text)
            votes.append(parsed.get("verdict") == "equivalent")
            reasons.append(str(parsed.get("why", ""))[:200])

        majority = Counter(votes).most_common(1)[0][0]
        return Verdict(equivalent=bool(majority), votes=votes, reasons=reasons)


def _parse(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ------------------------------------------------------------- calibration


def cohens_kappa(a: list[bool], b: list[bool]) -> float:
    """Agreement between two raters, corrected for chance.

    Raw agreement is misleading here: if 80% of cases are equivalent, a judge
    that always says "equivalent" scores 80% and has learned nothing. Kappa
    subtracts that floor.
    """
    if len(a) != len(b):
        raise ValueError("rating lists must be the same length")
    n = len(a)
    if n == 0:
        return 0.0

    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    pa_true, pb_true = sum(a) / n, sum(b) / n
    expected = pa_true * pb_true + (1 - pa_true) * (1 - pb_true)
    if expected >= 1.0:
        # Both raters were constant and agreed; kappa is undefined. Report
        # perfect agreement rather than dividing by zero, and let the caller's
        # sample-size check catch the degenerate case.
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


@dataclass(slots=True)
class Calibration:
    n: int
    kappa: float
    judge_positive_rate: float
    human_positive_rate: float
    disagreements: list[str]

    @property
    def trustworthy(self) -> bool:
        return self.n >= 30 and self.kappa >= KAPPA_FLOOR

    def report(self) -> str:
        lines = [
            f"  labelled runs   {self.n}",
            f"  cohen's kappa   {self.kappa:.3f}  (floor {KAPPA_FLOOR})",
            f"  judge says yes  {self.judge_positive_rate:.1%}",
            f"  human says yes  {self.human_positive_rate:.1%}",
        ]
        if self.disagreements:
            lines.append(f"  disagreed on    {', '.join(self.disagreements[:10])}")
        if not self.trustworthy:
            lines.append(
                "\n  The rubric is broken. Rewrite it against the disagreements above "
                "and recalibrate; do not ship an eval whose judge nobody trusts."
            )
        return "\n".join(lines)


def calibrate(labels_path: Path, judge: LLMCauseJudge) -> Calibration:
    """Score the judge against human labels.

    The labels file is JSONL, one object per line:

        {"id": "inc-001", "question": "...", "truth": "...",
         "proposed": "...", "human": true}

    Produced by having a human read ~100 real runs and mark each proposed
    cause equivalent or not. That is a day of work and it is the difference
    between a metric and a number.
    """
    rows = [
        json.loads(line)
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    human = [bool(r["human"]) for r in rows]
    machine: list[bool] = []
    disagreements: list[str] = []

    for row in rows:
        verdict = judge.verdict(
            question=row.get("question", ""),
            truth=row["truth"],
            proposed=row["proposed"],
        )
        machine.append(verdict.equivalent)
        if verdict.equivalent != bool(row["human"]):
            disagreements.append(str(row.get("id", "?")))

    n = len(rows)
    return Calibration(
        n=n,
        kappa=round(cohens_kappa(human, machine), 4),
        judge_positive_rate=sum(machine) / n if n else 0.0,
        human_positive_rate=sum(human) / n if n else 0.0,
        disagreements=disagreements,
    )


def main() -> None:
    import argparse

    from cairn_core.config import settings
    from cairn_eval.judge import KeywordJudge
    from cairn_router.client import RouterClient

    parser = argparse.ArgumentParser(description="Calibrate the LLM cause judge")
    parser.add_argument("labels", type=Path, help="JSONL of human-labelled runs")
    args = parser.parse_args()

    judge = LLMCauseJudge(
        RouterClient(settings().router, token="eval"),  # noqa: S106 - eval identity
        fallback=KeywordJudge(),
    )
    calibration = calibrate(args.labels, judge)
    print("\nCause-judge calibration")
    print(calibration.report())
    raise SystemExit(0 if calibration.trustworthy else 1)


def _self_check() -> None:
    # perfect agreement
    assert cohens_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    # total disagreement is worse than chance
    assert cohens_kappa([True, True, False, False], [False, False, True, True]) < 0

    # the case kappa exists for: a judge that always says yes on an 80%-yes
    # corpus gets 80% raw agreement and near-zero kappa
    human = [True] * 8 + [False] * 2
    lazy = [True] * 10
    assert abs(cohens_kappa(human, lazy)) < 0.01, cohens_kappa(human, lazy)

    good = [True] * 8 + [False] * 2
    assert cohens_kappa(human, good) == 1.0

    calibration = Calibration(
        n=100, kappa=0.82, judge_positive_rate=0.7, human_positive_rate=0.72, disagreements=[]
    )
    assert calibration.trustworthy
    assert not Calibration(100, 0.55, 0.7, 0.72, ["inc-004"]).trustworthy
    assert "rubric is broken" in Calibration(100, 0.55, 0.7, 0.72, []).report()
    # too small a sample is not trustworthy however good the kappa looks
    assert not Calibration(12, 0.95, 0.7, 0.72, []).trustworthy

    assert _parse('```json\n{"verdict": "equivalent"}\n```') == {"verdict": "equivalent"}
    assert _parse("no json") == {}
    print("llm_judge self-check ok")


if __name__ == "__main__":
    import sys

    if "--self-check" in sys.argv:
        _self_check()
    else:
        main()
