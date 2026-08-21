"""Client the orchestrator uses to reach the router.

Lives in the router package so the request contract has exactly one
definition. A second copy in the orchestrator would drift within a month.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from cairn_core.config import RouterSettings
from cairn_core.domain import Budget, LLMCall, Route, TaskClass
from cairn_core.sensitivity import Sensitivity


class RouterUnavailable(RuntimeError):
    """The router refused or could not serve the call. Carries the reason so
    the orchestrator can put an honest message on the SSE stream instead of a
    stack trace."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class RouterClient:
    def __init__(self, cfg: RouterSettings | None = None, token: str = "") -> None:
        cfg = cfg or RouterSettings()
        self._client = httpx.AsyncClient(
            base_url=cfg.url,
            timeout=httpx.Timeout(cfg.cloud_timeout_s + 30, connect=5.0),
            headers={"authorization": f"Bearer {token}"} if token else {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        task_class: TaskClass,
        messages: list[dict[str, str]],
        system: str | None = None,
        sensitivity: Sensitivity = Sensitivity.PUBLIC,
        budget: Budget | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
        trajectory_id: str | None = None,
        token: str | None = None,
    ) -> LLMCall:
        budget = budget or Budget()
        payload = {
            "task_class": task_class.value,
            "sensitivity": str(sensitivity),
            "messages": messages,
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_schema": response_schema,
            "trajectory_id": trajectory_id,
            "spent_usd": str(budget.spent_usd),
            "max_cost_usd": str(budget.max_cost_usd),
            "deadline_ms": budget.deadline_ms,
        }
        headers = {"authorization": f"Bearer {token}"} if token else None
        try:
            resp = await self._client.post("/v1/complete", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RouterUnavailable(f"router unreachable: {exc}", retryable=True) from exc

        if resp.status_code >= 400:
            detail = _detail(resp)
            # 503 means the routing policy refused (restricted data, no local
            # tier). Retrying will not help and must not silently re-route.
            raise RouterUnavailable(detail, retryable=resp.status_code in (502, 504))

        data = resp.json()
        return LLMCall(
            text=data["text"],
            model=data["model"],
            route=Route(data["route"]),
            route_reason=data["route_reason"],
            tokens_in=data["tokens_in"],
            tokens_out=data["tokens_out"],
            cached_read_tokens=data["cached_read_tokens"],
            cost_usd=Decimal(str(data["cost_usd"])),
            latency_ms=data["latency_ms"],
        )


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except json.JSONDecodeError:
        return resp.text[:500]
