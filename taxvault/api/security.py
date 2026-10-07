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
import uuid
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
_last_sweep = 0.0

#: Above this many tracked buckets, sweep the expired ones out. Without a
#: bound, every (address, path) pair ever seen stays in memory for the life of
#: the process -- a slow leak that a scanner turns into a fast one.
_MAX_BUCKETS = 50_000
_SWEEP_INTERVAL = 60.0


def _trusted_proxies() -> set[str]:
    """Addresses whose X-Forwarded-For we believe. `*` means "any"."""
    raw = os.getenv("TAXVAULT_TRUSTED_PROXIES", "").strip()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _client_key(request: Request) -> str:
    """The address to count against, which a client must not be able to choose.

    X-Forwarded-For is a request header: anyone can send it. Honouring it
    unconditionally means a single attacker gets a fresh rate-limit bucket per
    forged address, which is the same as having no rate limit on the one-time
    code endpoints. So it is honoured only when the request actually arrived
    from a proxy we were told about.
    """
    direct = request.client.host if request.client else "unknown"
    trusted = _trusted_proxies()
    if trusted and ("*" in trusted or direct in trusted):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # The left-most hop is the originating client; everything after it
            # was added by infrastructure we do not control.
            return forwarded.split(",")[0].strip() or direct
    return direct


def _sweep(now: float) -> None:
    """Drop buckets with nothing left in their window."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL and len(_hits) < _MAX_BUCKETS:
        return
    _last_sweep = now
    widest = max(window for _, window in list(RATE_LIMITS.values()) + [DEFAULT_LIMIT])
    for key in [k for k, bucket in _hits.items()
                if not bucket or now - bucket[-1] > widest]:
        _hits.pop(key, None)


def _rate_limited(request: Request) -> tuple[bool, int]:
    path = request.url.path
    limit, window = RATE_LIMITS.get(path, DEFAULT_LIMIT)
    key = (_client_key(request), path)
    now = time.time()
    _sweep(now)
    bucket = _hits[key]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        return True, int(window - (now - bucket[0])) + 1
    bucket.append(now)
    return False, 0


def rate_limit_buckets() -> int:
    """How many buckets are being tracked. Exposed so a probe can watch it."""
    return len(_hits)


def reset_rate_limits() -> None:
    """Tests share a process; without this they inherit each other's buckets."""
    global _last_sweep
    _hits.clear()
    _last_sweep = 0.0


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
        # A correlation id on every request and every response. Without one,
        # a client saying "it failed at about two o'clock" is unanswerable --
        # and the alternative, logging the request body, is how an SSN ends up
        # in a log aggregator.
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id

        limited, retry_after = _rate_limited(request)
        if limited:
            logger.warning("rate limited %s %s id=%s", request.method,
                           request.url.path, request_id)
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Slow down and try again shortly."},
                headers={"Retry-After": str(retry_after),
                         "X-Request-ID": request_id},
            )
        started = time.time()
        try:
            response = await call_next(request)
        except Exception:
            # Log the failure with its id and re-raise. The body is never
            # logged, so the id is the only way to tie a client's report to a
            # stack trace.
            logger.exception("unhandled error %s %s id=%s", request.method,
                             request.url.path, request_id)
            raise
        response.headers["X-Request-ID"] = request_id
        elapsed = (time.time() - started) * 1000
        if elapsed > 2000:
            logger.warning("slow request %s %s %.0fms id=%s", request.method,
                           request.url.path, elapsed, request_id)
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


def configure_logging() -> None:
    """Structured-ish logging with the redaction filter attached everywhere.

    Not JSON by default: a tax practice reads its own logs far more often than
    it ships them to an aggregator, and a human-readable line with a request
    id beats a JSON blob in a terminal. Set TAXVAULT_LOG_JSON=1 when something
    downstream wants to parse it.
    """
    level = os.getenv("TAXVAULT_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler()
    if os.getenv("TAXVAULT_LOG_JSON", "").strip().lower() in ("1", "true", "yes", "on"):
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
    # The redaction filter goes on the HANDLER as well as the root logger:
    # a library that logs through its own logger still reaches this handler,
    # and that is the path by which an SSN would otherwise escape.
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # Libraries that log a line per HTTP call. Useful when chasing a problem,
    # noise the rest of the time, and at INFO they bury our own lines.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(
            os.getenv("TAXVAULT_LOG_LEVEL_LIBS", "WARNING").upper()
        )


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload)
