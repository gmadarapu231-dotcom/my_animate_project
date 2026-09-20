"""Session tokens: signed, self-describing, stdlib-only.

HMAC-SHA256 over compact JSON rather than a JWT library, because we only ever
issue these to ourselves and a dependency is a liability. What matters:

* the signature is compared in constant time;
* expiry is checked on read, never trusted from the client;
* the signing key lives outside the database with mode 0600, so a leaked
  database backup does not let anyone mint a session;
* sessions are short. A tax system holds SSNs, so a month-long session of the
  kind a personal tool can afford would be negligent here.
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

PREFIX = "tax1"
#: 12 hours. Long enough for an appointment, short enough that a stolen token
#: on a shared machine expires the same day.
DEFAULT_TTL_SECONDS = 12 * 3600
_SECRET_ENV = "TAXVAULT_SESSION_SECRET"


class TokenError(ValueError):
    """The token is absent, malformed, unsigned by us, or expired."""


def _secret_path() -> Path:
    return Path(os.getenv("TAXVAULT_HOME", Path.home() / ".taxvault")) / "session_secret"


def session_secret() -> bytes:
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
    account_id: int
    email: str
    issued_at: int
    expires_at: int
    method: str = "unknown"
    #: True once the client has proved SSN, email and mobile together. Reading
    #: tax data requires it; signing in alone does not.
    identity_verified: bool = False

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))


def issue_token(
    account_id: int,
    email: str,
    *,
    method: str = "unknown",
    identity_verified: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    payload = {
        "aid": int(account_id),
        "em": email.strip().lower(),
        "iat": issued,
        "exp": issued + int(ttl_seconds),
        "m": method,
        "iv": bool(identity_verified),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{PREFIX}.{body}.{_b64(signature)}"


def read_token(token: str) -> SessionToken:
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise TokenError("not a session token")
    _, body, signature = parts

    expected = hmac.new(session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        presented = _unb64(signature)
    except Exception as exc:
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, presented):
        raise TokenError("signature does not verify")

    try:
        payload = json.loads(_unb64(body))
        parsed = SessionToken(
            account_id=int(payload["aid"]),
            email=str(payload["em"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            method=str(payload.get("m", "unknown")),
            identity_verified=bool(payload.get("iv", False)),
        )
    except Exception as exc:
        raise TokenError("malformed payload") from exc

    if parsed.expired:
        raise TokenError("this session expired - sign in again")
    return parsed


def looks_like_session_token(value: str) -> bool:
    return (value or "").strip().startswith(f"{PREFIX}.")
