"""Running an estimate: regular, planning, or both side by side."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from taxvault.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxvault.auth import audit
from taxvault.config import UnsupportedTaxYear, federal, latest_year
from taxvault.db.models import Estimate, TaxDocument, Taxpayer
from taxvault.enums import EstimateMethod
from taxvault.engines.estimate import (
    EstimateResult,
    compare_methods,
    profile_from_documents,
    run_estimate,
    wage_lines_from,
)
from taxvault.engines.capital import CapitalInput
from taxvault.engines.mortgage import USES, Loan
from taxvault.engines.retirement import (
    ContributionPlan,
    Distribution,
    compute_rmd,
    penalty_exceptions,
)
from taxvault.forms.w2 import W2

router = APIRouter(prefix="/api/estimates", tags=["estimates"])



class SaleDetail(BaseModel):
    """Schedule D, in as much detail as the client has.

    Everything is optional. A client who just knows they made money on shares
    fills in `short_term` and `long_term` and stops; one whose broker sent a
    consolidated 1099 fills in the rest and gets the netting, the carryforward
    and the wash-sale adjustment applied properly.
    """

    short_term: float = 0.0                 # negative for a loss
    long_term: float = 0.0
    capital_gain_distributions: float = 0.0  # 1099-DIV box 2a
    carryforward_short: float = 0.0          # a prior-year loss, as a positive
    carryforward_long: float = 0.0
    wash_sale_disallowed: float = 0.0        # 1099-B box 1g
    collectibles_gain: float = 0.0
    unrecaptured_1250_gain: float = 0.0

    def to_input(self) -> CapitalInput:
        return CapitalInput(
            short_term=Decimal(str(self.short_term)),
            long_term=Decimal(str(self.long_term)),
            capital_gain_distributions=Decimal(str(self.capital_gain_distributions)),
            carryforward_short=Decimal(str(self.carryforward_short)),
            carryforward_long=Decimal(str(self.carryforward_long)),
            wash_sale_disallowed=Decimal(str(self.wash_sale_disallowed)),
            collectibles_gain=Decimal(str(self.collectibles_gain)),
            unrecaptured_1250_gain=Decimal(str(self.unrecaptured_1250_gain)),
        )


class DistributionDetail(BaseModel):
    """One Form 1099-R."""

    payer: str = ""
    gross: float = 0.0                      # box 1
    taxable: float | None = None            # box 2a; null means "all of it"
    code: str = "7"                         # box 7
    federal_withheld: float = 0.0           # box 4
    state_withheld: float = 0.0
    is_roth: bool = False
    roth_years: int = 0
    roth_basis: float = 0.0                 # box 5
    age_at_distribution: float = 0.0
    rolled_over: float = 0.0
    penalty_exception: str = ""
    plan_kind: str = "401k"                 # 401k, 403b, ira, simple

    def to_input(self) -> Distribution:
        return Distribution(
            payer=self.payer,
            gross=Decimal(str(self.gross)),
            taxable=None if self.taxable is None else Decimal(str(self.taxable)),
            code=self.code,
            federal_withheld=Decimal(str(self.federal_withheld)),
            state_withheld=Decimal(str(self.state_withheld)),
            is_roth=self.is_roth,
            roth_years=self.roth_years,
            roth_basis=Decimal(str(self.roth_basis)),
            age_at_distribution=self.age_at_distribution,
            rolled_over=Decimal(str(self.rolled_over)),
            penalty_exception=self.penalty_exception,
            plan_kind=self.plan_kind,
        )


class LoanDetail(BaseModel):
    """One Form 1098.

    `used_for` is the field no lender reports and every client has to answer:
    a home equity loan is deductible only to the extent it bought, built or
    improved the home securing it.
    """

    lender: str = ""
    balance: float = 0.0                    # box 2, or the average for the year
    interest_paid: float = 0.0              # box 1
    points_paid: float = 0.0                # box 6
    mortgage_insurance: float = 0.0         # box 5
    origination: str = ""                   # box 3 or 11, ISO date
    kind: str = "acquisition"               # acquisition | home_equity | refinance
    used_for: str = "purchase"              # purchase | improve | other
    is_main_home: bool = True
    is_refinance: bool = False
    term_months: int = 360
    months_paid_this_year: int = 12
    unamortised_points_from_old_loan: float = 0.0

    def to_input(self) -> Loan:
        used = self.used_for if self.used_for in USES else "purchase"
        return Loan(
            lender=self.lender,
            balance=Decimal(str(self.balance)),
            interest_paid=Decimal(str(self.interest_paid)),
            points_paid=Decimal(str(self.points_paid)),
            mortgage_insurance=Decimal(str(self.mortgage_insurance)),
            origination=self.origination or None,
            kind=self.kind,
            used_for=used,
            is_main_home=self.is_main_home,
            is_refinance=self.is_refinance,
            term_months=self.term_months,
            months_paid_this_year=self.months_paid_this_year,
            unamortised_points_from_old_loan=Decimal(
                str(self.unamortised_points_from_old_loan)
            ),
        )


class PlanDetail(BaseModel):
    """What is going INTO an employer plan this year.

    This never changes the tax owed -- a pre-tax deferral is already out of
    W-2 box 1 -- but it drives the contribution-limit checks and the Roth
    comparison.
    """

    traditional_401k: float = 0.0
    roth_401k: float = 0.0
    employer_contribution: float = 0.0
    compensation: float = 0.0
    prior_year_wages: float = 0.0
    plan_kind: str = "401k"

    def to_input(self, age: int) -> ContributionPlan:
        return ContributionPlan(
            age=age,
            traditional_401k=Decimal(str(self.traditional_401k)),
            roth_401k=Decimal(str(self.roth_401k)),
            employer_contribution=Decimal(str(self.employer_contribution)),
            compensation=Decimal(str(self.compensation)),
            prior_year_wages=Decimal(str(self.prior_year_wages)),
            plan_kind=self.plan_kind,
        )


class RmdDetail(BaseModel):
    """Required minimum distributions, which are their own calculation."""

    birth_year: int = 0
    prior_year_balance: float = 0.0
    taken: float = 0.0
    is_roth_401k: bool = False
    still_working_for_plan_sponsor: bool = False
    owns_five_percent: bool = False


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
    capital_gain_distributions: float = 0
    capital_loss_carryforward_short: float = 0
    capital_loss_carryforward_long: float = 0
    wash_sale_disallowed: float = 0
    collectibles_gain: float = 0
    unrecaptured_1250_gain: float = 0
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

    # Structured detail. Each one overrides the simple figures above.
    sales: SaleDetail | None = None
    distributions: list[DistributionDetail] = Field(default_factory=list)
    loans: list[LoanDetail] = Field(default_factory=list)
    plan: PlanDetail | None = None
    rmd: RmdDetail | None = None

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

#: Handled explicitly below, not by the generic setattr pass -- these are
#: objects, and `profile_from_documents` only knows how to copy scalars.
STRUCTURED_KEYS = {"sales", "distributions", "loans", "plan", "rmd"}


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


def _load_other(session: Session, taxpayer: Taxpayer, year: int) -> list[TaxDocument]:
    """Every non-W-2 form on file for the year."""
    return list(session.scalars(
        select(TaxDocument).where(
            TaxDocument.taxpayer_id == taxpayer.id,
            TaxDocument.tax_year == year,
            TaxDocument.kind != "w2",
            TaxDocument.status != "rejected",
        ).order_by(TaxDocument.id)
    ).all())


def _amount(payload: dict[str, Any], key: str) -> Decimal:
    try:
        return Decimal(str(payload.get(key) or 0))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


def _autofill(
    profile: Any, documents: list[TaxDocument], situation: Situation,
) -> list[dict[str, str]]:
    """Fold uploaded 1099s and 1098s into the calculation.

    The forms are the source of truth for what they cover: a client who typed
    a dividend figure AND uploaded the 1099-DIV gets the form's number, and is
    told so, because adding the two would double it.

    Detail the client entered explicitly on the screen -- `sales`, `loans`,
    `distributions` -- wins over the documents, since that is them correcting
    what was read.
    """
    notes: list[dict[str, str]] = []
    if not documents:
        return notes

    def took(kind: str, what: str, payer: str) -> None:
        notes.append({
            "severity": "info", "box": "", "document": kind.replace("_", "-").upper(),
            "employer": payer or "",
            "message": f"{what} was taken from your {kind.replace('_', '-').upper()}"
                       + (f" from {payer}" if payer else "") + ".",
        })

    short = long_ = distributions_total = Decimal("0")
    wash = dividends = qualified = gain_distributions = Decimal("0")
    unrecaptured = collectibles = foreign = Decimal("0")
    rows: list[Distribution] = []
    loans: list[Loan] = []
    seen: set[str] = set()

    for document in documents:
        payload = document.payload or {}
        payer = document.employer_name or ""
        kind = document.kind
        seen.add(kind)

        if kind == "1099_b":
            short += _amount(payload, "short_term_gain")
            long_ += _amount(payload, "long_term_gain")
            wash += _amount(payload, "wash_sale_disallowed")
            took(kind, "Your capital gain", payer)
        elif kind == "1099_div":
            dividends += _amount(payload, "ordinary_dividends")
            qualified += _amount(payload, "qualified_dividends")
            gain_distributions += _amount(payload, "capital_gain_distributions")
            unrecaptured += _amount(payload, "unrecaptured_1250")
            collectibles += _amount(payload, "collectibles_gain")
            foreign += _amount(payload, "foreign_tax_paid")
            took(kind, "Your dividend income", payer)
        elif kind == "1099_int":
            profile.taxable_interest += _amount(payload, "interest_income")
        elif kind == "1099_r":
            taxable = payload.get("taxable_amount")
            rows.append(Distribution(
                payer=payer,
                gross=_amount(payload, "gross_distribution"),
                taxable=None if taxable in (None, "") else Decimal(str(taxable)),
                code=str(payload.get("distribution_code") or "7"),
                federal_withheld=_amount(payload, "federal_withheld"),
                state_withheld=_amount(payload, "state_withheld"),
                roth_basis=_amount(payload, "employee_contributions"),
                age_at_distribution=float(profile.age or 0),
                plan_kind=str(payload.get("plan_kind") or "401k"),
            ))
            distributions_total += _amount(payload, "gross_distribution")
            took(kind, "Your retirement distribution", payer)
        elif kind == "1098":
            loans.append(Loan(
                lender=payer,
                balance=_amount(payload, "outstanding_principal"),
                interest_paid=_amount(payload, "mortgage_interest"),
                points_paid=_amount(payload, "points_paid"),
                mortgage_insurance=_amount(payload, "mortgage_insurance"),
                origination=payload.get("best_origination") or None,
            ))
            took(kind, "Your mortgage interest", payer)

    # Dividends: the form wins over anything typed on the screen.
    if dividends or qualified:
        if situation.ordinary_dividends and dividends:
            notes.append({
                "severity": "info", "box": "1a", "document": "1099-DIV", "employer": "",
                "message": (
                    f"You entered {situation.ordinary_dividends:,.0f} of dividends and "
                    f"your 1099-DIV says {dividends:,.0f}. The form is used -- adding "
                    "both would count the same money twice."
                ),
            })
        profile.ordinary_dividends = dividends
        profile.qualified_dividends = qualified
        profile.foreign_tax_paid = profile.foreign_tax_paid or foreign

    if situation.sales is None and ("1099_b" in seen or "1099_div" in seen):
        profile.capital = CapitalInput(
            short_term=short or Decimal(str(situation.short_term_gains)),
            long_term=long_ or Decimal(str(situation.long_term_gains)),
            capital_gain_distributions=gain_distributions,
            carryforward_short=Decimal(str(situation.capital_loss_carryforward_short)),
            carryforward_long=Decimal(str(situation.capital_loss_carryforward_long)),
            wash_sale_disallowed=wash,
            collectibles_gain=collectibles,
            unrecaptured_1250_gain=unrecaptured,
        )

    if rows and not situation.distributions:
        profile.distributions = rows
        profile.retirement_distributions = Decimal("0")

    if loans and not situation.loans:
        profile.loans = loans
        profile.mortgage_interest = Decimal("0")
        notes.append({
            "severity": "warning", "box": "", "document": "1098", "employer": "",
            "message": (
                "Mortgage interest from your 1098 is treated as money that bought or "
                "improved the home. If any of this borrowing paid for something else -- a "
                "car, a card, tuition -- say so on the Home loans screen: that part is not "
                "deductible and the form does not show it."
            ),
        })
    return notes


def _build(session: Session, taxpayer: Taxpayer, body: EstimateRequest) -> tuple[Any, Any, Any]:
    year = body.tax_year or latest_year()
    # Check the year before anything else. Telling someone asking about 1999
    # that they have "no income on file" answers a question they did not ask.
    federal(year)
    forms = _load_forms(session, taxpayer, year)

    situation = body.situation.model_dump()
    planning_context = {k: situation.pop(k) for k in list(situation) if k in PLANNING_KEYS}
    for key in STRUCTURED_KEYS:
        situation.pop(key, None)
    home = situation.pop("resident_state", "") or (taxpayer.resident_state or "")
    status = situation.pop("filing_status", "single")

    profile, warnings = profile_from_documents(
        forms, tax_year=year, filing_status=status, resident_state=home, extra=situation,
    )
    detail = body.situation
    other = _load_other(session, taxpayer, year)
    warnings.extend(_autofill(profile, other, detail))

    if detail.sales is not None:
        profile.capital = detail.sales.to_input()
    if detail.distributions:
        profile.distributions = [d.to_input() for d in detail.distributions]
    if detail.loans:
        profile.loans = [loan.to_input() for loan in detail.loans]
    if detail.plan is not None:
        profile.retirement_plan = detail.plan.to_input(age=profile.age)

    # The "nothing to work with" check comes AFTER the forms have been folded
    # in. A retiree whose only document is a 1099-R has income; asking them to
    # add a W-2 they will never have is the wrong answer to the right worry.
    if not _has_income(profile, situation):
        raise HTTPException(
            status_code=400,
            detail=(
                f"There is no income on file for {year}. Add a W-2 or another tax form, "
                "or enter income in the situation details, and the estimate will follow."
            ),
        )

    return profile, wage_lines_from(forms), (warnings, planning_context, year)


def _has_income(profile: Any, situation: dict[str, Any]) -> bool:
    """Is there anything at all to compute an estimate from?"""
    if profile.wages or profile.distributions or profile.loans:
        return True
    if profile.capital is not None and not profile.capital.is_empty():
        return True
    scalars = (
        "self_employment_income", "retirement_distributions", "social_security_benefits",
        "other_income", "taxable_interest", "ordinary_dividends", "rental_income",
        "unemployment", "short_term_gains", "long_term_gains",
    )
    if any(getattr(profile, name, 0) for name in scalars):
        return True
    return any(situation.get(name) for name in scalars)


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
