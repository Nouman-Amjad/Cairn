"""Fakes for driving the loop without a GPU, a cloud key or Redis.

Scripted rather than random: a test that asserts on agent behaviour needs the
model's decisions to be an input, not a variable.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from cairn_core.domain import LLMCall, Route, TaskClass, ToolResult


class FakeRouter:
    """Returns a queued response per task class, and records every call."""

    def __init__(self, script: dict[TaskClass, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict[str, Any]] = []
        self.cost_per_call = Decimal("0.01")
        #: Local calls are free by default, matching the price book. Set this
        #: to model the spill case, where local work is being served by the
        #: cheap cloud model and therefore does cost money.
        self.charge_all = False

    async def complete(self, **kwargs: Any) -> LLMCall:
        task: TaskClass = kwargs["task_class"]
        self.calls.append(
            {
                "task": task,
                "sensitivity": str(kwargs.get("sensitivity")),
                "schema": kwargs.get("response_schema"),
                "messages": kwargs.get("messages"),
            }
        )
        queue = self.script.get(task) or []
        payload = queue.pop(0) if queue else {"done": True}
        text = payload if isinstance(payload, str) else json.dumps(payload)
        local = task not in (TaskClass.PLANNING, TaskClass.SYNTHESIS)
        return LLMCall(
            text=text,
            model="cairn-local" if local else "claude-sonnet-4-5",
            route=Route.LOCAL if local else Route.CLOUD_SONNET,
            route_reason=f"{task}:{'local' if local else 'cloud'}",
            tokens_in=500,
            tokens_out=120,
            cost_usd=Decimal("0") if (local and not self.charge_all) else self.cost_per_call,
            latency_ms=42,
        )

    def tasks(self) -> list[TaskClass]:
        return [c["task"] for c in self.calls]


class FakeToolBox:
    """Canned tool results, keyed by tool name."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def render_catalogue(self, include_writes: bool = True) -> str:
        return "- query_logs(service): logs\n- rollback_deploy(service): gated"

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((name, args))
        result = self.results.get(name)
        if result is None:
            return ToolResult(tool=name, ok=False, error=f"no such tool {name}")
        return result

    async def connect(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [str(e.type) for e in self.events]

    def of(self, kind: str) -> list[Any]:
        return [e for e in self.events if str(e.type) == kind]


def logs_result(tokens: int = 500, sensitivity: str = "internal") -> ToolResult:
    return ToolResult(
        tool="query_logs",
        ok=True,
        content={
            "total_matched": 1247,
            "summary": "1,247 matches. 89% have status=503.",
            "facets": {"status": {"503": 1104}},
            "rows": [{"line": "connection timeout"}],
        },
        sensitivity=sensitivity,
        tokens=tokens,
    )


def pending_result(approval_id: str = "b" * 32) -> ToolResult:
    return ToolResult(
        tool="rollback_deploy",
        ok=True,
        content={
            "status": "pending_approval",
            "approval_id": approval_id,
            "message": "queued for human approval",
        },
        pending_approval_id=approval_id,
    )
