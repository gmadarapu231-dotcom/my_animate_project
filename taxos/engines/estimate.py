"""Putting it together: documents in, a complete estimate out.

This is the layer the API and both clients talk to. It owns three jobs:

1. Turn the W-2s and other forms a client uploaded into a `TaxProfile`,
   including the things the forms imply rather than state -- excess Social
   Security across two employers, which state lines exist, how much was
   deferred to a 401(k).
2. Run the federal calculation, then every state return that follows from it.
3. Answer in one of two modes. **Regular** is the return as the documents
   stand. **Planning** is the same year re-run with the moves the client could
   still make, so the two can be shown side by side with the difference between
   them named in dollars.

The regular estimate is always computed, even when planning is asked for,
because "what you would have paid" is the only thing that makes "what you could
pay instead" meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from taxos.config import federal, states as state_params
from taxos.enums import EstimateMethod
from taxos.engines.federal import FederalResult, TaxProfile, compute_federal
from taxos.engines.payments import build_payment_options, estimated_payments_for_next_year
from taxos.engines.planning import Strategy, apply_strategies, build_strategies
from taxos.engines.state import StateResult, compute_states
from taxos.forms.w2 import W2, combine, excess_social_security
from taxos.money import ZERO, cents, money, positive


@dataclass
class EstimateResult:
    """One complete answer: federal, states, and what happens to the money."""

    tax_year: int
    method: str
    filing_status: str
    resident_state: str
    federal: FederalResult
    states: list[StateResult] = field(default_factory=list)
    strategies: list[Strategy] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    baseline: "EstimateResult | None" = None
    payment_direction: str = ""
    payment_options: list[Any] = field(default_factory=list)
    next_year: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def state_tax(self) -> Decimal:
        return cents(sum((s.total_tax for s in self.states), ZERO))

    @property
    def state_withheld(self) -> Decimal:
        return cents(sum((s.withheld for s in self.states), ZERO))

    @property
    def state_balance(self) -> Decimal:
        return cents(sum((s.balance for s in self.states), ZERO))

    @property
    def total_tax(self) -> Decimal:
        return cents(self.federal.total_tax + self.state_tax)

    @property
    def total_balance(self) -> Decimal:
        """Positive means owed overall, negative means a refund overall."""
        return cents(self.federal.balance + self.state_balance)

    @property
    def total_refund(self) -> Decimal:
        return positive(-self.total_balance)

    @property
    def total_owed(self) -> Decimal:
        return positive(self.total_balance)

    @property
    def saving_against_baseline(self) -> Decimal:
        if self.baseline is None:
            return ZERO
        return cents(self.baseline.total_tax - self.total_tax)

    def headline(self) -> str:
        if self.total_balance < ZERO:
            body = f"Estimated refund of {self.total_refund:,.0f}"
        elif self.total_balance > ZERO:
            body = f"Estimated balance of {self.total_owed:,.0f} to pay"
        else:
            body = "Withholding matched the tax almost exactly"
        if self.method == EstimateMethod.PLANNING.value and self.saving_against_baseline > ZERO:
            body += f", {self.saving_against_baseline:,.0f} better than filing as-is"
        return body

    def to_dict(self, *, include_baseline: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tax_year": self.tax_year,
            "method": self.method,
            "filing_status": self.filing_status,
            "resident_state": self.resident_state,
            "headline": self.headline(),
            "federal": self.federal.to_dict(),
            "states": [s.to_dict() for s in self.states],
            "totals": {
                "federal_tax": str(self.federal.total_tax),
                "state_tax": str(self.state_tax),
                "total_tax": str(self.total_tax),
                "federal_balance": str(self.federal.balance),
                "state_balance": str(self.state_balance),
                "total_balance": str(self.total_balance),
                "total_refund": str(self.total_refund),
                "total_owed": str(self.total_owed),
                "effective_rate": str(self.federal.effective_rate),
                "marginal_rate": str(self.federal.marginal_rate),
            },
            "strategies": [s.to_dict() for s in self.strategies],
            "applied": self.applied,
            "saving_against_baseline": str(self.saving_against_baseline),
            "payment": {
                "direction": self.payment_direction,
                "options": [
                    o.to_dict() if hasattr(o, "to_dict") else o for o in self.payment_options
                ],
            },
            "next_year": self.next_year,
            "warnings": self.warnings,
            "notes": self.notes,
        }
        if include_baseline and self.baseline is not None:
            out["baseline"] = self.baseline.to_dict(include_baseline=False)
        return out


# ---------------------------------------------------------------------------
# documents -> profile
# ---------------------------------------------------------------------------
def profile_from_documents(
    forms: Iterable[W2],
    *,
    tax_year: int,
    filing_status: str = "single",
    resident_state: str = "",
    extra: dict[str, Any] | None = None,
) -> tuple[TaxProfile, list[dict[str, str]]]:
    """Build the calculation input from the W-2s, and collect every warning.

    `extra` carries what no W-2 knows: dependants, investment income, itemised
    deductions, the client's age. Anything not supplied stays at zero, which is
    the right default -- it produces an estimate based only on what was proven.
    """
    forms = list(forms)
    warnings: list[dict[str, str]] = []
    for index, form in enumerate(forms, start=1):
        for finding in form.validate(year=tax_year):
            warnings.append({**finding, "document": f"W-2 #{index}",
                             "employer": form.employer_name or "unnamed employer"})

    totals = combine(forms)
    excess_ss = excess_social_security(forms, year=tax_year)
    if excess_ss > ZERO:
        warnings.append({
            "severity": "info", "box": "4", "document": "across W-2s", "employer": "",
            "message": (
                f"Two or more employers withheld {excess_ss:,.2f} more Social Security than "
                "the annual maximum. That is a refundable credit on Schedule 3 and is "
                "already included below."
            ),
        })

    extra = dict(extra or {})
    # A resident state given explicitly wins; otherwise take the W-2's own state.
    home = (resident_state or "").strip().upper()
    if not home:
        home = next((f.primary_state for f in forms if f.primary_state), "")

    profile = TaxProfile(
        tax_year=tax_year,
        filing_status=filing_status,
        resident_state=home,
        wages=totals["wages"],
        federal_withheld=totals["federal_withheld"],
        social_security_wages=totals["social_security_wages"],
        medicare_withheld=totals["medicare_withheld"],
        tips=totals["tips"],
        dependent_care_benefits=totals["dependent_care_benefits"],
        excess_social_security=excess_ss,
    )
    for key, value in extra.items():
        if hasattr(profile, key) and value not in (None, ""):
            setattr(profile, key, value)
    return profile, warnings


def wage_lines_from(forms: Iterable[W2]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for form in forms:
        for row in form.states:
            if row.state:
                lines.append({
                    "state": row.state, "wages": row.state_wages, "withheld": row.state_withheld,
                })
    return lines


# ---------------------------------------------------------------------------
# the estimate
# ---------------------------------------------------------------------------
def run_estimate(
    profile: TaxProfile,
    *,
    wage_lines: list[dict[str, Any]] | None = None,
    method: str = EstimateMethod.REGULAR.value,
    chosen_strategies: list[str] | None = None,
    as_of: date | None = None,
    warnings: list[dict[str, str]] | None = None,
    planning_context: dict[str, Any] | None = None,
    include_payments: bool = True,
) -> EstimateResult:
    """Run one estimate. In planning mode the regular estimate comes with it."""
    today = as_of or date.today()
    params = federal(profile.tax_year)
    profile = profile.normalised(params)

    def one(current: TaxProfile, label: str) -> EstimateResult:
        federal_result = compute_federal(current)
        lines = list(wage_lines or [])
        if not lines and current.resident_state:
            # No W-2 state lines (manual entry): assume wages were earned at home.
            lines = [{
                "state": current.resident_state,
                "wages": current.wages,
                "withheld": ZERO,
            }]
        state_results = compute_states(
            lines,
            resident_state=current.resident_state,
            filing_status=current.filing_status,
            dependents=current.children_under_17 + current.other_dependents,
            federal_taxable_income=federal_result.taxable_income,
            federal_deduction=federal_result.deduction_taken,
            resident_income=current.wages + current.self_employment_income,
            long_term_gains=current.long_term_gains,
            year=current.tax_year,
        )
        return EstimateResult(
            tax_year=current.tax_year, method=label,
            filing_status=current.filing_status, resident_state=current.resident_state,
            federal=federal_result, states=state_results,
            warnings=list(warnings or []),
            notes=list(federal_result.notes),
        )

    regular = one(profile, EstimateMethod.REGULAR.value)
    regular.notes.append(state_params(profile.tax_year).disclaimer)

    if method != EstimateMethod.PLANNING.value:
        result = regular
    else:
        context = dict(planning_context or {})
        strategies = build_strategies(profile, as_of=today, **context)
        if chosen_strategies is None:
            # Default to every move that is still open, saves money, and is
            # something the client can actually do.
            chosen = [
                s.id for s in strategies
                if s.still_available and s.actionable and s.total_saving > ZERO
            ]
        else:
            chosen = list(chosen_strategies)

        planned_profile = apply_strategies(profile, strategies, chosen)
        result = one(planned_profile, EstimateMethod.PLANNING.value)
        result.strategies = strategies
        result.applied = chosen
        result.baseline = regular
        if not chosen:
            result.notes.append(
                "No planning move changes this year's outcome. That is a real answer: "
                "the deadlines for most moves have passed, or the return is already at "
                "its lowest tax."
            )

    # --- what happens to the money ----------------------------------------
    if include_payments:
        direction, options = build_payment_options(
            result.federal.balance, year=result.tax_year, as_of=today,
        )
        result.payment_direction = direction
        result.payment_options = options
        for state in result.states:
            if state.balance == ZERO:
                continue
            _, state_options = build_payment_options(
                state.balance, year=result.tax_year, as_of=today,
                jurisdiction="state", state_code=state.code,
            )
            recommended = next(
                (o for o in state_options if o.recommended),
                state_options[0] if state_options else None,
            )
            if recommended is not None:
                recommended.label = f"{state.code}: {recommended.label}"
                result.payment_options.append(recommended)

        result.next_year = estimated_payments_for_next_year(
            current_year_tax=result.federal.total_tax,
            current_year_agi=result.federal.agi,
            expected_withholding=profile.federal_withheld,
            year=result.tax_year,
        )
    return result


def compare_methods(
    profile: TaxProfile,
    *,
    wage_lines: list[dict[str, Any]] | None = None,
    as_of: date | None = None,
    planning_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Both modes at once, for the screen that asks the client to choose."""
    regular = run_estimate(profile, wage_lines=wage_lines, method=EstimateMethod.REGULAR.value,
                           as_of=as_of, include_payments=False)
    planning = run_estimate(profile, wage_lines=wage_lines, method=EstimateMethod.PLANNING.value,
                            as_of=as_of, planning_context=planning_context,
                            include_payments=False)
    return {
        "regular": {
            "label": "Regular",
            "description": "Your return exactly as the documents stand today.",
            "total_tax": str(regular.total_tax),
            "balance": str(regular.total_balance),
            "headline": regular.headline(),
        },
        "planning": {
            "label": "Planning",
            "description": (
                "The same year with the moves you can still make applied, so you can see "
                "what each one is worth before deciding."
            ),
            "total_tax": str(planning.total_tax),
            "balance": str(planning.total_balance),
            "headline": planning.headline(),
            "available_moves": len([s for s in planning.strategies if s.still_available and s.actionable]),
            "closed_moves": len([s for s in planning.strategies if not s.still_available]),
            "maximum_saving": str(planning.saving_against_baseline),
        },
        "difference": str(cents(regular.total_tax - planning.total_tax)),
    }
