"""Core value objects shared across services.

The enums here are also the CHECK-constraint vocabulary in the database, so
adding a member is a migration, not just a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class TrajectoryState(StrEnum):
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SYNTHESIZING = "SYNTHESIZING"
    CRITIQUING = "CRITIQUING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {
        TrajectoryState.COMPLETE,
        TrajectoryState.PARTIAL,
        TrajectoryState.FAILED,
        TrajectoryState.ABANDONED,
    }
)

# Legal transitions, straight out of the state diagram in the architecture doc.
# The orchestrator asserts against this; an illegal transition is a bug, not a
# user error, so it raises rather than degrading.
TRANSITIONS: dict[TrajectoryState, frozenset[TrajectoryState]] = {
    TrajectoryState.PLANNING: frozenset({TrajectoryState.EXECUTING, TrajectoryState.FAILED}),
    TrajectoryState.EXECUTING: frozenset(
        {
            TrajectoryState.EXECUTING,
            TrajectoryState.WAITING_APPROVAL,
            TrajectoryState.SYNTHESIZING,
            TrajectoryState.PARTIAL,
            TrajectoryState.FAILED,
        }
    ),
    TrajectoryState.WAITING_APPROVAL: frozenset(
        {TrajectoryState.EXECUTING, TrajectoryState.ABANDONED}
    ),
    TrajectoryState.SYNTHESIZING: frozenset({TrajectoryState.CRITIQUING, TrajectoryState.FAILED}),
    TrajectoryState.CRITIQUING: frozenset(
        {
            TrajectoryState.SYNTHESIZING,
            TrajectoryState.COMPLETE,
            TrajectoryState.PARTIAL,
        }
    ),
}


def check_transition(src: TrajectoryState, dst: TrajectoryState) -> None:
    if dst not in TRANSITIONS.get(src, frozenset()):
        raise InvalidTransition(f"{src} -> {dst} is not a legal trajectory transition")


class InvalidTransition(RuntimeError):
    pass


class TaskClass(StrEnum):
    """What the LLM is being asked to do. Drives routing."""

    CLASSIFICATION = "classification"
    SUMMARIZATION = "summarization"
    ARG_EXTRACTION = "arg_extraction"
    COMPACTION = "compaction"
    CRITIQUE = "critique"
    PLANNING = "planning"
    SYNTHESIS = "synthesis"


#: Task classes cheap and low-stakes enough to spill to a small cloud model
#: when the local tier is saturated. Planning and synthesis are deliberately
#: absent: spilling those to Haiku silently degrades the answer a human reads.
CHEAP_ONLY: frozenset[TaskClass] = frozenset(
    {
        TaskClass.CLASSIFICATION,
        TaskClass.SUMMARIZATION,
        TaskClass.ARG_EXTRACTION,
        TaskClass.COMPACTION,
        TaskClass.CRITIQUE,
    }
)


class Route(StrEnum):
    LOCAL = "local"
    QUEUE_LOCAL = "queue_local"
    CLOUD_SONNET = "cloud_sonnet"
    CLOUD_HAIKU = "cloud_haiku"
    FAIL = "fail"


class StepKind(StrEnum):
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    LLM_CALL = "llm_call"
    CRITIQUE = "critique"
    COMPACTION = "compaction"


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(slots=True)
class Budget:
    """Per-trajectory resource envelope.

    Defaults are the numbers from the architecture doc. They are overridable
    per request but clamped to these as maxima by the gateway, because a
    caller that can raise its own cost ceiling does not have a ceiling.
    """

    max_iterations: int = 12
    max_tool_calls: int = 20
    max_total_tokens: int = 400_000
    wall_clock_timeout_s: float = 180.0
    max_cost_usd: Decimal = Decimal("1.50")
    context_high_water: float = 0.60
    critic_retries: int = 1

    # Consumption, mutated as the loop runs.
    iterations: int = 0
    tool_calls: int = 0
    tokens: int = 0
    spent_usd: Decimal = Decimal("0")
    started_monotonic: float = 0.0

    def clamp_to(self, ceiling: Budget) -> Budget:
        return Budget(
            max_iterations=min(self.max_iterations, ceiling.max_iterations),
            max_tool_calls=min(self.max_tool_calls, ceiling.max_tool_calls),
            max_total_tokens=min(self.max_total_tokens, ceiling.max_total_tokens),
            wall_clock_timeout_s=min(self.wall_clock_timeout_s, ceiling.wall_clock_timeout_s),
            max_cost_usd=min(self.max_cost_usd, ceiling.max_cost_usd),
            context_high_water=min(self.context_high_water, ceiling.context_high_water),
            critic_retries=min(self.critic_retries, ceiling.critic_retries),
        )

    def exhausted(self, now_monotonic: float) -> str | None:
        """Return the name of the first exhausted budget, or None."""
        if self.iterations >= self.max_iterations:
            return "max_iterations"
        if self.tool_calls >= self.max_tool_calls:
            return "max_tool_calls"
        if self.tokens >= self.max_total_tokens:
            return "max_total_tokens"
        if self.spent_usd >= self.max_cost_usd:
            return "max_cost_usd"
        if (
            self.started_monotonic
            and now_monotonic - self.started_monotonic >= self.wall_clock_timeout_s
        ):
            return "wall_clock_timeout"
        return None

    @property
    def deadline_ms(self) -> int:
        """Milliseconds of wall clock left. Used by the router to decide
        whether queueing for the local tier is viable."""
        import time

        if not self.started_monotonic:
            return int(self.wall_clock_timeout_s * 1000)
        remaining = self.wall_clock_timeout_s - (time.monotonic() - self.started_monotonic)
        return max(0, int(remaining * 1000))


class Message(BaseModel):
    role: str
    content: str


class LLMRequest(BaseModel):
    task_class: TaskClass
    messages: list[Message]
    system: str | None = None
    max_tokens: int = 2048
    temperature: float = 0.0
    response_schema: dict[str, object] | None = None
    trajectory_id: str | None = None
    prompt_version: str | None = None


class LLMCall(BaseModel):
    """Result of one model invocation, with everything needed for cost
    attribution and for the trajectory step row."""

    text: str
    model: str
    route: Route
    route_reason: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached_read_tokens: int = 0
    cost_usd: Decimal = Field(default=Decimal("0"))
    latency_ms: int = 0


class ToolResult(BaseModel):
    """Normalised MCP tool result as the orchestrator sees it."""

    tool: str
    ok: bool = True
    content: dict[str, object] = Field(default_factory=dict)
    artifact_id: str | None = None
    sensitivity: str = "public"
    tokens: int = 0
    pending_approval_id: str | None = None
    error: str | None = None


@dataclass(slots=True)
class UserContext:
    sub: str
    email: str
    groups: list[str] = field(default_factory=list)
    team: str | None = None
