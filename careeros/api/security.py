"""Transport security for the API.

Phase 1 ran on localhost, where "no auth" was defensible. A mobile app changes
that: the server now listens on a network interface and a phone talks to it
over the LAN, so the API needs a credential and a CORS policy.

There are now two kinds of credential, and either satisfies the gate:

* a **session token** from signing in with Google or an emailed code, which
  identifies a person -- see `careeros.auth`;
* the **service token** in `CAREEROS_API_TOKEN`, which identifies a machine
  and is what the CLI and the built-in dashboard use.

The gate is on when `careeros.auth.auth_mode()` says `required` (chosen
automatically as soon as a sign-in method is configured) or when a service
token is set. With neither, the API is open, which is the single-user localhost
case; `/api/health` reports `auth_required` so a client can tell which world it
is in.

The check is middleware rather than a per-router dependency on purpose: a
dependency has to be remembered on every new endpoint, and the one that gets
forgotten is the one that leaks.
"""

from __future__ import annotations

import hmac
import os
from typing import Awaitable, Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

#: Paths that never require a token: the health probe and the sign-in
#: endpoints themselves (you cannot present a session before you have one),
#: plus the interactive docs.
PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/describe",
        "/api/auth/google/start",
        "/api/auth/google/callback",
        "/api/auth/google/exchange",
        "/api/auth/email/start",
        "/api/auth/email/verify",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
    }
)

#: Local dev origins allowed when nothing is configured: the Expo web dev
#: server, Expo Go's web host, and Vite's default.
DEFAULT_DEV_ORIGINS = (
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def api_token() -> str | None:
    token = os.getenv("CAREEROS_API_TOKEN", "").strip()
    return token or None


def auth_required() -> bool:
    """Whether `/api/*` demands a credential of any kind."""
    from careeros.auth import AuthMode, auth_mode

    return api_token() is not None or auth_mode() is AuthMode.REQUIRED


def allowed_origins() -> list[str]:
    """Origins permitted to call the API from a browser.

    `CAREEROS_CORS_ORIGINS` is a comma-separated list. `*` is honoured only
    when a token is configured -- a wide-open CORS policy on an unauthenticated
    API would let any page the user visits read their career data.
    """
    configured = os.getenv("CAREEROS_CORS_ORIGINS", "").strip()
    if not configured:
        return list(DEFAULT_DEV_ORIGINS)
    origins = [o.strip() for o in configured.split(",") if o.strip()]
    if "*" in origins and not auth_required():
        # Refuse the dangerous combination rather than silently obeying it.
        return list(DEFAULT_DEV_ORIGINS)
    return origins


def _service_token_matches(presented: str) -> bool:
    expected = api_token()
    if not expected:
        return False
    # Constant-time compare: the token is a bearer secret.
    return hmac.compare_digest(presented, expected)


def _credential_accepted(presented: str) -> bool:
    """A session token or the service token. Nothing else."""
    from careeros.auth.tokens import TokenError, looks_like_session_token, read_token

    if looks_like_session_token(presented):
        try:
            read_token(presented)
        except TokenError:
            return False
        return True
    return _service_token_matches(presented)


def install_security(app: FastAPI, public_paths: Iterable[str] = PUBLIC_PATHS) -> None:
    """Attach CORS and the bearer-token gate."""
    public = frozenset(public_paths)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,      # bearer token, not cookies
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def require_token(
        request: Request, call_next: Callable[[Request], Awaitable]
    ):
        if not auth_required():
            return await call_next(request)
        path = request.url.path
        # Preflight carries no Authorization header by design.
        if request.method == "OPTIONS" or path in public or not path.startswith("/api/"):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented or not _credential_accepted(presented):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Sign in to continue. POST /api/auth/google/start or "
                        "/api/auth/email/start, or present CAREEROS_API_TOKEN "
                        "as a bearer token for machine access."
                    ),
                    "sign_in": "/api/auth/describe",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
