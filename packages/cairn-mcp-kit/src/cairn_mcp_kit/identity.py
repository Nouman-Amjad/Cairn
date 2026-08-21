"""Per-request identity inside an MCP server.

MCP tool functions do not take an auth argument, so the caller's claims ride
a contextvar populated by ASGI middleware. This is the only place that reads
the Authorization header on a tool server, and the only place that decides
what identity a tool call runs as.

The stdio path (a developer running the server against a local MCP client) gets a
dev identity from the environment. It is deliberately incapable of write
scopes: debugging convenience must not become a production bypass.
"""

from __future__ import annotations

import os
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from cairn_core.auth import AuthError, InternalClaims, bearer, verify_internal
from cairn_core.config import settings
from cairn_core.telemetry import bind, get_logger

log = get_logger(__name__)

_claims: ContextVar[InternalClaims | None] = ContextVar("cairn_claims", default=None)
#: The caller's raw token, kept so a tool server can forward the *user's*
#: identity to a downstream service (the approval service, specifically)
#: rather than substituting its own. Substituting a service identity there
#: would quietly defeat `approver != requester`.
_raw_token: ContextVar[str] = ContextVar("cairn_raw_token", default="")

#: Paths that must work before a token exists.
OPEN_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def current_claims() -> InternalClaims:
    claims = _claims.get()
    if claims is None:
        raise AuthError("no authenticated caller in context")
    return claims


def set_claims(claims: InternalClaims | None, raw_token: str = "") -> None:
    _claims.set(claims)
    _raw_token.set(raw_token)


def current_token() -> str:
    return _raw_token.get()


def dev_claims() -> InternalClaims:
    """Identity for the stdio transport. Read-only, always."""
    return InternalClaims(
        sub=os.environ.get("CAIRN_DEV_USER", "dev-local"),
        email=os.environ.get("CAIRN_DEV_EMAIL", "dev@localhost"),
        groups=("engineering",),
        team=os.environ.get("CAIRN_DEV_TEAM"),
        trajectory_id="stdio",
        scopes=frozenset({"tools:read"}),
        jti="stdio",
    )


class ClaimsMiddleware(BaseHTTPMiddleware):
    """Verify the internal JWT on every request and stash the claims.

    A tool server never trusts that the gateway already checked. The gateway
    is one hop away on a flat pod network; treating its say-so as proof is how
    a single compromised service becomes a cluster-wide authorization bypass.
    """

    def __init__(self, app: ASGIApp, service: str) -> None:
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        try:
            raw = bearer(request.headers)
            claims = verify_internal(raw, settings().auth)
        except AuthError as exc:
            log.warning("unauthenticated_call", service=self.service, path=request.url.path)
            return JSONResponse({"error": str(exc)}, status_code=401)

        claims_token = _claims.set(claims)
        raw_token = _raw_token.set(raw)
        bind(user=claims.sub, trajectory_id=claims.trajectory_id)
        try:
            return await call_next(request)
        finally:
            _claims.reset(claims_token)
            _raw_token.reset(raw_token)
