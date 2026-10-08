"""Telling the client their money arrived.

This is the one part of the money flow that **can** safely be automatic, and
the distinction is worth being precise about. Confirming a payment is a
DECISION: it books revenue off a bank statement a person has read, and
automating it on a string match would one day book a client's tax payment as
the practice's income. Sending the receipt is a STATEMENT: the money is
already confirmed, the ledger entry already exists, and telling the client
changes nothing except that they now know.

So: the agent never confirms, and the agent always tells.

A receipt is sent once. Recording `receipt_sent_at` on the declaration is not
bookkeeping tidiness -- being told twice that your $379 was received reads
like it was taken twice, and that is a phone call.

Email and SMS both go out where both are configured, because the two fail
differently: an email lands in spam, a text does not; a text is 160
characters, an email can carry the figures. Where neither is configured the
receipt is queued rather than dropped, so nothing is silently lost and the
practice can see what is waiting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from taxvault.auth import codes
from taxvault.db.models import Account, PaymentDeclaration, Taxpayer
from taxvault.engines.remittance import BUCKET_FEE
from taxvault.money import cents, money

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Receipt:
    """What the client is told, on each channel."""

    declaration_id: int
    amount: Decimal
    reference: str
    method: str
    tax_year: int | None
    practice: str
    to_email: str = ""
    to_mobile: str = ""
    subject: str = ""
    body: str = ""
    sms: str = ""
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "declaration_id": self.declaration_id,
            "amount": str(cents(self.amount)),
            "reference": self.reference,
            "tax_year": self.tax_year,
            "to_email": codes.mask_email(self.to_email) if self.to_email else "",
            "to_mobile": codes.mask_mobile(self.to_mobile) if self.to_mobile else "",
            "subject": self.subject,
            "body": self.body,
            "sms": self.sms,
            "sent": self.sent,
            "failed": self.failed,
        }


def build_receipt(
    session: Session, declaration: PaymentDeclaration, *, practice: str = ""
) -> Receipt:
    """Compose the receipt. No sending, so it can be previewed and tested."""
    import os

    practice = practice or os.getenv("TAXVAULT_PRACTICE_NAME", "your tax preparer")
    amount = money(declaration.amount)
    taxpayer = session.get(Taxpayer, declaration.taxpayer_id)
    account = session.get(Account, taxpayer.account_id) if taxpayer else None

    name = ""
    if taxpayer:
        name = (taxpayer.first_name or "").strip()
    email = (account.email if account else "") or ""
    # `mobile_e164` is stored in the clear: a phone number on its own
    # identifies nobody and the SSN is the thing under seal.
    mobile = (account.mobile_e164 if account else "") or ""
    if account and not account.mobile_verified_at:
        # An unverified number is a number somebody typed, possibly wrongly,
        # possibly somebody else's. A receipt naming an amount is not going
        # there.
        mobile = ""

    kind = ("preparation fee" if declaration.bucket == BUCKET_FEE
            else "tax payment")
    year = f" for {declaration.tax_year}" if declaration.tax_year else ""

    receipt = Receipt(
        declaration_id=declaration.id, amount=amount,
        reference=declaration.reference, method=declaration.method,
        tax_year=declaration.tax_year, practice=practice,
        to_email=email, to_mobile=mobile,
    )
    # ASCII only in the subject. An em dash forces RFC-2047 encoding,
    # which renders fine but shows as =?utf-8?b?...?= in anything that
    # does not decode it, and some filters score it.
    receipt.subject = f"Received: {amount:,.2f} - {practice}"

    # The body says what was received, what for, and what happens next, in
    # that order, because that is the order the client wants it. It also says
    # plainly that nothing came out of their refund, because that is the thing
    # clients worry about and the thing this practice does not do.
    lines = [
        f"{('Hello ' + name) if name else 'Hello'},",
        "",
        f"We have received your {kind}{year}.",
        "",
        f"  Amount received   {amount:,.2f}",
        f"  Paid by           {declaration.method}",
        f"  Reference         {declaration.reference}",
        f"  Confirmed         {(declaration.confirmed_at or _now()):%d %B %Y}",
        "",
    ]
    if declaration.bucket == BUCKET_FEE:
        lines += [
            "That settles the preparation fee. It is separate from any tax you",
            "owe, and nothing has been or will be deducted from your refund —",
            "the IRS pays you directly.",
            "",
            "Your return is now with us to finish. We will send it to you to",
            "read and sign before anything is filed: you are responsible for",
            "what is on it, so you see it first.",
        ]
    else:
        lines += [
            "This is money we are holding for you to pay your tax. It is kept",
            "separate from our own fees and is only ever sent to the authority",
            "you have authorised, for the year you authorised it.",
            "",
            "You can withdraw that instruction at any time before we send it.",
        ]
    lines += [
        "",
        "If anything above is not what you expected, reply to this message",
        "straight away.",
        "",
        f"{practice}",
        "",
        "We will never ask you for your Social Security number, a password or",
        "a payment code by email or text.",
    ]
    receipt.body = "\n".join(lines)

    # SMS is tight, so it carries the figure, the reference and nothing else
    # that cannot be checked at a glance.
    receipt.sms = (
        f"{practice}: we have received your {amount:,.2f} "
        f"(ref {declaration.reference}). Nothing is taken from your refund. "
        "We will send your return to you to sign before filing."
    )
    return receipt


def send_receipt(
    session: Session, declaration: PaymentDeclaration, *, practice: str = "",
    dry_run: bool = False,
) -> Receipt:
    """Send the receipt on every configured channel, once.

    Returns the receipt either way, with `sent` and `failed` filled in, so a
    caller can report what actually happened rather than assuming.
    """
    receipt = build_receipt(session, declaration, practice=practice)
    if declaration.receipt_sent_at is not None:
        receipt.failed.append(
            f"already sent on {declaration.receipt_sent_at:%d %B %Y}"
        )
        return receipt
    if dry_run:
        return receipt

    if receipt.to_email and not codes.email_settings_missing():
        try:
            _send_email(receipt)
            receipt.sent.append("email")
        except Exception as exc:
            logger.error("receipt email failed for declaration %s: %s",
                         declaration.id, exc)
            receipt.failed.append(f"email: {exc}")
    elif receipt.to_email:
        receipt.failed.append("email: not configured on this server")

    if receipt.to_mobile and not codes.sms_settings_missing():
        try:
            codes.send_sms_code  # noqa: B018 - the module is the gateway
            _send_sms(receipt)
            receipt.sent.append("sms")
        except Exception as exc:
            logger.error("receipt sms failed for declaration %s: %s",
                         declaration.id, exc)
            receipt.failed.append(f"sms: {exc}")
    elif receipt.to_mobile:
        receipt.failed.append("sms: not configured on this server")

    if receipt.sent:
        declaration.receipt_sent_at = _now()
        declaration.receipt_channels = ",".join(receipt.sent)
        session.flush()
    return receipt


def _send_email(receipt: Receipt) -> None:
    import os
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = receipt.subject
    message["From"] = os.environ["TAXVAULT_SMTP_FROM"]
    message["To"] = receipt.to_email
    message.set_content(receipt.body)

    host = os.environ["TAXVAULT_SMTP_HOST"]
    port = int(os.getenv("TAXVAULT_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls()
            server.ehlo()
        elif not _is_loopback(host):
            # A receipt names a client and an amount. Sending that in the
            # clear across a network because the relay did not offer TLS is
            # not a trade worth making, so refuse rather than downgrade.
            raise RuntimeError(
                f"{host} does not offer STARTTLS. A receipt carries a client's "
                "name and the amount they paid, so it is not sent unencrypted. "
                "Use a relay that supports TLS."
            )
        user, password = os.getenv("TAXVAULT_SMTP_USER"), os.getenv("TAXVAULT_SMTP_PASSWORD")
        if user and password:
            server.login(user, password)
        server.send_message(message)


def _is_loopback(host: str) -> bool:
    """Only a relay on this machine may be used without TLS."""
    return (host or "").strip().lower() in ("localhost", "127.0.0.1", "::1")


def _send_sms(receipt: Receipt) -> None:
    """Reuses the gateway the sign-in codes go through, so there is one path."""
    import base64
    import os
    import urllib.parse
    import urllib.request

    sid = os.environ["TAXVAULT_SMS_ACCOUNT_SID"]
    token = os.environ["TAXVAULT_SMS_AUTH_TOKEN"]
    sender = os.environ["TAXVAULT_SMS_FROM"]
    payload = urllib.parse.urlencode({
        "To": codes._e164(receipt.to_mobile),
        "From": sender,
        "Body": receipt.sms,
    }).encode("ascii")
    request = urllib.request.Request(
        codes._TWILIO_URL.format(sid=urllib.parse.quote(sid)),
        data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    credential = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {credential}")
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status >= 300:
            raise RuntimeError(f"the SMS gateway returned {response.status}")


# ===========================================================================
# The job
# ===========================================================================
def send_pending_receipts(
    session: Session, *, practice: str = "", dry_run: bool = False
) -> dict[str, Any]:
    """Every confirmed payment the client has not been told about yet.

    Run from the worker. Safe to run as often as you like: a receipt already
    sent is not sent again.
    """
    rows = session.scalars(
        select(PaymentDeclaration).where(
            PaymentDeclaration.status == "confirmed",
            PaymentDeclaration.receipt_sent_at.is_(None),
        ).order_by(PaymentDeclaration.confirmed_at)
    ).all()

    sent, failed, previews = 0, 0, []
    for declaration in rows:
        receipt = send_receipt(
            session, declaration, practice=practice, dry_run=dry_run
        )
        previews.append(receipt.to_dict())
        if receipt.sent:
            sent += 1
        elif not dry_run:
            failed += 1

    return {
        "looked_at": len(rows),
        "sent": sent,
        "failed": failed,
        "dry_run": dry_run,
        "receipts": previews,
        "note": (
            "A receipt goes out once. Sending it is safe to automate because "
            "the money is already confirmed and the ledger entry already "
            "exists -- the client is being told, not charged."
        ),
    }


def awaiting_receipt(session: Session) -> list[dict[str, Any]]:
    """Confirmed payments nobody has told the client about."""
    rows = session.scalars(
        select(PaymentDeclaration).where(
            PaymentDeclaration.status == "confirmed",
            PaymentDeclaration.receipt_sent_at.is_(None),
        )
    ).all()
    return [
        {
            "declaration_id": row.id,
            "taxpayer_id": row.taxpayer_id,
            "amount": str(money(row.amount)),
            "reference": row.reference,
            "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        }
        for row in rows
    ]
