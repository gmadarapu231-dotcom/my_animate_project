"""Sign in with Google, via the OAuth authorization-code flow.

The identity comes from Google's userinfo endpoint, called with an access token
this server obtained itself from Google's token endpoint over TLS. That is why
there is no JWT signature verification here and no crypto dependency: we are
not accepting an ID token handed to us by a client, we are asking Google
directly who the token belongs to.

PKCE is used even though this is a confidential client. It costs one hash and
removes the value of an intercepted authorization code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: Identity only. Gmail scopes are added on request, so a user can sign in
#: without granting mailbox access at all.
IDENTITY_SCOPES = ("openid", "email", "profile")
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)

DEFAULT_REDIRECT_URI = "http://localhost:8000/api/auth/google/callback"

#: (method, url, form-encoded body or None, headers) -> (status, parsed json)
JsonFetcher = Callable[[str, str, bytes | None, dict[str, str]], "tuple[int, Any]"]


class GoogleAuthError(RuntimeError):
    pass


class GoogleNotConfigured(GoogleAuthError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("Google sign-in is not configured: set " + ", ".join(missing))


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def config() -> GoogleConfig:
    return GoogleConfig(
        client_id=os.getenv("CAREEROS_GOOGLE_CLIENT_ID", "").strip(),
        client_secret=os.getenv("CAREEROS_GOOGLE_CLIENT_SECRET", "").strip(),
        redirect_uri=os.getenv("CAREEROS_GOOGLE_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
    )


def missing_settings() -> list[str]:
    cfg = config()
    out = []
    if not cfg.client_id:
        out.append("CAREEROS_GOOGLE_CLIENT_ID")
    if not cfg.client_secret:
        out.append("CAREEROS_GOOGLE_CLIENT_SECRET")
    return out


def _require_config() -> GoogleConfig:
    missing = missing_settings()
    if missing:
        raise GoogleNotConfigured(missing)
    return config()


# --- PKCE ------------------------------------------------------------------
def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# --- default transport -----------------------------------------------------
def _urllib_fetch(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:  # noqa: S310 - fixed hosts
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"").decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:400]}
    except urllib.error.URLError as exc:
        raise GoogleAuthError(f"could not reach {urllib.parse.urlsplit(url).hostname}: {exc.reason}") from exc


# --- the flow --------------------------------------------------------------
def authorization_url(
    *,
    state: str,
    verifier: str,
    include_gmail: bool = False,
    redirect_uri: str | None = None,
    login_hint: str | None = None,
) -> str:
    cfg = _require_config()
    scopes = list(IDENTITY_SCOPES) + (list(GMAIL_SCOPES) if include_gmail else [])
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": redirect_uri or cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        # offline + consent so a Gmail grant yields a refresh token the mail
        # layer can keep using; identity-only sign-in does not need one but it
        # is harmless.
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent" if include_gmail else "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None
    granted_scopes: tuple[str, ...]
    refresh_token: str | None


def exchange_code(
    code: str,
    *,
    verifier: str,
    redirect_uri: str | None = None,
    fetch: JsonFetcher | None = None,
) -> GoogleIdentity:
    """Trade the authorization code for tokens, then ask Google who this is."""
    cfg = _require_config()
    send = fetch or _urllib_fetch

    form = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "redirect_uri": redirect_uri or cfg.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode("ascii")

    status, payload = send(
        "POST",
        TOKEN_ENDPOINT,
        form,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error_description") or payload.get("error") or "")
        raise GoogleAuthError(f"Google rejected the sign-in code{': ' + detail if detail else ''}")

    access_token = str(payload["access_token"])
    granted = tuple(str(payload.get("scope", "")).split())
    refresh = payload.get("refresh_token")

    status, info = send(
        "GET", USERINFO_ENDPOINT, None, {"Authorization": f"Bearer {access_token}"}
    )
    if status != 200 or not isinstance(info, dict):
        raise GoogleAuthError("Google issued a token but would not return the account's identity")

    email = str(info.get("email", "")).strip().lower()
    if not email:
        raise GoogleAuthError("Google returned no email address for this account")
    if not info.get("email_verified", False):
        # An unverified address is not an identity: anyone could have typed it.
        raise GoogleAuthError(f"{email} is not verified with Google")

    return GoogleIdentity(
        subject=str(info.get("sub", "")),
        email=email,
        email_verified=True,
        name=(str(info["name"]).strip() if info.get("name") else None),
        picture=(str(info["picture"]) if info.get("picture") else None),
        granted_scopes=granted,
        refresh_token=str(refresh) if refresh else None,
    )


def grants_gmail(scopes: tuple[str, ...]) -> bool:
    return any(scope in scopes for scope in GMAIL_SCOPES)
