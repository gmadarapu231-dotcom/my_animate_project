"""Sign-in, account resolution, and the mode the server is running in.

Three modes, and the default is chosen from what is configured rather than
demanded up front:

* `open`     -- no sign-in. The single-user localhost case the CLI and the
                built-in dashboard have always used. Chosen when neither Google
                nor SMTP is set up, because requiring a sign-in nobody can
                perform is just a locked door with no key.
* `required` -- a session token is needed for every `/api/*` call. Chosen
                automatically as soon as a sign-in method exists.

`CAREEROS_AUTH` overrides the choice in either direction.

Accounts are keyed on a *verified* email address. Google's `email_verified` is
checked, and an emailed code is proof by possession; an unverified address is
refused rather than trusted, because otherwise anyone could type someone
else's.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.auth import email_code as codes
from careeros.auth import google
from careeros.auth.tokens import DEFAULT_TTL_SECONDS, TokenError, issue_token, read_token
from careeros.db.models import AuthChallenge, User

logger = logging.getLogger(__name__)

CHALLENGE_TTL_SECONDS = 15 * 60


class AuthError(RuntimeError):
    """Sign-in failed. The message is safe to show the user."""


class AuthMode(str, Enum):
    OPEN = "open"
    REQUIRED = "required"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def auth_mode() -> AuthMode:
    configured = os.getenv("CAREEROS_AUTH", "").strip().lower()
    if configured in ("required", "on", "1", "true"):
        return AuthMode.REQUIRED
    if configured in ("open", "off", "0", "false", "none"):
        return AuthMode.OPEN
    # No explicit choice: require sign-in exactly when one is possible.
    if not google.missing_settings() or not codes.missing_settings():
        return AuthMode.REQUIRED
    return AuthMode.OPEN


def describe_auth() -> dict[str, Any]:
    """Everything a login screen needs, and no secrets."""
    google_missing = google.missing_settings()
    smtp_missing = codes.missing_settings()
    methods = []
    if not google_missing:
        methods.append("google")
    if not smtp_missing:
        methods.append("email_code")
    return {
        "mode": auth_mode().value,
        "methods": methods,
        "google": {
            "available": not google_missing,
            "missing_env": google_missing,
            "redirect_uri": google.config().redirect_uri,
            "scopes_identity": list(google.IDENTITY_SCOPES),
            "scopes_gmail": list(google.GMAIL_SCOPES),
        },
        "email_code": {
            "available": not smtp_missing,
            "missing_env": smtp_missing,
            "code_length": codes.CODE_LENGTH,
            "expires_in_seconds": codes.CODE_TTL_SECONDS,
        },
        "passwords": "never stored or requested",
        "session_ttl_seconds": DEFAULT_TTL_SECONDS,
    }


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
@dataclass
class Identity:
    email: str
    name: str | None = None
    picture: str | None = None
    google_subject: str | None = None
    method: str = "unknown"


@dataclass
class SignInResult:
    token: str
    user: User
    created: bool
    granted_gmail: bool = False
    refresh_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "created": self.created,
            "granted_gmail": self.granted_gmail,
            "user": {
                "id": self.user.id,
                "email": self.user.email,
                "full_name": self.user.full_name,
                "picture_url": self.user.picture_url,
                "home_country": self.user.home_country,
                "has_profile": bool(self.user.current_title or self.user.tracks),
            },
        }


def find_user(session: Session, email: str) -> User | None:
    target = email.strip().lower()
    return session.scalars(select(User).where(User.email == target)).first()


def upsert_account(session: Session, identity: Identity) -> tuple[User, bool]:
    """Find the account for a verified address, or open one."""
    email = identity.email.strip().lower()
    if "@" not in email:
        raise AuthError(f"{email!r} is not an email address")

    user = find_user(session, email)
    created = False
    if user is None:
        user = User(
            email=email,
            full_name=identity.name or email.split("@", 1)[0],
            links={},
            preferred_locations=[],
            industries=[],
        )
        session.add(user)
        created = True

    # Google's subject is the stable key if the address is ever renamed.
    if identity.google_subject:
        user.google_subject = identity.google_subject
    if identity.picture and not user.picture_url:
        user.picture_url = identity.picture
    if identity.name and (created or not user.full_name):
        user.full_name = identity.name
    user.email_verified_at = user.email_verified_at or _now()
    user.last_login_at = _now()
    user.last_auth_method = identity.method

    session.flush()
    return user, created


# ---------------------------------------------------------------------------
# challenges
# ---------------------------------------------------------------------------
def _expire_old(session: Session) -> None:
    """Housekeeping on every sign-in attempt; no scheduler needed."""
    cutoff = _now() - timedelta(days=1)
    for row in session.scalars(select(AuthChallenge).where(AuthChallenge.created_at < cutoff)).all():
        session.delete(row)


def start_google(
    session: Session,
    *,
    include_gmail: bool = False,
    redirect_uri: str | None = None,
    login_hint: str | None = None,
) -> dict[str, Any]:
    """Begin the OAuth flow: persist the state and PKCE verifier, return the URL."""
    if google.missing_settings():
        raise AuthError(
            "Google sign-in is not configured on this server. Set "
            + ", ".join(google.missing_settings())
        )
    _expire_old(session)
    state = secrets.token_urlsafe(24)
    verifier = google.new_verifier()
    session.add(
        AuthChallenge(
            kind="google_state",
            state=state,
            verifier=verifier,
            redirect_uri=redirect_uri,
            include_gmail=include_gmail,
            expires_at=_now() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        )
    )
    session.flush()
    url = google.authorization_url(
        state=state,
        verifier=verifier,
        include_gmail=include_gmail,
        redirect_uri=redirect_uri,
        login_hint=login_hint,
    )
    return {"authorization_url": url, "state": state, "expires_in_seconds": CHALLENGE_TTL_SECONDS}


def sign_in_with_google(
    session: Session,
    *,
    code: str,
    state: str,
    fetch: google.JsonFetcher | None = None,
) -> SignInResult:
    challenge = session.scalars(
        select(AuthChallenge).where(
            AuthChallenge.kind == "google_state", AuthChallenge.state == state
        )
    ).first()
    if challenge is None:
        raise AuthError("That sign-in link is not one this server issued. Start again.")
    if challenge.consumed_at is not None:
        raise AuthError("That sign-in link was already used. Start again.")
    if challenge.expires_at < _now():
        raise AuthError("That sign-in link expired. Start again.")

    # Single-use, marked before the exchange so a replay cannot race it.
    challenge.consumed_at = _now()
    session.flush()

    try:
        identity = google.exchange_code(
            code,
            verifier=challenge.verifier or "",
            redirect_uri=challenge.redirect_uri,
            fetch=fetch,
        )
    except google.GoogleAuthError as exc:
        raise AuthError(str(exc)) from exc

    user, created = upsert_account(
        session,
        Identity(
            email=identity.email,
            name=identity.name,
            picture=identity.picture,
            google_subject=identity.subject or None,
            method="google",
        ),
    )
    return SignInResult(
        token=issue_token(user.id, user.email, method="google"),
        user=user,
        created=created,
        granted_gmail=google.grants_gmail(identity.granted_scopes),
        refresh_token=identity.refresh_token,
    )


def start_email_code(
    session: Session,
    email: str,
    *,
    sender: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Email a one-time code. Returns when it was sent, never the code itself."""
    address = (email or "").strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise AuthError("That does not look like an email address.")
    if codes.missing_settings() and sender is None:
        raise AuthError(
            "This server cannot send email yet. Set "
            + ", ".join(codes.missing_settings())
            + ", or sign in with Google."
        )
    _expire_old(session)

    # Supersede any code already outstanding for this address.
    for row in session.scalars(
        select(AuthChallenge).where(
            AuthChallenge.kind == "email_code",
            AuthChallenge.email == address,
            AuthChallenge.consumed_at.is_(None),
        )
    ).all():
        row.consumed_at = _now()

    code = codes.new_code()
    session.add(
        AuthChallenge(
            kind="email_code",
            email=address,
            code_hash=codes.hash_code(code, address),
            expires_at=_now() + timedelta(seconds=codes.CODE_TTL_SECONDS),
        )
    )
    session.flush()

    try:
        # `is not None`, not `or`: a callable object can be falsy.
        send = sender if sender is not None else codes.send_code
        send(address, code)
    except codes.EmailCodeError as exc:
        raise AuthError(str(exc)) from exc

    return {
        "sent_to": address,
        "code_length": codes.CODE_LENGTH,
        "expires_in_seconds": codes.CODE_TTL_SECONDS,
        "attempts_allowed": codes.MAX_ATTEMPTS,
    }


def sign_in_with_code(session: Session, email: str, code: str) -> SignInResult:
    address = (email or "").strip().lower()
    challenge = session.scalars(
        select(AuthChallenge)
        .where(
            AuthChallenge.kind == "email_code",
            AuthChallenge.email == address,
            AuthChallenge.consumed_at.is_(None),
        )
        .order_by(AuthChallenge.id.desc())
    ).first()
    if challenge is None:
        raise AuthError("No sign-in code is outstanding for that address. Request one.")
    if challenge.expires_at < _now():
        challenge.consumed_at = _now()
        raise AuthError("That code expired. Request a new one.")
    if challenge.attempts >= codes.MAX_ATTEMPTS:
        challenge.consumed_at = _now()
        raise AuthError("Too many wrong codes. Request a new one.")

    challenge.attempts += 1
    session.flush()
    if not codes.code_matches(code or "", address, challenge.code_hash or ""):
        remaining = max(0, codes.MAX_ATTEMPTS - challenge.attempts)
        raise AuthError(f"That code is not right. {remaining} attempt(s) left.")

    challenge.consumed_at = _now()
    user, created = upsert_account(session, Identity(email=address, method="email_code"))
    return SignInResult(
        token=issue_token(user.id, user.email, method="email_code"),
        user=user,
        created=created,
    )


# ---------------------------------------------------------------------------
# reading a session back
# ---------------------------------------------------------------------------
def resolve_session(session: Session, token: str) -> User:
    """The account a token belongs to, or an `AuthError` saying why not."""
    try:
        parsed = read_token(token)
    except TokenError as exc:
        raise AuthError(str(exc)) from exc

    user = session.get(User, parsed.user_id)
    if user is None:
        raise AuthError("That account no longer exists.")
    # The email is signed into the token, so a mismatch means the row was
    # re-pointed at a different person. Refuse rather than serve their data.
    if user.email.strip().lower() != parsed.email:
        raise AuthError("That session no longer matches the account. Sign in again.")
    return user
