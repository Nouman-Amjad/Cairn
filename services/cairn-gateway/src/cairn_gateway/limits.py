"""Rate limiting and per-user cost budgets.

Both live in Redis because both must hold across gateway replicas: a limit
that resets when a request lands on a different pod is decoration.

The cost budget is the defence against denial-of-wallet (T8). An attacker —
or an enthusiastic engineer with a loop — cannot spend more than the daily
allowance, and the global breaker stops the whole system at 150% of forecast
regardless of who is spending.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import redis.asyncio as redis

from cairn_core.config import RedisSettings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Verdict:
    allowed: bool
    reason: str = ""
    retry_after_s: int = 0


#: Sliding-window counter in one round trip. Not a token bucket: the extra
#: precision is not worth a Lua script here, and a per-minute window is what
#: the limit is actually expressed in.
_RATE_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local current = redis.call('INCR', key)
if current == 1 then
  redis.call('EXPIRE', key, window)
end
local ttl = redis.call('TTL', key)
if current > limit then
  return {0, ttl}
end
return {1, ttl}
"""


class Limiter:
    def __init__(self, cfg: RedisSettings | None = None) -> None:
        cfg = cfg or RedisSettings()
        self._redis = redis.from_url(
            cfg.url, socket_timeout=cfg.socket_timeout_s, decode_responses=True
        )
        self._rate = self._redis.register_script(_RATE_SCRIPT)

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def check_rate(self, user: str, per_minute: int) -> Verdict:
        try:
            allowed, ttl = await self._rate(
                keys=[f"cairn:rate:{user}:{int(time.time() // 60)}"],
                args=[per_minute, 90],
            )
        except Exception as exc:
            # Fail open on rate limiting specifically. A Redis outage should
            # not take incident response down; the cost cap below is the
            # control that actually bounds damage, and it fails closed.
            log.warning("rate_limit_unavailable", error=str(exc))
            return Verdict(allowed=True, reason="limiter unavailable")

        if not allowed:
            return Verdict(
                allowed=False,
                reason=f"rate limit of {per_minute}/min exceeded",
                retry_after_s=max(1, int(ttl)),
            )
        return Verdict(allowed=True)

    async def check_budget(self, user: str, daily_limit_usd: float) -> Verdict:
        key = _budget_key(user)
        try:
            spent = Decimal(_text(await self._redis.get(key)))
        except Exception as exc:
            # Fails closed: if we cannot tell how much someone has spent, we
            # do not let them spend more.
            log.error("budget_check_unavailable", error=str(exc))
            return Verdict(allowed=False, reason="cost budget unavailable")

        if spent >= Decimal(str(daily_limit_usd)):
            return Verdict(
                allowed=False,
                reason=(
                    f"daily cost budget of ${daily_limit_usd:.2f} reached "
                    f"(${spent:.2f} spent). It resets at midnight UTC."
                ),
            )
        return Verdict(allowed=True)

    async def record_spend(self, user: str, amount: Decimal) -> None:
        """Charge a user, and the global counter the breaker watches."""
        if amount <= 0:
            return
        try:
            pipe = self._redis.pipeline()
            pipe.incrbyfloat(_budget_key(user), float(amount))
            pipe.expire(_budget_key(user), 172_800)  # yesterday stays inspectable
            pipe.incrbyfloat(_global_key(), float(amount))
            pipe.expire(_global_key(), 172_800)
            await pipe.execute()
        except Exception as exc:
            log.warning("spend_record_failed", user=user, error=str(exc))

    async def spent_today(self, user: str) -> Decimal:
        try:
            return Decimal(_text(await self._redis.get(_budget_key(user))))
        except Exception:
            return Decimal("0")

    async def spent_globally_today(self) -> Decimal:
        try:
            return Decimal(_text(await self._redis.get(_global_key())))
        except Exception:
            return Decimal("0")

    async def check_circuit(self, daily_forecast_usd: float, trip_ratio: float) -> Verdict:
        """The global circuit breaker.

        Per-user caps bound one enthusiastic engineer. They do not bound a
        bug that makes every query cost ten times what it should, because
        every user stays individually under their cap while the total goes
        somewhere nobody authorised.

        Tripping does not stop the product: it forces every call onto the
        local tier, where the marginal cost is zero, and pages someone. That
        is degraded, not down — which is the right shape for a cost control.
        """
        if daily_forecast_usd <= 0:
            return Verdict(allowed=True)

        ceiling = Decimal(str(daily_forecast_usd)) * Decimal(str(trip_ratio))
        try:
            spent = await self.spent_globally_today()
        except Exception as exc:
            log.error("circuit_check_unavailable", error=str(exc))
            return Verdict(allowed=True, reason="breaker state unavailable")

        if spent >= ceiling:
            log.error(
                "cost_circuit_breaker_tripped",
                spent_usd=str(spent),
                ceiling_usd=str(ceiling),
                forecast_usd=daily_forecast_usd,
            )
            return Verdict(
                allowed=False,
                reason=(
                    f"global spend ${spent:.2f} has passed {trip_ratio:.0%} of the "
                    f"${daily_forecast_usd:.2f} daily forecast. Cloud inference is "
                    "disabled; queries run on the local tier only until midnight UTC."
                ),
            )
        return Verdict(allowed=True)

    async def healthy(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


def _text(value: object) -> str:
    """Redis returns bytes or str depending on decode_responses."""
    if value is None:
        return "0"
    return value.decode() if isinstance(value, bytes) else str(value)


def _budget_key(user: str) -> str:
    return f"cairn:budget:{user}:{datetime.now(UTC):%Y-%m-%d}"


def _global_key() -> str:
    return f"cairn:budget:_global:{datetime.now(UTC):%Y-%m-%d}"
