"""Tool schema versioning (§5.5).

Tools are versioned in the *name*, not in a header: `query_logs` becomes
`query_logs_v2`. The model sees the tool list; it does not see headers, so a
header-versioned tool is a tool the model cannot choose between.

The lifecycle:

1. Register `query_logs_v2` alongside `query_logs`.
2. Mark the old one deprecated. Its description gains a line telling the model
   to prefer the new one — which is the only deprecation notice an agent will
   ever read.
3. Both run for one release cycle. `cairn_deprecated_tool_calls_total` counts
   who is still calling the old one.
4. Remove it when that counter is flat, not when the calendar says so.

Additive optional fields are not a version bump. Anything else is: a renamed
field, a narrowed type, a changed default, or a different meaning for the same
argument.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

from prometheus_client import Counter

from cairn_core.telemetry import get_logger

log = get_logger(__name__)


def _counter(name: str, documentation: str, labels: list[str]) -> Counter:
    """Register a counter, tolerating a second import of this module.

    prometheus_client raises on duplicate registration. That happens whenever
    a module is imported twice under different names — `python -m pkg.mod`
    after the package `__init__` already imported it, or a uvicorn reload —
    and crashing on it turns a papercut into a broken dev loop.
    """
    try:
        return Counter(name, documentation, labels)
    except ValueError:
        from prometheus_client import REGISTRY

        existing = REGISTRY._names_to_collectors.get(name)
        if existing is None:  # pragma: no cover - registry contract
            raise
        return existing  # type: ignore[return-value]


DEPRECATED_CALLS = _counter(
    "cairn_deprecated_tool_calls_total",
    "Calls to a deprecated tool version",
    ["tool", "superseded_by"],
)

F = TypeVar("F", bound=Callable[..., Any])

_VERSION_SUFFIX = re.compile(r"_v(\d+)$")


def base_name(tool: str) -> str:
    """`query_logs_v2` -> `query_logs`."""
    return _VERSION_SUFFIX.sub("", tool)


def version_of(tool: str) -> int:
    """Unsuffixed tools are v1."""
    match = _VERSION_SUFFIX.search(tool)
    return int(match.group(1)) if match else 1


def deprecation_note(superseded_by: str, removal: str) -> str:
    """The line appended to a deprecated tool's description.

    Written for the model, since the model is the only reader that matters:
    it must say what to call instead, plainly enough to act on.
    """
    return (
        f"\n\nDEPRECATED: use `{superseded_by}` instead. This version still "
        f"works and will be removed in {removal}. Prefer the new tool for any "
        "new call."
    )


def deprecated(superseded_by: str, removal: str = "the next release") -> Callable[[F], F]:
    """Mark a tool implementation as deprecated.

    Counts every call so removal is a data decision. A tool nobody calls can
    go; a tool something still calls weekly cannot, whatever the plan said.
    """

    def decorate(fn: F) -> F:
        import functools

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            DEPRECATED_CALLS.labels(tool=fn.__name__, superseded_by=superseded_by).inc()
            log.info("deprecated_tool_called", tool=fn.__name__, use=superseded_by)
            return await fn(*args, **kwargs)

        wrapper.__cairn_deprecated__ = superseded_by  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate


def is_breaking(old_schema: dict[str, Any], new_schema: dict[str, Any]) -> list[str]:
    """Would this schema change require a version bump?

    Run in CI against the previous release's tool list, so "we thought it was
    additive" is caught before it reaches a model that has already learned the
    old shape.
    """
    reasons: list[str] = []
    old_props = old_schema.get("properties") or {}
    new_props = new_schema.get("properties") or {}

    for name in old_props:
        if name not in new_props:
            reasons.append(f"removed property {name!r}")
            continue
        old_type = old_props[name].get("type")
        new_type = new_props[name].get("type")
        if old_type != new_type:
            reasons.append(f"{name!r} changed type {old_type} -> {new_type}")

    old_required = set(old_schema.get("required") or [])
    new_required = set(new_schema.get("required") or [])
    # Newly required fields break every existing caller; dropping a
    # requirement does not.
    for name in sorted(new_required - old_required):
        reasons.append(f"{name!r} became required")

    return reasons


def _self_check() -> None:
    assert base_name("query_logs_v2") == "query_logs"
    assert base_name("query_logs") == "query_logs"
    assert version_of("query_logs") == 1
    assert version_of("query_logs_v3") == 3
    # not a version suffix
    assert base_name("get_traces_v") == "get_traces_v"

    note = deprecation_note("query_logs_v2", "release 0.4")
    assert "query_logs_v2" in note and "DEPRECATED" in note

    props: dict[str, dict[str, str]] = {
        "service": {"type": "string"},
        "limit": {"type": "integer"},
    }
    v1: dict[str, Any] = {"properties": props, "required": ["service"]}
    assert is_breaking(v1, v1) == []

    # additive optional field is fine
    additive: dict[str, Any] = {
        "properties": {**props, "cursor": {"type": "string"}},
        "required": ["service"],
    }
    assert is_breaking(v1, additive) == []

    assert is_breaking(
        v1, {"properties": {"service": {"type": "string"}}, "required": ["service"]}
    ) == ["removed property 'limit'"]
    retyped = is_breaking(
        v1,
        {"properties": {**props, "limit": {"type": "string"}}, "required": ["service"]},
    )
    assert retyped == ["'limit' changed type integer -> string"], retyped
    newly_required = is_breaking(v1, {"properties": props, "required": ["service", "limit"]})
    assert newly_required == ["'limit' became required"]
    # relaxing a requirement is not breaking
    assert is_breaking(v1, {"properties": props, "required": []}) == []

    print("versioning self-check ok")


if __name__ == "__main__":
    _self_check()
