"""MCP client pool: the orchestrator's only way to touch anything.

One session per server per trajectory, opened on demand and closed with the
trajectory. Sessions are cheap because the servers are stateless; what is not
cheap is re-listing tools on every call, so the catalogue is fetched once.

Every call forwards the user's short-lived internal token. The orchestrator
holds no backend credential of its own — it holds a user's identity, for five
minutes, scoped to one investigation.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from cairn_core.config import MCPSettings
from cairn_core.domain import ToolResult
from cairn_core.sensitivity import Sensitivity
from cairn_core.telemetry import get_logger, span
from cairn_core.tokens import estimate_json

log = get_logger(__name__)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    server: str

    @property
    def writes(self) -> bool:
        return self.server == "actions"

    def render(self) -> str:
        """How the tool appears in a prompt. Terse on purpose: the full JSON
        schema of six tools costs more context than the plan it informs."""
        params = ", ".join(sorted((self.schema.get("properties") or {}).keys()))
        return f"- {self.name}({params}): {self.description}"


class ToolBox:
    """Connections to every MCP server, for the life of one trajectory."""

    def __init__(self, token: str, cfg: MCPSettings | None = None) -> None:
        self.cfg = cfg or MCPSettings()
        self._token = token
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._catalogue: dict[str, ToolSpec] = {}

    async def __aenter__(self) -> ToolBox:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def connect(self) -> None:
        targets = {
            "observability": self.cfg.observability_url,
            "runbooks": self.cfg.runbooks_url,
            "actions": self.cfg.actions_url,
        }
        for name, url in targets.items():
            try:
                read, write, _ = await self._stack.enter_async_context(
                    streamablehttp_client(
                        url,
                        headers={"authorization": f"Bearer {self._token}"},
                        timeout=self.cfg.timeout_s,
                    )
                )
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                for tool in (await session.list_tools()).tools:
                    self._catalogue[tool.name] = ToolSpec(
                        name=tool.name,
                        description=(tool.description or "").strip(),
                        schema=tool.inputSchema or {},
                        server=name,
                    )
            except Exception as exc:
                # One unavailable tool server degrades the investigation; it
                # does not end it. An agent with metrics but no runbooks is
                # still useful, and saying which surface is missing is more
                # honest than failing the whole query.
                log.warning("mcp_server_unavailable", server=name, error=str(exc))

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()

    @property
    def catalogue(self) -> dict[str, ToolSpec]:
        return self._catalogue

    def render_catalogue(self, *, include_writes: bool = True) -> str:
        specs = [spec for spec in self._catalogue.values() if include_writes or not spec.writes]
        return "\n".join(spec.render() for spec in sorted(specs, key=lambda s: s.name))

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        spec = self._catalogue.get(name)
        if spec is None:
            return ToolResult(
                tool=name,
                ok=False,
                error=(f"No tool named {name!r}. Available: " + ", ".join(sorted(self._catalogue))),
            )

        session = self._sessions[spec.server]
        with span("mcp.call", **{"cairn.tool": name, "cairn.mcp_server": spec.server}) as sp:
            try:
                raw = await session.call_tool(name, args)
            except Exception as exc:
                log.warning("tool_call_failed", tool=name, error=str(exc))
                return ToolResult(tool=name, ok=False, error=f"{type(exc).__name__}: {exc}")

            payload = _payload(raw)
            result = _to_result(name, payload)
            sp.set_attribute("cairn.result_tokens", result.tokens)
            sp.set_attribute("cairn.sensitivity", result.sensitivity)
            if result.artifact_id:
                sp.set_attribute("cairn.artifact_id", result.artifact_id)
        return result


def _payload(raw: Any) -> dict[str, Any]:
    """Unwrap an MCP CallToolResult into the dict the tool returned."""
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP wraps a bare dict return under "result" when it synthesises
        # an output schema; unwrap that so tools see their own shape.
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for block in getattr(raw, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
            if isinstance(parsed, dict):
                return parsed
            return {"result": parsed}
    return {}


def _to_result(name: str, payload: dict[str, Any]) -> ToolResult:
    ok = bool(payload.get("ok", True))
    pending = payload.get("status") == "pending_approval"
    return ToolResult(
        tool=name,
        ok=ok or pending,
        content=payload,
        artifact_id=payload.get("artifact_id"),
        sensitivity=str(payload.get("sensitivity", "public")),
        tokens=estimate_json(payload),
        pending_approval_id=payload.get("approval_id") if pending else None,
        error=None if ok or pending else str(payload.get("error", "tool failed")),
    )


def sensitivity_of(result: ToolResult) -> Sensitivity:
    return Sensitivity.parse(result.sensitivity)
