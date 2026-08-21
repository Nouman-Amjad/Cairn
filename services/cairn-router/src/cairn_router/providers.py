"""Model providers: local vLLM and the Anthropic API.

The router is the only component in the system that holds the Anthropic API
key. Not the orchestrator, not the MCP servers. If the orchestrator — the one
component with model-controlled behaviour — is compromised through prompt
injection, it cannot exfiltrate a key it never had.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from decimal import Decimal
from typing import Any

import httpx

from cairn_core.config import RouterSettings
from cairn_core.domain import LLMCall, LLMRequest, Route
from cairn_core.telemetry import get_logger

log = get_logger(__name__)


class ProviderError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a completion.

    Models wrap JSON in prose or fences no matter how firmly the prompt says
    not to. Failing the whole trajectory over a stray ```json is not a
    tradeoff worth defending.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class LocalProvider:
    """vLLM, OpenAI-compatible surface.

    Health and queue depth come from vLLM's own Prometheus metrics rather
    than from a synthetic probe: `num_requests_waiting` is the number the
    routing decision actually cares about.
    """

    def __init__(self, cfg: RouterSettings) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.vllm_url, timeout=cfg.vllm_timeout_s)
        self._metrics_client = httpx.AsyncClient(
            base_url=cfg.vllm_url.rsplit("/v1", 1)[0], timeout=2.0
        )
        self._healthy = False
        self._queue_depth = 0
        self._cache_usage = 0.0
        self._last_poll = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._metrics_client.aclose()

    async def poll(self) -> None:
        """Refresh health and queue depth. Called on a 2s ticker, not per
        request: the routing decision tolerates 2s of staleness and the
        metrics endpoint does not tolerate being hit per token."""
        try:
            resp = await self._metrics_client.get("/metrics")
            resp.raise_for_status()
            self._queue_depth = int(_scrape(resp.text, "vllm:num_requests_waiting"))
            self._cache_usage = _scrape(resp.text, "vllm:gpu_cache_usage_perc")
            self._healthy = True
        except Exception as exc:
            if self._healthy:
                log.warning("local_tier_unhealthy", error=str(exc))
            self._healthy = False
            self._queue_depth = 0
        self._last_poll = time.monotonic()

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def queue_depth(self) -> int:
        return self._queue_depth

    @property
    def cache_usage(self) -> float:
        return self._cache_usage

    async def complete(self, req: LLMRequest) -> tuple[str, int, int, int]:
        messages: list[dict[str, Any]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.extend({"role": m.role, "content": m.content} for m in req.messages)

        body: dict[str, Any] = {
            "model": self.cfg.vllm_model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if req.response_schema:
            # Grammar-constrained decoding. This is what makes an 8B reliable
            # at structured output; without it the local tier fails schema
            # validation often enough to be useless for tool arguments.
            body["guided_json"] = req.response_schema
            body["guided_decoding_backend"] = "xgrammar"

        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            self._healthy = False
            raise ProviderError(f"local inference failed: {exc}") from exc

        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return (
            data["choices"][0]["message"]["content"] or "",
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(cached),
        )


def _scrape(metrics: str, name: str) -> float:
    """Minimal Prometheus text-format read. Sums across label sets, which is
    what `sum(vllm:num_requests_waiting)` does in the KEDA trigger too."""
    total = 0.0
    found = False
    for line in metrics.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        rest = line[len(name) :]
        if rest and rest[0] not in " {":
            continue  # different metric sharing a prefix
        try:
            total += float(line.rsplit(None, 1)[1])
            found = True
        except (IndexError, ValueError):
            continue
    if not found:
        raise ValueError(f"metric {name} not present")
    return total


class CloudProvider:
    def __init__(self, cfg: RouterSettings) -> None:
        self.cfg = cfg
        self._client: Any | None = None
        # Per-pod sliding window. Deliberately not shared in Redis: a pod that
        # cannot reach the API should stop trying regardless of whether its
        # neighbours can, and a shared counter turns one bad pod into a
        # cluster-wide circuit trip.
        self._window: deque[tuple[float, bool]] = deque(maxlen=200)

    @property
    def configured(self) -> bool:
        return self.cfg.anthropic_api_key is not None

    def _sdk(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(
                api_key=self.cfg.anthropic_api_key.get_secret_value()
                if self.cfg.anthropic_api_key
                else "",
                base_url=self.cfg.anthropic_base_url,
                timeout=self.cfg.cloud_timeout_s,
                max_retries=2,
            )
        return self._client

    def error_rate(self, window_s: int) -> float:
        cutoff = time.monotonic() - window_s
        recent = [ok for at, ok in self._window if at >= cutoff]
        if len(recent) < 5:  # too few samples to trip a breaker on
            return 0.0
        return 1.0 - (sum(recent) / len(recent))

    def model_for(self, route: Route) -> str:
        return (
            self.cfg.cloud_model_cheap
            if route is Route.CLOUD_HAIKU
            else self.cfg.cloud_model_strong
        )

    async def complete(self, req: LLMRequest, route: Route) -> tuple[str, int, int, int]:
        system_blocks = []
        if req.system:
            # The system prompt plus tool definitions are byte-identical on
            # every call. Marking the block cacheable turns ~2.8k tokens of
            # prefill into a cache read at a tenth of the price.
            system_blocks.append(
                {
                    "type": "text",
                    "text": req.system,
                    "cache_control": {"type": "ephemeral"},
                }
            )

        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        if req.response_schema:
            # Prefill the opening brace: cheaper and more reliable than asking
            # nicely, and it costs one token.
            messages.append({"role": "assistant", "content": "{"})

        try:
            resp = await self._sdk().messages.create(
                model=self.model_for(route),
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                system=system_blocks or None,
                messages=messages,
            )
            self._window.append((time.monotonic(), True))
        except Exception as exc:
            self._window.append((time.monotonic(), False))
            raise ProviderError(f"cloud inference failed: {exc}") from exc

        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        if req.response_schema:
            text = "{" + text
        usage = resp.usage
        cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        return (
            text,
            int(usage.input_tokens) + cached,
            int(usage.output_tokens),
            cached,
        )


def build_call(
    *,
    text: str,
    model: str,
    route: Route,
    reason: str,
    tokens_in: int,
    tokens_out: int,
    cached: int,
    cost: Decimal,
    latency_ms: int,
) -> LLMCall:
    return LLMCall(
        text=text,
        model=model,
        route=route,
        route_reason=reason,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_read_tokens=cached,
        cost_usd=cost,
        latency_ms=latency_ms,
    )


def _self_check() -> None:
    sample = (
        "# HELP vllm:num_requests_waiting Waiting\n"
        'vllm:num_requests_waiting{model_name="cairn-local"} 7.0\n'
        'vllm:num_requests_running{model_name="cairn-local"} 3.0\n'
        'vllm:gpu_cache_usage_perc{model_name="cairn-local"} 0.42\n'
    )
    assert _scrape(sample, "vllm:num_requests_waiting") == 7.0
    assert _scrape(sample, "vllm:gpu_cache_usage_perc") == 0.42
    try:
        _scrape(sample, "vllm:absent")
        raise AssertionError("missing metric must raise, not read as zero")
    except ValueError:
        pass

    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('sure! {"a": [1,2]} hope that helps') == {"a": [1, 2]}
    assert _extract_json("no json here") is None
    assert _extract_json("[1,2,3]") is None  # arrays are not our schema shape
    print("providers self-check ok")


if __name__ == "__main__":
    _self_check()
