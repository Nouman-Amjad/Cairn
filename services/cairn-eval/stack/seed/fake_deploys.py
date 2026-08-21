"""A fake ArgoCD, serving the deploy timeline from the scenario corpus.

Speaks enough of the ArgoCD API for `cairn-mcp-observability` to work against
it unmodified: `/api/v1/applications/{name}` returns a status block with the
same `history` shape. Read-only, and `/rollback` records the call rather than
doing anything, so an eval run can exercise the write path without one.

Standard library only — this runs in a bare `python:3.12-slim` with no build
step, and adding a web framework to serve two endpoints would be silly.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEPLOYS = Path(__file__).with_name("deploys.json")
PORT = 8080

#: Rollbacks requested during a run. Asserted on by the eval rather than
#: executed, because an eval that can roll something back has a blast radius.
ROLLBACKS: list[dict[str, Any]] = []


def timeline() -> dict[str, list[dict[str, Any]]]:
    if not DEPLOYS.exists():
        return {}
    data: dict[str, list[dict[str, Any]]] = json.loads(DEPLOYS.read_text(encoding="utf-8"))
    return data


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/ready"):
            return self._send({"status": "ok"})

        if self.path.startswith("/api/v1/applications/"):
            service = self.path.rsplit("/", 1)[-1].split("?")[0]
            history = timeline().get(service, [])
            if not history:
                return self._send({"error": "not found"}, status=404)
            return self._send(
                {
                    "metadata": {"name": service},
                    "status": {
                        "sync": {"revision": history[0].get("revision", "")},
                        "history": [
                            {
                                "id": index,
                                "revision": row.get("revision", ""),
                                "deployedAt": row.get("at"),
                            }
                            for index, row in enumerate(history)
                        ],
                    },
                }
            )

        if self.path == "/_rollbacks":  # inspected by the eval, not by ArgoCD
            return self._send(ROLLBACKS)

        self._send({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path.endswith("/rollback"):
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length) if length else b"{}"
            service = self.path.split("/api/v1/applications/")[-1].split("/")[0]
            ROLLBACKS.append({"service": service, "body": json.loads(body or b"{}")})
            # Recorded, not performed.
            return self._send({"status": "recorded", "executed": False})

        self._send({"error": "not found"}, status=404)

    def log_message(self, *_args: Any) -> None:
        """Silence per-request logging; the eval's own output is the signal."""


def main() -> None:
    print(f"fake deploy API on :{PORT} ({len(timeline())} services)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
