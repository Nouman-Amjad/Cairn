"""Authentication: external OIDC at the edge, short-lived internal JWT inside.

Two distinct tokens, on purpose.

The *external* token is whatever the IdP issues. It is verified once, at the
gateway, against JWKS.

The *internal* token is minted by the gateway per trajectory, lives 5 minutes,
and carries `sub`, `groups`, `trajectory_id` and coarse `scopes`. Every MCP
call forwards it and every tool server verifies it independently. Binding the
trajectory into the token is what stops a leaked token from being replayed
against a different investigation.

The internal token is a *pre-filter*, never the authorization decision. That
decision is OPA's, in `policy.py`, re-evaluated per tool call.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from cairn_core.config import AuthSettings
from cairn_core.domain import UserContext

ALGO = "HS256"


class AuthError(Exception):
    """401. Never carries token material in its message."""


@dataclass(slots=True, frozen=True)
class InternalClaims:
    sub: str
    email: str
    groups: tuple[str, ...]
    team: str | None
    trajectory_id: str
    scopes: frozenset[str]
    jti: str

    @property
    def user(self) -> UserContext:
        return UserContext(sub=self.sub, email=self.email, groups=list(self.groups), team=self.team)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


# Coarse scopes, derived from IdP group membership. Fine-grained resource
# ownership ("may this person roll back *this* service") is OPA's job.
GROUP_SCOPES: dict[str, frozenset[str]] = {
    "engineering": frozenset({"tools:read"}),
    "sre": frozenset({"tools:read", "tools:write", "approvals:grant"}),
    "platform-admin": frozenset({"tools:read", "tools:write", "approvals:grant", "policy:admin"}),
}


def scopes_for(groups: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for group in groups:
        out |= GROUP_SCOPES.get(group, frozenset())
    return frozenset(out)


@lru_cache(maxsize=4)
def _jwk_client(url: str, cache_s: int) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True, lifespan=cache_s)


def verify_oidc(token: str, cfg: AuthSettings) -> UserContext:
    """Verify the IdP token at the edge. Called exactly once per request."""
    jwks_url = cfg.jwks_url or f"{cfg.oidc_issuer.rstrip('/')}/v1/keys"
    try:
        key = _jwk_client(jwks_url, cfg.jwks_cache_s).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=cfg.oidc_audience,
            issuer=cfg.oidc_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError("invalid identity token") from exc

    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]
    return UserContext(
        sub=str(claims["sub"]),
        email=str(claims.get("email", "")),
        groups=[str(g) for g in groups],
        team=claims.get("team"),
    )


def mint_internal(user: UserContext, trajectory_id: str | uuid.UUID, cfg: AuthSettings) -> str:
    now = int(time.time())
    payload = {
        "iss": cfg.internal_jwt_issuer,
        "aud": "cairn-internal",
        "sub": user.sub,
        "email": user.email,
        "groups": user.groups,
        "team": user.team,
        "trajectory_id": str(trajectory_id),
        "scopes": sorted(scopes_for(user.groups)),
        "iat": now,
        "exp": now + cfg.internal_jwt_ttl_s,
        "jti": uuid.uuid4().hex,
    }
    encoded: str = jwt.encode(payload, cfg.internal_jwt_key.get_secret_value(), algorithm=ALGO)
    return encoded


def verify_internal(token: str, cfg: AuthSettings) -> InternalClaims:
    """Accepts the current key and, during rotation, the previous one."""
    keys = [cfg.internal_jwt_key.get_secret_value()]
    if cfg.internal_jwt_key_previous:
        keys.append(cfg.internal_jwt_key_previous.get_secret_value())

    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGO],
                audience="cairn-internal",
                issuer=cfg.internal_jwt_issuer,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
            break
        except jwt.PyJWTError as exc:
            last = exc
    else:
        raise AuthError("invalid internal token") from last

    return InternalClaims(
        sub=str(claims["sub"]),
        email=str(claims.get("email", "")),
        groups=tuple(claims.get("groups") or ()),
        team=claims.get("team"),
        trajectory_id=str(claims.get("trajectory_id", "")),
        scopes=frozenset(claims.get("scopes") or ()),
        jti=str(claims["jti"]),
    )


def bearer(headers: dict[str, str] | object) -> str:
    """Pull the token out of an Authorization header mapping."""
    get = getattr(headers, "get", None)
    raw = get("authorization") or get("Authorization") if get else None
    if not raw or not raw.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token: str = raw.split(None, 1)[1].strip()
    return token


def _self_check() -> None:
    cfg = AuthSettings()
    user = UserContext(sub="u1", email="a@b.com", groups=["sre"], team="checkout")
    tok = mint_internal(user, "11111111-1111-1111-1111-111111111111", cfg)
    claims = verify_internal(tok, cfg)
    assert claims.sub == "u1"
    assert claims.trajectory_id == "11111111-1111-1111-1111-111111111111"
    assert claims.has("tools:write") and claims.has("approvals:grant")

    plain = UserContext(sub="u2", email="c@d.com", groups=["engineering"])
    plain_claims = verify_internal(mint_internal(plain, "t", cfg), cfg)
    assert plain_claims.has("tools:read")
    assert not plain_claims.has("tools:write"), "engineering must not get write scope"

    # a token signed with a different key must not verify
    other = AuthSettings(internal_jwt_key="a-different-key-also-padded-to-32b")
    try:
        verify_internal(mint_internal(user, "t", other), cfg)
        raise AssertionError("cross-key verification must fail")
    except AuthError:
        pass

    # rotation window: previous key still accepted
    rotating = AuthSettings(
        internal_jwt_key="a-different-key-also-padded-to-32b",
        internal_jwt_key_previous=cfg.internal_jwt_key,
    )
    assert verify_internal(tok, rotating).sub == "u1"
    print("auth self-check ok")


if __name__ == "__main__":
    _self_check()
