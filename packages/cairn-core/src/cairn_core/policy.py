"""OPA client: the actual authorization decision for every tool call.

This runs inside each MCP server, as a sidecar call to localhost. The
orchestrator's opinion about what it is allowed to do is not consulted, and
neither is the model's. If the agent is prompt-injected into calling
`rollback_deploy`, the call arrives here carrying the *user's* claims and is
denied because that user lacks the scope. Injection becomes a failed authz
event instead of an outage.

There is no fail-open path. An unreachable OPA denies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from cairn_core.auth import InternalClaims
from cairn_core.config import PolicySettings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)


class Denied(PermissionError):
    """403. Carries the reason the policy gave, which is safe to show a user."""

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


@dataclass(slots=True)
class Decision:
    allow: bool
    reason: str = ""
    requires_approval: bool = False
    required_approvals: int = 1


class PolicyClient:
    def __init__(self, cfg: PolicySettings | None = None) -> None:
        from cairn_core.config import settings

        # Default to the process-wide settings object rather than a fresh
        # PolicySettings(): two independent copies of the same config drift,
        # and the one that drifts here decides whether authorization runs.
        self.cfg = cfg or settings().policy
        self._client = httpx.AsyncClient(base_url=self.cfg.opa_url, timeout=self.cfg.timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def evaluate(
        self, *, tool: str, args: dict[str, Any], claims: InternalClaims
    ) -> Decision:
        if not self.cfg.enabled:
            # Only reachable in local dev where no OPA sidecar exists. Read
            # tools pass; write tools still require approval downstream.
            return Decision(allow=True, reason="policy disabled (dev)")

        payload = {
            "input": {
                "tool": tool,
                "args": args,
                "user": {
                    "sub": claims.sub,
                    "email": claims.email,
                    "groups": list(claims.groups),
                    "team": claims.team,
                    "scopes": sorted(claims.scopes),
                },
                "trajectory_id": claims.trajectory_id,
            }
        }
        path = f"/v1/data/{self.cfg.decision_path.strip('/')}"
        try:
            resp = await self._client.post(path, json=payload)
            resp.raise_for_status()
            result = resp.json().get("result")
        except Exception as exc:
            # Deny, loudly. A policy engine that cannot be reached is not a
            # reason to skip authorization.
            log.error("opa_unreachable", tool=tool, error=str(exc))
            return Decision(allow=False, reason="policy engine unavailable")

        if isinstance(result, bool):
            return Decision(allow=result, reason="" if result else "denied by policy")
        if isinstance(result, dict):
            return Decision(
                allow=bool(result.get("allow", False)),
                reason=str(result.get("reason", "denied by policy")),
                requires_approval=bool(result.get("requires_approval", False)),
                required_approvals=int(result.get("required_approvals", 1)),
            )
        return Decision(allow=False, reason="policy returned no decision")

    async def enforce(self, *, tool: str, args: dict[str, Any], claims: InternalClaims) -> Decision:
        decision = await self.evaluate(tool=tool, args=args, claims=claims)
        if not decision.allow:
            log.warning("tool_denied", tool=tool, user=claims.sub, reason=decision.reason)
            raise Denied(tool, decision.reason)
        return decision
