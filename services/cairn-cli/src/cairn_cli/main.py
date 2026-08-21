"""`cairn ask "why did checkout spike at 3am?"`

The CLI exists because the people this system is for are already in a
terminal at 3am. It talks to the gateway and nothing else — same auth, same
rate limits, same cost budget as the web UI, because a second door into the
system is a second place to forget a control.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

import httpx
import typer
from httpx_sse import connect_sse
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

app = typer.Typer(
    name="cairn",
    help="Ask Cairn about a production incident.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

GATEWAY = os.environ.get("CAIRN_GATEWAY_URL", "http://localhost:8000")


def _headers() -> dict[str, str]:
    """Bearer token from the environment, or dev identity for a local stack."""
    if token := os.environ.get("CAIRN_TOKEN"):
        return {"authorization": f"Bearer {token}"}
    if user := os.environ.get("CAIRN_DEV_USER"):
        return {
            "x-cairn-dev-user": user,
            "x-cairn-dev-groups": os.environ.get("CAIRN_DEV_GROUPS", "engineering"),
        }
    return {}


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What you want to know")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit raw events")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Answer only")] = False,
) -> None:
    """Ask a question and stream the investigation as it happens."""
    try:
        with httpx.Client(base_url=GATEWAY, timeout=30.0, headers=_headers()) as client:
            response = client.post("/v1/queries", json={"query": question})
    except httpx.HTTPError as exc:
        _fail(f"cannot reach the gateway at {GATEWAY}: {exc}")
        return

    if response.status_code == 402:
        _fail(f"budget: {_detail(response)}")
    if response.status_code == 429:
        _fail(f"rate limited: {_detail(response)}")
    if response.status_code >= 400:
        _fail(f"{response.status_code}: {_detail(response)}")

    trajectory_id = response.json()["trajectory_id"]
    if not quiet and not json_out:
        console.print(f"[dim]trajectory {trajectory_id}[/dim]\n")

    _stream(trajectory_id, json_out=json_out, quiet=quiet)


def _stream(trajectory_id: str, *, json_out: bool, quiet: bool) -> None:
    url = f"/v1/queries/{trajectory_id}/events"
    with (
        # No read timeout, deliberately: an SSE stream stays open for the life
        # of the investigation, and the loop's own 180s wall clock is the
        # bound that matters. Connect still times out.
        httpx.Client(
            base_url=GATEWAY,
            timeout=httpx.Timeout(None, connect=10.0),
            headers=_headers(),
        ) as client,
        connect_sse(client, "GET", url) as source,
    ):
        for event in source.iter_sse():
            payload = json.loads(event.data)
            data = payload.get("data") or {}

            if json_out:
                console.print_json(event.data)
                continue

            match payload.get("type"):
                case "plan" if not quiet:
                    console.print("[bold]Plan[/bold]")
                    for index, step in enumerate(data.get("steps") or [], 1):
                        console.print(f"  {index}. {step.get('goal', '')}")
                    console.print()
                case "step" if not quiet:
                    tool = data.get("tool", "tool")
                    summary = str(data.get("summary", ""))[:110]
                    marker = "[red]x[/red]" if not data.get("ok", True) else "[green]-[/green]"
                    restricted = (
                        " [yellow](restricted: local only)[/yellow]"
                        if data.get("sensitivity") == "restricted"
                        else ""
                    )
                    console.print(f"  {marker} [cyan]{tool}[/cyan]{restricted} {summary}")
                case "notice" if not quiet:
                    console.print(f"  [dim]{data.get('message', '')}[/dim]")
                case "approval":
                    console.print(
                        "\n[yellow]Waiting for human approval.[/yellow] "
                        "The investigation will resume when someone decides."
                    )
                case "answer":
                    console.print()
                    console.print(Markdown(str(data.get("answer", ""))))
                    if not quiet:
                        confidence = data.get("confidence")
                        console.print(
                            f"\n[dim]{data.get('state', '')} · "
                            f"confidence {confidence} · ${data.get('cost_usd', 0)}[/dim]"
                        )
                case "error":
                    _fail(str(data.get("message", "failed")))
                case "done":
                    return


@app.command()
def approvals() -> None:
    """List write actions waiting on you."""
    body = _get("/v1/approvals")
    rows = body.get("approvals") or []
    if not rows:
        console.print("[dim]Nothing waiting on you.[/dim]")
        return

    table = Table(title="Pending approvals")
    for column in ("id", "action", "requested by", "expires", "have/need"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["id"])[:8],
            row["action"],
            row["requested_by"],
            row["expires_at"][11:16],
            f"{len(row.get('approvals') or [])}/{row['required_approvals']}",
        )
    console.print(table)


@app.command()
def approve(
    approval_id: Annotated[str, typer.Argument(help="Approval id (full uuid)")],
    deny: Annotated[bool, typer.Option("--deny", help="Deny instead of approve")] = False,
    reason: Annotated[str, typer.Option(help="Why")] = "",
) -> None:
    """Approve or deny a queued action.

    The server rejects self-approval regardless of what this sends; the check
    lives there, not here.
    """
    with httpx.Client(base_url=GATEWAY, timeout=30.0, headers=_headers()) as client:
        response = client.post(
            f"/v1/approvals/{approval_id}/decision",
            json={"approve": not deny, "reason": reason or None},
        )
    if response.status_code == 403:
        _fail(_detail(response))
    if response.status_code >= 400:
        _fail(f"{response.status_code}: {_detail(response)}")
    console.print(f"[green]{response.json()['state']}[/green]")


@app.command()
def trajectory(
    trajectory_id: Annotated[str, typer.Argument(help="Trajectory id")],
    steps: Annotated[bool, typer.Option("--steps", help="Show every step")] = False,
) -> None:
    """Show a past investigation, including what each step cost."""
    body = _get(f"/v1/trajectories/{trajectory_id}")

    console.print(f"[bold]{body['query']}[/bold]")
    console.print(
        f"[dim]{body['state']} · ${body['cost_usd']} · "
        f"{body['tokens']['local']} local / {body['tokens']['cloud']} cloud tokens · "
        f"prompts {body['prompt_version']}[/dim]\n"
    )
    if body.get("answer"):
        console.print(Markdown(body["answer"]))

    if steps:
        table = Table(title="Steps")
        for column in ("#", "kind", "tool/model", "route", "cost", "ms"):
            table.add_column(column)
        for step in body.get("steps", []):
            table.add_row(
                str(step["seq"]),
                step["kind"],
                step.get("tool") or step.get("model") or "",
                step.get("route_reason") or "",
                step.get("cost_usd") or "",
                str(step.get("latency_ms") or ""),
            )
        console.print(table)


@app.command()
def whoami() -> None:
    """Who the gateway thinks you are, and what you have spent today."""
    body = _get("/v1/me")
    console.print(f"{body['sub']}  [dim]{', '.join(body['groups'])}[/dim]")
    console.print(f"spent today: ${body['spent_today_usd']} of ${body['daily_budget_usd']:.2f}")


def _get(path: str) -> dict[str, Any]:
    try:
        with httpx.Client(base_url=GATEWAY, timeout=30.0, headers=_headers()) as client:
            response = client.get(path)
    except httpx.HTTPError as exc:
        _fail(f"cannot reach the gateway at {GATEWAY}: {exc}")
        raise typer.Exit(1) from exc
    if response.status_code >= 400:
        _fail(f"{response.status_code}: {_detail(response)}")
    body: dict[str, Any] = response.json()
    return body


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text[:200]))
    except ValueError:
        return response.text[:200]


if __name__ == "__main__":
    app()
