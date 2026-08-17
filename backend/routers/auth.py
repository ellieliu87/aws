"""Auth router - Cognito when configured, mock LDAP-style otherwise.

Two ways to answer "who is calling?", chosen by whether a Cognito user pool is
configured:

  mock      the token is a UUID and `_token_store` remembers what it means.
            Fine for one process; it is also the single reason a second
            replica rejects a token the first one issued.

  cognito   the token is a signed JWT that carries the claims. Verified
            offline against the pool's public keys, so any replica can
            validate a token no replica issued. See services/cognito_auth.py.

The three dependencies below are the only things the rest of the app uses, so
switching modes changes nothing outside this file.
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from models.schemas import LoginRequest, LoginResponse, UserInfo

router = APIRouter()

# In-memory token store: token -> {username, role, department}. Used only in
# mock mode; with Cognito configured nothing is written here, which is the
# whole point.
_token_store: dict[str, dict] = {}

MOCK_PASSWORD = "capital1"

# The single permitted user for this build. Username + password are required;
# any other combination is rejected. Profile is what the UI badge shows.
MOCK_USERNAME = "pqr557"
_DEFAULT_PROFILES = {
    "pqr557": {
        "role": "Quantitative Analyst",
        "department": "Capital Markets & Analytics",
        # `groups` controls which domain packs the user can see / use.
        # The wildcard "*" grants access to every pack. When you onboard
        # additional users, list specific group names (e.g.
        # ["portfolio_managers", "treasury_desk"]) and have each pack
        # declare matching `user_groups` in its `pack.py:register`.
        "groups": ["*"],
    },
}

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _principal(token: str | None) -> dict:
    """Resolve a bearer token to {username, role, department, groups}.

    The one place the two modes differ. Everything below it — and every
    `Depends(get_current_user)` in the app — is mode-agnostic.
    """
    from services import cognito_auth

    if cognito_auth.enabled():
        try:
            return cognito_auth.principal(token or "")
        except cognito_auth.AuthError as e:
            # The reason is logged inside the verifier; the caller gets the
            # same message for every failure so a probe learns nothing.
            raise HTTPException(status_code=401, detail="Invalid or expired token") from e

    if not token or token not in _token_store:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return _token_store[token]


def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    return _principal(token)["username"]


def get_user_record(token: str = Depends(oauth2_scheme)) -> dict:
    return _principal(token)


def get_current_user_groups(token: str = Depends(oauth2_scheme)) -> list[str]:
    """Return the calling user's group memberships. Used to filter pack-scoped
    artifacts; an empty list (or "*") means "all groups"."""
    return list(_principal(token).get("groups", []))


@router.get("/config")
async def auth_config():
    """How this deployment expects the browser to sign in.

    The frontend asks before rendering anything, rather than being built with
    the answer baked in. A static bundle on a CDN is compiled once and served
    everywhere, so a `VITE_COGNITO_DOMAIN` at build time would mean a separate
    build per environment — and the pool id and client id are chosen by
    CloudFormation, so they are not knowable when the bundle is built anyway.

    Deliberately unauthenticated. Everything here is public by construction: a
    client id is an identifier rather than a secret, and the Hosted UI domain
    is where the browser is about to be sent in plain sight. There is no secret
    to leak because a public client has none — that is what PKCE replaces.
    """
    from services import cognito_auth

    if not cognito_auth.hosted_ui_available():
        # Either no pool (local development, mock login) or a pool without a
        # Hosted UI domain. Both mean "show the password form".
        return {"mode": "password", "hosted_ui": False}

    return {
        "mode": "hosted_ui",
        "hosted_ui": True,
        "domain": cognito_auth.hosted_domain(),
        "client_id": cognito_auth.client_id(),
        # The scopes the ID token needs to carry email and profile claims.
        "scopes": ["openid", "email", "profile"],
        # The browser appends its own origin — the redirect must match what is
        # registered on the client exactly, and only the browser knows which of
        # the registered origins it is being served from.
        "callback_path": "/auth/callback",
        "logout_path": "/login",
    }


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    if not request.username:
        raise HTTPException(status_code=400, detail="Username required")

    from services import cognito_auth

    if cognito_auth.enabled():
        try:
            token = cognito_auth.login(request.username, request.password or "")
        except cognito_auth.AuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        # Read the profile back out of the token rather than trusting the
        # request: the claims are what every subsequent call will be judged
        # against, so the login response should show exactly those.
        who = cognito_auth.principal(token)
        return LoginResponse(
            token=token,
            username=who["username"],
            role=who["role"],
            department=who["department"],
            groups=who["groups"],
        )

    if request.username.lower() != MOCK_USERNAME or request.password != MOCK_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    profile = _DEFAULT_PROFILES[MOCK_USERNAME]
    token = str(uuid.uuid4())
    _token_store[token] = {
        "username": MOCK_USERNAME,
        "role": profile["role"],
        "department": profile["department"],
        "groups": list(profile.get("groups", [])),
    }
    return LoginResponse(
        token=token,
        username=MOCK_USERNAME,
        role=profile["role"],
        department=profile["department"],
        groups=list(profile.get("groups", [])),
    )


@router.post("/logout")
async def logout(token: str = Depends(oauth2_scheme)):
    from services import cognito_auth

    if cognito_auth.enabled():
        # Revokes the refresh token, so no new tokens can be minted. The
        # bearer already issued stays valid until it expires — see the
        # revocation note in services/cognito_auth.py.
        cognito_auth.logout(token or "")
        return {"message": "Logged out"}

    if token and token in _token_store:
        del _token_store[token]
    return {"message": "Logged out"}


@router.get("/me", response_model=UserInfo)
async def me(user: dict = Depends(get_user_record)):
    return UserInfo(**user)
