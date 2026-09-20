"""The schema.

Shape of the data follows the client's journey:

    Account -> Taxpayer (identity, SSN sealed) -> TaxDocument (W-2 etc.)
            -> Estimate (regular | planning) -> FilingRecord (prior years)
            -> PaymentPlan

Two rules run through it:

1. **Nothing identifying is stored in the clear.** SSN, bank details and the
   uploaded document bodies are ciphertext columns (`taxos.crypto`). The only
   plaintext identifiers are an email address, which the account is keyed on,
   and the last four of the SSN, which the UI needs to echo back.
2. **Every computed number keeps its inputs.** `Estimate.inputs` holds the
   figures the calculation ran on and `Estimate.breakdown` holds every line it
   produced. An estimate nobody can reconstruct is an estimate nobody can
   defend in an audit.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ===========================================================================
# 1. Account and identity
# ===========================================================================
class Account(Base, TimestampMixin):
    """A sign-in. Keyed on a verified email address; there is no password column.

    Passwords are the single largest source of breaches in tax software, and a
    preparer's client base is a high-value target. This system never has one to
    steal: sign-in is a one-time code to a verified address, and the session is
    a signed token (`taxos.auth.tokens`).
    """

    __tablename__ = "account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(200))
    mobile_e164: Mapped[Optional[str]] = mapped_column(String(20))
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    mobile_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    role: Mapped[str] = mapped_column(String(16), default="client")  # client | preparer | admin
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)

    taxpayers: Mapped[list["Taxpayer"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class AuthChallenge(Base, TimestampMixin):
    """An outstanding one-time code, hashed and attempt-capped.

    Covers both channels: an emailed sign-in code and an SMS code proving the
    mobile number. The code itself is never stored -- only a salted hash -- so
    a database read does not let anyone complete a sign-in in flight.
    """

    __tablename__ = "auth_challenge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))  # email | sms
    purpose: Mapped[str] = mapped_column(String(32), default="sign_in")
    destination: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[Optional[str]] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Taxpayer(Base, TimestampMixin):
    """The person the return is for. One account may hold several (spouse, dependants).

    `ssn_encrypted` is AES-GCM ciphertext bound to this row's id.
    `ssn_index` is a keyed HMAC -- the only thing any lookup ever matches on.
    """

    __tablename__ = "taxpayer"
    __table_args__ = (
        Index("ix_taxpayer_ssn_index", "ssn_index"),
        UniqueConstraint("account_id", "ssn_index", name="uq_taxpayer_account_ssn"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"), index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(100))
    last_name: Mapped[Optional[str]] = mapped_column(String(100))
    relationship_to_filer: Mapped[str] = mapped_column(String(24), default="self")  # self|spouse|dependent
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)

    ssn_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    ssn_index: Mapped[Optional[str]] = mapped_column(String(64))
    ssn_last4: Mapped[Optional[str]] = mapped_column(String(4))
    ssn_kind: Mapped[Optional[str]] = mapped_column(String(8))  # ssn | itin
    ssn_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    email: Mapped[Optional[str]] = mapped_column(String(320))
    mobile_e164: Mapped[Optional[str]] = mapped_column(String(20))
    resident_state: Mapped[Optional[str]] = mapped_column(String(2))
    is_blind: Mapped[bool] = mapped_column(Boolean, default=False)
    can_be_claimed: Mapped[bool] = mapped_column(Boolean, default=False)

    account: Mapped["Account"] = relationship(back_populates="taxpayers")
    documents: Mapped[list["TaxDocument"]] = relationship(
        back_populates="taxpayer", cascade="all, delete-orphan"
    )
    estimates: Mapped[list["Estimate"]] = relationship(
        back_populates="taxpayer", cascade="all, delete-orphan"
    )
    filings: Mapped[list["FilingRecord"]] = relationship(
        back_populates="taxpayer", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or (self.email or f"Taxpayer {self.id}")


# ===========================================================================
# 2. Documents
# ===========================================================================
class TaxDocument(Base, TimestampMixin):
    """An uploaded form, and what was read out of it.

    `payload` holds the extracted fields (the W-2 boxes), which are figures
    rather than identifiers and so are stored as JSON for querying. `raw_blob`
    holds the original upload, encrypted, because the original is what an audit
    asks for and it contains the employer's EIN and the client's address.
    """

    __tablename__ = "tax_document"
    __table_args__ = (Index("ix_document_year", "taxpayer_id", "tax_year", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    taxpayer_id: Mapped[int] = mapped_column(
        ForeignKey("taxpayer.id", ondelete="CASCADE"), index=True
    )
    tax_year: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="uploaded")
    source: Mapped[str] = mapped_column(String(16), default="upload")  # upload | manual | import

    employer_name: Mapped[Optional[str]] = mapped_column(String(200))
    employer_ein_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    employer_ein_last4: Mapped[Optional[str]] = mapped_column(String(4))
    state_code: Mapped[Optional[str]] = mapped_column(String(2))

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_blob: Mapped[Optional[str]] = mapped_column(Text)
    original_filename: Mapped[Optional[str]] = mapped_column(String(255))
    content_type: Mapped[Optional[str]] = mapped_column(String(100))
    checksum: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    parse_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3))
    parse_warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)

    taxpayer: Mapped["Taxpayer"] = relationship(back_populates="documents")


# ===========================================================================
# 3. Estimates
# ===========================================================================
class Estimate(Base, TimestampMixin):
    """One run of the calculator: regular or planning, federal plus states."""

    __tablename__ = "estimate"
    __table_args__ = (Index("ix_estimate_lookup", "taxpayer_id", "tax_year", "method"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    taxpayer_id: Mapped[int] = mapped_column(
        ForeignKey("taxpayer.id", ondelete="CASCADE"), index=True
    )
    tax_year: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(16), default="regular")
    filing_status: Mapped[str] = mapped_column(String(32))
    resident_state: Mapped[Optional[str]] = mapped_column(String(2))

    federal_agi: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    federal_taxable_income: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    federal_tax: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    federal_withheld: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    federal_balance: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))  # +owed / -refund
    state_tax: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    state_withheld: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    state_balance: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    total_balance: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    effective_rate: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))
    marginal_rate: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))

    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    options: Mapped[list[Any]] = mapped_column(JSON, default=list)  # planning strategies
    selected_options: Mapped[list[Any]] = mapped_column(JSON, default=list)
    params_version: Mapped[Optional[str]] = mapped_column(String(64))

    taxpayer: Mapped["Taxpayer"] = relationship(back_populates="estimates")
    payment_plans: Mapped[list["PaymentPlan"]] = relationship(
        back_populates="estimate", cascade="all, delete-orphan"
    )


# ===========================================================================
# 4. Filing history
# ===========================================================================
class FilingRecord(Base, TimestampMixin):
    """What is on record for one taxpayer, one year, one jurisdiction.

    `source` matters for trust: `irs_transcript` came from the IRS Transcript
    Delivery System under an authorisation on file; `client_stated` is what the
    client told us; `inferred` is what we deduced from the documents. The UI
    shows the difference rather than presenting all three as fact.
    """

    __tablename__ = "filing_record"
    __table_args__ = (
        UniqueConstraint("taxpayer_id", "tax_year", "jurisdiction", "state_code",
                         name="uq_filing_year_jurisdiction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    taxpayer_id: Mapped[int] = mapped_column(
        ForeignKey("taxpayer.id", ondelete="CASCADE"), index=True
    )
    tax_year: Mapped[int] = mapped_column(Integer)
    jurisdiction: Mapped[str] = mapped_column(String(8))  # federal | state
    state_code: Mapped[Optional[str]] = mapped_column(String(2), default="")
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    source: Mapped[str] = mapped_column(String(24), default="client_stated")
    filed_on: Mapped[Optional[date]] = mapped_column(Date)
    accepted_on: Mapped[Optional[date]] = mapped_column(Date)
    refund_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    balance_due: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    confirmation_number: Mapped[Optional[str]] = mapped_column(String(64))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    taxpayer: Mapped["Taxpayer"] = relationship(back_populates="filings")


# ===========================================================================
# 5. Payment
# ===========================================================================
class PaymentPlan(Base, TimestampMixin):
    """How the balance gets settled, or the refund delivered.

    Bank details are encrypted and only the last four of the account number is
    kept in the clear, which is all a confirmation screen needs.
    """

    __tablename__ = "payment_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    estimate_id: Mapped[int] = mapped_column(
        ForeignKey("estimate.id", ondelete="CASCADE"), index=True
    )
    jurisdiction: Mapped[str] = mapped_column(String(8), default="federal")
    state_code: Mapped[Optional[str]] = mapped_column(String(2), default="")
    direction: Mapped[str] = mapped_column(String(16))  # refund | balance_due | even
    method: Mapped[str] = mapped_column(String(32))
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    instalments: Mapped[int] = mapped_column(Integer, default=1)
    instalment_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    first_due_on: Mapped[Optional[date]] = mapped_column(Date)
    setup_fee: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    projected_interest: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    projected_penalty: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    total_cost: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))

    bank_routing_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    bank_account_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    bank_account_last4: Mapped[Optional[str]] = mapped_column(String(4))
    bank_account_type: Mapped[Optional[str]] = mapped_column(String(16))

    schedule: Mapped[list[Any]] = mapped_column(JSON, default=list)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    estimate: Mapped["Estimate"] = relationship(back_populates="payment_plans")


class AuditEvent(Base):
    """An append-only trail. IRS Publication 4557 requires one, and it is also
    the only way to answer "who looked at this client's SSN, and when"."""

    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_account_time", "account_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    account_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    actor: Mapped[Optional[str]] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64))
    subject: Mapped[Optional[str]] = mapped_column(String(128))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
