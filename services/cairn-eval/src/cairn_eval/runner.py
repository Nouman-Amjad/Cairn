"""Run the incident suite through the real agent loop.

The loop, the transcript, the context management, the tool-result capping and
the trajectory persistence are all the production code paths. What is faked
is the outside world: the observability backends and, in replay mode, the
model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from cairn_core.config import settings
from cairn_core.db import session
from cairn_core.domain import Budget, TrajectoryState
from cairn_core.prompts import load
from cairn_core.repo import create_trajectory
from cairn_core.telemetry import get_logger, setup
from cairn_eval.incidents import Incident, build_suite
from cairn_eval.judge import CauseJudge, Score, Summary, judge, summarise
from cairn_eval.models import HeuristicRouter, ReplayRouter
from cairn_eval.world import FakeWorld
from cairn_orchestrator.loop import AgentLoop

log = get_logger(__name__)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "recordings.json"


class NullBus:
    async def publish(self, event: Any) -> None:
        return None


@dataclass(slots=True)
class RunReport:
    mode: str
    prompt_version: str
    summary: Summary
    scores: list[Score]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "prompt_version": self.prompt_version,
            "summary": self.summary.to_dict(),
            "scores": [s.to_dict() for s in self.scores],
        }


async def run_incident(
    incident: Incident,
    *,
    mode: str,
    budget: Budget,
    cause_judge: CauseJudge | None = None,
) -> Score:
    cfg = settings()
    world = FakeWorld(incident)
    trajectory_id = uuid.uuid4()

    if mode == "heuristic":
        router: Any = HeuristicRouter(world)
    else:
        from cairn_router.client import RouterClient

        live = (
            RouterClient(cfg.router, token="eval")  # noqa: S106 - eval identity, not a secret
            if mode in ("live", "record")
            else None
        )
        router = ReplayRouter(
            fixture_path=FIXTURES,
            incident_id=incident.id,
            live=live,
            record=(mode == "record"),
        )

    prompts = load(cfg.prompt_dir)
    async with session() as sess:
        await create_trajectory(
            sess,
            user_id="eval",
            query=incident.question,
            prompt_version=prompts.version,
            trajectory_id=trajectory_id,
        )

    started = time.monotonic()
    loop = AgentLoop(
        trajectory_id=trajectory_id,
        query=incident.question,
        token="eval",  # noqa: S106 - the eval world does not verify tokens
        budget=budget,
        prompts=prompts,
        bus=NullBus(),  # type: ignore[arg-type]
        router=router,
        tools=world,  # type: ignore[arg-type]
    )
    result = await loop.run()
    elapsed = time.monotonic() - started

    if isinstance(router, ReplayRouter):
        router.save()

    return judge(
        incident,
        answer=result.answer,
        state=result.state,
        confidence=result.confidence or 0.0,
        cost_usd=result.cost_usd,
        iterations=result.iterations,
        tool_calls=len(world.calls),
        # Scored against what the tools actually returned, not against the
        # fixture: naming a metric you never fetched is not retrieval.
        retrieved_text=world.retrieved_text,
        real_artifacts=world.artifacts,
        duration_s=elapsed,
        cause_judge=cause_judge,
    )


async def run_suite(
    *,
    count: int | None = None,
    mode: str,
    concurrency: int = 4,
    max_cost_usd: Decimal | None = None,
    cause_judge: CauseJudge | None = None,
) -> RunReport:
    incidents = list(build_suite(count))
    semaphore = asyncio.Semaphore(concurrency)

    async def one(incident: Incident) -> Score:
        async with semaphore:
            budget = Budget(max_cost_usd=max_cost_usd or Budget().max_cost_usd)
            try:
                return await run_incident(
                    incident, mode=mode, budget=budget, cause_judge=cause_judge
                )
            except Exception as exc:
                log.error("incident_crashed", incident=incident.id, error=str(exc))
                return judge(
                    incident,
                    answer=None,
                    state=TrajectoryState.FAILED,
                    confidence=0.0,
                    cost_usd=Decimal("0"),
                    iterations=0,
                    tool_calls=0,
                )

    scores = await asyncio.gather(*(one(i) for i in incidents))
    return RunReport(
        mode=mode,
        prompt_version=load(settings().prompt_dir).version,
        summary=summarise(list(scores)),
        scores=list(scores),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Cairn eval suite")
    parser.add_argument(
        "--mode",
        choices=["heuristic", "replay", "record", "live"],
        default="heuristic",
        help="where model responses come from (see cairn_eval.models)",
    )
    parser.add_argument("--count", type=int, default=None, help="limit the suite")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("eval-results.json"))
    parser.add_argument("--max-cost-usd", type=Decimal, default=None)
    args = parser.parse_args()

    cfg = settings()
    setup(cfg.otel.model_copy(update={"service_name": "cairn-eval"}))

    report = asyncio.run(
        run_suite(
            count=args.count,
            mode=args.mode,
            concurrency=args.concurrency,
            max_cost_usd=args.max_cost_usd,
        )
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    summary = report.summary
    print(f"\nCairn eval: {report.mode} mode, prompts {report.prompt_version}")
    print(
        f"  scenarios              {summary.n} "
        f"({summary.n_solvable} solvable, {summary.n_unsolvable} unsolvable)\n"
    )
    for label, value, target in [
        ("root-cause top-1", f"{summary.root_cause_top1:.1%}", ">= 70%"),
        ("evidence recall", f"{summary.evidence_recall:.1%}", ">= 85%"),
        ("distractor resistance", f"{summary.distractor_resistance:.1%}", ">= 90%"),
        ("abstention precision", f"{summary.abstention_precision:.1%}", ">= 80%"),
        ("tool efficiency", f"{summary.tool_efficiency:.1f}", "<= 9"),
        ("cost per resolution", f"${summary.cost_per_resolution:.4f}", "<= $0.60"),
        ("hallucinated citations", f"{summary.hallucinated_citation_rate:.1%}", "0%"),
    ]:
        print(f"  {label:24s} {value:>10s}   target {target}")

    if summary.below_target:
        print("\n  BELOW TARGET:")
        for miss in summary.below_target:
            print(f"    {miss}")
    if summary.failures:
        print(f"\n  failed: {', '.join(summary.failures)}")
    print("\n  by archetype:")
    for name, accuracy in summary.by_archetype.items():
        print(f"    {name:28s} {accuracy:.0%}")
    print(f"\nwritten to {args.out}")

    if report.mode == "heuristic":
        print(
            "\nNOTE: heuristic mode scores the harness, not the agent. "
            "Record fixtures against a live router before quoting a number."
        )


if __name__ == "__main__":
    main()
