"""Running an estimate: regular, planning, or both side by side."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from taxos.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxos.auth import audit
from taxos.config import UnsupportedTaxYear, federal, latest_year
from taxos.db.models import Estimate, TaxDocument, Taxpayer
from taxos.enums import EstimateMethod
from taxos.engines.estimate import (
    EstimateResult,
    compare_methods,
    profile_from_documents,
    run_estimate,
    wage_lines_from,
)
from taxos.forms.w2 import W2

router = APIRouter(prefix="/api/estimates", tags=["estimates"])


class Situation(BaseModel):
    """Everything no W-2 knows: the household, other income, deductions.

    All optional. Left empty, the estimate runs on the documents alone, which
    is the honest baseline rather than a guess dressed up as an answer.
    """

    filing_status: str = "single"
    resident_state: str = ""
    age: int = 40
    spouse_age: int = 0
    is_blind: bool = False
    spouse_is_blind: bool = False
    children_under_17: int = 0
    other_dependents: int = 0
    can_be_claimed_by_another: bool = False

    taxable_interest: float = 0
    ordinary_dividends: float = 0
    qualified_dividends: float = 0
    short_term_gains: float = 0
    long_term_gains: float = 0
    self_employment_income: float = 0
    qbi_income: float = 0
    is_specified_service_business: bool = False
    rental_income: float = 0
    retirement_distributions: float = 0
    social_security_benefits: float = 0
    unemployment: float = 0
    other_income: float = 0

    hsa_contribution: float = 0
    traditional_ira: float = 0
    student_loan_interest: float = 0
    educator_expenses: float = 0
    covered_by_retirement_plan: bool = True

    state_local_income_tax: float = 0
    property_tax: float = 0
    mortgage_interest: float = 0
    charitable_cash: float = 0
    charitable_noncash: float = 0
    medical_expenses: float = 0
    car_loan_interest: float = 0
    overtime_premium: float = 0

    dependent_care_expenses: float = 0
    qualified_education_expenses: float = 0
    education_credit_kind: str = "american_opportunity"
    estimated_payments: float = 0

    # Planning context -- what the client already has in place.
    existing_401k: float = 0
    existing_hsa: float = 0
    has_hdhp: bool = False
    hdhp_family: bool = False
    has_employer_plan: bool = True


class EstimateRequest(BaseModel):
    tax_year: int | None = None
    method: str = Field(default=EstimateMethod.REGULAR.value,
                        description="regular or planning")
    situation: Situation = Field(default_factory=Situation)
    strategies: list[str] | None = Field(
        default=None,
        description="Which planning moves to apply. Omit to apply every one that helps.",
    )
    save: bool = True


PLANNING_KEYS = {
    "existing_401k", "existing_hsa", "has_hdhp", "hdhp_family", "has_employer_plan",
}


def _load_forms(session: Session, taxpayer: Taxpayer, year: int) -> list[W2]:
    documents = session.scalars(
        select(TaxDocument).where(
            TaxDocument.taxpayer_id == taxpayer.id,
            TaxDocument.tax_year == year,
            TaxDocument.kind == "w2",
            TaxDocument.status != "rejected",
        )
    ).all()
    return [W2.from_dict(d.payload or {}) for d in documents]


def _build(session: Session, taxpayer: Taxpayer, body: EstimateRequest) -> tuple[Any, Any, Any]:
    year = body.tax_year or latest_year()
    # Check the year before anything else. Telling someone asking about 1999
    # that they have "no income on file" answers a question they did not ask.
    federal(year)
    forms = _load_forms(session, taxpayer, year)

    situation = body.situation.model_dump()
    planning_context = {k: situation.pop(k) for k in list(situation) if k in PLANNING_KEYS}
    home = situation.pop("resident_state", "") or (taxpayer.resident_state or "")
    status = situation.pop("filing_status", "single")

    profile, warnings = profile_from_documents(
        forms, tax_year=year, filing_status=status, resident_state=home, extra=situation,
    )
    if not forms and profile.wages == 0 and not any(
        situation.get(k) for k in ("self_employment_income", "retirement_distributions",
                                   "social_security_benefits", "other_income")
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"There is no income on file for {year}. Add a W-2, or enter income in "
                "the situation details, and the estimate will follow."
            ),
        )
    return profile, wage_lines_from(forms), (warnings, planning_context, year)


def _persist(session: Session, taxpayer: Taxpayer, result: EstimateResult) -> Estimate:
    row = Estimate(
        taxpayer_id=taxpayer.id,
        tax_year=result.tax_year,
        method=result.method,
        filing_status=result.filing_status,
        resident_state=result.resident_state or None,
        federal_agi=result.federal.agi,
        federal_taxable_income=result.federal.taxable_income,
        federal_tax=result.federal.total_tax,
        federal_withheld=result.federal.total_payments,
        federal_balance=result.federal.balance,
        state_tax=result.state_tax,
        state_withheld=result.state_withheld,
        state_balance=result.state_balance,
        total_balance=result.total_balance,
        effective_rate=result.federal.effective_rate,
        marginal_rate=result.federal.marginal_rate,
        inputs={"headline": result.headline()},
        breakdown={"federal_lines": result.federal.lines,
                   "states": [s.to_dict() for s in result.states]},
        options=[s.to_dict() for s in result.strategies],
        selected_options=result.applied,
        params_version=f"federal:{result.tax_year}",
    )
    session.add(row)
    session.flush()
    return row


@router.post("")
def create_estimate(
    body: EstimateRequest, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run an estimate and, unless told not to, keep it."""
    if body.method not in (EstimateMethod.REGULAR.value, EstimateMethod.PLANNING.value):
        raise HTTPException(
            status_code=400,
            detail=f"{body.method!r} is not a method. Choose 'regular' or 'planning'.",
        )
    try:
        profile, lines, (warnings, planning_context, year) = _build(session, taxpayer, body)
        result = run_estimate(
            profile, wage_lines=lines, method=body.method,
            chosen_strategies=body.strategies, warnings=warnings,
            planning_context=planning_context,
        )
    except UnsupportedTaxYear as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = result.to_dict()
    if body.save:
        row = _persist(session, taxpayer, result)
        payload["estimate_id"] = row.id
        audit(session, "estimate_run", account_id=account.id, actor=account.email,
              subject=f"estimate:{row.id}", ip_address=client_ip(request),
              method=body.method, tax_year=year)
    return payload


@router.post("/compare")
def compare(
    body: EstimateRequest,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Both methods at once, for the screen where the client chooses."""
    profile, lines, (_, planning_context, _) = _build(session, taxpayer, body)
    return compare_methods(profile, wage_lines=lines, planning_context=planning_context)


@router.get("")
def list_estimates(
    tax_year: int | None = Query(default=None),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(Estimate).where(Estimate.taxpayer_id == taxpayer.id)
    if tax_year:
        query = query.where(Estimate.tax_year == tax_year)
    rows = session.scalars(query.order_by(Estimate.id.desc()).limit(50)).all()
    return {
        "estimates": [
            {
                "id": row.id, "tax_year": row.tax_year, "method": row.method,
                "filing_status": row.filing_status, "resident_state": row.resident_state,
                "federal_balance": str(row.federal_balance),
                "state_balance": str(row.state_balance),
                "total_balance": str(row.total_balance),
                "headline": (row.inputs or {}).get("headline", ""),
                "run_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.get("/{estimate_id}")
def get_estimate(
    estimate_id: int,
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    row = session.get(Estimate, estimate_id)
    if row is None or row.taxpayer_id != taxpayer.id:
        raise HTTPException(status_code=404, detail="No such estimate.")
    return {
        "id": row.id, "tax_year": row.tax_year, "method": row.method,
        "filing_status": row.filing_status, "resident_state": row.resident_state,
        "federal": {
            "agi": str(row.federal_agi), "taxable_income": str(row.federal_taxable_income),
            "tax": str(row.federal_tax), "payments": str(row.federal_withheld),
            "balance": str(row.federal_balance),
        },
        "state": {"tax": str(row.state_tax), "withheld": str(row.state_withheld),
                  "balance": str(row.state_balance)},
        "total_balance": str(row.total_balance),
        "effective_rate": str(row.effective_rate),
        "marginal_rate": str(row.marginal_rate),
        "breakdown": row.breakdown,
        "options": row.options,
        "selected_options": row.selected_options,
        "headline": (row.inputs or {}).get("headline", ""),
        "run_at": row.created_at.isoformat() if row.created_at else None,
    }
