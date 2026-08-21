"""The side effects.

This module is the only place in Cairn that changes something outside itself,
and it is reachable only from the approval service after a human decision. It
holds the credentials for the systems being changed; the approval service
holds the decision. Neither one alone can produce a production change.

Every executor is idempotent on its own terms as a second line of defence:
the approval row already guarantees single execution, but a retry after a
network timeout must not create two tickets.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

import httpx

from cairn_core.config import BackendSettings, settings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

IDENT = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
SHA = re.compile(r"^[0-9a-f]{7,40}$")


class ExecutionError(RuntimeError):
    pass


class Executor(Protocol):
    async def __call__(self, args: dict[str, Any], *, requested_by: str) -> dict[str, Any]: ...


def _ident(value: str, what: str) -> str:
    if not IDENT.match(str(value)):
        raise ExecutionError(f"{what} {value!r} is not a valid identifier")
    return value


def _sha(value: str, what: str) -> str:
    if not SHA.match(str(value)):
        raise ExecutionError(f"{what} {value!r} is not a git sha")
    return value


def _cfg() -> BackendSettings:
    return settings().backends


async def create_ticket(args: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    cfg = _cfg()
    if not (cfg.jira_url and cfg.jira_token):
        raise ExecutionError("jira is not configured")

    summary = str(args["title"])[:250]
    async with httpx.AsyncClient(
        base_url=cfg.jira_url,
        timeout=30.0,
        headers={"authorization": f"Bearer {cfg.jira_token.get_secret_value()}"},
    ) as client:
        # Jira has no idempotency key, so look before leaping. JQL is
        # parameterised through the `jql` field, and the summary is quoted and
        # escaped rather than concatenated raw.
        existing = await client.get(
            "/rest/api/3/search",
            params={
                "jql": f'project = "{_jql(cfg.jira_project)}" AND summary ~ "{_jql(summary)}" '
                "AND created >= -1d",
                "maxResults": 1,
            },
        )
        if existing.status_code < 400:
            issues = existing.json().get("issues") or []
            if issues:
                return {"ok": True, "issue": issues[0]["key"], "deduplicated": True}

        resp = await client.post(
            "/rest/api/3/issue",
            json={
                "fields": {
                    "project": {"key": cfg.jira_project},
                    "summary": summary,
                    "description": _adf(str(args.get("description", ""))),
                    "issuetype": {"name": args.get("issue_type", "Incident")},
                    "labels": ["cairn", f"requested-by-{requested_by}"[:255]],
                }
            },
        )
    if resp.status_code >= 400:
        raise ExecutionError(f"jira: {resp.status_code} {resp.text[:300]}")
    return {"ok": True, "issue": resp.json().get("key")}


async def post_incident_summary(args: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    cfg = settings().approval
    if not cfg.slack_bot_token:
        raise ExecutionError("slack is not configured")
    channel = str(args["channel"])
    async with httpx.AsyncClient(
        base_url="https://slack.com/api",
        timeout=20.0,
        headers={"authorization": f"Bearer {cfg.slack_bot_token.get_secret_value()}"},
    ) as client:
        resp = await client.post(
            "/chat.postMessage",
            json={
                "channel": channel,
                "text": str(args["summary"])[:38_000],
                "unfurl_links": False,
            },
        )
    body = resp.json()
    if not body.get("ok"):
        raise ExecutionError(f"slack: {body.get('error')}")
    return {"ok": True, "channel": channel, "ts": body.get("ts")}


async def rollback_deploy(args: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    """Roll an ArgoCD application back to a previous revision.

    Uses ArgoCD's own history id rather than a raw sha where possible: rolling
    back to "the sha the agent believes was previous" trusts a model with a
    production change, and rolling back to a revision ArgoCD has actually
    deployed does not.
    """
    cfg = _cfg()
    if not (cfg.argocd_url and cfg.argocd_token):
        raise ExecutionError("argocd is not configured")

    service = _ident(args["service"], "service")
    to_sha = _sha(args["to_sha"], "to_sha")

    async with httpx.AsyncClient(
        base_url=cfg.argocd_url,
        timeout=60.0,
        headers={"authorization": f"Bearer {cfg.argocd_token.get_secret_value()}"},
    ) as client:
        app = await client.get(f"/api/v1/applications/{service}")
        if app.status_code >= 400:
            raise ExecutionError(f"argocd: application {service} not found")

        history = app.json().get("status", {}).get("history", []) or []
        target = next((h for h in history if str(h.get("revision", "")).startswith(to_sha)), None)
        if target is None:
            raise ExecutionError(
                f"{to_sha} is not in the deploy history for {service}; "
                "refusing to roll back to a revision that was never deployed"
            )
        if str(app.json().get("status", {}).get("sync", {}).get("revision", "")).startswith(to_sha):
            return {"ok": True, "service": service, "revision": to_sha, "already_at_target": True}

        resp = await client.post(
            f"/api/v1/applications/{service}/rollback", json={"id": target["id"]}
        )
    if resp.status_code >= 400:
        raise ExecutionError(f"argocd rollback: {resp.status_code} {resp.text[:300]}")
    return {"ok": True, "service": service, "revision": to_sha, "history_id": target["id"]}


async def scale_deployment(args: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    service = _ident(args["service"], "service")
    namespace = _ident(args.get("namespace", "default"), "namespace")
    replicas = int(args["replicas"])
    if not 0 <= replicas <= 100:
        raise ExecutionError("replicas must be between 0 and 100")

    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - a path
    try:
        # Blocking read of a tiny local file mounted by the kubelet. An
        # aiofiles dependency to save microseconds here would be silly.
        with open(token_path) as fh:  # noqa: ASYNC230
            token = fh.read()
    except OSError as exc:
        raise ExecutionError("not running in-cluster; cannot scale") from exc

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        verify="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        timeout=30.0,
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/merge-patch+json",
        },
    ) as client:
        resp = await client.patch(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{service}/scale",
            json={"spec": {"replicas": replicas}},
        )
    if resp.status_code >= 400:
        raise ExecutionError(f"kubernetes: {resp.status_code} {resp.text[:300]}")
    return {"ok": True, "service": service, "namespace": namespace, "replicas": replicas}


async def silence_alert(args: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    cfg = _cfg()
    if not cfg.prometheus_url:
        raise ExecutionError("alertmanager is not configured")
    alertmanager = cfg.prometheus_url.replace("prometheus", "alertmanager")

    duration_m = int(args.get("duration_minutes", 60))
    if not 1 <= duration_m <= 1440:
        raise ExecutionError("silence duration must be between 1 and 1440 minutes")

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    async with httpx.AsyncClient(base_url=alertmanager, timeout=20.0) as client:
        resp = await client.post(
            "/api/v2/silences",
            json={
                "matchers": [
                    {
                        "name": "alertname",
                        "value": str(args["alert"]),
                        "isRegex": False,
                        "isEqual": True,
                    }
                ],
                "startsAt": now.isoformat(),
                "endsAt": (now + timedelta(minutes=duration_m)).isoformat(),
                "createdBy": f"cairn ({requested_by})",
                "comment": str(args.get("reason", "silenced via Cairn"))[:500],
            },
        )
    if resp.status_code >= 400:
        raise ExecutionError(f"alertmanager: {resp.status_code} {resp.text[:300]}")
    return {"ok": True, "silence_id": resp.json().get("silenceID"), "minutes": duration_m}


def _jql(value: str) -> str:
    """Escape a JQL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _adf(text: str) -> dict[str, Any]:
    """Jira's Atlassian Document Format, minimal paragraph form."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": para}]}
            for para in (text or " ").split("\n\n")[:50]
        ],
    }


EXECUTORS: dict[str, Executor] = {
    "create_ticket": create_ticket,
    "post_incident_summary": post_incident_summary,
    "rollback_deploy": rollback_deploy,
    "scale_deployment": scale_deployment,
    "silence_alert": silence_alert,
}


def _self_check() -> None:
    for bad in ('a"; DROP', "Checkout", "-x", "a" * 100, "a b"):
        try:
            _ident(bad, "service")
            raise AssertionError(f"{bad!r} should be rejected")
        except ExecutionError:
            pass
    assert _ident("checkout-api", "service") == "checkout-api"
    assert _sha("abc1234", "sha") == "abc1234"
    for bad_sha in ("HEAD~1", "main", "zzzzzzz", "abc"):
        try:
            _sha(bad_sha, "sha")
            raise AssertionError(f"{bad_sha!r} should be rejected")
        except ExecutionError:
            pass
    assert _jql('a"b') == 'a\\"b'
    assert set(EXECUTORS) == {
        "create_ticket",
        "post_incident_summary",
        "rollback_deploy",
        "scale_deployment",
        "silence_alert",
    }
    print("executors self-check ok")


if __name__ == "__main__":
    _self_check()
