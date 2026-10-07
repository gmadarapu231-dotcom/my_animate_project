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

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Callable

from taxvault.crypto import data_key

CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 5


logger = logging.getLogger(__name__)


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
    return [name for name in ("TAXVAULT_SMTP_HOST", "TAXVAULT_SMTP_FROM") if not os.getenv(name)]


def sms_settings_missing() -> list[str]:
    return [
        name for name in
        ("TAXVAULT_SMS_ACCOUNT_SID", "TAXVAULT_SMS_AUTH_TOKEN", "TAXVAULT_SMS_FROM")
        if not os.getenv(name)
    ]


def send_email_code(destination: str, code: str) -> None:
    missing = email_settings_missing()
    if missing:
        raise DeliveryError("Email is not configured on this server: set " + ", ".join(missing))
    message = EmailMessage()
    message["Subject"] = f"{code} is your sign-in code"
    message["From"] = os.environ["TAXVAULT_SMTP_FROM"]
    message["To"] = destination
    message.set_content(
        f"Your sign-in code is {code}.\n\n"
        f"It expires in {CODE_TTL_SECONDS // 60} minutes and can be used once.\n\n"
        "Nobody from this service will ever ask you for this code, or for your "
        "Social Security number, by phone or email. If you did not request this, "
        "ignore it and the code will expire on its own.\n"
    )
    host = os.environ["TAXVAULT_SMTP_HOST"]
    port = int(os.getenv("TAXVAULT_SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            user, password = os.getenv("TAXVAULT_SMTP_USER"), os.getenv("TAXVAULT_SMTP_PASSWORD")
            if user and password:
                server.login(user, password)
            server.send_message(message)
    except Exception as exc:
        raise DeliveryError(f"Could not send the email: {exc}") from exc


#: Twilio's REST endpoint. Hard-coded rather than configurable because a
#: configurable SMS endpoint is a way to exfiltrate one-time codes.
_TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def send_sms_code(destination: str, code: str) -> None:
    """Send the code by SMS through Twilio's REST API.

    No SDK: it is one form-encoded POST with basic auth, and a tax system does
    not need another dependency with its own transitive tree for that. The
    endpoint is fixed in code on purpose -- a configurable SMS endpoint is a
    way for a compromised environment variable to redirect every sign-in code
    to somebody else.

    Set `TAXVAULT_SMS_PROVIDER=console` in development to print the code to the
    log instead of sending it. That is refused outside development, because a
    code in a log is a code in a log aggregator.
    """
    missing = sms_settings_missing()
    provider = os.getenv("TAXVAULT_SMS_PROVIDER", "twilio").strip().lower()

    if provider == "console":
        from taxvault.settings import is_real_deployment

        if is_real_deployment():
            raise DeliveryError(
                "TAXVAULT_SMS_PROVIDER=console is not allowed outside development: "
                "it would write every sign-in code to the server log."
            )
        logger.warning("SMS console mode: code for %s is %s", mask_mobile(destination), code)
        return

    if missing:
        raise DeliveryError(
            "SMS is not configured on this server: set " + ", ".join(missing)
            + ". Identity verification needs a verified mobile number, so no "
            "client can finish signing up until this is set."
        )

    sid = os.environ["TAXVAULT_SMS_ACCOUNT_SID"]
    token = os.environ["TAXVAULT_SMS_AUTH_TOKEN"]
    sender = os.environ["TAXVAULT_SMS_FROM"]
    body = (
        f"{code} is your TaxVault code. It expires in "
        f"{CODE_TTL_SECONDS // 60} minutes. We will never ask you for this code "
        "or for your SSN by phone or text."
    )
    payload = urllib.parse.urlencode(
        {"To": _e164(destination), "From": sender, "Body": body}
    ).encode("ascii")
    request = urllib.request.Request(
        _TWILIO_URL.format(sid=urllib.parse.quote(sid)),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    credential = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {credential}")

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status >= 300:
                raise DeliveryError(f"The SMS gateway returned {response.status}.")
    except urllib.error.HTTPError as exc:
        # Read the provider's reason but never echo it to the client: it can
        # contain the number and the message body, and the message body is the
        # code. The log gets the status; the client gets a generic failure.
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            pass
        logger.error("SMS gateway rejected the send: %s %s", exc.code, detail)
        raise DeliveryError(
            "The text message could not be sent. Check the number and try again."
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.error("SMS gateway unreachable: %s", exc)
        raise DeliveryError(
            "The text message could not be sent right now. Try again shortly."
        ) from exc


def _e164(mobile: str) -> str:
    """Normalise to +1XXXXXXXXXX. A gateway silently drops anything else."""
    digits = re.sub(r"\D", "", mobile or "")
    if digits.startswith("1") and len(digits) == 11:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if mobile.strip().startswith("+"):
        return "+" + digits
    raise DeliveryError(f"{mask_mobile(mobile)} is not a usable mobile number.")


def sender_for(channel: str) -> Callable[[str, str], None]:
    return send_sms_code if channel == "sms" else send_email_code
