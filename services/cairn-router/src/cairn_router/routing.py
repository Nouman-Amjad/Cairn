"""The routing decision.

Kept as a pure function over a snapshot of the world so it can be tested
exhaustively without a GPU, a cloud key, or a clock. Everything that talks to
something lives in `providers.py`; everything that decides lives here.

The invariant the tests exist to protect: **every fallback path degrades
toward slower, never toward leakier.** Restricted work either runs locally,
queues for the local tier, or fails. There is no branch that sends it to a
third-party API, and adding one should require deleting a test that says so
in English.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cairn_core.domain import CHEAP_ONLY, Route, TaskClass
from cairn_core.sensitivity import Sensitivity


@dataclass(frozen=True, slots=True)
class Tier:
    LOCAL = "local"
    CLOUD = "cloud"
    LOCAL_FORCED = "local_forced"


@dataclass(frozen=True, slots=True)
class LocalState:
    healthy: bool
    queue_depth: int
    #: True while a scaled-to-zero GPU node is provisioning. Distinguished
    #: from plain unhealthy because the answer differs: an unhealthy tier is
    #: a failure, a cold one is a wait.
    cold_starting: bool = False


@dataclass(frozen=True, slots=True)
class CloudState:
    configured: bool
    error_rate_5m: float = 0.0


@dataclass(frozen=True, slots=True)
class Limits:
    local_queue_depth: int = 40
    queue_deadline_ms: int = 20_000
    cloud_error_rate_trip: float = 0.10
    #: Fraction of the per-trajectory cost cap at which cloud calls stop and
    #: everything falls back to local. Not 1.0: the last call must still fit.
    cost_backoff_fraction: float = 0.80


@dataclass(frozen=True, slots=True)
class Decision:
    route: Route
    reason: str

    @property
    def is_cloud(self) -> bool:
        return self.route in (Route.CLOUD_SONNET, Route.CLOUD_HAIKU)


def decide(
    *,
    task: TaskClass,
    sensitivity: Sensitivity,
    tier: str,
    local: LocalState,
    cloud: CloudState,
    limits: Limits,
    spent_usd: Decimal,
    max_cost_usd: Decimal,
    deadline_ms: int,
) -> Decision:
    """Implements the fallback table from the architecture doc, §6.3."""

    local_available = local.healthy and local.queue_depth < limits.local_queue_depth

    # ---- Restricted data: local or nothing. No exceptions, no flag. -------
    if sensitivity is Sensitivity.RESTRICTED:
        if local_available:
            return Decision(Route.LOCAL, "restricted:local")
        if local.healthy or local.cold_starting:
            if deadline_ms > limits.queue_deadline_ms:
                return Decision(Route.QUEUE_LOCAL, "restricted:queue_local")
            return Decision(Route.FAIL, "restricted:deadline_too_short_to_queue")
        return Decision(Route.FAIL, "restricted:local_tier_unavailable")

    # ---- Tasks pinned local regardless of load ---------------------------
    # Log summarization is here because raw log lines are the PII risk. It is
    # classified `internal` often enough that a spill would be legal, and
    # sending the highest-volume stream of raw production text to a vendor is
    # still the wrong default.
    if tier == Tier.LOCAL_FORCED:
        if local_available:
            return Decision(Route.LOCAL, f"{task}:local_forced")
        if deadline_ms > limits.queue_deadline_ms:
            return Decision(Route.QUEUE_LOCAL, f"{task}:local_forced_queue")
        return Decision(Route.FAIL, f"{task}:local_forced_unavailable")

    # ---- Local-preferred tasks -------------------------------------------
    if tier == Tier.LOCAL:
        if local_available:
            return Decision(Route.LOCAL, f"{task}:local")
        if not cloud.configured:
            return Decision(Route.QUEUE_LOCAL, f"{task}:no_cloud_configured")
        if task in CHEAP_ONLY:
            # Spilling a cheap task to Haiku costs cents and saves the query.
            # Spilling planning or synthesis would silently degrade the answer
            # a human reads, so those are not in CHEAP_ONLY.
            reason = "cold_start_spill" if local.cold_starting else "local_saturated_spill"
            return Decision(Route.CLOUD_HAIKU, f"{task}:{reason}")
        return Decision(Route.QUEUE_LOCAL, f"{task}:queue_local")

    # ---- Cloud-preferred tasks -------------------------------------------
    if not cloud.configured:
        return Decision(Route.LOCAL, f"{task}:cloud_unconfigured_fallback")
    if spent_usd > max_cost_usd * Decimal(str(limits.cost_backoff_fraction)):
        return Decision(Route.LOCAL, f"{task}:cost_backoff")
    if cloud.error_rate_5m > limits.cloud_error_rate_trip:
        return Decision(Route.LOCAL, f"{task}:cloud_error_rate")
    return Decision(Route.CLOUD_SONNET, f"{task}:cloud")


def _self_check() -> None:
    limits = Limits()
    healthy = LocalState(healthy=True, queue_depth=0)
    saturated = LocalState(healthy=True, queue_depth=99)
    dead = LocalState(healthy=False, queue_depth=0)
    cold = LocalState(healthy=False, queue_depth=0, cold_starting=True)
    cloud_ok = CloudState(configured=True)

    def call(**kw: object) -> Decision:
        base = dict(
            task=TaskClass.SYNTHESIS,
            sensitivity=Sensitivity.PUBLIC,
            tier=Tier.CLOUD,
            local=healthy,
            cloud=cloud_ok,
            limits=limits,
            spent_usd=Decimal("0"),
            max_cost_usd=Decimal("1.50"),
            deadline_ms=120_000,
        )
        base.update(kw)
        return decide(**base)  # type: ignore[arg-type]

    # The property the whole design rests on: restricted never leaves.
    for local_state in (healthy, saturated, dead, cold):
        for task in TaskClass:
            for tier in (Tier.LOCAL, Tier.CLOUD, Tier.LOCAL_FORCED):
                d = call(
                    task=task,
                    tier=tier,
                    sensitivity=Sensitivity.RESTRICTED,
                    local=local_state,
                )
                assert not d.is_cloud, f"restricted leaked to cloud: {task} {d}"

    assert call().route is Route.CLOUD_SONNET
    assert call(task=TaskClass.SUMMARIZATION, tier=Tier.LOCAL).route is Route.LOCAL

    # cheap task spills when local is saturated; expensive task queues instead
    assert (
        call(task=TaskClass.COMPACTION, tier=Tier.LOCAL, local=saturated).route is Route.CLOUD_HAIKU
    )
    assert (
        call(task=TaskClass.PLANNING, tier=Tier.LOCAL, local=saturated).route is Route.QUEUE_LOCAL
    )

    # forced-local never spills, even for a cheap task
    d = call(task=TaskClass.SUMMARIZATION, tier=Tier.LOCAL_FORCED, local=saturated)
    assert d.route is Route.QUEUE_LOCAL, d

    # cost backoff and cloud errors both fall back to local
    assert call(spent_usd=Decimal("1.40")).route is Route.LOCAL
    assert call(cloud=CloudState(configured=True, error_rate_5m=0.5)).route is Route.LOCAL
    assert call(cloud=CloudState(configured=False)).route is Route.LOCAL

    # restricted + short deadline fails honestly rather than waiting forever
    d = call(sensitivity=Sensitivity.RESTRICTED, local=dead, deadline_ms=1_000)
    assert d.route is Route.FAIL, d

    # restricted + cold start + time to spare: wait, and say so
    d = call(sensitivity=Sensitivity.RESTRICTED, local=cold, deadline_ms=100_000)
    assert d.route is Route.QUEUE_LOCAL, d
    print("routing self-check ok")


if __name__ == "__main__":
    _self_check()
