"""Model sources for an eval run.

Four modes, and the distinction between them matters for how much you should
believe a number:

* ``live``      — the real router. Honest, slow, costs money, not reproducible.
* ``record``    — live, and every response is written to a fixture file.
* ``replay``    — fixtures only. Deterministic; this is what gates CI.
* ``heuristic`` — a scripted stand-in that reads the fake world and follows
  simple rules. It exercises the *harness* end to end without a model.

A heuristic-mode score says nothing about agent quality. It is here so the
pipeline, the judge and the gate are themselves tested, and so a fresh clone
can run `make eval` before anyone has recorded anything. Any accuracy figure
published from it should say so.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from cairn_core.domain import LLMCall, Route, TaskClass
from cairn_core.telemetry import get_logger

log = get_logger(__name__)


@dataclass
class ReplayRouter:
    """Plays back recorded responses, optionally recording new ones."""

    fixture_path: Path
    incident_id: str
    live: Any | None = None
    record: bool = False
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _data: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.fixture_path.exists():
            self._data = json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def _key(self, task: TaskClass) -> str:
        index = self._counts.get(task.value, 0)
        self._counts[task.value] = index + 1
        return f"{self.incident_id}|{task.value}|{index}"

    async def complete(self, **kwargs: Any) -> LLMCall:
        task: TaskClass = kwargs["task_class"]
        key = self._key(task)

        if not self.record and key in self._data:
            return _rehydrate(self._data[key])

        if self.live is None:
            raise KeyError(
                f"no recording for {key}. Run the suite with --mode record "
                "against a live router first."
            )

        call: LLMCall = await self.live.complete(**kwargs)
        self._data[key] = {
            "text": call.text,
            "model": call.model,
            "route": call.route.value,
            "route_reason": call.route_reason,
            "tokens_in": call.tokens_in,
            "tokens_out": call.tokens_out,
            "cost_usd": str(call.cost_usd),
            "latency_ms": call.latency_ms,
        }
        return call

    def save(self) -> None:
        if not self.record:
            return
        self.fixture_path.parent.mkdir(parents=True, exist_ok=True)
        merged = {}
        if self.fixture_path.exists():
            merged = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        merged.update(self._data)
        self.fixture_path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")


def _rehydrate(payload: dict[str, Any]) -> LLMCall:
    return LLMCall(
        text=payload["text"],
        model=payload["model"],
        route=Route(payload["route"]),
        route_reason=payload["route_reason"],
        tokens_in=payload["tokens_in"],
        tokens_out=payload["tokens_out"],
        cost_usd=Decimal(payload["cost_usd"]),
        latency_ms=payload["latency_ms"],
    )


# ---------------------------------------------------------------- heuristic

#: Signal -> mechanism. Ordered most-specific first, because two scenarios
#: share the "QueuePool limit" symptom with different causes and only the
#: replica-downscale evidence distinguishes them.
#:
#: `{match}` is filled from the first capture group; `{service}` from the
#: dominant service in the retrieved evidence. Keyword rules, not reasoning —
#: this stand-in exists to move data through the pipeline, not to be good.
_SIGNALS: list[tuple[str, str]] = [
    (
        r"rds_replica_scale_down|rds_replica_count",
        "the RDS read replica downscale left {service}'s database connection pool "
        "undersized, so connection wait exceeded the acquire timeout",
    ),
    (
        r"queuepool limit|connection is not available|db_pool_wait",
        "the deploy raised concurrency without raising maxPoolSize, exhausting "
        "{service}'s database connection pool and queueing requests behind the "
        "acquire timeout",
    ),
    (
        r"no space left on device|volume_full",
        "{service}'s persistent volume filled to capacity, so writes fail with ENOSPC",
    ),
    (
        r"outofmemoryerror|oomkilled|oom_kill",
        "a memory leak in {service} drove the working set past the container limit "
        "and the pod was OOMKilled",
    ),
    (
        r"circuit breaker open for ([\w-]+)|upstream connect error to ([\w-]+)",
        "an outage in the upstream dependency {match}, which {service} surfaced as "
        "errors after its circuit breaker opened",
    ),
    (
        r"coredns_config_reload|no such host",
        "a coredns config reload broke DNS resolution, so {service} lookups fail intermittently",
    ),
    (
        r"config reload|nullpointerexception in pricingresolver",
        "a config reload enabled a feature flag that exercised a null-unsafe path "
        "in {service}; no code was deployed",
    ),
    (
        r"cfs_throttled|node_saturation",
        "another workload saturated the node, so {service} was CFS throttled and denied CPU",
    ),
    (
        r"x509|certificate has expired|tls_handshake_failure",
        "an expired TLS certificate broke every outbound handshake from {service}",
    ),
    (
        r"consumer_rebalance|nobrokersavailable",
        "the {service} consumer group is thrashing in a rebalance loop, so committed "
        "offsets stall and lag grows without bound",
    ),
]

#: Where the causal service name is read from in retrieved evidence.
_SERVICE_HINTS = (
    re.compile(r"app=([a-z][a-z0-9-]{2,})"),
    re.compile(r"\"pod\":\s*\"([a-z][a-z0-9-]{2,}?)-[0-9a-f]{4}"),
    re.compile(r"\"service\":\s*\"([a-z][a-z0-9-]{2,})\""),
)

_TOOL_ORDER = [
    "get_deploy_timeline",
    "query_metrics",
    "query_logs",
    "search_runbooks",
]


@dataclass
class HeuristicRouter:
    """A scripted stand-in. Reads the evidence, applies keyword rules.

    Not a model, and not pretending to be one — see the module docstring.
    """

    world: Any
    calls: list[TaskClass] = field(default_factory=list)
    _step: int = 0

    async def complete(self, **kwargs: Any) -> LLMCall:
        task: TaskClass = kwargs["task_class"]
        self.calls.append(task)
        prompt = "\n".join(m["content"] for m in kwargs.get("messages", []))

        if task is TaskClass.PLANNING:
            payload: Any = {
                "steps": [
                    {"goal": "check deploys", "tools": ["get_deploy_timeline"], "why": "cheap"},
                    {"goal": "quantify", "tools": ["query_metrics"], "why": "blast radius"},
                    {"goal": "read errors", "tools": ["query_logs"], "why": "specifics"},
                ],
                "hypotheses": ["a recent change"],
            }
        elif task is TaskClass.ARG_EXTRACTION:
            payload = self._next_tool()
        elif task is TaskClass.SYNTHESIS:
            payload = self._answer(prompt)
        elif task is TaskClass.CRITIQUE:
            payload = {"verdict": "accept", "reasons": []}
        else:
            payload = "summary of the evidence"

        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMCall(
            text=text,
            model="heuristic",
            route=Route.LOCAL,
            route_reason=f"{task}:heuristic",
            tokens_in=400,
            tokens_out=120,
            cost_usd=Decimal("0"),
            latency_ms=1,
        )

    def _next_tool(self) -> dict[str, Any]:
        if self._step >= len(_TOOL_ORDER):
            return {"done": True}
        tool = _TOOL_ORDER[self._step]
        self._step += 1
        return {
            "tool": tool,
            "args": {"service": self.world.incident.service, "query": "up"},
        }

    def _service(self, evidence: str) -> str:
        """Most-mentioned service in the retrieved evidence.

        Read from the evidence, never from the scenario's ground truth. A
        stand-in that peeks at the answer makes the whole pipeline test
        vacuous — everything would pass and prove nothing.
        """
        seen: Counter[str] = Counter()
        for pattern in _SERVICE_HINTS:
            seen.update(pattern.findall(evidence))
        return seen.most_common(1)[0][0] if seen else "the affected service"

    def _answer(self, evidence: str) -> dict[str, Any]:
        haystack = evidence.lower()
        service = self._service(haystack)

        for pattern, phrase in _SIGNALS:
            match = re.search(pattern, haystack)
            if match:
                found = next((g for g in match.groups() if g), "")
                return {
                    "root_cause": phrase.format(match=found, service=service),
                    "confidence": 0.8,
                    "evidence": [{"step": 1, "fact": match.group(0)[:120]}],
                    "unknowns": [],
                    "recommended_actions": [],
                }

        return {
            "root_cause": (
                "I could not determine a cause: no evidence in the retrieved "
                "metrics, logs or deploy timeline explains the reported symptom."
            ),
            "confidence": 0.2,
            "evidence": [],
            "unknowns": ["nothing anomalous appeared in metrics or logs"],
            "recommended_actions": [],
        }
