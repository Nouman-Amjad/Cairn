"""The CI gate.

Two independent ways to fail, and both matter:

**Regression against the baseline** (§9.7)
  - root-cause accuracy may not drop more than 5 points
  - mean cost per resolution may not rise more than 20%

**Absolute launch targets** (§12.2)
  Every one of the seven metrics has a number. A run that is stable against
  yesterday but below target is still not shippable, and a gate that only
  compares to itself will happily ratchet a system down to zero one
  four-point drop at a time.

The cost gate is the one people forget to build. A prompt change that is
functionally fine but triples token consumption will not fail a health check,
will not page anyone, and will show up on the invoice five weeks later.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn_eval.judge import TARGETS, UPPER_BOUND_METRICS

ACCURACY_DROP_LIMIT = 0.05
COST_RISE_LIMIT = 0.20
#: Below this the suite is too small for a 5-point threshold to mean
#: anything, and the gate says so instead of pretending.
MIN_INCIDENTS = 20
#: The unsolvable scenarios are the whole basis of abstention_precision.
#: A run missing them is not a run.
MIN_UNSOLVABLE = 10


@dataclass(slots=True)
class GateResult:
    passed: bool
    reasons: list[str]
    notes: list[str]


def _pct(value: float) -> str:
    return f"{value:.1%}"


def check_targets(summary: dict[str, Any]) -> list[str]:
    """Absolute launch targets, independent of any baseline."""
    misses = []
    for metric, target in sorted(TARGETS.items()):
        value = summary.get(metric)
        if value is None:
            continue
        if metric in UPPER_BOUND_METRICS:
            if value > target:
                misses.append(f"{metric} {value:.4g} above the {target:.4g} ceiling")
        elif value < target:
            misses.append(f"{metric} {_pct(value)} below the {_pct(target)} target")
    return misses


def compare(current: dict[str, Any], baseline: dict[str, Any] | None) -> GateResult:
    reasons: list[str] = []
    notes: list[str] = []
    summary = current["summary"]

    # --- the suite actually ran ------------------------------------------
    if summary["n"] < MIN_INCIDENTS:
        reasons.append(
            f"only {summary['n']} scenarios ran (minimum {MIN_INCIDENTS}); "
            "the suite did not complete"
        )
    if summary.get("n_unsolvable", 0) < MIN_UNSOLVABLE:
        reasons.append(
            f"only {summary.get('n_unsolvable', 0)} unsolvable scenarios ran "
            f"(expected {MIN_UNSOLVABLE}); abstention precision is not measurable"
        )

    if current.get("mode") == "heuristic":
        notes.append(
            "heuristic mode: this run scores the harness, not the agent. "
            "It is not a quality signal."
        )

    # --- absolute targets -------------------------------------------------
    reasons.extend(check_targets(summary))

    # --- regression against the baseline ---------------------------------
    if baseline is None:
        notes.append("no baseline found; recording this run as the new baseline")
        return GateResult(passed=not reasons, reasons=reasons, notes=notes)

    base = baseline["summary"]

    accuracy_delta = summary["root_cause_top1"] - base["root_cause_top1"]
    if accuracy_delta < -ACCURACY_DROP_LIMIT:
        reasons.append(
            f"root-cause accuracy dropped {abs(accuracy_delta):.1%} "
            f"({_pct(base['root_cause_top1'])} -> {_pct(summary['root_cause_top1'])}), "
            f"limit is {ACCURACY_DROP_LIMIT:.0%}"
        )
    else:
        notes.append(
            f"root-cause top-1 {_pct(base['root_cause_top1'])} -> "
            f"{_pct(summary['root_cause_top1'])}"
        )

    base_cost = base["cost_per_resolution"]
    cost = summary["cost_per_resolution"]
    if base_cost > 0:
        cost_delta = (cost - base_cost) / base_cost
        if cost_delta > COST_RISE_LIMIT:
            reasons.append(
                f"cost per resolution rose {cost_delta:.0%} "
                f"(${base_cost:.4f} -> ${cost:.4f}), limit is {COST_RISE_LIMIT:.0%}"
            )
        else:
            notes.append(f"cost per resolution ${base_cost:.4f} -> ${cost:.4f} ({cost_delta:+.0%})")

    # --- warnings, not gates ---------------------------------------------
    for metric, label, threshold in [
        ("abstention_precision", "abstention precision", 0.10),
        ("evidence_recall", "evidence recall", 0.10),
        ("distractor_resistance", "distractor resistance", 0.10),
    ]:
        delta = summary.get(metric, 0) - base.get(metric, 0)
        if delta < -threshold:
            notes.append(
                f"WARNING: {label} fell {abs(delta):.0%}"
                + (
                    "; the agent is making more confident wrong claims"
                    if metric == "abstention_precision"
                    else ""
                )
            )

    regressed = sorted(
        name
        for name, score in summary.get("by_archetype", {}).items()
        if score < base.get("by_archetype", {}).get(name, 0) - 0.2
    )
    if regressed:
        notes.append("archetypes that got worse: " + ", ".join(regressed))

    return GateResult(passed=not reasons, reasons=reasons, notes=notes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate a merge on eval results")
    parser.add_argument("--current", type=Path, default=Path("eval-results.json"))
    parser.add_argument("--baseline", type=Path, default=Path("fixtures/eval-baseline.json"))
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write the current run as the new baseline (main branch only)",
    )
    args = parser.parse_args()

    current = json.loads(args.current.read_text(encoding="utf-8"))
    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else None
    )

    result = compare(current, baseline)
    # ASCII only: this runs on a Windows console often enough that a
    # box-drawing character is a crash rather than a cosmetic issue.
    for note in result.notes:
        print(f"  - {note}")
    for reason in result.reasons:
        print(f"  FAIL {reason}")

    if result.passed and args.update_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(current, indent=2), encoding="utf-8")
        print(f"  - baseline updated: {args.baseline}")

    print("\nEVAL GATE:", "PASS" if result.passed else "FAIL")
    sys.exit(0 if result.passed else 1)


def _self_check() -> None:
    def report(**overrides: Any) -> dict[str, Any]:
        summary = {
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
        summary.update(overrides)
        return {"mode": "replay", "summary": summary}

    base = report()
    assert compare(base, base).passed

    # regression thresholds
    assert compare(report(root_cause_top1=0.76), base).passed, "4 points is tolerated"
    dropped = compare(report(root_cause_top1=0.74), base)
    assert not dropped.passed and "accuracy dropped" in dropped.reasons[0]

    assert compare(report(cost_per_resolution=0.47), base).passed, "18% is tolerated"
    expensive = compare(report(cost_per_resolution=0.49), base)
    assert not expensive.passed and "cost per resolution rose" in expensive.reasons[0]

    # absolute targets fail even with a flattering baseline
    weak = report(root_cause_top1=0.60)
    assert not compare(weak, weak).passed, "below target must fail against itself"
    assert any("below the 70.0% target" in r for r in compare(weak, weak).reasons)

    for metric, value, fragment in [
        ("evidence_recall", 0.5, "evidence_recall"),
        ("distractor_resistance", 0.5, "distractor_resistance"),
        ("abstention_precision", 0.5, "abstention_precision"),
        ("tool_efficiency", 20.0, "tool_efficiency"),
        ("cost_per_resolution", 0.9, "cost_per_resolution"),
        ("hallucinated_citation_rate", 0.05, "hallucinated_citation_rate"),
    ]:
        bad = report(**{metric: value})
        result = compare(bad, base)
        assert not result.passed, metric
        assert any(fragment in r for r in result.reasons), (metric, result.reasons)

    # a truncated suite cannot pass by scoring well on what did run
    assert not compare(report(n=5), base).passed
    assert not compare(report(n_unsolvable=0), base).passed

    first = compare(base, None)
    assert first.passed and "no baseline" in first.notes[0]

    warned = compare(report(abstention_precision=0.85), base)
    assert warned.passed and any("abstention precision fell" in n for n in warned.notes)

    heuristic = {"mode": "heuristic", "summary": base["summary"]}
    assert any("not a quality signal" in n for n in compare(heuristic, base).notes)
    print("gate self-check ok")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        main()
