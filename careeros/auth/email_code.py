"""Sign-in codes, for accounts that are not on Google.

A six-digit code is emailed to the address and exchanged for a session. Only
the SHA-256 of the code is stored, attempts are capped, and the code expires in
ten minutes -- so a stolen database row is not a login.

The SMTP credential configured here is the *operator's* outbound mail account,
used to send the code. It is not the user's mailbox password, and nothing in
this system ever asks for one: reading the user's mail is OAuth-only, in
`careeros.gmail`.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 5


class EmailCodeError(RuntimeError):
    pass


class SmtpNotConfigured(EmailCodeError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("Emailed sign-in codes need SMTP: set " + ", ".join(missing))


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    starttls: bool

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


def smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host=os.getenv("CAREEROS_SMTP_HOST", "").strip(),
        port=int(os.getenv("CAREEROS_SMTP_PORT", "587") or 587),
        user=os.getenv("CAREEROS_SMTP_USER", "").strip(),
        password=os.getenv("CAREEROS_SMTP_PASSWORD", ""),
        sender=os.getenv("CAREEROS_SMTP_FROM", "").strip()
        or os.getenv("CAREEROS_SMTP_USER", "").strip(),
        starttls=os.getenv("CAREEROS_SMTP_STARTTLS", "1").strip() not in ("0", "false", "no"),
    )


def missing_settings() -> list[str]:
    cfg = smtp_config()
    out = []
    if not cfg.host:
        out.append("CAREEROS_SMTP_HOST")
    if not cfg.sender:
        out.append("CAREEROS_SMTP_FROM")
    return out


def new_code() -> str:
    """Uniform over 000000-999999, from the CSPRNG."""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def hash_code(code: str, email: str) -> str:
    """Salted with the address, so one rainbow table does not cover everyone."""
    material = f"{email.strip().lower()}:{code.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def code_matches(code: str, email: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_code(code, email), stored_hash)


def _message(to: str, code: str, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your CareerOS sign-in code"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(
        f"Your CareerOS sign-in code is {code}\n\n"
        f"It expires in {CODE_TTL_SECONDS // 60} minutes and can be used once.\n\n"
        "If you did not ask to sign in, ignore this message -- nobody can get "
        "in without the code.\n"
    )
    return msg


def send_code(to: str, code: str, *, sender_factory: Callable[[], smtplib.SMTP] | None = None) -> None:
    missing = missing_settings()
    if missing:
        raise SmtpNotConfigured(missing)
    cfg = smtp_config()
    message = _message(to, code, cfg.sender)

    def connect() -> smtplib.SMTP:
        client = smtplib.SMTP(cfg.host, cfg.port, timeout=20)
        if cfg.starttls:
            client.starttls()
        if cfg.user:
            client.login(cfg.user, cfg.password)
        return client

    factory = sender_factory if sender_factory is not None else connect
    try:
        client = factory()
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailCodeError(f"could not connect to {cfg.host}: {exc}") from exc
    try:
        client.send_message(message)
    except smtplib.SMTPException as exc:
        raise EmailCodeError(f"could not send the code: {exc}") from exc
    finally:
        try:
            client.quit()
        except Exception:
            pass
