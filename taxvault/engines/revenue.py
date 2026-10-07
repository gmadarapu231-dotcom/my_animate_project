"""How a preparation fee gets from the client into the practice's account.

The honest shape of this is decided by Zelle, not by us. There is no API by
which an application can learn that a Zelle transfer arrived: the money lands
in the practice's bank account and **the bank is the only witness**. Which
means the two facts people conflate are days apart:

  * the client's *"I've sent it"* -- a claim, worth nothing on its own;
  * the practice's *"yes, it's here"* -- confirmed against the bank statement.

Booking the first as revenue is how a practice records income it never
received and files a return it was never paid for. So a client **declares** a
payment and a preparer **confirms** it, and only confirmation writes to the
trust ledger. Nothing else counts.

What makes confirmation possible at all is the reference. A bank line reading
`ZELLE FROM J SMITH 450.00` cannot be matched against three clients named
Smith who each owe something, so every declaration carries a short code the
client is asked to put in the payment memo, and `reconcile_bank_rows` matches
on it. A code with a check character, because a client mistyping one digit of
a reference should fail loudly rather than land on someone else's account.

Two accounts, and they are never the same account:

  * **Fee** -- the practice's revenue. Collected up front, before the return
    is filed, which is both the commercial point and the compliant one: a fee
    taken out of a refund is a refund transfer and needs licensing.
  * **Tax** -- the client's money, passing through. Not revenue, not
    available, and it is what ends practices when the two are mixed.

`firm_account` reports the first, across every client, for a period, and
deducts the platform's own cut to show what the practice actually keeps.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from taxvault.db.models import FeeQuoteRecord, PaymentDeclaration, Taxpayer, TrustEntry
from taxvault.engines.commission import quote_platform_fee
from taxvault.engines.remittance import (
    BUCKET_FEE,
    BUCKET_TAX,
    RemittanceError,
    record_funds,
)
from taxvault.money import ZERO, cents, money, positive

DECLARED, CONFIRMED, REJECTED = "declared", "confirmed", "rejected"

#: Characters used in a reference. No I, O, 0 or 1: a client reading a code off
#: a screen and typing it into a banking app confuses them, and a mistyped
#: reference that happens to be valid lands on the wrong client's account.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ===========================================================================
# The reference
# ===========================================================================
def new_reference(*, tax_year: int, bucket: str = BUCKET_FEE) -> str:
    """A short code for the payment memo, with a check character.

    Shaped `TV-FEE-26-K7M4Q2-3`: the year so a practice can see at a glance
    which season it belongs to, six random characters so two clients never
    collide, and a check character so a single mistyped digit is rejected
    instead of silently crediting the wrong client.
    """
    tag = "FEE" if bucket == BUCKET_FEE else "TAX"
    body = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    stem = f"TV-{tag}-{tax_year % 100:02d}-{body}"
    return f"{stem}-{_check_character(stem)}"


def _check_character(stem: str) -> str:
    digest = hashlib.sha256(stem.encode("ascii")).digest()
    return _ALPHABET[digest[0] % len(_ALPHABET)]


def reference_is_valid(reference: str) -> bool:
    """Does this reference's check character match its body?"""
    text = (reference or "").strip().upper()
    if text.count("-") != 4:
        return False
    stem, _, check = text.rpartition("-")
    return bool(check) and _check_character(stem) == check


def find_reference(text: str) -> str:
    """Pull a TaxVault reference out of a bank statement line.

    Bank descriptions are noise with a payment memo somewhere inside them, so
    this looks for the shape rather than expecting the line to be clean.
    """
    import re

    for match in re.finditer(
        rf"TV-(?:FEE|TAX)-\d{{2}}-[{_ALPHABET}]{{6}}-[{_ALPHABET}]",
        (text or "").upper(),
    ):
        candidate = match.group(0)
        if reference_is_valid(candidate):
            return candidate
    return ""


# ===========================================================================
# Asking for the money
# ===========================================================================
@dataclass
class PaymentInstruction:
    """Exactly what to tell the client, and the reference to quote."""

    amount: Decimal = ZERO
    reference: str = ""
    method: str = "zelle"
    pay_to: str = ""
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    declaration_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def request_fee_payment(
    session: Session, taxpayer: Taxpayer, *, quote: FeeQuoteRecord,
    method: str = "zelle", pay_to: str = "", practice: str = "",
) -> PaymentInstruction:
    """Create a declaration slot and the instructions that go with it.

    Called when the client accepts the quote, BEFORE the return is filed.
    Collecting up front is the compliant way round: a fee deducted from a
    refund is a refund transfer, which needs a bank partner and state money
    transmitter licensing.
    """
    outstanding = positive(money(quote.total) - confirmed_total(
        session, taxpayer_id=taxpayer.id, bucket=BUCKET_FEE, tax_year=quote.tax_year
    ))
    if outstanding <= ZERO:
        raise RemittanceError(
            f"The fee for {quote.tax_year} is already paid in full. There is nothing "
            "to request."
        )

    reference = new_reference(tax_year=quote.tax_year, bucket=BUCKET_FEE)
    declaration = PaymentDeclaration(
        taxpayer_id=taxpayer.id, fee_quote_id=quote.id, bucket=BUCKET_FEE,
        amount=outstanding, tax_year=quote.tax_year, method=method,
        reference=reference, status=DECLARED, note="Awaiting the client's payment",
    )
    session.add(declaration)
    session.flush()

    destination = pay_to or "the address on your engagement letter"
    instruction = PaymentInstruction(
        amount=outstanding, reference=reference, method=method,
        pay_to=destination, declaration_id=declaration.id,
    )
    if method == "zelle":
        instruction.steps = [
            f"Open your bank's app and choose Zelle.",
            f"Send {outstanding:,.2f} to {destination}.",
            f"Put {reference} in the memo or note field. This is how the payment "
            "is matched to your account.",
            "Come back here and tell us you have sent it.",
        ]
        instruction.notes = [
            "Zelle transfers are usually instant but cannot be reversed, so check "
            "the destination before sending.",
            f"We confirm the payment against our bank, not from your message, so "
            f"it may show as pending for a day. The {reference} reference is what "
            "makes that quick.",
        ]
    else:
        instruction.steps = [
            f"Pay {outstanding:,.2f} by {method}.",
            f"Quote {reference} as the reference.",
        ]

    instruction.notes.append(
        f"This is the preparation fee{f' for {practice}' if practice else ''}. It is "
        "set by the work your return takes, not by your refund, and it is the same "
        "whether you get money back or owe. Nothing is deducted from your refund -- "
        "the IRS pays you directly."
    )
    return instruction


def declare_payment(
    session: Session, taxpayer: Taxpayer, *, declaration_id: int,
    amount: Decimal | float | str | None = None, note: str = "",
) -> PaymentDeclaration:
    """The client saying they have sent it. Still not money.

    Deliberately weak: it records a claim and a timestamp and nothing else
    changes. The firm's revenue does not move, the trust ledger is untouched,
    and the return is no closer to being filed.
    """
    declaration = session.get(PaymentDeclaration, declaration_id)
    if declaration is None or declaration.taxpayer_id != taxpayer.id:
        raise RemittanceError("No such payment request on this account.")
    if declaration.status == CONFIRMED:
        raise RemittanceError("That payment has already been confirmed.")
    if amount is not None:
        stated = cents(money(amount))
        if stated <= ZERO:
            raise RemittanceError("A payment has to be more than nothing.")
        declaration.amount = stated
    declaration.status = DECLARED
    declaration.note = note or "Client reports the payment has been sent"
    session.flush()
    return declaration


# ===========================================================================
# Confirming it, which is the only thing that counts
# ===========================================================================
def confirm_payment(
    session: Session, *, declaration_id: int, confirmed_by: str,
    bank_reference: str = "", amount: Decimal | float | str | None = None,
) -> tuple[PaymentDeclaration, TrustEntry]:
    """A preparer confirming the money is in the bank. NOW it is revenue.

    `confirmed_by` is required and recorded. "The system confirmed it" is not
    an answer to "who booked this payment", and this is the entry an audit
    asks about.
    """
    if not (confirmed_by or "").strip():
        raise RemittanceError(
            "A confirmation has to name the person who checked the bank."
        )
    declaration = session.get(PaymentDeclaration, declaration_id)
    if declaration is None:
        raise RemittanceError("No such payment request.")
    if declaration.status == CONFIRMED:
        raise RemittanceError("That payment has already been confirmed.")

    taxpayer = session.get(Taxpayer, declaration.taxpayer_id)
    if taxpayer is None:  # pragma: no cover - the FK cascades
        raise RemittanceError("The client record for that payment is gone.")

    received = cents(money(amount)) if amount is not None else money(declaration.amount)
    if received <= ZERO:
        raise RemittanceError("A confirmed payment has to be more than nothing.")

    entry = record_funds(
        session, taxpayer, amount=received, bucket=declaration.bucket,
        direction="received", method=declaration.method,
        reference=declaration.reference, tax_year=declaration.tax_year,
        note=f"Confirmed by {confirmed_by}"
        + (f" against {bank_reference}" if bank_reference else ""),
    )
    declaration.status = CONFIRMED
    declaration.confirmed_at = entry.at
    declaration.confirmed_by = confirmed_by
    declaration.bank_reference = bank_reference or None
    declaration.amount = received
    declaration.trust_entry_id = entry.id

    if declaration.bucket == BUCKET_FEE and declaration.fee_quote_id:
        quote = session.get(FeeQuoteRecord, declaration.fee_quote_id)
        if quote is not None:
            paid = confirmed_total(
                session, taxpayer_id=taxpayer.id, bucket=BUCKET_FEE,
                tax_year=quote.tax_year,
            )
            if paid >= money(quote.total):
                quote.paid_at = entry.at
                quote.payment_method = declaration.method
                quote.payment_reference = declaration.reference
    session.flush()
    return declaration, entry


def reject_payment(
    session: Session, *, declaration_id: int, rejected_by: str, reason: str = ""
) -> PaymentDeclaration:
    """No matching credit in the bank. The claim is closed, not deleted."""
    declaration = session.get(PaymentDeclaration, declaration_id)
    if declaration is None:
        raise RemittanceError("No such payment request.")
    if declaration.status == CONFIRMED:
        raise RemittanceError(
            "That payment is already confirmed. Reverse it with a ledger entry "
            "rather than rejecting it -- the ledger is append-only."
        )
    declaration.status = REJECTED
    declaration.confirmed_by = rejected_by
    declaration.note = reason or "No matching credit found in the bank"
    session.flush()
    return declaration


# ===========================================================================
# Reconciling against the bank
# ===========================================================================
@dataclass
class BankRow:
    """One credit line from a bank export."""

    description: str = ""
    amount: Decimal = ZERO
    on: date | None = None
    bank_reference: str = ""


@dataclass
class ReconcileResult:
    matched: list[dict[str, Any]] = field(default_factory=list)
    mismatched: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_matched(self) -> Decimal:
        return cents(sum((money(row["amount"]) for row in self.matched), ZERO))

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "mismatched": self.mismatched,
            "unmatched": self.unmatched,
            "total_matched": str(self.total_matched),
            "counts": {
                "matched": len(self.matched),
                "mismatched": len(self.mismatched),
                "unmatched": len(self.unmatched),
            },
        }


def reconcile_bank_rows(
    session: Session, rows: list[BankRow], *, confirmed_by: str,
    auto_confirm: bool = False,
) -> ReconcileResult:
    """Match bank credits to outstanding declarations by reference.

    `auto_confirm=False` by default and that is the right default: this
    proposes matches for a person to approve. A rule that books money
    automatically on a string match will, one day, book a client's tax payment
    as practice revenue because the memo was copied from an earlier transfer.

    A row whose amount differs from the declared amount is reported as
    mismatched rather than part-confirmed. A short payment is a conversation,
    not an arithmetic adjustment.
    """
    result = ReconcileResult()
    for row in rows:
        reference = find_reference(row.description) or find_reference(row.bank_reference)
        if not reference:
            result.unmatched.append({
                "description": row.description,
                "amount": str(cents(money(row.amount))),
                "reason": "no TaxVault reference in the description",
            })
            continue
        declaration = session.scalars(
            select(PaymentDeclaration).where(
                PaymentDeclaration.reference == reference,
                PaymentDeclaration.status != CONFIRMED,
            )
        ).first()
        if declaration is None:
            result.unmatched.append({
                "description": row.description,
                "amount": str(cents(money(row.amount))),
                "reference": reference,
                "reason": "the reference is valid but matches no open request "
                          "(already confirmed, or from another system)",
            })
            continue

        credited = cents(money(row.amount))
        expected = money(declaration.amount)
        entry = {
            "declaration_id": declaration.id,
            "taxpayer_id": declaration.taxpayer_id,
            "reference": reference,
            "bucket": declaration.bucket,
            "expected": str(expected),
            "amount": str(credited),
            "description": row.description,
        }
        if credited != expected:
            entry["reason"] = (
                f"the bank credited {credited:,.2f} against {expected:,.2f} expected"
            )
            result.mismatched.append(entry)
            continue

        if auto_confirm:
            _, written = confirm_payment(
                session, declaration_id=declaration.id, confirmed_by=confirmed_by,
                bank_reference=row.bank_reference or row.description, amount=credited,
            )
            entry["confirmed"] = True
            entry["trust_entry_id"] = written.id
        else:
            entry["confirmed"] = False
        result.matched.append(entry)
    return result


# ===========================================================================
# The practice's account
# ===========================================================================
def confirmed_total(
    session: Session, *, taxpayer_id: int | None = None, bucket: str = BUCKET_FEE,
    tax_year: int | None = None, since: datetime | None = None,
    until: datetime | None = None,
) -> Decimal:
    """Money that actually moved, summed. Declarations do not appear here."""
    query = select(func.coalesce(func.sum(TrustEntry.amount), 0)).where(
        TrustEntry.bucket == bucket, TrustEntry.direction == "received"
    )
    if taxpayer_id is not None:
        query = query.where(TrustEntry.taxpayer_id == taxpayer_id)
    if tax_year is not None:
        query = query.where(TrustEntry.tax_year == tax_year)
    if since is not None:
        query = query.where(TrustEntry.at >= since)
    if until is not None:
        query = query.where(TrustEntry.at < until)
    return cents(money(session.scalar(query) or 0))


@dataclass
class FirmAccount:
    """What the practice earned, what it still holds, and what it keeps."""

    period_from: str = ""
    period_to: str = ""
    #: Fees confirmed in the period. This is revenue.
    fees_collected: Decimal = ZERO
    fees_by_method: dict[str, str] = field(default_factory=dict)
    returns_paid: int = 0
    #: Quoted but not yet confirmed.
    fees_outstanding: Decimal = ZERO
    fees_declared_awaiting_confirmation: Decimal = ZERO
    declarations_awaiting: int = 0
    #: Client money held in trust. NOT revenue and not available.
    tax_held_in_trust: Decimal = ZERO
    #: The platform's own cut, where the practice is on a platform plan.
    platform_cost: Decimal = ZERO
    platform_model: str = ""
    #: What the practice actually keeps.
    net_to_practice: Decimal = ZERO
    average_fee: Decimal = ZERO
    lines: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def line(self, label: str, amount: Decimal, note: str = "") -> None:
        self.lines.append(
            {"label": label, "amount": str(cents(amount)), "note": note}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(cents(value)) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def firm_account(
    session: Session, *, since: datetime | None = None,
    until: datetime | None = None, platform_model: str = "",
) -> FirmAccount:
    """The practice's own account for a period, across every client."""
    start = since or (_now() - timedelta(days=365))
    end = until or _now()
    account = FirmAccount(
        period_from=start.date().isoformat(), period_to=end.date().isoformat()
    )

    account.fees_collected = confirmed_total(
        session, bucket=BUCKET_FEE, since=start, until=end
    )
    account.tax_held_in_trust = cents(
        confirmed_total(session, bucket=BUCKET_TAX)
        - cents(money(session.scalar(
            select(func.coalesce(func.sum(TrustEntry.amount), 0)).where(
                TrustEntry.bucket == BUCKET_TAX,
                TrustEntry.direction.in_(("disbursed", "refunded")),
            )
        ) or 0))
    )

    by_method = session.execute(
        select(TrustEntry.method, func.coalesce(func.sum(TrustEntry.amount), 0))
        .where(TrustEntry.bucket == BUCKET_FEE, TrustEntry.direction == "received",
               TrustEntry.at >= start, TrustEntry.at < end)
        .group_by(TrustEntry.method)
    ).all()
    account.fees_by_method = {
        (method or "unknown"): str(cents(money(total))) for method, total in by_method
    }

    paid_quotes = session.scalars(
        select(FeeQuoteRecord).where(
            FeeQuoteRecord.paid_at.is_not(None),
            FeeQuoteRecord.paid_at >= start,
            FeeQuoteRecord.paid_at < end,
        )
    ).all()
    account.returns_paid = len(paid_quotes)
    if account.returns_paid:
        account.average_fee = cents(account.fees_collected / account.returns_paid)

    # Quoted and unpaid: the practice's receivable.
    unpaid_quotes = session.scalars(
        select(FeeQuoteRecord).where(FeeQuoteRecord.paid_at.is_(None))
    ).all()
    account.fees_outstanding = cents(
        sum((money(q.total) for q in unpaid_quotes), ZERO)
    )

    awaiting = session.scalars(
        select(PaymentDeclaration).where(
            PaymentDeclaration.status == DECLARED,
            PaymentDeclaration.bucket == BUCKET_FEE,
        )
    ).all()
    account.declarations_awaiting = len(awaiting)
    account.fees_declared_awaiting_confirmation = cents(
        sum((money(d.amount) for d in awaiting), ZERO)
    )

    # The platform's cut, where there is one. A practice running its own
    # install has none, and `platform_model=""` means exactly that.
    if platform_model:
        account.platform_model = platform_model
        per_return = quote_platform_fee(
            model=platform_model,
            preparation_fee=account.average_fee,
            returns_this_period=account.returns_paid,
        )
        account.platform_cost = cents(per_return.amount * account.returns_paid)
    account.net_to_practice = cents(account.fees_collected - account.platform_cost)

    account.line("Preparation fees confirmed", account.fees_collected,
                 note=f"{account.returns_paid} return(s)")
    if account.platform_cost:
        account.line(f"Platform fee ({platform_model})", -account.platform_cost)
    account.line("Net to the practice", account.net_to_practice)

    account.notes.append(
        "Only money confirmed against the bank is counted. A client saying they "
        "have paid does not move this figure -- that is what the "
        f"{account.declarations_awaiting} awaiting confirmation "
        f"({account.fees_declared_awaiting_confirmation:,.2f}) is."
    )
    if account.tax_held_in_trust > ZERO:
        account.notes.append(
            f"{account.tax_held_in_trust:,.2f} is held in trust for clients to pay "
            "their tax. It is NOT revenue, it is not available to the practice, and "
            "it is reported separately for that reason."
        )
    if account.fees_outstanding > ZERO:
        account.notes.append(
            f"{account.fees_outstanding:,.2f} is quoted and unpaid. Fees are collected "
            "before a return is filed, so this is work that should not yet have gone "
            "out."
        )
    return account


def outstanding_requests(session: Session, *, bucket: str = BUCKET_FEE
                         ) -> list[dict[str, Any]]:
    """Open payment requests, for the preparer's confirmation queue."""
    rows = session.scalars(
        select(PaymentDeclaration)
        .where(PaymentDeclaration.status == DECLARED,
               PaymentDeclaration.bucket == bucket)
        .order_by(PaymentDeclaration.at)
    ).all()
    return [
        {
            "declaration_id": row.id,
            "taxpayer_id": row.taxpayer_id,
            "reference": row.reference,
            "amount": str(cents(money(row.amount))),
            "method": row.method,
            "tax_year": row.tax_year,
            "requested_at": row.at.isoformat() if row.at else None,
            "note": row.note or "",
        }
        for row in rows
    ]
