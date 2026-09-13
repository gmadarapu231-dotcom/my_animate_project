"""Transport security for the API.

Phase 1 ran on localhost, where "no auth" was defensible. A mobile app changes
that: the server now listens on a network interface and a phone talks to it
over the LAN, so the API needs a credential and a CORS policy.

Both are opt-in by configuration, and the default is the safe one:

* `CAREEROS_API_TOKEN` unset  -> the API binds to localhost and is open, which
  is the single-user desktop case the CLI and the built-in dashboard use.
  `/api/health` reports `auth_required: false` so a client can tell.
* `CAREEROS_API_TOKEN` set    -> every `/api/*` request needs
  `Authorization: Bearer <token>`. This is required as soon as the server is
  reachable from another device.

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

#: Paths that never require a token: the health probe (so a client can discover
#: whether auth is on) and the interactive docs.
PUBLIC_PATHS = frozenset({"/api/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})

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
    return api_token() is not None


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


def _token_matches(presented: str) -> bool:
    expected = api_token()
    if not expected:
        return True
    # Constant-time compare: the token is a bearer secret.
    return hmac.compare_digest(presented, expected)


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
        if scheme.lower() != "bearer" or not presented or not _token_matches(presented):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Missing or invalid bearer token. Set the same value as "
                        "CAREEROS_API_TOKEN in the app's Settings screen."
                    )
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
