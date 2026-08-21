"""The agent loop, as a state machine persisted to Postgres.

Not a framework. Every transition is a committed row, so the loop can be
killed mid-flight — pod eviction, spot reclaim, deploy — and resumed on
another pod from exactly where it stopped.

Two behaviours are worth stating plainly because they look like bugs and are
not:

* `PARTIAL` is a legitimate outcome. An agent that says "here is what I found
  and here is what I could not determine" is more useful during an incident
  than one that fabricates a conclusion. The critic exists to force that
  outcome into the open rather than hide it.
* `PENDING_APPROVAL` suspends the loop with no timer and no polling. The
  approval service wakes it via NOTIFY when a human decides.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from prometheus_client import Counter, Histogram

from cairn_core.config import AppSettings, settings
from cairn_core.db import session
from cairn_core.domain import (
    Budget,
    LLMCall,
    StepKind,
    TaskClass,
    ToolResult,
    TrajectoryState,
)
from cairn_core.prompts import PromptBundle, load
from cairn_core.repo import (
    accrue_cost,
    append_step,
    audit,
    escalate_sensitivity,
    get_trajectory,
    transition,
)
from cairn_core.sensitivity import Sensitivity, escalate
from cairn_core.telemetry import get_logger, span
from cairn_orchestrator.context import Evidence, Transcript
from cairn_orchestrator.events import Event, EventBus, EventType
from cairn_orchestrator.schemas import ACTION_SCHEMA, ANSWER_SCHEMA, CRITIQUE_SCHEMA, PLAN_SCHEMA
from cairn_orchestrator.tools import ToolBox, sensitivity_of
from cairn_router.client import RouterClient, RouterUnavailable

log = get_logger(__name__)

TRAJECTORY_DURATION = Histogram(
    "cairn_trajectory_duration_seconds",
    "End-to-end trajectory duration",
    buckets=(5, 10, 20, 30, 45, 60, 90, 120, 180),
)
TRAJECTORY_COST = Histogram(
    "cairn_trajectory_cost_usd",
    "Cost per trajectory",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5),
)
TERMINAL = Counter("cairn_trajectory_terminal_total", "Trajectories by terminal state", ["state"])
ITERATIONS = Histogram(
    "cairn_trajectory_iterations", "Iterations used", buckets=(1, 2, 3, 5, 7, 9, 12)
)
COMPACTIONS = Counter("cairn_compactions_total", "Transcript compactions")
CRITIC_REJECTS = Counter("cairn_critic_rejections_total", "Critic rejections")


class Suspended(Exception):
    """The loop is waiting on a human. Not an error."""

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"waiting on approval {approval_id}")
        self.approval_id = approval_id


@dataclass(slots=True)
class LoopResult:
    state: TrajectoryState
    answer: str | None = None
    confidence: float | None = None
    cost_usd: Decimal = Decimal("0")
    iterations: int = 0
    reason: str = ""


class AgentLoop:
    """Collaborators are injectable but never optional after construction.

    Taking `prompts=None` and then resolving it in `__post_init__` reads
    fine and leaves every downstream use looking nullable, which is both
    noise and a lie. They are resolved here, once.
    """

    def __init__(
        self,
        *,
        trajectory_id: uuid.UUID,
        query: str,
        token: str,
        budget: Budget,
        cfg: AppSettings | None = None,
        prompts: PromptBundle | None = None,
        bus: EventBus | None = None,
        router: RouterClient | None = None,
        tools: ToolBox | None = None,
    ) -> None:
        self.trajectory_id = trajectory_id
        self.query = query
        self.token = token
        self.budget = budget
        self.cfg: AppSettings = cfg or settings()
        self.prompts: PromptBundle = prompts or load(self.cfg.prompt_dir)
        self.bus: EventBus = bus or EventBus(self.cfg.redis)
        self.router: RouterClient = router or RouterClient(self.cfg.router, token=token)
        self._tools: ToolBox | None = tools
        self.sensitivity: Sensitivity = Sensitivity.PUBLIC
        self.transcript = Transcript(query=query, high_water=budget.context_high_water)
        self._seq = 0
        self._event_seq = 0

    @property
    def tools(self) -> ToolBox:
        if self._tools is None:  # pragma: no cover - guarded by run()
            raise RuntimeError("tools are not connected; call run()")
        return self._tools

    # ------------------------------------------------------------------ run
    async def run(self, *, resuming: bool = False) -> LoopResult:
        self.budget.started_monotonic = time.monotonic()
        started = time.monotonic()
        owned_tools = self._tools is None
        if owned_tools:
            self._tools = ToolBox(self.token, self.cfg.mcp)
            await self._tools.connect()

        try:
            with span("agent.run", **{"cairn.trajectory_id": str(self.trajectory_id)}):
                if resuming:
                    await self._rehydrate()
                    result = await self._execute_phase()
                else:
                    plan = await self._plan()
                    if plan is None:
                        return await self._fail("could not produce a usable plan")
                    result = await self._execute_phase()
                return result
        except Suspended as suspended:
            await self._emit(
                EventType.APPROVAL,
                {"approval_id": suspended.approval_id, "state": "waiting"},
            )
            return LoopResult(
                state=TrajectoryState.WAITING_APPROVAL,
                cost_usd=self.budget.spent_usd,
                iterations=self.budget.iterations,
                reason="waiting for human approval",
            )
        except RouterUnavailable as exc:
            return await self._fail(exc.reason)
        except Exception as exc:
            log.exception("loop_crashed", trajectory_id=str(self.trajectory_id))
            return await self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            TRAJECTORY_DURATION.observe(time.monotonic() - started)
            TRAJECTORY_COST.observe(float(self.budget.spent_usd))
            ITERATIONS.observe(self.budget.iterations)
            if owned_tools and self._tools:
                await self._tools.aclose()

    # ---------------------------------------------------------------- plan
    async def _plan(self) -> dict[str, Any] | None:
        await self._emit(EventType.STATE, {"state": TrajectoryState.PLANNING})
        prompt = self.prompts.render(
            "planner",
            tools=self.tools.render_catalogue(),
            query=self.query,
        )

        for attempt in range(2):
            call = await self._llm(
                TaskClass.PLANNING,
                messages=[{"role": "user", "content": prompt}],
                schema=PLAN_SCHEMA,
                max_tokens=1200,
            )
            plan = _json(call.text)
            if plan and isinstance(plan.get("steps"), list) and plan["steps"]:
                self.transcript.plan = plan
                async with session() as sess:
                    await append_step(
                        sess,
                        self.trajectory_id,
                        kind=StepKind.PLAN,
                        output=plan,
                        model=call.model,
                        route=call.route.value,
                        route_reason=call.route_reason,
                        tokens_in=call.tokens_in,
                        tokens_out=call.tokens_out,
                        cost_usd=call.cost_usd,
                        latency_ms=call.latency_ms,
                    )
                    await transition(
                        sess,
                        self.trajectory_id,
                        frm=TrajectoryState.PLANNING,
                        to=TrajectoryState.EXECUTING,
                        plan=plan,
                    )
                await self._emit(EventType.PLAN, plan)
                return plan
            log.warning("invalid_plan", attempt=attempt + 1)

        return None

    # ------------------------------------------------------------- execute
    async def _execute_phase(self) -> LoopResult:
        await self._emit(EventType.STATE, {"state": TrajectoryState.EXECUTING})

        while True:
            exhausted = self.budget.exhausted(time.monotonic())
            if exhausted:
                await self._emit(
                    EventType.NOTICE,
                    {"message": f"budget exhausted ({exhausted}); answering with what I have"},
                )
                return await self._wrap_up(partial_reason=exhausted)

            self.budget.iterations += 1
            action = await self._next_action()

            if action.get("done") or action.get("tool") in (None, "", "none"):
                return await self._wrap_up()

            result = await self._run_tool(action)
            if result is None:  # suspended on approval; never returns here
                continue

            if self.transcript.over_high_water():
                await self._compact()

    async def _next_action(self) -> dict[str, Any]:
        prompt = (
            f"Question: {self.query}\n\n"
            f"Plan:\n{json.dumps(self.transcript.plan, indent=2)}\n\n"
            f"Evidence so far:\n{self.transcript.render_evidence()}\n\n"
            f"Tools:\n{self.tools.render_catalogue()}\n\n"
            "Choose the single next tool call that most advances the "
            "investigation, or set done=true if the evidence already answers "
            "the question. Do not repeat a call you have already made."
        )
        call = await self._llm(
            TaskClass.ARG_EXTRACTION,
            messages=[{"role": "user", "content": prompt}],
            schema=ACTION_SCHEMA,
            max_tokens=800,
        )
        return _json(call.text) or {"done": True}

    async def _run_tool(self, action: dict[str, Any]) -> ToolResult | None:
        name = str(action.get("tool"))
        args = action.get("args") or {}
        self.budget.tool_calls += 1
        self._seq += 1

        async with session() as sess:
            await append_step(
                sess, self.trajectory_id, kind=StepKind.TOOL_CALL, tool_name=name, input=args
            )

        result = await self.tools.call(name, args)
        level = sensitivity_of(result)
        self.sensitivity = escalate(self.sensitivity, level)

        if result.pending_approval_id:
            await self._suspend(result)
            raise Suspended(result.pending_approval_id)

        content = result.content
        if result.tokens > 8_000:
            # Summarise oversized results on the local tier before they enter
            # the transcript. Forced local: raw tool output is the PII risk.
            content = await self._summarise(name, result)

        async with session() as sess:
            await append_step(
                sess,
                self.trajectory_id,
                kind=StepKind.TOOL_RESULT,
                tool_name=name,
                output=_clip(content),
                artifact_id=result.artifact_id,
                sensitivity=level,
                error={"error": result.error} if result.error else None,
            )
            await escalate_sensitivity(sess, self.trajectory_id, level)

        self.transcript.add(
            Evidence(
                seq=self._seq,
                tool=name,
                content=content,
                artifact_id=result.artifact_id,
            )
        )
        await self._emit(
            EventType.STEP,
            {
                "seq": self._seq,
                "tool": name,
                "args": args,
                "ok": result.ok,
                "sensitivity": str(level),
                "artifact_id": result.artifact_id,
                "summary": str(content.get("summary") or content.get("error") or "")[:400],
            },
        )
        return result

    async def _summarise(self, tool: str, result: ToolResult) -> dict[str, Any]:
        call = await self._llm(
            TaskClass.SUMMARIZATION,
            messages=[
                {
                    "role": "user",
                    "content": self.prompts.render(
                        "summarizer",
                        query=self.query,
                        content=json.dumps(result.content, default=str)[:60_000],
                    ),
                }
            ],
            max_tokens=700,
        )
        # Keep the small, high-signal fields; the raw payload stays in the
        # artifact store where the agent can drill into it if it needs to.
        kept: dict[str, Any] = {
            key: result.content[key]
            for key in ("total_matched", "facets", "artifact_id", "next_cursor")
            if key in result.content
        }
        return {**kept, "summary": call.text.strip(), "summarised_from": tool}

    async def _compact(self) -> None:
        victims = self.transcript.compactable()
        if not victims:
            return
        body = "\n\n".join(item.render() for item in victims)
        call = await self._llm(
            TaskClass.COMPACTION,
            messages=[
                {
                    "role": "user",
                    "content": self.prompts.render("compactor", content=body[:60_000]),
                }
            ],
            max_tokens=800,
        )
        self.transcript.replace_with_digest(victims, call.text.strip())
        COMPACTIONS.inc()

        async with session() as sess:
            await append_step(
                sess,
                self.trajectory_id,
                kind=StepKind.COMPACTION,
                output={"steps_compacted": len(victims), "digest": call.text[:4000]},
                model=call.model,
                route=call.route.value,
                tokens_in=call.tokens_in,
                tokens_out=call.tokens_out,
                cost_usd=call.cost_usd,
            )
        await self._emit(
            EventType.NOTICE,
            {"message": f"compacted {len(victims)} earlier steps to free context"},
        )

    # ------------------------------------------------------ synthesise/critic
    async def _wrap_up(self, partial_reason: str = "") -> LoopResult:
        async with session() as sess:
            await transition(
                sess,
                self.trajectory_id,
                frm=TrajectoryState.EXECUTING,
                to=TrajectoryState.SYNTHESIZING,
            )
        await self._emit(EventType.STATE, {"state": TrajectoryState.SYNTHESIZING})

        answer = await self._synthesise()
        if answer is None:
            return await self._fail("could not synthesise an answer")

        async with session() as sess:
            await transition(
                sess,
                self.trajectory_id,
                frm=TrajectoryState.SYNTHESIZING,
                to=TrajectoryState.CRITIQUING,
            )

        retries = 0
        verdict = await self._critique(answer)
        while verdict.get("verdict") == "reject" and retries < self.budget.critic_retries:
            CRITIC_REJECTS.inc()
            retries += 1
            await self._emit(
                EventType.NOTICE,
                {
                    "message": "critic rejected the answer; revising",
                    "reasons": verdict.get("reasons", []),
                },
            )
            async with session() as sess:
                await transition(
                    sess,
                    self.trajectory_id,
                    frm=TrajectoryState.CRITIQUING,
                    to=TrajectoryState.SYNTHESIZING,
                )
            revised = await self._synthesise(feedback=verdict)
            if revised:
                answer = revised
            async with session() as sess:
                await transition(
                    sess,
                    self.trajectory_id,
                    frm=TrajectoryState.SYNTHESIZING,
                    to=TrajectoryState.CRITIQUING,
                )
            verdict = await self._critique(answer)

        rejected = verdict.get("verdict") == "reject"
        final = TrajectoryState.PARTIAL if rejected or partial_reason else TrajectoryState.COMPLETE
        if rejected:
            # Say so, rather than presenting a contested conclusion as fact.
            answer.setdefault("unknowns", []).extend(verdict.get("missing_evidence", []))
            answer["confidence"] = min(float(answer.get("confidence", 0.5)), 0.4)

        text = _format_answer(answer, partial_reason=partial_reason, rejected=rejected)
        confidence = float(answer.get("confidence", 0.0))

        async with session() as sess:
            await transition(
                sess,
                self.trajectory_id,
                frm=TrajectoryState.CRITIQUING,
                to=final,
                answer=text,
                confidence=confidence,
            )
            await audit(
                sess,
                actor="cairn-orchestrator",
                event=f"trajectory.{final.value.lower()}",
                trajectory_id=self.trajectory_id,
                cost_usd=str(self.budget.spent_usd),
                iterations=self.budget.iterations,
            )

        TERMINAL.labels(state=final.value).inc()
        await self._emit(
            EventType.ANSWER,
            {
                "answer": text,
                "confidence": confidence,
                "state": final.value,
                "cost_usd": str(self.budget.spent_usd),
                "unknowns": answer.get("unknowns", []),
            },
        )
        await self._emit(EventType.DONE, {"state": final.value})
        return LoopResult(
            state=final,
            answer=text,
            confidence=confidence,
            cost_usd=self.budget.spent_usd,
            iterations=self.budget.iterations,
            reason=partial_reason,
        )

    async def _synthesise(self, feedback: dict[str, Any] | None = None) -> dict[str, Any] | None:
        prompt = self.prompts.render(
            "synthesizer",
            query=self.query,
            evidence=self.transcript.render_for_synthesis(),
        )
        if feedback:
            prompt += (
                "\n\nA reviewer rejected your previous answer for these reasons:\n"
                + json.dumps(feedback.get("reasons", []), indent=2)
                + "\nAddress them, or lower your confidence and say what is missing."
            )
        call = await self._llm(
            TaskClass.SYNTHESIS,
            messages=[{"role": "user", "content": prompt}],
            schema=ANSWER_SCHEMA,
            max_tokens=1600,
        )
        return _json(call.text)

    async def _critique(self, answer: dict[str, Any]) -> dict[str, Any]:
        call = await self._llm(
            TaskClass.CRITIQUE,
            messages=[
                {
                    "role": "user",
                    "content": self.prompts.render(
                        "critic",
                        query=self.query,
                        answer=json.dumps(answer, indent=2, default=str),
                        evidence=self.transcript.render_evidence(),
                    ),
                }
            ],
            schema=CRITIQUE_SCHEMA,
            max_tokens=600,
        )
        verdict = _json(call.text) or {"verdict": "accept", "reasons": []}
        async with session() as sess:
            await append_step(
                sess,
                self.trajectory_id,
                kind=StepKind.CRITIQUE,
                output=verdict,
                model=call.model,
                route=call.route.value,
                tokens_in=call.tokens_in,
                tokens_out=call.tokens_out,
                cost_usd=call.cost_usd,
            )
        return verdict

    # ------------------------------------------------------------- plumbing
    async def _llm(
        self,
        task: TaskClass,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> LLMCall:
        call = await self.router.complete(
            task_class=task,
            messages=messages,
            system=self.prompts.raw("system"),
            sensitivity=self.sensitivity,
            budget=self.budget,
            max_tokens=max_tokens,
            response_schema=schema,
            trajectory_id=str(self.trajectory_id),
            token=self.token,
        )
        self.budget.tokens += call.tokens_in + call.tokens_out
        self.budget.spent_usd += call.cost_usd

        async with session() as sess:
            await append_step(
                sess,
                self.trajectory_id,
                kind=StepKind.LLM_CALL,
                input={"task_class": task.value},
                model=call.model,
                route=call.route.value,
                route_reason=call.route_reason,
                tokens_in=call.tokens_in,
                tokens_out=call.tokens_out,
                cost_usd=call.cost_usd,
                latency_ms=call.latency_ms,
                sensitivity=self.sensitivity,
            )
            await accrue_cost(
                sess,
                self.trajectory_id,
                tokens=call.tokens_in + call.tokens_out,
                cost_usd=call.cost_usd,
                local=call.route.value == "local",
            )
        return call

    async def _suspend(self, result: ToolResult) -> None:
        approval_id = result.pending_approval_id
        async with session() as sess:
            await append_step(
                sess,
                self.trajectory_id,
                kind=StepKind.TOOL_RESULT,
                tool_name=result.tool,
                output=result.content,
            )
            await transition(
                sess,
                self.trajectory_id,
                frm=TrajectoryState.EXECUTING,
                to=TrajectoryState.WAITING_APPROVAL,
                waiting_approval_id=uuid.UUID(approval_id) if approval_id else None,
            )
        self.transcript.add(Evidence(seq=self._seq, tool=result.tool, content=result.content))

    async def _rehydrate(self) -> None:
        """Rebuild the transcript from Postgres after a resume.

        The database is the record; the in-memory transcript is a cache of it.
        That is what makes a pod eviction a hiccup rather than a lost
        investigation.
        """
        from cairn_core.repo import steps_for

        async with session() as sess:
            trajectory = await get_trajectory(sess, self.trajectory_id)
            steps = await steps_for(sess, self.trajectory_id)

        if trajectory:
            self.transcript.plan = trajectory.plan or {}
            self.sensitivity = Sensitivity.parse(trajectory.sensitivity)
            self.budget.spent_usd = Decimal(trajectory.cost_usd)
            self.budget.tokens = trajectory.tokens_local + trajectory.tokens_cloud

        seq = 0
        for step in steps:
            if step.kind == StepKind.TOOL_RESULT.value and step.output:
                seq += 1
                self.transcript.add(
                    Evidence(
                        seq=seq,
                        tool=step.tool_name or "tool",
                        content=step.output,
                        artifact_id=step.artifact_id,
                    )
                )
            elif step.kind == StepKind.COMPACTION.value and step.output:
                seq += 1
                self.transcript.add(
                    Evidence(
                        seq=seq,
                        tool="compacted",
                        content={"digest": step.output.get("digest", "")},
                        compacted=True,
                    )
                )
            elif step.kind == StepKind.TOOL_CALL.value:
                self.budget.tool_calls += 1
        self._seq = seq

        async with session() as sess:
            await transition(
                sess,
                self.trajectory_id,
                frm=TrajectoryState.WAITING_APPROVAL,
                to=TrajectoryState.EXECUTING,
                waiting_approval_id=None,
            )

    async def _fail(self, reason: str) -> LoopResult:
        async with session() as sess:
            trajectory = await get_trajectory(sess, self.trajectory_id)
            if trajectory:
                from cairn_core.repo import force_state

                await force_state(sess, self.trajectory_id, TrajectoryState.FAILED, answer=reason)
        TERMINAL.labels(state=TrajectoryState.FAILED.value).inc()
        await self._emit(EventType.ERROR, {"message": reason})
        await self._emit(EventType.DONE, {"state": TrajectoryState.FAILED.value})
        return LoopResult(
            state=TrajectoryState.FAILED,
            reason=reason,
            cost_usd=self.budget.spent_usd,
            iterations=self.budget.iterations,
        )

    async def _emit(self, kind: EventType, data: dict[str, Any]) -> None:
        self._event_seq += 1
        await self.bus.publish(
            Event(
                type=kind,
                trajectory_id=str(self.trajectory_id),
                seq=self._event_seq,
                data=data,
            )
        )


def _json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clip(content: dict[str, Any], limit: int = 200_000) -> dict[str, Any]:
    """Keep a step row from becoming a megabyte of JSONB."""
    raw = json.dumps(content, default=str)
    if len(raw) <= limit:
        return content
    return {
        "truncated": True,
        "original_bytes": len(raw),
        "preview": raw[:8_000],
        "note": "full payload is in the artifact store",
    }


def _format_answer(
    answer: dict[str, Any], *, partial_reason: str = "", rejected: bool = False
) -> str:
    lines = [answer.get("root_cause", "No root cause determined.")]

    if evidence := answer.get("evidence"):
        lines.append("\n**Evidence**")
        for item in evidence[:8]:
            if isinstance(item, dict):
                lines.append(f"- (step {item.get('step', '?')}) {item.get('fact', '')}")
            else:
                lines.append(f"- {item}")

    if unknowns := answer.get("unknowns"):
        lines.append("\n**Not determined**")
        lines.extend(f"- {u}" for u in unknowns[:6])

    if actions := answer.get("recommended_actions"):
        lines.append("\n**Suggested next steps**")
        lines.extend(f"- {a}" for a in actions[:6])

    if partial_reason:
        lines.append(
            f"\n_Stopped early: {partial_reason}. The findings above are what "
            "the evidence gathered so far supports._"
        )
    if rejected:
        lines.append(
            "\n_The reviewer was not satisfied this conclusion follows from the "
            "evidence; confidence has been lowered accordingly._"
        )
    return "\n".join(lines)


__all__ = ["AgentLoop", "LoopResult", "Suspended"]
