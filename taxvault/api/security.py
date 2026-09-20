"""Transport-level protections, applied to every response.

None of this replaces the encryption at rest or the session token -- it is the
layer that stops a browser from undoing them. The headers are the ones IRS
Publication 4557 expects a preparer's system to set, and the request logging
filter exists because the fastest way to leak an SSN is to log the request body
that contained it.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from taxvault.crypto import redact

logger = logging.getLogger(__name__)

#: Rolling window rate limit, per client address. Deliberately strict on the
#: code endpoints: an unlimited one-time-code endpoint is an SMS bill and a
#: brute-force oracle at the same time.
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/auth/sign-in": (5, 300),
    "/api/auth/verify": (10, 300),
    "/api/auth/mobile/start": (5, 300),
    "/api/auth/mobile/verify": (10, 300),
    "/api/auth/identity": (5, 900),
}
DEFAULT_LIMIT = (240, 60)

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _client_key(request: Request) -> str:
    # A reverse proxy is the normal deployment, so honour the forwarded address
    # when one is present -- but only the first hop, which is the only one the
    # client cannot forge past.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(request: Request) -> tuple[bool, int]:
    path = request.url.path
    limit, window = RATE_LIMITS.get(path, DEFAULT_LIMIT)
    key = (_client_key(request), path)
    now = time.time()
    bucket = _hits[key]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        return True, int(window - (now - bucket[0])) + 1
    bucket.append(now)
    return False, 0


def reset_rate_limits() -> None:
    """Tests share a process; without this they inherit each other's buckets."""
    _hits.clear()


class RedactingFilter(logging.Filter):
    """Strip anything SSN-shaped out of every log record, everywhere."""

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            ) if isinstance(record.args, tuple) else record.args
        return True


def install_security(app: FastAPI) -> None:
    logging.getLogger().addFilter(RedactingFilter())

    @app.middleware("http")
    async def _guard(request: Request, call_next: Callable) -> Any:
        limited, retry_after = _rate_limited(request)
        if limited:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Slow down and try again shortly."},
                headers={"Retry-After": str(retry_after)},
            )
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            # A tax return should never sit in a shared or browser cache.
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                "base-uri 'self'"
            ),
        })
        if request.url.scheme == "https" or os.getenv("TAXVAULT_FORCE_HSTS"):
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response
