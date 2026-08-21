"""cairn-mcp-actions: every write, behind a gate.

The tools here never perform their action. They validate arguments, compute
an idempotency key, and file an approval request. What comes back to the
agent is `PENDING_APPROVAL` — a normal, successful tool result describing a
queued action, not an error.

That distinction is load-bearing. Modelling a pending approval as an error
teaches the agent to retry, which is exactly wrong: it would file the same
request again, spam the approver, and eventually get an action approved
through sheer persistence.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from cairn_core.auth import AuthError, bearer, verify_internal
from cairn_core.config import settings
from cairn_core.telemetry import get_logger
from cairn_mcp_actions.executors import EXECUTORS, ExecutionError
from cairn_mcp_kit import build, error, guarded, run
from cairn_mcp_kit.identity import current_claims, current_token

log = get_logger(__name__)

INSTRUCTIONS = """
Actions that change production. Every one requires human approval.

When a tool returns status `pending_approval`, the request is queued and a
human has been asked. That is a success, not a failure. Do NOT call the tool
again — carry on investigating other lines of enquiry and mention the pending
action in your answer.

Only propose an action you can justify from evidence you have already
gathered. An approver reading the request will see your reason.
""".strip()

server = build("cairn-mcp-actions", INSTRUCTIONS)


def _hash(action: str, parts: dict[str, Any]) -> str:
    from cairn_approval.service import idempotency_key

    return idempotency_key(action, parts)


async def request_approval(
    *,
    action: str,
    args: dict[str, Any],
    key: str,
    reason: str,
    required_approvals: int = 1,
) -> dict[str, Any]:
    """File the request with the approval service and tell the agent to wait."""
    claims = current_claims()
    cfg = settings()

    if not claims.has("tools:write"):
        return error(
            action,
            "you are not permitted to request write actions. Recommend the "
            "action in your answer instead so a human can run it.",
            kind="denied",
        )

    payload = {
        "action": action,
        "args": args,
        "idempotency_key": key,
        "reason": reason,
        "trajectory_id": claims.trajectory_id if claims.trajectory_id != "stdio" else None,
        "required_approvals": required_approvals,
        "ttl_s": cfg.approval.default_ttl_s,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{cfg.approval.url}/v1/approvals",
                json=payload,
                # The caller's own token, forwarded. The approval is filed as
                # the requesting *user*, which is what makes
                # `approver != requester` mean anything downstream.
                headers={"authorization": f"Bearer {current_token()}"},
            )
            resp.raise_for_status()
            approval = resp.json()
    except httpx.HTTPError as exc:
        return error(
            action,
            f"could not reach the approval service: {exc}. The action has NOT "
            "been queued. Report this in your answer rather than retrying.",
            kind="unavailable",
        )

    return {
        "tool": action,
        "ok": True,
        "status": "pending_approval",
        "approval_id": approval["id"],
        "state": approval["state"],
        "expires_at": approval["expires_at"],
        "message": (
            f"{action} has been queued for human approval "
            f"({approval['required_approvals']} approver(s) needed). "
            "Continue investigating; do not request this action again."
        ),
    }


@server.tool(
    name="create_ticket",
    description=(
        "File an incident ticket. Requires human approval. Use when the "
        "investigation has produced something a team needs to act on later."
    ),
)
@guarded("create_ticket", scope="tools:write")
async def create_ticket(
    title: Annotated[str, Field(max_length=250, description="One-line summary")],
    description: Annotated[str, Field(description="What you found, with evidence")],
    reason: Annotated[
        str, Field(description="Why this ticket is warranted; the approver reads this")
    ],
    issue_type: Annotated[str, Field(description="Jira issue type")] = "Incident",
) -> dict[str, Any]:
    claims = current_claims()
    args = {"title": title, "description": description, "issue_type": issue_type}
    return await request_approval(
        action="create_ticket",
        args=args,
        key=_hash("create_ticket", {"traj": claims.trajectory_id, "title": title}),
        reason=reason,
    )


@server.tool(
    name="post_incident_summary",
    description=(
        "Post a summary to a Slack channel. Requires human approval. Use at "
        "the end of an investigation, not during it."
    ),
)
@guarded("post_incident_summary", scope="tools:write")
async def post_incident_summary(
    channel: Annotated[str, Field(description="Channel, e.g. #incidents")],
    summary: Annotated[str, Field(description="The message to post")],
    reason: Annotated[str, Field(description="Why this should be posted")],
) -> dict[str, Any]:
    claims = current_claims()
    return await request_approval(
        action="post_incident_summary",
        args={"channel": channel, "summary": summary},
        key=_hash("post_incident_summary", {"traj": claims.trajectory_id, "channel": channel}),
        reason=reason,
    )


@server.tool(
    name="rollback_deploy",
    description=(
        "Roll a service back to a previously deployed revision. Requires TWO "
        "human approvals. Only propose this when the deploy timeline shows a "
        "release immediately before the regression AND you have evidence "
        "linking the two. `to_sha` must be a revision that was actually "
        "deployed — get it from get_deploy_timeline."
    ),
)
@guarded("rollback_deploy", scope="tools:write")
async def rollback_deploy(
    service: Annotated[str, Field(description="Service to roll back")],
    from_sha: Annotated[str, Field(description="The revision currently live")],
    to_sha: Annotated[str, Field(description="The revision to return to")],
    reason: Annotated[str, Field(description="The evidence linking this deploy to the incident")],
) -> dict[str, Any]:
    args = {"service": service, "from_sha": from_sha, "to_sha": to_sha}
    return await request_approval(
        action="rollback_deploy",
        args=args,
        key=_hash("rollback_deploy", args),
        reason=reason,
        # The one action in the system that can cause an outage on its own.
        required_approvals=2,
    )


@server.tool(
    name="scale_deployment",
    description=(
        "Change a deployment's replica count. Requires human approval. Use "
        "when metrics show saturation, not as a guess."
    ),
)
@guarded("scale_deployment", scope="tools:write")
async def scale_deployment(
    service: Annotated[str, Field(description="Deployment name")],
    replicas: Annotated[int, Field(ge=0, le=100, description="Target replica count")],
    reason: Annotated[str, Field(description="The saturation evidence")],
    namespace: Annotated[str, Field(description="Kubernetes namespace")] = "default",
    window_minutes: Annotated[
        int, Field(ge=5, le=1440, description="How long this should hold")
    ] = 60,
) -> dict[str, Any]:
    args = {
        "service": service,
        "replicas": replicas,
        "namespace": namespace,
        "window_minutes": window_minutes,
    }
    return await request_approval(
        action="scale_deployment",
        args=args,
        key=_hash(
            "scale_deployment",
            {"service": service, "replicas": replicas, "window": window_minutes},
        ),
        reason=reason,
    )


@server.tool(
    name="silence_alert",
    description=(
        "Silence an alert for a bounded window. Requires human approval. Use "
        "only when you have identified the alert as a known consequence of an "
        "incident already being handled."
    ),
)
@guarded("silence_alert", scope="tools:write")
async def silence_alert(
    alert: Annotated[str, Field(description="Alertname to silence")],
    duration_minutes: Annotated[int, Field(ge=1, le=1440, description="How long")],
    reason: Annotated[str, Field(description="Why this alert is safe to silence")],
) -> dict[str, Any]:
    args = {"alert": alert, "duration_minutes": duration_minutes, "reason": reason}
    return await request_approval(
        action="silence_alert",
        args=args,
        key=_hash("silence_alert", {"alert": alert, "duration": duration_minutes}),
        reason=reason,
    )


@server.custom_route("/internal/execute", methods=["POST"])  # type: ignore[untyped-decorator]
async def execute(request: Request) -> JSONResponse:
    """Callback from the approval service after a human said yes.

    Not an MCP tool, and not reachable by the agent: the orchestrator has no
    network path here (see the NetworkPolicy), and this route requires a
    token minted by the approval service itself.
    """
    try:
        claims = verify_internal(bearer(request.headers), settings().auth)
    except AuthError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)

    if claims.sub != "cairn-approval":
        log.warning("execute_called_by_non_approver", caller=claims.sub)
        return JSONResponse(
            {"ok": False, "error": "only the approval service may execute actions"},
            status_code=403,
        )

    body = await request.json()
    action = str(body.get("action", ""))
    executor = EXECUTORS.get(action)
    if executor is None:
        return JSONResponse({"ok": False, "error": f"unknown action {action!r}"}, status_code=400)

    log.info(
        "executing_approved_action",
        action=action,
        approval_id=body.get("approval_id"),
        requested_by=body.get("requested_by"),
    )
    try:
        result = await executor(
            body.get("args") or {}, requested_by=str(body.get("requested_by", "unknown"))
        )
    except ExecutionError as exc:
        log.error("action_failed", action=action, error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)
    except Exception as exc:
        log.exception("action_crashed", action=action)
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=200)

    return JSONResponse(result)


def main() -> None:
    run(server, "cairn-mcp-actions")


if __name__ == "__main__":
    main()
