"""Cognito-issued JWTs, so a token means the same thing on every replica.

`routers/auth.py` mints a UUID and remembers what it stands for in a module
dict. The token itself carries nothing — it is a pointer into one process's
memory, which is exactly why a second replica rejects a token the first one
issued. Of everything Phase 5 moved, this is the piece that actually blocks
horizontal scaling.

The fix is not a shared token table. It is to stop needing one: a Cognito JWT
*contains* the claims, and any node can verify it offline against the pool's
public keys. Nothing is looked up, so there is nothing to share.

    login   ->  Cognito InitiateAuth  ->  signed JWT
    request ->  verify signature + exp + iss + aud  ->  claims

What stays in process memory is a cache of Cognito's *public* keys, which is
safe to have per-replica because it holds no session state — replicas
disagreeing about it is harmless.

Which token
-----------
The ID token, not the access token, because this API needs the profile claims
(`custom:role`, `custom:department`) that Cognito puts only on the ID token.
The purist position is that ID tokens are for the client and access tokens are
for APIs; that applies when the API is a separate resource server. Here the API
*is* the application that authenticated the user. If this ever becomes a
third-party resource server, define scopes and switch to the access token —
`_verify` already branches on `token_use`, so that is a small change.

Revocation
----------
Offline verification never asks Cognito anything, so a signed-out access token
keeps validating until it expires. `global_sign_out` kills the refresh token
immediately, so no *new* tokens can be minted, but the one in the user's hand
lives out its TTL. That is the standard stateless-JWT trade; the mitigation is
a short token lifetime, not a denylist — a denylist would reintroduce the
shared lookup this module exists to remove.

Entirely opt-in. Without CMA_COGNITO_USER_POOL_ID the mock login in
routers/auth.py is untouched, so local development needs no AWS account.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("cma.cognito_auth")

# Cognito custom attributes arrive prefixed. Kept as constants because the
# prefix is easy to forget and the failure is a silently empty profile.
ROLE_CLAIM = "custom:role"
DEPARTMENT_CLAIM = "custom:department"
GROUPS_CLAIM = "cognito:groups"


class AuthError(Exception):
    """Verification failed. The caller turns this into a 401."""


def user_pool_id() -> str:
    return os.getenv("CMA_COGNITO_USER_POOL_ID", "").strip()


def client_id() -> str:
    return os.getenv("CMA_COGNITO_CLIENT_ID", "").strip()


def region() -> str:
    configured = os.getenv("CMA_COGNITO_REGION", "").strip()
    if configured:
        return configured
    # The pool id is `<region>_<suffix>`, so it already carries the answer.
    pool = user_pool_id()
    if "_" in pool:
        return pool.split("_", 1)[0]
    from cof.llm_config import bedrock_region

    return bedrock_region()


def enabled() -> bool:
    return bool(user_pool_id() and client_id())


def issuer() -> str:
    return f"https://cognito-idp.{region()}.amazonaws.com/{user_pool_id()}"


def jwks_url() -> str:
    return f"{issuer()}/.well-known/jwks.json"


# ── key cache ─────────────────────────────────────────────────────────────
# Module-level on purpose, and safe: these are public keys, not sessions.
_jwk_client = None
_jwk_client_for = ""


def _keys():
    """A PyJWKClient for the current pool, rebuilt if the pool changes.

    PyJWT caches the fetched key set itself, so a request does not hit
    Cognito. `lifespan` bounds that cache so a key rotation is picked up
    without a restart.
    """
    global _jwk_client, _jwk_client_for

    from jwt import PyJWKClient

    url = jwks_url()
    if _jwk_client is None or _jwk_client_for != url:
        _jwk_client = PyJWKClient(url, cache_keys=True, lifespan=3600)
        _jwk_client_for = url
    return _jwk_client


def reset_key_cache() -> None:
    """Drop the cached client. For tests and for a pool reconfiguration."""
    global _jwk_client, _jwk_client_for
    _jwk_client = None
    _jwk_client_for = ""


# ── verification ──────────────────────────────────────────────────────────
def verify(token: str) -> dict[str, Any]:
    """Verify a Cognito JWT and return its claims, or raise AuthError."""
    import jwt

    if not token:
        raise AuthError("No token supplied")
    try:
        signing_key = _keys().get_signing_key_from_jwt(token)
    except Exception as e:
        # An unknown `kid` is the common case here: a token from a different
        # pool, or one signed by nothing at all.
        raise AuthError(f"Could not resolve a signing key: {e}") from e

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer(),
            audience=client_id(),
            # Clock skew is real: a token minted by Cognito a moment ago can
            # have an `iat` fractionally ahead of this host's clock, which
            # without leeway is rejected as "not yet valid" — a confusing
            # intermittent 401 that looks like anything but a clock problem.
            # 60s is the usual allowance; it shortens no expiry meaningfully.
            leeway=60,
            options={"require": ["exp", "iss", "aud", "token_use"]},
        )
    except Exception as e:
        raise AuthError(f"{type(e).__name__}: {e}") from e

    # `aud` is checked above for ID tokens. Access tokens carry `client_id`
    # instead and would fail that check, so reaching here means an ID token —
    # but assert it rather than infer it, because a future switch to access
    # tokens should fail loudly rather than silently accept the wrong shape.
    if claims.get("token_use") != "id":
        raise AuthError(f"Expected an ID token, got token_use={claims.get('token_use')!r}")
    return claims


def principal(token: str) -> dict[str, Any]:
    """Verified claims mapped onto the shape routers/auth.py already uses."""
    claims = verify(token)
    groups = claims.get(GROUPS_CLAIM) or []
    if isinstance(groups, str):
        groups = [groups]
    return {
        "username": (
            claims.get("cognito:username")
            or claims.get("preferred_username")
            or claims.get("email")
            or claims.get("sub", "")
        ),
        "role": claims.get(ROLE_CLAIM) or "User",
        "department": claims.get(DEPARTMENT_CLAIM) or "",
        "groups": list(groups),
    }


# ── login / logout ────────────────────────────────────────────────────────
def _idp():
    import boto3

    return boto3.client("cognito-idp", region_name=region())


def login(username: str, password: str) -> str:
    """Exchange credentials for an ID token, or raise AuthError.

    USER_PASSWORD_AUTH keeps the existing login form working — the frontend
    still posts a username and password to /api/auth/login and gets a token
    back, so nothing there changes. The production-grade path is the Hosted UI
    with the authorization-code flow, which keeps the password away from this
    service entirely; this is the migration step, not the destination.
    """
    try:
        response = _idp().initiate_auth(
            ClientId=client_id(),
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except Exception as e:
        name = type(e).__name__
        # Do not echo Cognito's message back to the caller: it distinguishes
        # "no such user" from "wrong password", which is a free account probe.
        log.info("cognito auth failed for %r: %s", username, name)
        raise AuthError("Invalid credentials") from e

    challenge = response.get("ChallengeName")
    if challenge:
        # e.g. NEW_PASSWORD_REQUIRED for a user created by an admin. Worth a
        # distinct message, because the credentials were actually correct.
        raise AuthError(f"Additional step required: {challenge}")

    token = (response.get("AuthenticationResult") or {}).get("IdToken")
    if not token:
        raise AuthError("Cognito returned no ID token")
    return token


def logout(token: str) -> None:
    """Best-effort global sign-out: revokes refresh tokens for this user.

    Needs an access token, and the bearer here is an ID token, so this is a
    no-op unless one is supplied. Kept as a seam rather than dropped, so the
    endpoint has somewhere to grow when the token choice changes.
    """
    if not token:
        return
    try:
        _idp().global_sign_out(AccessToken=token)
    except Exception as e:
        log.debug("global_sign_out skipped: %s", type(e).__name__)


def status() -> dict:
    info: dict = {"enabled": enabled(), "pool": user_pool_id() or "(unset, mock auth)"}
    if enabled():
        info["issuer"] = issuer()
        try:
            _keys().get_signing_keys()
            info["jwks_reachable"] = True
        except Exception as e:
            info["jwks_reachable"] = False
            info["error"] = f"{type(e).__name__}: {e}"
    return info
