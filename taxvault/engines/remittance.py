"""Holding a client's tax money, and sending it to the tax authority for them.

This is where the practice touches somebody else's money, so it carries the
most rules -- and they are enforced here rather than left to whoever is
operating the screen.

**The two buckets.** A preparation fee is the practice's revenue the moment it
is earned. A tax payment is the client's money, passing through. They never
mix. `bucket="fee"` and `bucket="tax"` are separate ledgers per client, and the
tax bucket is what a segregated trust account holds. Commingling client funds
is what ends practices, and it is one careless line away unless something
stops it.

**No remittance without authorisation.** Moving a client's money because a
balance exists is not the same as moving it because they asked. Every payment
needs a live `RemittanceAuthorization` naming the amount, the year and the
jurisdiction, revocable until the money actually leaves.

**Never send money you do not hold.** The trust balance must cover the payment.
A practice fronting a client's tax out of its own account is lending, which is
a different business with different licences.

**The ledger is append-only.** A correction is a new entry. A ledger you can
edit is not a ledger.

The rail is EFTPS Batch Provider, the IRS's own channel for a firm paying on
behalf of many clients. Direct Pay is not: it is built for an individual paying
their own tax and caps at two payments per 24 hours.

Nothing in this module moves money. It maintains the ledger, enforces the
rules, and produces the file an operator submits -- the submission itself is a
human action on the IRS's own system.
"""

from __future__ import annotations

import csv
import io
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from taxvault.db.models import (
    RemittanceAuthorization,
    RemittanceBatch,
    Taxpayer,
    TrustEntry,
)
from taxvault.money import ZERO, cents, money

#: Buckets. The whole point of this module is that these never mix.
BUCKET_TAX = "tax"
BUCKET_FEE = "fee"

#: What the client is shown before they authorise, stored verbatim with the
#: authorisation -- consent to a statement nobody kept is not consent.
AUTHORIZATION_STATEMENT = (
    "I authorise {practice} to pay {amount} to {payee} on my behalf for tax year "
    "{year}, from funds I have sent for that purpose. I understand this money is "
    "held separately from the preparation fee, that I can withdraw this "
    "instruction at any time before it is sent, and that I remain responsible to "
    "{payee} for the tax itself."
)


class RemittanceError(RuntimeError):
    """A rule was broken. The message is safe to show an operator."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# the trust ledger
# ---------------------------------------------------------------------------
def balance(session: Session, taxpayer_id: int, *, bucket: str = BUCKET_TAX) -> Decimal:
    """What the practice currently holds for this client, in this bucket."""
    entries = session.scalars(
        select(TrustEntry)
        .where(TrustEntry.taxpayer_id == taxpayer_id, TrustEntry.bucket == bucket)
        .order_by(TrustEntry.id)
    ).all()
    total = ZERO
    for entry in entries:
        amount = money(entry.amount)
        total += amount if entry.direction == "received" else -amount
    return cents(total)


def record_funds(
    session: Session, taxpayer: Taxpayer, *, amount: Decimal | float | str,
    bucket: str = BUCKET_TAX, direction: str = "received", method: str = "zelle",
    reference: str = "", tax_year: int | None = None, note: str = "",
) -> TrustEntry:
    """Append one movement to the ledger, with the balance it leaves behind."""
    if bucket not in (BUCKET_TAX, BUCKET_FEE):
        raise RemittanceError(f"{bucket!r} is not a ledger bucket.")
    if direction not in ("received", "disbursed", "refunded"):
        raise RemittanceError(f"{direction!r} is not a ledger direction.")

    value = cents(money(amount))
    if value <= ZERO:
        raise RemittanceError("A ledger entry has to move more than nothing.")

    running = balance(session, taxpayer.id, bucket=bucket)
    if direction == "received":
        running += value
    else:
        if value > running:
            raise RemittanceError(
                f"Cannot take {value:,.2f} out: only {running:,.2f} is held for this "
                "client. The practice does not front a client's tax."
            )
        running -= value

    entry = TrustEntry(
        taxpayer_id=taxpayer.id, bucket=bucket, direction=direction, amount=value,
        balance_after=running, method=method, reference=reference or None,
        tax_year=tax_year, note=note or None,
    )
    session.add(entry)
    session.flush()
    return entry


def statement(session: Session, taxpayer_id: int) -> dict[str, Any]:
    """Both ledgers for one client, so the separation is visible to them."""
    entries = session.scalars(
        select(TrustEntry)
        .where(TrustEntry.taxpayer_id == taxpayer_id)
        .order_by(TrustEntry.id.desc())
        .limit(100)
    ).all()
    return {
        "held_for_tax": str(balance(session, taxpayer_id, bucket=BUCKET_TAX)),
        "fees_received": str(balance(session, taxpayer_id, bucket=BUCKET_FEE)),
        "entries": [
            {
                "id": e.id, "at": e.at.isoformat() if e.at else None,
                "bucket": e.bucket, "direction": e.direction,
                "amount": str(e.amount), "balance_after": str(e.balance_after),
                "method": e.method, "reference": e.reference,
                "tax_year": e.tax_year, "note": e.note,
            }
            for e in entries
        ],
        "note": (
            "Money held for tax is yours and is kept separate from what you have paid "
            "for preparation. It is only ever sent to the tax authority you "
            "authorised, and it is returned to you if you withdraw the instruction."
        ),
    }


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------
def authorize(
    session: Session, taxpayer: Taxpayer, *, amount: Decimal | float | str,
    tax_year: int, jurisdiction: str = "federal", state_code: str = "",
    estimate_id: int | None = None, ip_address: str = "",
    practice_name: str = "this practice",
) -> RemittanceAuthorization:
    """Record the client's instruction to pay on their behalf."""
    value = cents(money(amount))
    if value <= ZERO:
        raise RemittanceError("There is nothing to authorise.")

    payee = "the IRS" if jurisdiction == "federal" else f"the {state_code} revenue department"
    live = session.scalars(
        select(RemittanceAuthorization).where(
            RemittanceAuthorization.taxpayer_id == taxpayer.id,
            RemittanceAuthorization.tax_year == tax_year,
            RemittanceAuthorization.jurisdiction == jurisdiction,
            RemittanceAuthorization.state_code == (state_code or ""),
            RemittanceAuthorization.status == "authorized",
        )
    ).first()
    if live is not None:
        raise RemittanceError(
            f"An instruction to pay {payee} for {tax_year} is already on file "
            f"({money(live.amount):,.2f}). Withdraw it before adding another."
        )

    text = AUTHORIZATION_STATEMENT.format(
        practice=practice_name, amount=f"${value:,.2f}", payee=payee, year=tax_year,
    )
    authorization = RemittanceAuthorization(
        taxpayer_id=taxpayer.id, estimate_id=estimate_id, tax_year=tax_year,
        jurisdiction=jurisdiction, state_code=(state_code or ""), amount=value,
        status="authorized", authorized_at=_now(), authorized_ip=ip_address or None,
        statement=text,
    )
    session.add(authorization)
    session.flush()
    return authorization


def revoke(session: Session, authorization: RemittanceAuthorization) -> RemittanceAuthorization:
    """Withdraw an instruction. Only possible while the money is still here."""
    if authorization.status in ("remitted", "batched"):
        raise RemittanceError(
            "That payment has already gone into a batch and cannot be withdrawn "
            "here. Recovering it is a matter for the tax authority."
        )
    if authorization.status == "revoked":
        return authorization
    authorization.status = "revoked"
    authorization.revoked_at = _now()
    session.flush()
    return authorization


def ready_to_remit(
    session: Session, *, jurisdiction: str = "federal", state_code: str = "",
) -> list[tuple[RemittanceAuthorization, Taxpayer, Decimal]]:
    """Every live authorisation, with what is actually held behind it.

    An authorisation with no money behind it is the case an operator most needs
    to see: the client said yes and never paid.
    """
    rows = session.scalars(
        select(RemittanceAuthorization).where(
            RemittanceAuthorization.status == "authorized",
            RemittanceAuthorization.jurisdiction == jurisdiction,
            RemittanceAuthorization.state_code == (state_code or ""),
        ).order_by(RemittanceAuthorization.id)
    ).all()

    out: list[tuple[RemittanceAuthorization, Taxpayer, Decimal]] = []
    for authorization in rows:
        taxpayer = session.get(Taxpayer, authorization.taxpayer_id)
        if taxpayer is None:
            continue
        out.append((authorization, taxpayer, balance(session, taxpayer.id, bucket=BUCKET_TAX)))
    return out


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------
@dataclass
class BatchResult:
    batch: RemittanceBatch
    included: list[dict[str, Any]]
    skipped: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.batch.reference,
            "channel": self.batch.channel,
            "jurisdiction": self.batch.jurisdiction,
            "state_code": self.batch.state_code,
            "status": self.batch.status,
            "item_count": self.batch.item_count,
            "total": str(self.batch.total),
            "settlement_date": (self.batch.settlement_date.isoformat()
                                if self.batch.settlement_date else None),
            "included": self.included,
            "skipped": self.skipped,
        }


def prepare_batch(
    session: Session, *, jurisdiction: str = "federal", state_code: str = "",
    settlement_date: date | None = None, tax_year: int | None = None,
) -> BatchResult:
    """Assemble the next batch, and say exactly who was left out and why.

    Nothing is sent here. This produces the list and, with `batch_file`, the
    file; an operator submits it on the IRS's own system and records the result.
    """
    included: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total = ZERO

    batch = RemittanceBatch(
        reference=f"BATCH-{date.today():%Y%m%d}-{secrets.token_hex(3).upper()}",
        channel="eftps_batch" if jurisdiction == "federal" else "state_bulk",
        jurisdiction=jurisdiction, state_code=(state_code or ""),
        status="prepared", settlement_date=settlement_date or date.today(),
    )
    session.add(batch)
    session.flush()

    for authorization, taxpayer, held in ready_to_remit(
        session, jurisdiction=jurisdiction, state_code=state_code
    ):
        if tax_year and authorization.tax_year != tax_year:
            continue
        amount = money(authorization.amount)
        row = {
            "authorization_id": authorization.id,
            "taxpayer_id": taxpayer.id,
            "name": taxpayer.display_name,
            "ssn_last4": taxpayer.ssn_last4,
            "tax_year": authorization.tax_year,
            "amount": str(amount),
            "held": str(held),
        }
        if held < amount:
            # Authorised but not funded. Including it would mean the practice
            # lending its own money, which is a different business entirely.
            row["reason"] = (
                f"Only {held:,.2f} held against an authorised {amount:,.2f}. "
                "Collect the balance before this can go."
            )
            skipped.append(row)
            continue

        record_funds(
            session, taxpayer, amount=amount, bucket=BUCKET_TAX, direction="disbursed",
            method="eftps_batch", reference=batch.reference,
            tax_year=authorization.tax_year,
            note=f"Remitted to {'IRS' if jurisdiction == 'federal' else state_code}",
        )
        authorization.status = "batched"
        authorization.batch_id = batch.id
        total += amount
        included.append(row)

    batch.item_count = len(included)
    batch.total = cents(total)
    session.flush()
    return BatchResult(batch=batch, included=included, skipped=skipped)


def batch_file(session: Session, batch: RemittanceBatch) -> str:
    """The data an operator submits, as CSV.

    The field set is what EFTPS Batch Provider needs for a 1040 payment. The
    exact record layout must be confirmed against the IRS Batch Provider guide
    before a first live submission; this produces the values, correctly
    derived, in a shape an operator can map.

    The taxpayer identification column carries only the last four digits. The
    full number is read from the vault at submission time, so a batch file
    sitting on a disk or in an inbox is not a list of Social Security numbers.
    """
    authorizations = session.scalars(
        select(RemittanceAuthorization)
        .where(RemittanceAuthorization.batch_id == batch.id)
        .order_by(RemittanceAuthorization.id)
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "taxpayer_identification", "name_control", "tax_form", "tax_period",
        "payment_amount", "settlement_date", "reference",
    ])
    for authorization in authorizations:
        taxpayer = session.get(Taxpayer, authorization.taxpayer_id)
        if taxpayer is None:
            continue
        # EFTPS identifies a taxpayer by TIN plus a name control: the first four
        # alphabetic characters of the surname.
        surname = (taxpayer.last_name or taxpayer.display_name or "").upper()
        writer.writerow([
            f"***-**-{taxpayer.ssn_last4 or '????'}",
            "".join(ch for ch in surname if ch.isalpha())[:4],
            "1040",
            f"{authorization.tax_year}12",
            f"{money(authorization.amount):.2f}",
            (batch.settlement_date or date.today()).strftime("%Y%m%d"),
            f"{batch.reference}-{authorization.id}",
        ])
    return buffer.getvalue()


def mark_submitted(
    session: Session, batch: RemittanceBatch, *,
    confirmations: dict[int, str] | None = None,
) -> RemittanceBatch:
    """Record that the batch went, with a confirmation per client where given."""
    if batch.status == "submitted":
        raise RemittanceError("That batch has already been submitted.")
    confirmations = confirmations or {}
    batch.status = "submitted"
    batch.submitted_at = _now()

    for authorization in session.scalars(
        select(RemittanceAuthorization).where(RemittanceAuthorization.batch_id == batch.id)
    ).all():
        authorization.status = "remitted"
        authorization.remitted_at = _now()
        authorization.confirmation_number = confirmations.get(authorization.id)
    session.flush()
    return batch


def compliance_checklist() -> list[dict[str, str]]:
    """What has to be true before a first live remittance.

    Written down because the code cannot enforce any of it, and because a
    practice that discovers these after taking client money discovers them
    expensively.
    """
    return [
        {
            "item": "EFTPS Batch Provider enrolment",
            "why": (
                "The IRS's own channel for a firm paying on behalf of many clients. "
                "Direct Pay is not: it is built for an individual paying their own "
                "tax and caps at two payments per 24 hours."
            ),
            "reference": "IRS Publication 4169",
        },
        {
            "item": "Segregated client trust account",
            "why": (
                "Tax money belongs to the client until it reaches the tax authority. "
                "It must sit in its own bank account, never in the operating account, "
                "and never fund anything else. This system keeps the ledgers apart; "
                "only a bank can keep the money apart."
            ),
            "reference": "State accountancy board trust account rules",
        },
        {
            "item": "Money transmitter licensing",
            "why": (
                "Taking a client's money and sending it onward is money transmission "
                "in most states. Some exempt tax and payroll processors; many do not, "
                "and the exemption is not something to assume. Take advice for each "
                "state you serve before the first dollar moves."
            ),
            "reference": "State money transmitter statutes; FinCEN registration",
        },
        {
            "item": "Written client authorisation per payment",
            "why": (
                "Recorded here with the amount, year, jurisdiction, time, address and "
                "the exact statement the client agreed to. Without it the practice is "
                "moving someone else's money on its own say-so."
            ),
            "reference": "Stored on every RemittanceAuthorization",
        },
        {
            "item": "Professional indemnity cover extended to funds handling",
            "why": (
                "Standard preparer cover often excludes holding client money, and a "
                "mis-sent payment is the practice's liability either way."
            ),
            "reference": "Your insurer",
        },
        {
            "item": "A refund path for money taken by an irreversible rail",
            "why": (
                "Zelle cannot be recalled by the sender. Taking tax funds that way "
                "works only because the practice can send them back by its own ACH if "
                "an instruction is withdrawn. That path has to exist before the first "
                "payment is accepted."
            ),
            "reference": "Practice operating procedure",
        },
        {
            "item": "Reconciliation before every batch",
            "why": (
                "The trust account balance must equal the sum of what the ledger says "
                "is held for clients. A difference means money is somewhere it should "
                "not be, and the batch waits until it is explained."
            ),
            "reference": "Daily, and before any submission",
        },
    ]
