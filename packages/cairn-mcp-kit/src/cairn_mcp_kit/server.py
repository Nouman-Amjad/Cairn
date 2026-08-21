"""Server construction and the two transports.

Every Cairn MCP server supports both Streamable HTTP (how it runs in-cluster)
and stdio (how a developer runs it against a local MCP client). The
stdio path costs about twenty lines and pays for itself the first time you
need to see exactly what the agent sees.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from cairn_core.config import settings
from cairn_core.telemetry import get_logger, setup

log = get_logger(__name__)


def build(name: str, instructions: str) -> FastMCP:
    cfg = settings()
    setup(cfg.otel.model_copy(update={"service_name": name}))
    server = FastMCP(
        name=name,
        instructions=instructions,
        host="0.0.0.0",  # noqa: S104 - in-cluster, NetworkPolicy fronted
        port=cfg.port,
        # Stateless: tool servers are behind an HPA and any pod must be able
        # to serve any call. Session affinity would pin an incident to a pod
        # that may be evicted mid-investigation.
        stateless_http=True,
        json_response=True,
    )

    @server.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": name})

    @server.custom_route("/metrics", methods=["GET"])  # type: ignore[untyped-decorator]
    async def metrics(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return server


def http_app(server: FastMCP, name: str) -> Any:
    from cairn_mcp_kit.identity import ClaimsMiddleware

    app = server.streamable_http_app()
    app.add_middleware(ClaimsMiddleware, service=name)
    return app


def run(server: FastMCP, name: str, *, readiness: Callable[[], bool] | None = None) -> None:
    """Entry point. `--stdio` (or CAIRN_MCP_STDIO=1) picks the local transport."""
    import os
    import sys

    stdio = "--stdio" in sys.argv or os.environ.get("CAIRN_MCP_STDIO") == "1"

    if stdio:
        import asyncio

        from cairn_mcp_kit.identity import dev_claims, set_claims

        set_claims(dev_claims())
        log.info("mcp_stdio_start", service=name)
        asyncio.run(server.run_stdio_async())
        return

    import uvicorn

    @server.custom_route("/readyz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def readyz(_request: Request) -> JSONResponse:
        ok = readiness() if readiness else True
        return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)

    cfg = settings()
    log.info("mcp_http_start", service=name, port=cfg.port)
    uvicorn.run(http_app(server, name), host="0.0.0.0", port=cfg.port, access_log=False)  # noqa: S104
