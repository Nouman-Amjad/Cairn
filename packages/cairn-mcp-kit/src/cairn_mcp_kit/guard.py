"""Per-tool authorization.

Every tool function is wrapped. The wrapper re-checks the caller's claims
against OPA *for this tool with these arguments*, every call, with no cache.

This is where a prompt injection dies. The model can be talked into asking for
anything; what it cannot do is change whose token the request carries or what
that person is allowed to do. A denied call comes back as a normal tool result
so the agent can reason about the refusal instead of retrying it forever.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec

from prometheus_client import Counter, Histogram

from cairn_core.auth import AuthError
from cairn_core.policy import Denied, PolicyClient
from cairn_core.telemetry import get_logger, span
from cairn_mcp_kit.identity import current_claims

log = get_logger(__name__)

P = ParamSpec("P")

TOOL_CALLS = Counter("cairn_tool_calls_total", "MCP tool calls", ["tool", "outcome"])
TOOL_LATENCY = Histogram(
    "cairn_tool_latency_seconds",
    "MCP tool latency",
    ["tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
)

_policy: PolicyClient | None = None


def policy() -> PolicyClient:
    global _policy
    if _policy is None:
        _policy = PolicyClient()
    return _policy


def guarded(
    tool_name: str, *, scope: str = "tools:read"
) -> Callable[[Callable[P, Awaitable[dict[str, Any]]]], Callable[P, Awaitable[dict[str, Any]]]]:
    def decorate(
        fn: Callable[P, Awaitable[dict[str, Any]]],
    ) -> Callable[P, Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
            started = time.perf_counter()
            with span("mcp.call", **{"cairn.tool": tool_name}) as sp:
                try:
                    claims = current_claims()
                except AuthError as exc:
                    TOOL_CALLS.labels(tool=tool_name, outcome="unauthenticated").inc()
                    return {"tool": tool_name, "ok": False, "error": str(exc)}

                sp.set_attribute("cairn.user", claims.sub)
                if not claims.has(scope):
                    TOOL_CALLS.labels(tool=tool_name, outcome="denied").inc()
                    return _denial(tool_name, f"caller lacks {scope}")

                try:
                    await policy().enforce(tool=tool_name, args=_safe_args(kwargs), claims=claims)
                except Denied as exc:
                    TOOL_CALLS.labels(tool=tool_name, outcome="denied").inc()
                    log.warning("tool_denied", tool=tool_name, user=claims.sub)
                    return _denial(tool_name, exc.reason)

                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    TOOL_CALLS.labels(tool=tool_name, outcome="error").inc()
                    log.exception("tool_failed", tool=tool_name)
                    return {
                        "tool": tool_name,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                finally:
                    TOOL_LATENCY.labels(tool=tool_name).observe(time.perf_counter() - started)

            TOOL_CALLS.labels(tool=tool_name, outcome="ok").inc()
            return result

        return wrapper

    return decorate


def _denial(tool: str, reason: str) -> dict[str, Any]:
    return {
        "tool": tool,
        "ok": False,
        "kind": "denied",
        "error": f"Not permitted: {reason}. This is a policy decision about the "
        "requesting user, not a transient failure. Do not retry; if this "
        "action is needed, say so in your answer so a human can run it.",
    }


def _safe_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Arguments as OPA sees them: scalars and short strings only.

    A 40k-token log filter in the policy input would be sent to OPA on every
    call and logged in its decision log. Policies key off identifiers, not
    payloads.
    """
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:512]
        elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            out[key] = [str(v)[:128] for v in value[:20]]
    return out
