"""Event bus between the orchestrator and the gateway.

The loop runs on an orchestrator pod; the SSE connection lives on a gateway
pod; neither knows which pod the other is. Redis pub/sub bridges them.

Events are also written to a short-lived list so a client that reconnects
mid-investigation can replay what it missed. Without that, a laptop lid
closing during a 40-second answer loses the answer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import redis.asyncio as redis

from cairn_core.config import RedisSettings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

REPLAY_TTL_S = 900
REPLAY_MAX = 500


class EventType(StrEnum):
    STATE = "state"
    PLAN = "plan"
    STEP = "step"
    TOKEN = "token"  # noqa: S105 - an SSE event name, not a credential
    NOTICE = "notice"
    APPROVAL = "approval"
    ANSWER = "answer"
    ERROR = "error"
    DONE = "done"


@dataclass(slots=True)
class Event:
    type: EventType
    trajectory_id: str
    seq: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def __post_init__(self) -> None:
        self.at = self.at or datetime.now(UTC).isoformat()

    def to_json(self) -> str:
        payload = asdict(self)
        payload["type"] = str(self.type)
        return json.dumps(payload, default=str)

    @staticmethod
    def from_json(raw: str) -> Event:
        payload = json.loads(raw)
        return Event(
            type=EventType(payload["type"]),
            trajectory_id=payload["trajectory_id"],
            seq=payload.get("seq", 0),
            data=payload.get("data") or {},
            at=payload.get("at", ""),
        )


def _text(value: object) -> str:
    """Redis hands back bytes or str depending on decode_responses."""
    return value.decode() if isinstance(value, bytes) else str(value)


def channel(trajectory_id: str) -> str:
    return f"cairn:events:{trajectory_id}"


def replay_key(trajectory_id: str) -> str:
    return f"cairn:replay:{trajectory_id}"


class EventBus:
    def __init__(self, cfg: RedisSettings | None = None) -> None:
        cfg = cfg or RedisSettings()
        self._redis = redis.from_url(
            cfg.url, socket_timeout=cfg.socket_timeout_s, decode_responses=True
        )

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def publish(self, event: Event) -> None:
        payload = event.to_json()
        try:
            pipe = self._redis.pipeline()
            pipe.publish(channel(event.trajectory_id), payload)
            pipe.rpush(replay_key(event.trajectory_id), payload)
            pipe.ltrim(replay_key(event.trajectory_id), -REPLAY_MAX, -1)
            pipe.expire(replay_key(event.trajectory_id), REPLAY_TTL_S)
            await pipe.execute()
        except Exception as exc:
            # A dropped progress event must not kill an in-flight
            # investigation. The trajectory in Postgres remains the record.
            log.warning("event_publish_failed", type=str(event.type), error=str(exc))

    async def replay(self, trajectory_id: str, after_seq: int = -1) -> list[Event]:
        try:
            raw = await self._redis.lrange(replay_key(trajectory_id), 0, -1)
        except Exception as exc:
            log.warning("event_replay_failed", error=str(exc))
            return []
        events = [Event.from_json(_text(item)) for item in raw]
        return [e for e in events if e.seq > after_seq]

    async def subscribe(self, trajectory_id: str):  # type: ignore[no-untyped-def]
        """Async iterator of events for one trajectory."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel(trajectory_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message["data"]
                yield Event.from_json(raw.decode() if isinstance(raw, bytes) else str(raw))
        finally:
            await pubsub.unsubscribe(channel(trajectory_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]
