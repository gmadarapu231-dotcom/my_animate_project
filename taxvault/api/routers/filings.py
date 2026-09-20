"""Prior-year filing history, and what the gaps cost."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from taxvault.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxvault.auth import audit
from taxvault.config import latest_year, supported_years
from taxvault.db.models import Estimate, FilingRecord, Taxpayer
from taxvault.enums import FilingState, Jurisdiction
from taxvault.engines.compliance import check_filing_history, summarise
from taxvault.money import money

router = APIRouter(prefix="/api/filings", tags=["filings"])


class FilingEntry(BaseModel):
    tax_year: int
    jurisdiction: str = Jurisdiction.FEDERAL.value
    state_code: str = ""
    state: str = FilingState.FILED.value
    filed_on: date | None = None
    refund_amount: float | None = None
    balance_due: float | None = None
    confirmation_number: str = ""
    notes: str = ""


@router.get("/history")
def history(
    request: Request,
    years: str = Query(default="", description="Comma-separated years. Defaults to the last six."),
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Which years are filed, which are not, and what the missing ones cost.

    Balances come from estimates already run for those years, so a year that
    has never been estimated is reported as unfiled with the cost left open
    rather than guessed at.
    """
    wanted: list[int] | None = None
    if years.strip():
        try:
            wanted = [int(y) for y in years.split(",") if y.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Years must be numbers.") from exc

    balances: dict[Any, Any] = {}
    for row in session.scalars(
        select(Estimate).where(Estimate.taxpayer_id == taxpayer.id).order_by(Estimate.id)
    ).all():
        if row.federal_balance is not None:
            balances[row.tax_year] = money(row.federal_balance)
        if row.state_balance is not None and row.resident_state:
            balances[(row.tax_year, Jurisdiction.STATE.value, row.resident_state)] = money(
                row.state_balance
            )

    statuses = check_filing_history(
        session, taxpayer, years=wanted, estimated_balances=balances
    )
    audit(session, "filing_history_checked", account_id=account.id, actor=account.email,
          subject=f"taxpayer:{taxpayer.id}", ip_address=client_ip(request),
          ssn_last4=taxpayer.ssn_last4)
    return {
        "taxpayer": {
            "name": taxpayer.display_name,
            "ssn": f"***-**-{taxpayer.ssn_last4}" if taxpayer.ssn_last4 else None,
            "resident_state": taxpayer.resident_state,
        },
        "summary": summarise(statuses),
        "years": [s.to_dict() for s in statuses],
        "estimated_years": sorted({int(k) for k in balances if isinstance(k, int)}),
        "supported_years": supported_years(),
        "current_year": latest_year(),
        "source_note": (
            "Filing history here comes from what you have told us and what your uploaded "
            "documents imply. Confirming it against IRS records needs a signed Form 8821 "
            "or 2848 and an IRS e-Services connection, which this server does not have."
        ),
    }


@router.post("/record")
def record(
    body: FilingEntry, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record that a year was filed. Marked `client_stated`, never as fact."""
    code = (body.state_code or "").upper()
    existing = session.scalars(
        select(FilingRecord).where(
            FilingRecord.taxpayer_id == taxpayer.id,
            FilingRecord.tax_year == body.tax_year,
            FilingRecord.jurisdiction == body.jurisdiction,
            FilingRecord.state_code == code,
        )
    ).first()
    row = existing or FilingRecord(
        taxpayer_id=taxpayer.id, tax_year=body.tax_year,
        jurisdiction=body.jurisdiction, state_code=code,
    )
    row.state = body.state
    row.source = "client_stated"
    row.filed_on = body.filed_on
    row.refund_amount = body.refund_amount
    row.balance_due = body.balance_due
    row.confirmation_number = body.confirmation_number or None
    row.notes = body.notes or None
    if existing is None:
        session.add(row)
    session.flush()
    audit(session, "filing_recorded", account_id=account.id, actor=account.email,
          subject=f"filing:{row.id}", ip_address=client_ip(request),
          tax_year=body.tax_year, jurisdiction=body.jurisdiction)
    return {
        "id": row.id, "tax_year": row.tax_year, "jurisdiction": row.jurisdiction,
        "state_code": row.state_code, "state": row.state, "source": row.source,
        "note": "Recorded as reported by you. It is not confirmation from the IRS.",
    }
