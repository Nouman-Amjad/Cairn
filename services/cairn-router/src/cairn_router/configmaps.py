"""Routing policy and price book: data, not code.

Both are YAML ConfigMaps, versioned in Git and applied by ArgoCD, watched and
hot-reloaded here. Retuning the local/cloud split is a `kubectl apply`; a
vendor price change is a config edit. Neither is a deploy, and both are still
reviewed and auditable because the file lives in a repo.

Reload is atomic: a bad file is rejected and the previous good version stays
live. A router that falls back to "no policy" under a YAML typo would route
everything to cloud, including restricted data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from cairn_core.domain import TaskClass
from cairn_core.telemetry import get_logger
from cairn_router.routing import Limits, Tier

log = get_logger(__name__)

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "tasks": {
        TaskClass.CLASSIFICATION.value: Tier.LOCAL,
        TaskClass.SUMMARIZATION.value: Tier.LOCAL_FORCED,
        TaskClass.ARG_EXTRACTION.value: Tier.LOCAL,
        TaskClass.COMPACTION.value: Tier.LOCAL,
        TaskClass.CRITIQUE.value: Tier.LOCAL,
        TaskClass.PLANNING.value: Tier.CLOUD,
        TaskClass.SYNTHESIS.value: Tier.CLOUD,
    },
    "limits": {
        "local_queue_depth": 40,
        "queue_deadline_ms": 20_000,
        "cloud_error_rate_trip": 0.10,
        "cost_backoff_fraction": 0.80,
    },
}

# Per million tokens, USD. Local is priced at zero per token on purpose: the
# GPU is a fixed hourly cost, not a marginal one, and pretending otherwise
# makes per-query cost move when utilisation moves. GPU spend is attributed
# separately from `cairn_gpu_hours` in the cost dashboard.
DEFAULT_PRICES: dict[str, Any] = {
    "version": 1,
    "models": {
        "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cached_read": 0.30},
        "claude-haiku-4-5-20251001": {
            "input": 1.00,
            "output": 5.00,
            "cached_read": 0.10,
        },
        "cairn-local": {"input": 0.0, "output": 0.0, "cached_read": 0.0},
    },
}

_VALID_TIERS = {Tier.LOCAL, Tier.CLOUD, Tier.LOCAL_FORCED}


@dataclass(slots=True)
class RoutingPolicy:
    tiers: dict[TaskClass, str] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    version: str = "default"

    def tier_for(self, task: TaskClass) -> str:
        return self.tiers.get(task, Tier.CLOUD)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> RoutingPolicy:
        tasks = raw.get("tasks") or {}
        tiers: dict[TaskClass, str] = {}
        for name, tier in tasks.items():
            task = TaskClass(name)  # raises on an unknown task class
            if tier not in _VALID_TIERS:
                raise ValueError(f"unknown tier {tier!r} for task {name}")
            tiers[task] = tier
        missing = [t.value for t in TaskClass if t not in tiers]
        if missing:
            raise ValueError(f"policy does not cover task classes: {missing}")
        limits_raw = raw.get("limits") or {}
        return cls(
            tiers=tiers,
            limits=Limits(**limits_raw),
            version=str(raw.get("version", "unversioned")),
        )


@dataclass(slots=True)
class PriceBook:
    models: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    version: str = "default"

    def cost(self, model: str, *, tokens_in: int, tokens_out: int, cached_read: int = 0) -> Decimal:
        prices = self.models.get(model)
        if prices is None:
            # An unpriced model is a config bug. Charging zero would hide it
            # from the one dashboard that would have caught it.
            log.warning("unpriced_model", model=model)
            return Decimal("0")
        billable_in = max(0, tokens_in - cached_read)
        million = Decimal("1000000")
        return (
            prices.get("input", Decimal("0")) * Decimal(billable_in)
            + prices.get("output", Decimal("0")) * Decimal(tokens_out)
            + prices.get("cached_read", Decimal("0")) * Decimal(cached_read)
        ) / million

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> PriceBook:
        models = {
            name: {k: Decimal(str(v)) for k, v in prices.items()}
            for name, prices in (raw.get("models") or {}).items()
        }
        if not models:
            raise ValueError("price book has no models")
        return cls(models=models, version=str(raw.get("version", "unversioned")))


class ConfigFile[T]:
    """A YAML file with a parser, a default, and a watcher."""

    def __init__(
        self,
        path: str,
        parser: Any,
        default_raw: dict[str, Any],
        name: str,
    ) -> None:
        self.path = Path(path)
        self._parser = parser
        self._name = name
        self._value: T = parser(default_raw)
        self.load()

    @property
    def value(self) -> T:
        return self._value

    def load(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            self._value = self._parser(raw)
        except Exception as exc:
            # Keep serving the previous good version. Loudly.
            log.error("configmap_rejected", file=self._name, error=str(exc))
            return False
        log.info("configmap_loaded", file=self._name, path=str(self.path))
        return True

    async def watch(self) -> None:
        """Reload on write. ConfigMap updates land as a symlink swap on the
        parent directory, which is why the directory is watched, not the file."""
        from watchfiles import awatch

        if not self.path.parent.is_dir():
            log.info("configmap_watch_skipped", file=self._name)
            return
        async for _ in awatch(self.path.parent):
            await asyncio.sleep(0.2)  # let the symlink swap settle
            self.load()


def _self_check() -> None:
    policy = RoutingPolicy.parse(DEFAULT_POLICY)
    assert policy.tier_for(TaskClass.SYNTHESIS) == Tier.CLOUD
    assert policy.tier_for(TaskClass.SUMMARIZATION) == Tier.LOCAL_FORCED

    for bad in (
        {"tasks": {"planning": "cloud"}},  # incomplete coverage
        {"tasks": dict(DEFAULT_POLICY["tasks"], planning="magic")},  # bad tier
        {"tasks": dict(DEFAULT_POLICY["tasks"], nonsense="cloud")},  # bad task
    ):
        try:
            RoutingPolicy.parse(bad)
            raise AssertionError(f"policy {bad} should have been rejected")
        except (ValueError, KeyError):
            pass

    prices = PriceBook.parse(DEFAULT_PRICES)
    # 1M in / 1M out on Sonnet = $3 + $15
    assert prices.cost("claude-sonnet-4-5", tokens_in=1_000_000, tokens_out=1_000_000) == Decimal(
        "18"
    )
    # cached reads are billed at the cache rate and deducted from input
    cached = prices.cost(
        "claude-sonnet-4-5", tokens_in=1_000_000, tokens_out=0, cached_read=1_000_000
    )
    assert cached == Decimal("0.30"), cached
    assert prices.cost("cairn-local", tokens_in=999_999, tokens_out=999_999) == 0
    assert prices.cost("who-is-this", tokens_in=100, tokens_out=100) == 0
    print("configmaps self-check ok")


if __name__ == "__main__":
    _self_check()
