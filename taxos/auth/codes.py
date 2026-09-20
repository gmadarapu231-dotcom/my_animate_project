"""One-time codes, over email and SMS.

No passwords exist anywhere in this system. A password is a thing that can be
reused, phished, and found in a breach dump next to the same person's SSN, and
a tax preparer's client list is exactly the target that attracts that. A code
sent to a channel the client already controls is both simpler and stronger.

Codes are hashed with the destination mixed in, so a hash lifted from the
database cannot be replayed against a different address. Attempts are capped,
because a six-digit code with unlimited guesses is a four-digit code.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from typing import Callable

from taxos.crypto import data_key

CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 5


class DeliveryError(RuntimeError):
    """The code could not be sent. The message is safe to show the user."""


def new_code() -> str:
    """A uniformly random numeric code. `secrets`, never `random`."""
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


def hash_code(code: str, destination: str) -> str:
    """Keyed hash, bound to the destination so it cannot be replayed elsewhere."""
    key = data_key("auth-code")
    message = f"{destination.strip().lower()}|{(code or '').strip()}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def code_matches(presented: str, destination: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_code(presented, destination), stored_hash or "")


# ---------------------------------------------------------------------------
# destinations
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def normalise_email(value: str) -> str:
    address = (value or "").strip().lower()
    if not _EMAIL.match(address):
        raise ValueError("That does not look like an email address.")
    return address


def normalise_mobile(value: str) -> str:
    """To E.164. A bare 10-digit number is assumed to be US, which is the
    right assumption for a US tax product and is stated rather than hidden."""
    digits = re.sub(r"[^\d+]", "", value or "")
    if digits.startswith("+"):
        rest = re.sub(r"\D", "", digits)
        if len(rest) < 8 or len(rest) > 15:
            raise ValueError("That does not look like a mobile number.")
        return f"+{rest}"
    digits = re.sub(r"\D", "", digits)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ValueError("Enter a 10-digit US mobile number, or a full number starting with +.")


def mask_mobile(e164: str) -> str:
    return f"(•••) •••-{e164[-4:]}" if len(e164) >= 4 else "•••"


def mask_email(address: str) -> str:
    name, _, domain = (address or "").partition("@")
    if not domain:
        return "•••"
    head = name[0] if name else "•"
    return f"{head}{'•' * max(2, len(name) - 1)}@{domain}"


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------
def email_settings_missing() -> list[str]:
    return [name for name in ("TAXOS_SMTP_HOST", "TAXOS_SMTP_FROM") if not os.getenv(name)]


def sms_settings_missing() -> list[str]:
    return [
        name for name in
        ("TAXOS_SMS_ACCOUNT_SID", "TAXOS_SMS_AUTH_TOKEN", "TAXOS_SMS_FROM")
        if not os.getenv(name)
    ]


def send_email_code(destination: str, code: str) -> None:
    missing = email_settings_missing()
    if missing:
        raise DeliveryError("Email is not configured on this server: set " + ", ".join(missing))
    message = EmailMessage()
    message["Subject"] = f"{code} is your sign-in code"
    message["From"] = os.environ["TAXOS_SMTP_FROM"]
    message["To"] = destination
    message.set_content(
        f"Your sign-in code is {code}.\n\n"
        f"It expires in {CODE_TTL_SECONDS // 60} minutes and can be used once.\n\n"
        "Nobody from this service will ever ask you for this code, or for your "
        "Social Security number, by phone or email. If you did not request this, "
        "ignore it and the code will expire on its own.\n"
    )
    host = os.environ["TAXOS_SMTP_HOST"]
    port = int(os.getenv("TAXOS_SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            user, password = os.getenv("TAXOS_SMTP_USER"), os.getenv("TAXOS_SMTP_PASSWORD")
            if user and password:
                server.login(user, password)
            server.send_message(message)
    except Exception as exc:
        raise DeliveryError(f"Could not send the email: {exc}") from exc


def send_sms_code(destination: str, code: str) -> None:
    """Send over SMS. The provider call is deliberately not implemented.

    Wiring a real gateway means credentials and a billing account, so this
    raises a clear error rather than silently doing nothing -- which would
    leave a client staring at a screen waiting for a code that is not coming.
    """
    missing = sms_settings_missing()
    if missing:
        raise DeliveryError(
            "SMS is not configured on this server: set " + ", ".join(missing)
            + ". Until then, verify the mobile number by another means."
        )
    raise DeliveryError(
        "SMS delivery is configured but no gateway client is installed. Add the "
        "provider SDK and implement `send_sms_code`."
    )


def sender_for(channel: str) -> Callable[[str, str], None]:
    return send_sms_code if channel == "sms" else send_email_code
