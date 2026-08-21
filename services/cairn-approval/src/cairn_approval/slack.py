"""Slack: request signing and the interactive message.

Signature verification is not optional and not "checked later". The Slack
webhook is a public endpoint that can execute production changes; the
signature and the timestamp window are the only things standing between the
internet and `rollback_deploy`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from cairn_core.config import ApprovalSettings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)

SIGNATURE_VERSION = "v0"


class SignatureError(Exception):
    pass


def verify_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    max_age_s: int = 300,
    now: float | None = None,
) -> None:
    """Slack's v0 scheme. Raises `SignatureError` on anything suspicious."""
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise SignatureError("missing or malformed timestamp") from exc

    age = abs((now if now is not None else time.time()) - sent_at)
    if age > max_age_s:
        # Replay window. Without this, a captured request approves an action
        # forever.
        raise SignatureError(f"timestamp outside the {max_age_s}s window")

    basestring = b"%s:%s:%s" % (SIGNATURE_VERSION.encode(), timestamp.encode(), body)
    expected = (
        SIGNATURE_VERSION
        + "="
        + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, signature or ""):
        raise SignatureError("signature mismatch")


def approval_blocks(
    *,
    approval_id: str,
    action: str,
    args: dict[str, Any],
    requester: str,
    trajectory_id: str | None,
    reason: str,
    required_approvals: int,
    expires_in_s: int,
    ui_base_url: str | None = None,
) -> list[dict[str, Any]]:
    """The message an approver actually reads at 3am.

    Everything needed to decide is on the card: what will change, who asked,
    why, and what evidence led here. An approver who has to open three tabs
    to understand a request approves it without understanding it.
    """
    arg_lines = "\n".join(f"• *{k}*: `{v}`" for k, v in sorted(args.items()))
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Approval needed: {action}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Requested by* <@{requester}>\n*Why*: {reason}",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": arg_lines or "_no arguments_"}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Expires in {expires_in_s // 60} min · "
                        f"{required_approvals} approval(s) required · "
                        f"id `{approval_id[:8]}`"
                    ),
                }
            ],
        },
    ]

    if trajectory_id and ui_base_url:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{ui_base_url}/trajectories/{trajectory_id}"
                    "|View the full investigation>",
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"cairn_approval:{approval_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "cairn_approve",
                    "value": approval_id,
                    # Slack's own second look. Cheap, and it has stopped a
                    # fat-fingered rollback more than once.
                    "confirm": {
                        "title": {"type": "plain_text", "text": f"Run {action}?"},
                        "text": {"type": "mrkdwn", "text": arg_lines or action},
                        "confirm": {"type": "plain_text", "text": "Do it"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "action_id": "cairn_deny",
                    "value": approval_id,
                },
            ],
        }
    )
    return blocks


class SlackClient:
    def __init__(self, cfg: ApprovalSettings) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return self.cfg.slack_bot_token is not None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url="https://slack.com/api",
                timeout=10.0,
                headers={
                    "authorization": f"Bearer {self.cfg.slack_bot_token.get_secret_value()}"  # type: ignore[union-attr]
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    async def post(self, channel: str, blocks: list[dict[str, Any]], text: str) -> str | None:
        if not self.configured:
            log.warning("slack_not_configured", channel=channel)
            return None
        resp = await self._http().post(
            "/chat.postMessage",
            json={"channel": channel, "blocks": blocks, "text": text},
        )
        body = resp.json()
        if not body.get("ok"):
            log.error("slack_post_failed", error=body.get("error"))
            return None
        return str(body.get("ts"))

    async def update(self, channel: str, ts: str, blocks: list[dict[str, Any]], text: str) -> None:
        if not self.configured:
            return
        await self._http().post(
            "/chat.update",
            json={"channel": channel, "ts": ts, "blocks": blocks, "text": text},
        )


def resolved_blocks(action: str, state: str, actor: str, detail: str = "") -> list[dict[str, Any]]:
    """Replace the buttons once decided, so the card cannot be clicked twice."""
    icon = {"APPROVED": "✅", "EXECUTED": "✅", "DENIED": "⛔", "EXPIRED": "⏰", "FAILED": "❌"}
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{icon.get(state, '•')} *{action}* — {state.lower()} by {actor}"
                + (f"\n{detail}" if detail else ""),
            },
        }
    ]


def _self_check() -> None:
    secret = "8f742231b10e8888abcd99yyyzzz85a5"  # noqa: S105 - Slack's own doc example
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    ts = str(int(time.time()))
    basestring = b"v0:%s:%s" % (ts.encode(), body)
    good = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()

    verify_signature(body=body, timestamp=ts, signature=good, signing_secret=secret)

    for bad in ("v0=deadbeef", "", "v0=" + "0" * 64):
        try:
            verify_signature(body=body, timestamp=ts, signature=bad, signing_secret=secret)
            raise AssertionError(f"signature {bad!r} must be rejected")
        except SignatureError:
            pass

    # a body edited in flight must fail even with a valid-looking signature
    try:
        verify_signature(
            body=body + b"&extra=1", timestamp=ts, signature=good, signing_secret=secret
        )
        raise AssertionError("tampered body must be rejected")
    except SignatureError:
        pass

    # replay outside the window
    old = str(int(time.time()) - 3600)
    old_sig = (
        "v0="
        + hmac.new(secret.encode(), b"v0:%s:%s" % (old.encode(), body), hashlib.sha256).hexdigest()
    )
    try:
        verify_signature(body=body, timestamp=old, signature=old_sig, signing_secret=secret)
        raise AssertionError("stale timestamp must be rejected")
    except SignatureError:
        pass

    blocks = approval_blocks(
        approval_id="a" * 32,
        action="rollback_deploy",
        args={"service": "checkout-api", "to_sha": "abc123"},
        requester="U123",
        trajectory_id="t1",
        reason="503s began 4 min after the deploy",
        required_approvals=2,
        expires_in_s=900,
        ui_base_url="https://cairn.example.com",
    )
    assert json.dumps(blocks)
    actions = next(b for b in blocks if b["type"] == "actions")
    assert {e["action_id"] for e in actions["elements"]} == {"cairn_approve", "cairn_deny"}
    assert actions["elements"][0]["confirm"]["title"]["text"] == "Run rollback_deploy?"
    print("slack self-check ok")


if __name__ == "__main__":
    _self_check()
