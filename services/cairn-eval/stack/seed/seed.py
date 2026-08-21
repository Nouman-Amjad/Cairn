"""Load the scenario corpus into a real Prometheus and a real Loki.

The in-process eval (`make eval`) is fast and gates CI. This is the other
half: the same scenarios served by the same backends the production tool
servers talk to, so `query_metrics` and `query_logs` are exercised against
real PromQL and real LogQL rather than a dict lookup.

Both paths read `scenarios/*.yaml`, so they cannot drift apart.

    docker compose -f services/cairn-eval/stack/docker-compose.yml up -d
    python services/cairn-eval/stack/seed/seed.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"
PROMETHEUS = "http://localhost:59090"
LOKI = "http://localhost:53100"

#: Series are written relative to this so every scenario's window lines up
#: with the timestamps in its own question text.
INCIDENT_AT = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)
STEP = timedelta(seconds=30)


def post(url: str, body: bytes, content_type: str) -> None:
    request = urllib.request.Request(  # noqa: S310 - fixed localhost URLs
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        if response.status >= 300:
            raise RuntimeError(f"{url} -> {response.status}")


def wait_for(url: str, name: str, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310
                print(f"  {name} is up")
                return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise SystemExit(f"{name} did not come up at {url}; is the stack running?")


def parse_labels(raw: str) -> dict[str, str]:
    """`app=checkout-api,status=502` -> a label dict."""
    labels: dict[str, str] = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            labels[key.strip()] = value.strip()
    return labels


def to_openmetrics(scenario: dict[str, Any]) -> str:
    """Render a scenario's metrics as OpenMetrics text for backfill.

    Prometheus ingests this through `promtool tsdb create-blocks-from
    openmetrics`, which is the supported way to write history — the remote
    write path would stamp everything with the current wall clock and put the
    incident in the wrong place.
    """
    lines: list[str] = []
    for entry in scenario.get("world", {}).get("query_metrics", []) or []:
        name = str(entry["metric"]).replace("-", "_").replace(".", "_")
        labels = parse_labels(str(entry.get("labels", "")))
        rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        selector = f"{{{rendered}}}" if rendered else ""

        lines.append(f"# TYPE {name} gauge")
        for index, (_, value) in enumerate(entry.get("series", [])):
            at = INCIDENT_AT - timedelta(minutes=30) + index * STEP
            if value is None:
                continue
            lines.append(f"{name}{selector} {float(value)} {at.timestamp():.0f}")
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


def push_logs(scenario: dict[str, Any]) -> int:
    """Push a scenario's log lines to Loki as one stream per pod."""
    streams: dict[tuple[str, str, str], list[list[str]]] = {}
    scenario_id = scenario["id"]

    for index, row in enumerate(scenario.get("world", {}).get("query_logs", []) or []):
        pod = str(row.get("pod", "unknown"))
        level = str(row.get("level", "info"))
        app = pod.rsplit("-", 1)[0]
        at = INCIDENT_AT + timedelta(seconds=index)
        key = (app, pod, level)
        streams.setdefault(key, []).append([str(int(at.timestamp() * 1e9)), str(row["line"])])

    if not streams:
        return 0

    payload = {
        "streams": [
            {
                "stream": {
                    "app": app,
                    "pod": pod,
                    "level": level,
                    "scenario": scenario_id,
                    "namespace": "default",
                },
                "values": values,
            }
            for (app, pod, level), values in streams.items()
        ]
    }
    post(f"{LOKI}/loki/api/v1/push", json.dumps(payload).encode(), "application/json")
    return sum(len(v) for v in streams.values())


def write_deploys(scenarios: list[dict[str, Any]], out: Path) -> None:
    """The fake deploy API reads this; one file, keyed by service."""
    timeline: dict[str, list[dict[str, Any]]] = {}
    for scenario in scenarios:
        for row in scenario.get("world", {}).get("get_deploy_timeline", []) or []:
            timeline.setdefault(str(row.get("service", "unknown")), []).append(row)
    out.write_text(json.dumps(timeline, indent=2, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the eval stack")
    parser.add_argument("--skip-wait", action="store_true")
    parser.add_argument(
        "--metrics-out",
        type=Path,
        default=Path("openmetrics.txt"),
        help="OpenMetrics file for promtool backfill",
    )
    args = parser.parse_args()

    files = sorted(SCENARIOS.glob("*.yaml"))
    if not files:
        raise SystemExit(f"no scenarios in {SCENARIOS}")
    scenarios = [yaml.safe_load(f.read_text(encoding="utf-8")) for f in files]

    if not args.skip_wait:
        wait_for(f"{LOKI}/ready", "loki")
        wait_for(f"{PROMETHEUS}/-/ready", "prometheus")

    metrics = "".join(to_openmetrics(s) for s in scenarios)
    args.metrics_out.write_text(metrics, encoding="utf-8")
    print(f"  wrote {args.metrics_out} ({metrics.count(chr(10))} lines)")
    print("  backfill with:")
    print(
        f"    docker compose exec prometheus promtool tsdb create-blocks-from "
        f"openmetrics /prometheus/{args.metrics_out.name} /prometheus"
    )

    total = 0
    for scenario in scenarios:
        try:
            total += push_logs(scenario)
        except Exception as exc:  # one bad stream should not stop the seed
            print(f"  ! {scenario['id']}: {exc}", file=sys.stderr)
    print(f"  pushed {total} log lines to loki")

    deploys = Path(__file__).with_name("deploys.json")
    write_deploys(scenarios, deploys)
    print(f"  wrote {deploys.name}")


def _self_check() -> None:
    scenario = {
        "id": "inc-test",
        "world": {
            "query_metrics": [
                {
                    "metric": "http_request_duration_p95",
                    "labels": "app=checkout-api,status=500",
                    "series": [[0, 0.2], [1, 2.4]],
                }
            ],
            "query_logs": [
                {"ts": "x", "line": "boom", "pod": "checkout-api-7f9d", "level": "error"}
            ],
            "get_deploy_timeline": [{"service": "checkout-api", "revision": "abc123"}],
        },
    }

    assert parse_labels("app=checkout-api,status=502") == {
        "app": "checkout-api",
        "status": "502",
    }
    assert parse_labels("") == {}

    text = to_openmetrics(scenario)
    assert "# TYPE http_request_duration_p95 gauge" in text
    assert 'app="checkout-api"' in text and 'status="500"' in text
    assert text.rstrip().endswith("# EOF")
    # timestamps must be the incident window, not now
    assert str(int((INCIDENT_AT - timedelta(minutes=30)).timestamp())) in text

    # a metric name with dashes must be sanitised or Prometheus rejects it
    dashed = to_openmetrics(
        {"world": {"query_metrics": [{"metric": "a-b.c", "labels": "", "series": [[0, 1]]}]}}
    )
    assert "a_b_c" in dashed and "a-b.c" not in dashed

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "deploys.json"
        write_deploys([scenario], out)
        assert json.loads(out.read_text())["checkout-api"][0]["revision"] == "abc123"

    print("seed self-check ok")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        main()
