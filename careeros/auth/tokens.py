"""Session tokens: signed, self-describing, and stdlib-only.

A JWT library would be one more dependency for a format we only ever issue to
ourselves, so this is HMAC-SHA256 over compact JSON. The parts that matter are
the parts people get wrong:

* the signature is compared in constant time;
* expiry is checked on read, not trusted from the client;
* the secret is generated once and stored outside the database with 0600, so a
  leaked database backup does not let anyone mint sessions;
* rotating the secret invalidates every existing session, which is the desired
  behaviour for a rotation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

PREFIX = "cos1"
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # a month; this is a personal tool
_SECRET_ENV = "CAREEROS_SESSION_SECRET"


class TokenError(ValueError):
    """The token is absent, malformed, unsigned by us, or expired."""


def _secret_path() -> Path:
    home = Path(os.getenv("CAREEROS_HOME", Path.home() / ".careeros"))
    return home / "session_secret"


def session_secret() -> bytes:
    """The signing key: from the environment, or generated once on disk."""
    configured = os.getenv(_SECRET_ENV, "").strip()
    if configured:
        return configured.encode("utf-8")

    path = _secret_path()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value.encode("utf-8")

    generated = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with the right mode from the start rather than chmod-ing after.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(generated)
    return generated.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class SessionToken:
    user_id: int
    email: str
    issued_at: int
    expires_at: int
    method: str = "unknown"

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))


def issue_token(
    user_id: int,
    email: str,
    *,
    method: str = "unknown",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    payload = {
        "uid": int(user_id),
        "em": email.strip().lower(),
        "iat": issued,
        "exp": issued + int(ttl_seconds),
        "m": method,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{PREFIX}.{body}.{_b64(signature)}"


def read_token(token: str) -> SessionToken:
    """Verify and decode. Raises `TokenError` for every failure mode."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise TokenError("not a CareerOS session token")
    _, body, signature = parts

    expected = hmac.new(session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        presented = _unb64(signature)
    except Exception as exc:  # malformed base64
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, presented):
        raise TokenError("signature does not verify")

    try:
        payload = json.loads(_unb64(body))
        parsed = SessionToken(
            user_id=int(payload["uid"]),
            email=str(payload["em"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            method=str(payload.get("m", "unknown")),
        )
    except Exception as exc:
        raise TokenError("malformed payload") from exc

    if parsed.expired:
        raise TokenError("session expired - sign in again")
    return parsed


def looks_like_session_token(value: str) -> bool:
    return (value or "").strip().startswith(f"{PREFIX}.")
