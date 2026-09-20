"""What has and has not been filed, and what the gap costs.

Where the filing history comes from matters more than the calculation, so the
source is explicit and never blurred:

* ``irs_transcript`` -- the IRS Transcript Delivery System, which requires an
  e-Services account and a Form 8821 or 2848 authorisation signed by the
  client. That integration is a `TranscriptProvider`; none ships enabled,
  because shipping one would mean shipping credentials nobody has yet.
* ``state_portal``   -- the equivalent from a state revenue department.
* ``client_stated``  -- what the client said. Useful, not authoritative.
* ``inferred``       -- deduced from the documents we hold: a W-2 for 2023 with
  no 2023 return on record is a strong signal, not proof.

The UI shows the source next to every year, because "we could not find a 2023
return" and "the IRS confirms no 2023 return was filed" are very different
sentences and only one of them justifies telling a client they have a problem.

Penalties follow the statute: failure-to-file is 5% of unpaid tax per month to
a 25% cap, failure-to-pay is 0.5% per month to its own 25% cap, and in any
month both apply the file penalty is reduced by the pay penalty -- the detail
that makes a hand estimate wrong. A year owed nothing carries no penalty at
all, which is why so many unfiled years turn out to be harmless. A year owed a
refund is worse than harmless: the refund is forfeited three years after the
due date, and that clock is the single most urgent fact on this screen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from taxos.config import federal, supported_years
from taxos.db.models import FilingRecord, TaxDocument, Taxpayer
from taxos.enums import FilingState, Jurisdiction
from taxos.money import ZERO, cents, money, positive


class TranscriptProviderError(RuntimeError):
    """The provider could not answer. The message is safe to show a preparer."""


class TranscriptProvider(Protocol):
    """A source of authoritative filing history for one taxpayer."""

    name: str

    def fetch(self, *, ssn_index: str, years: list[int]) -> list[dict[str, Any]]:
        ...


class LocalTranscriptProvider:
    """The default: whatever this system has recorded, and nothing more.

    It answers from `filing_record`, which holds what the client told us and
    what a preparer confirmed. It never invents a year.
    """

    name = "local"

    def __init__(self, session: Session, taxpayer_id: int):
        self.session = session
        self.taxpayer_id = taxpayer_id

    def fetch(self, *, ssn_index: str, years: list[int]) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(FilingRecord).where(
                FilingRecord.taxpayer_id == self.taxpayer_id,
                FilingRecord.tax_year.in_(years),
            )
        ).all()
        return [
            {
                "tax_year": row.tax_year,
                "jurisdiction": row.jurisdiction,
                "state_code": row.state_code or "",
                "state": row.state,
                "source": row.source,
                "filed_on": row.filed_on.isoformat() if row.filed_on else None,
                "refund_amount": str(row.refund_amount) if row.refund_amount is not None else None,
                "balance_due": str(row.balance_due) if row.balance_due is not None else None,
            }
            for row in rows
        ]


class IRSTranscriptProvider:
    """Placeholder for the real IRS Transcript Delivery System integration.

    Deliberately inert. Turning this on requires an IRS e-Services account, a
    registered application, and a Form 8821 or 2848 on file for each client.
    Raising a clear error beats returning a plausible-looking empty history that
    a preparer might read as "nothing was ever filed".
    """

    name = "irs_transcript"

    def fetch(self, *, ssn_index: str, years: list[int]) -> list[dict[str, Any]]:
        raise TranscriptProviderError(
            "IRS Transcript Delivery is not connected on this server. It needs an "
            "IRS e-Services account and a signed Form 8821 or 2848 for the client. "
            "Until then, filing history comes from what the client reports and what "
            "the uploaded documents imply."
        )


# ---------------------------------------------------------------------------
# penalties
# ---------------------------------------------------------------------------
def _months_late(due: date, as_of: date) -> int:
    """Whole or part months, which is how the statute counts them."""
    if as_of <= due:
        return 0
    months = (as_of.year - due.year) * 12 + (as_of.month - due.month)
    if as_of.day > due.day:
        months += 1
    return max(1, months)


def estimate_penalties(
    unpaid_tax: Decimal | float | str,
    *,
    due_date: date,
    as_of: date,
    year: int,
    filed: bool = False,
    on_installment: bool = False,
) -> dict[str, Any]:
    """Failure-to-file, failure-to-pay and interest on one unfiled year."""
    # Penalty rates are set in statute, not indexed yearly, so an old year with
    # no parameter file of its own can safely borrow the nearest one we hold.
    try:
        params = federal(year)
    except Exception:
        params = federal(min(supported_years(), key=lambda y: abs(y - year)))
    tax = positive(money(unpaid_tax))
    if tax <= ZERO:
        return {
            "months_late": _months_late(due_date, as_of),
            "failure_to_file": "0", "failure_to_pay": "0", "interest": "0", "total": "0",
            "note": (
                "No tax was owed for this year, so there is no failure-to-file penalty "
                "and no failure-to-pay penalty. The penalties are percentages of an "
                "unpaid balance, and a balance of zero produces nothing."
            ),
        }

    months = _months_late(due_date, as_of)
    if months <= 0:
        return {"months_late": 0, "failure_to_file": "0", "failure_to_pay": "0",
                "interest": "0", "total": "0", "note": "Not yet late."}

    ftf_rate = params.rate("penalties_and_interest", "failure_to_file_monthly")
    ftf_cap = params.rate("penalties_and_interest", "failure_to_file_cap")
    ftp_rate = params.rate(
        "penalties_and_interest",
        "failure_to_pay_with_installment" if on_installment else "failure_to_pay_monthly",
    )
    ftp_cap = params.rate("penalties_and_interest", "failure_to_pay_cap")

    ftp_months = months
    ftp = min(tax * ftp_rate * ftp_months, tax * ftp_cap)

    if filed:
        ftf = ZERO
    else:
        # The 5% file penalty runs for at most 5 months (to the 25% cap), and in
        # any month both run the file penalty is reduced by the pay penalty.
        ftf_months = min(months, 5)
        ftf = min(tax * ftf_rate * ftf_months, tax * ftf_cap)
        ftf = positive(ftf - tax * ftp_rate * ftf_months)
        minimum = params.amount("penalties_and_interest", "failure_to_file_minimum")
        if months > 2:  # more than 60 days late
            ftf = max(ftf, min(minimum, tax))

    annual = params.rate("penalties_and_interest", "underpayment_interest_annual")
    days = max(0, (as_of - due_date).days)
    # Interest compounds daily on tax and penalties alike.
    interest = (tax + ftf + ftp) * annual * money(days) / money(365)

    total = cents(ftf + ftp + interest)
    return {
        "months_late": months,
        "days_late": days,
        "failure_to_file": str(cents(ftf)),
        "failure_to_pay": str(cents(ftp)),
        "interest": str(cents(interest)),
        "total": str(total),
        "note": (
            f"Unpaid tax of {tax:,.0f} is {months} month(s) late. The failure-to-file "
            "penalty is the expensive one at 5% a month, ten times the failure-to-pay "
            "penalty -- so filing without paying is always better than not filing."
        ),
    }


# ---------------------------------------------------------------------------
# the history check
# ---------------------------------------------------------------------------
@dataclass
class YearStatus:
    tax_year: int
    jurisdiction: str
    state_code: str = ""
    status: str = FilingState.UNKNOWN.value
    source: str = "none"
    due_date: str = ""
    filed_on: str | None = None
    has_documents: bool = False
    estimated_balance: str | None = None
    estimated_refund: str | None = None
    penalties: dict[str, Any] = field(default_factory=dict)
    refund_expires_on: str | None = None
    refund_forfeited: bool = False
    action: str = ""
    severity: str = "ok"   # ok | attention | urgent
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_filing_history(
    session: Session,
    taxpayer: Taxpayer,
    *,
    years: Iterable[int] | None = None,
    as_of: date | None = None,
    provider: TranscriptProvider | None = None,
    estimated_balances: dict[Any, Decimal] | None = None,
) -> list[YearStatus]:
    """Year by year, what is on record and what it costs to leave it alone.

    `estimated_balances` lets the caller pass what each year would have owed
    (from the estimator) so an unfiled year can be priced. Without it, a year
    is reported as unfiled with the penalty left unquantified rather than
    guessed at.

    Keys may be a bare year, which means the federal balance for that year, or
    a `(year, jurisdiction, state_code)` tuple for a specific return. A federal
    balance is never reused for a state row: the two are different amounts, and
    applying one to both would double the total owed.
    """
    today = as_of or date.today()
    # Six years is the IRS's own rule of thumb for how far back to file to be
    # considered current, so that is the default window.
    if years is None:
        years = list(range(today.year - 6, today.year))
    years = sorted(set(int(y) for y in years))
    balances: dict[tuple[int, str, str], Decimal] = {}
    for key, value in (estimated_balances or {}).items():
        if isinstance(key, tuple):
            year_key, jurisdiction_key, *rest = key
            balances[(int(year_key), str(jurisdiction_key), str(rest[0]) if rest else "")] = value
        else:
            balances[(int(key), Jurisdiction.FEDERAL.value, "")] = value

    provider = provider or LocalTranscriptProvider(session, taxpayer.id)
    try:
        records = provider.fetch(ssn_index=taxpayer.ssn_index or "", years=years)
    except TranscriptProviderError as exc:
        records = []
        provider_note = str(exc)
    else:
        provider_note = ""

    by_key: dict[tuple[int, str, str], dict[str, Any]] = {
        (int(r["tax_year"]), r["jurisdiction"], r.get("state_code") or ""): r for r in records
    }

    documents = session.scalars(
        select(TaxDocument).where(TaxDocument.taxpayer_id == taxpayer.id)
    ).all()
    doc_years = {doc.tax_year for doc in documents}
    doc_states: dict[int, set[str]] = {}
    for doc in documents:
        if doc.state_code:
            doc_states.setdefault(doc.tax_year, set()).add(doc.state_code.upper())

    out: list[YearStatus] = []
    for year in years:
        try:
            params = federal(year)
            due = date.fromisoformat(params.due_date)
        except Exception:
            # Outside the years we hold parameters for: 15 April of the next year
            # is right for every year in living memory bar pandemic postponements.
            due = date(year + 1, 4, 15)

        jurisdictions: list[tuple[str, str]] = [(Jurisdiction.FEDERAL.value, "")]
        for code in sorted(doc_states.get(year, set())):
            jurisdictions.append((Jurisdiction.STATE.value, code))

        for jurisdiction, code in jurisdictions:
            record = by_key.get((year, jurisdiction, code))
            status = YearStatus(
                tax_year=year, jurisdiction=jurisdiction, state_code=code,
                due_date=due.isoformat(), has_documents=year in doc_years,
            )
            if provider_note:
                status.notes.append(provider_note)

            if record:
                status.status = record.get("state") or FilingState.FILED.value
                status.source = record.get("source", "client_stated")
                status.filed_on = record.get("filed_on")
                if status.status in (FilingState.FILED.value, FilingState.ACCEPTED.value):
                    status.action = "Nothing to do."
                    status.severity = "ok"
                    out.append(status)
                    continue
            else:
                status.status = (
                    FilingState.NOT_FILED.value if status.has_documents else FilingState.UNKNOWN.value
                )
                status.source = "inferred" if status.has_documents else "none"

            if status.status == FilingState.UNKNOWN.value:
                status.action = "Confirm with the client whether a return was filed for this year."
                status.severity = "attention"
                status.notes.append(
                    "No return on record and no documents held for this year. That is not "
                    "evidence of a missing return -- it may simply be a year before this "
                    "client joined, or a year with no filing requirement."
                )
                out.append(status)
                continue

            # --- an unfiled year with documents behind it ---------------------
            balance = balances.get((year, jurisdiction, code))
            refund_deadline = due + timedelta(days=365 * 3)
            status.refund_expires_on = refund_deadline.isoformat()

            if balance is None:
                where = "federal" if jurisdiction == Jurisdiction.FEDERAL.value else code
                status.action = (
                    f"Prepare the {year} {where} return to find out whether it owes or refunds."
                )
                status.severity = "attention"
                status.notes.append(
                    f"Documents for {year} are on file but no return is recorded. Until the "
                    "year is computed there is no way to say whether it costs anything."
                )
            elif money(balance) > ZERO:
                owed = cents(money(balance))
                status.estimated_balance = str(owed)
                status.penalties = estimate_penalties(
                    owed, due_date=due, as_of=today, year=year,
                )
                total = money(status.penalties["total"])
                status.severity = "urgent"
                status.action = (
                    f"File {year} now. About {owed:,.0f} is owed, and penalties and interest "
                    f"have added roughly {total:,.0f} on top. Both keep growing."
                )
                status.notes.append(
                    "The failure-to-file penalty stops accruing the day the return is "
                    "filed, even if the balance cannot be paid that day."
                )
            else:
                refund = positive(-money(balance))
                status.estimated_refund = str(refund)
                if today > refund_deadline:
                    status.refund_forfeited = True
                    status.severity = "attention"
                    status.action = (
                        f"The {year} refund of about {refund:,.0f} can no longer be claimed: "
                        f"the three-year window closed on {refund_deadline:%d %B %Y}."
                    )
                    status.notes.append(
                        "The return can still be filed, and should be where the year affects "
                        "a carryforward or a later year's figures -- but the money is gone."
                    )
                else:
                    days_left = (refund_deadline - today).days
                    status.severity = "urgent" if days_left < 180 else "attention"
                    status.action = (
                        f"File {year} to claim about {refund:,.0f}. The deadline to claim it is "
                        f"{refund_deadline:%d %B %Y} -- {days_left} days away."
                    )
                    status.notes.append(
                        "No penalty applies to a year that owed nothing, so the only cost of "
                        "waiting is losing the refund entirely once the window closes."
                    )
            out.append(status)

    return out


def summarise(statuses: list[YearStatus]) -> dict[str, Any]:
    """The one-line version for the top of the screen."""
    unfiled = [s for s in statuses if s.status == FilingState.NOT_FILED.value]
    unfiled_years = sorted({s.tax_year for s in unfiled})
    urgent = [s for s in statuses if s.severity == "urgent"]
    owed = sum(
        (money(s.estimated_balance) for s in statuses if s.estimated_balance), ZERO
    )
    penalties = sum(
        (money(s.penalties.get("total", 0)) for s in statuses if s.penalties), ZERO
    )
    refunds = sum(
        (money(s.estimated_refund) for s in statuses
         if s.estimated_refund and not s.refund_forfeited), ZERO
    )
    forfeited = sum(
        (money(s.estimated_refund) for s in statuses
         if s.estimated_refund and s.refund_forfeited), ZERO
    )
    if not unfiled:
        headline = "No missing returns found on the records held."
    elif owed > ZERO:
        headline = (
            f"{len(unfiled_years)} unfiled year(s) across {len(unfiled)} return(s), about "
            f"{owed:,.0f} owed plus {penalties:,.0f} in penalties and interest."
        )
    elif refunds > ZERO:
        headline = (
            f"{len(unfiled_years)} unfiled year(s) owing nothing, with about {refunds:,.0f} in "
            "refunds still claimable."
        )
    else:
        headline = f"{len(unfiled_years)} unfiled year(s) to review."
    return {
        "headline": headline,
        "unfiled_years": unfiled_years,
        "unfiled_returns": len(unfiled),
        "urgent_count": len(urgent),
        "estimated_owed": str(cents(owed)),
        "estimated_penalties": str(cents(penalties)),
        "claimable_refunds": str(cents(refunds)),
        "forfeited_refunds": str(cents(forfeited)),
    }
