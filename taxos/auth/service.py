"""Sign-in and identity verification.

There are two different gates here and conflating them is the mistake worth
avoiding:

* **Sign-in** proves control of an email address. It is enough to open the app
  and see an empty account.
* **Identity verification** proves SSN, email and mobile together. It is what
  is required before any tax data is read or written, which is why the session
  token carries `identity_verified` as a separate claim rather than treating a
  sign-in as sufficient for everything.

The SSN half deserves a word. Matching an SSN against a stored blind index
tells us the number is the one we already hold for this account -- it does not
tell us the person typing it is its owner. Real proof of that is knowledge-based
authentication or a document check against a bureau, which this system does not
do and does not pretend to. What it does instead is make the weaker claim
honestly: the number matches, a code was delivered to a phone and an address on
file, and the whole sequence is written to the audit trail.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from taxos.auth import codes
from taxos.auth.tokens import DEFAULT_TTL_SECONDS, TokenError, issue_token, read_token
from taxos.crypto import CryptoError, normalise_ssn, seal_ssn, ssn_index
from taxos.db.models import Account, AuditEvent, AuthChallenge, Taxpayer

logger = logging.getLogger(__name__)

#: How many failed code attempts before the account is frozen, and for how long.
LOCKOUT_THRESHOLD = 8
LOCKOUT_MINUTES = 30


class AuthError(RuntimeError):
    """Sign-in failed. The message is safe to show the user."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def audit(
    session: Session, action: str, *, account_id: int | None = None,
    actor: str | None = None, subject: str | None = None,
    ip_address: str | None = None, **detail: Any,
) -> None:
    """Write one line to the trail. Never raises: an audit failure must not
    take down the operation it was recording, but it must be logged."""
    try:
        session.add(AuditEvent(
            account_id=account_id, actor=actor, action=action, subject=subject,
            ip_address=ip_address, detail=detail,
        ))
        session.flush()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to write audit event %s", action)


def describe_auth() -> dict[str, Any]:
    """What the sign-in screen needs, and no secrets."""
    email_missing = codes.email_settings_missing()
    sms_missing = codes.sms_settings_missing()
    return {
        "methods": ["email_code"],
        "email": {"available": not email_missing, "missing_env": email_missing},
        "sms": {"available": not sms_missing, "missing_env": sms_missing},
        "passwords": "never stored or requested",
        "code_length": codes.CODE_LENGTH,
        "code_expires_in_seconds": codes.CODE_TTL_SECONDS,
        "session_ttl_seconds": DEFAULT_TTL_SECONDS,
        "identity_requires": ["ssn", "verified_email", "verified_mobile"],
        "development_mode": development_mode(),
    }


def development_mode() -> bool:
    """When set, codes are returned in the API response instead of delivered.

    This exists so the system is runnable before SMTP and an SMS gateway are
    wired up. It is off unless explicitly switched on, it is reported by
    `/api/auth/describe` so nobody can forget it is on, and it refuses to
    engage when a real database is configured.
    """
    if os.getenv("TAXOS_DEV_CODES", "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    url = os.getenv("TAXOS_DATABASE_URL", "")
    if url and not url.startswith("sqlite"):
        logger.warning("TAXOS_DEV_CODES ignored: a non-SQLite database is configured")
        return False
    return True


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
@dataclass
class SignInResult:
    token: str
    account: Account
    created: bool
    identity_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "created": self.created,
            "identity_verified": self.identity_verified,
            "expires_in_seconds": DEFAULT_TTL_SECONDS,
            "account": {
                "id": self.account.id,
                "email": self.account.email,
                "full_name": self.account.full_name,
                "mobile": codes.mask_mobile(self.account.mobile_e164 or ""),
                "mobile_verified": self.account.mobile_verified_at is not None,
                "role": self.account.role,
            },
        }


def find_account(session: Session, email: str) -> Account | None:
    return session.scalars(
        select(Account).where(Account.email == email.strip().lower())
    ).first()


def _check_not_locked(account: Account) -> None:
    if account.locked_until and account.locked_until > _now():
        minutes = int((account.locked_until - _now()).total_seconds() // 60) + 1
        raise AuthError(
            f"This account is temporarily locked after too many failed codes. "
            f"Try again in {minutes} minute(s)."
        )


def _expire_stale(session: Session) -> None:
    """Housekeeping on every attempt, so no scheduler is needed."""
    cutoff = _now() - timedelta(days=1)
    for row in session.scalars(
        select(AuthChallenge).where(AuthChallenge.created_at < cutoff)
    ).all():
        session.delete(row)


def _issue_challenge(
    session: Session, *, channel: str, purpose: str, destination: str,
    account_id: int | None, sender: Callable[[str, str], None] | None,
) -> dict[str, Any]:
    _expire_stale(session)
    # Supersede anything already outstanding for this destination and purpose.
    for row in session.scalars(
        select(AuthChallenge).where(
            AuthChallenge.destination == destination,
            AuthChallenge.purpose == purpose,
            AuthChallenge.consumed_at.is_(None),
        )
    ).all():
        row.consumed_at = _now()

    code = codes.new_code()
    session.add(AuthChallenge(
        channel=channel, purpose=purpose, destination=destination,
        code_hash=codes.hash_code(code, destination), account_id=account_id,
        expires_at=_now() + timedelta(seconds=codes.CODE_TTL_SECONDS),
    ))
    session.flush()

    delivered = False
    send = sender if sender is not None else codes.sender_for(channel)
    try:
        send(destination, code)
        delivered = True
    except codes.DeliveryError:
        if not development_mode():
            raise
    masked = (codes.mask_mobile(destination) if channel == "sms"
              else codes.mask_email(destination))
    response = {
        "channel": channel,
        "sent_to": masked,
        "delivered": delivered,
        "code_length": codes.CODE_LENGTH,
        "expires_in_seconds": codes.CODE_TTL_SECONDS,
        "attempts_allowed": codes.MAX_ATTEMPTS,
    }
    if not delivered and development_mode():
        response["development_code"] = code
        response["warning"] = (
            "Development mode: the code is in this response because no delivery "
            "channel is configured. Never run a real deployment this way."
        )
    return response


def _consume_challenge(
    session: Session, *, destination: str, purpose: str, code: str
) -> AuthChallenge:
    challenge = session.scalars(
        select(AuthChallenge)
        .where(
            AuthChallenge.destination == destination,
            AuthChallenge.purpose == purpose,
            AuthChallenge.consumed_at.is_(None),
        )
        .order_by(AuthChallenge.id.desc())
    ).first()
    if challenge is None:
        raise AuthError("No code is outstanding for that. Request a new one.")
    if challenge.expires_at < _now():
        challenge.consumed_at = _now()
        raise AuthError("That code expired. Request a new one.")
    if challenge.attempts >= codes.MAX_ATTEMPTS:
        challenge.consumed_at = _now()
        raise AuthError("Too many wrong codes. Request a new one.")

    challenge.attempts += 1
    session.flush()
    if not codes.code_matches(code or "", destination, challenge.code_hash or ""):
        remaining = max(0, codes.MAX_ATTEMPTS - challenge.attempts)
        raise AuthError(f"That code is not right. {remaining} attempt(s) left.")
    challenge.consumed_at = _now()
    return challenge


# ---------------------------------------------------------------------------
# sign-in
# ---------------------------------------------------------------------------
def start_sign_in(
    session: Session, email: str, *, sender: Callable[[str, str], None] | None = None
) -> dict[str, Any]:
    address = codes.normalise_email(email)
    account = find_account(session, address)
    if account is not None:
        _check_not_locked(account)
    return _issue_challenge(
        session, channel="email", purpose="sign_in", destination=address,
        account_id=account.id if account else None, sender=sender,
    )


def complete_sign_in(
    session: Session, email: str, code: str, *, ip_address: str | None = None
) -> SignInResult:
    address = codes.normalise_email(email)
    account = find_account(session, address)
    if account is not None:
        _check_not_locked(account)
    try:
        _consume_challenge(session, destination=address, purpose="sign_in", code=code)
    except AuthError:
        if account is not None:
            account.failed_attempts += 1
            if account.failed_attempts >= LOCKOUT_THRESHOLD:
                account.locked_until = _now() + timedelta(minutes=LOCKOUT_MINUTES)
                account.failed_attempts = 0
            session.flush()
        raise

    created = account is None
    if account is None:
        account = Account(email=address, full_name=address.split("@", 1)[0])
        session.add(account)
        session.flush()
    account.email_verified_at = account.email_verified_at or _now()
    account.last_login_at = _now()
    account.failed_attempts = 0
    account.locked_until = None
    session.flush()

    verified = _identity_is_verified(session, account)
    audit(session, "sign_in", account_id=account.id, actor=account.email,
          ip_address=ip_address, created=created)
    return SignInResult(
        token=issue_token(account.id, account.email, method="email_code",
                          identity_verified=verified),
        account=account, created=created, identity_verified=verified,
    )


# ---------------------------------------------------------------------------
# mobile
# ---------------------------------------------------------------------------
def start_mobile_verification(
    session: Session, account: Account, mobile: str,
    *, sender: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    number = codes.normalise_mobile(mobile)
    account.mobile_e164 = number
    account.mobile_verified_at = None
    session.flush()
    return _issue_challenge(
        session, channel="sms", purpose="verify_mobile", destination=number,
        account_id=account.id, sender=sender,
    )


def complete_mobile_verification(
    session: Session, account: Account, code: str, *, ip_address: str | None = None
) -> dict[str, Any]:
    if not account.mobile_e164:
        raise AuthError("No mobile number is on file. Enter one first.")
    _consume_challenge(
        session, destination=account.mobile_e164, purpose="verify_mobile", code=code
    )
    account.mobile_verified_at = _now()
    session.flush()
    audit(session, "mobile_verified", account_id=account.id, actor=account.email,
          ip_address=ip_address, mobile=codes.mask_mobile(account.mobile_e164))
    return {
        "mobile": codes.mask_mobile(account.mobile_e164),
        "verified": True,
    }


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def _identity_is_verified(session: Session, account: Account) -> bool:
    if account.email_verified_at is None or account.mobile_verified_at is None:
        return False
    return session.scalars(
        select(Taxpayer).where(
            Taxpayer.account_id == account.id,
            Taxpayer.relationship_to_filer == "self",
            Taxpayer.ssn_verified_at.is_not(None),
        )
    ).first() is not None


def verify_identity(
    session: Session,
    account: Account,
    *,
    ssn: str,
    email: str,
    mobile: str,
    first_name: str = "",
    last_name: str = "",
    date_of_birth: Any = None,
    resident_state: str = "",
    ip_address: str | None = None,
) -> dict[str, Any]:
    """Bind an SSN to this account, once email and mobile are both proven.

    The three factors must agree with the account: an SSN submitted with
    somebody else's email is the shape of an account-takeover attempt, so the
    email and mobile are checked against the signed-in account rather than
    taken at face value from the request body.
    """
    address = codes.normalise_email(email)
    if address != account.email:
        raise AuthError(
            "The email address does not match the signed-in account. For safety, "
            "identity has to be confirmed on the account it belongs to."
        )
    if account.email_verified_at is None:
        raise AuthError("Verify the email address first.")

    number = codes.normalise_mobile(mobile)
    if account.mobile_verified_at is None or number != (account.mobile_e164 or ""):
        raise AuthError(
            "Verify this mobile number first: we send a code to it and you enter the code."
        )

    try:
        tax_id = normalise_ssn(ssn)
    except ValueError as exc:
        raise AuthError(str(exc)) from exc

    index = ssn_index(tax_id)
    # Is this SSN already attached to a different account? That is either a
    # duplicate registration or an attempt to claim someone else's record.
    clash = session.scalars(
        select(Taxpayer).where(
            Taxpayer.ssn_index == index, Taxpayer.account_id != account.id
        )
    ).first()
    if clash is not None:
        audit(session, "ssn_conflict", account_id=account.id, actor=account.email,
              ip_address=ip_address, subject=f"ssn:{tax_id.last4}")
        raise AuthError(
            "That Social Security number is already registered to a different account. "
            "If this is yours, contact support rather than opening a second account -- "
            "two accounts with one SSN cause rejected filings."
        )

    taxpayer = session.scalars(
        select(Taxpayer).where(
            Taxpayer.account_id == account.id,
            Taxpayer.relationship_to_filer == "self",
        )
    ).first()
    if taxpayer is None:
        taxpayer = Taxpayer(account_id=account.id, relationship_to_filer="self")
        session.add(taxpayer)
        session.flush()

    ciphertext, blind_index, last4, kind = seal_ssn(
        tax_id.digits, context=f"taxpayer:{taxpayer.id}"
    )
    taxpayer.ssn_encrypted = ciphertext
    taxpayer.ssn_index = blind_index
    taxpayer.ssn_last4 = last4
    taxpayer.ssn_kind = kind
    taxpayer.ssn_verified_at = _now()
    taxpayer.email = account.email
    taxpayer.mobile_e164 = account.mobile_e164
    if first_name:
        taxpayer.first_name = first_name.strip()
    if last_name:
        taxpayer.last_name = last_name.strip()
    if date_of_birth:
        taxpayer.date_of_birth = date_of_birth
    if resident_state:
        taxpayer.resident_state = resident_state.strip().upper()
    if not account.full_name or account.full_name == account.email.split("@", 1)[0]:
        account.full_name = taxpayer.display_name
    session.flush()

    audit(session, "identity_verified", account_id=account.id, actor=account.email,
          subject=f"taxpayer:{taxpayer.id}", ip_address=ip_address,
          ssn_last4=last4, kind=kind)

    return {
        "taxpayer_id": taxpayer.id,
        "name": taxpayer.display_name,
        "ssn": f"***-**-{last4}",
        "ssn_kind": kind,
        "email": codes.mask_email(account.email),
        "mobile": codes.mask_mobile(account.mobile_e164 or ""),
        "resident_state": taxpayer.resident_state,
        "identity_verified": True,
        "token": issue_token(account.id, account.email, method="identity",
                             identity_verified=True),
        "assurance": (
            "This confirms the Social Security number matches this account and that "
            "codes were delivered to the email address and mobile number on file. It "
            "is not a government identity check: before a return is transmitted, the "
            "IRS matches the name and SSN against Social Security Administration "
            "records itself."
        ),
    }


def resolve_session(session: Session, token: str) -> tuple[Account, bool]:
    """The account a token belongs to, and whether identity was proven."""
    try:
        parsed = read_token(token)
    except TokenError as exc:
        raise AuthError(str(exc)) from exc
    account = session.get(Account, parsed.account_id)
    if account is None:
        raise AuthError("That account no longer exists.")
    if account.email.strip().lower() != parsed.email:
        raise AuthError("That session no longer matches the account. Sign in again.")
    if account.locked_until and account.locked_until > _now():
        raise AuthError("This account is temporarily locked.")
    # Trust the database over the token: revoking verification must take effect
    # immediately, not when the token happens to expire.
    return account, parsed.identity_verified and _identity_is_verified(session, account)
