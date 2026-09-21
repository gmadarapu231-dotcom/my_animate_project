"""Home loans: how much of the interest is actually deductible.

Form 1098 box 1 is not a deduction. Three things stand between it and
Schedule A, and each one loses clients money when it is missed:

  * **The debt ceiling.** Interest is deductible on up to $750,000 of debt
    used to buy, build or substantially improve the home ($375,000 filing
    separately). Debt taken on or before 15 December 2017 keeps the older
    $1,000,000 ceiling for the life of the loan, and a refinance inherits the
    grandfather up to the balance refinanced. Above the ceiling, the interest
    is prorated -- not disallowed outright -- which is the part people get
    wrong in both directions.
  * **What the money was used for.** A home equity loan or HELOC is deductible
    ONLY to the extent the proceeds bought, built or substantially improved
    the home securing it, and it shares the same ceiling rather than having
    one of its own. Borrowing against the house to clear a credit card or buy
    a car buys no deduction at all, however the lender labels the loan.
  * **Whether itemising is worth it.** All of this is on Schedule A, so it is
    worth nothing until the itemised total beats the standard deduction --
    which OBBBA pushed to $16,100 single and $32,200 joint for 2026. Most
    households with a mortgage still take the standard deduction, and telling
    a client that plainly is more use than a deduction they cannot reach.

Mortgage insurance is the awkward one. It was deductible through 2021, not at
all for 2022 to 2025, and is deductible again permanently from 2026, with a
phase-out starting at $100,000 of AGI. So the same client with the same loan
gets a different answer in two consecutive years, and the year's parameter
file -- not this module -- decides which.

Principal is never deductible. Neither is homeowners insurance, HOA dues, or
the part of a payment that is escrow until the tax is actually paid out of it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from taxvault.config import FederalParams
from taxvault.money import ZERO, cents, money, positive

#: How the proceeds of a loan were used. Only the first two are deductible.
USE_PURCHASE = "purchase"          # bought or built the home
USE_IMPROVE = "improve"            # substantially improved it
USE_OTHER = "other"                # anything else: no deduction
USES = (USE_PURCHASE, USE_IMPROVE, USE_OTHER)


@dataclass
class Loan:
    """One mortgage, home equity loan or HELOC, as Form 1098 reports it."""

    balance: Decimal = ZERO             # average balance for the year (box 2 is 1 Jan)
    interest_paid: Decimal = ZERO       # box 1
    points_paid: Decimal = ZERO         # box 6
    mortgage_insurance: Decimal = ZERO  # box 5
    origination: date | str | None = None
    kind: str = "acquisition"           # acquisition | home_equity | refinance
    used_for: str = USE_PURCHASE
    is_main_home: bool = True
    is_refinance: bool = False
    term_months: int = 360
    months_paid_this_year: int = 12
    unamortised_points_from_old_loan: Decimal = ZERO
    lender: str = ""

    def origination_date(self) -> date | None:
        value = self.origination
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return None


@dataclass
class MortgageResult:
    total_interest: Decimal = ZERO
    deductible_interest: Decimal = ZERO
    disallowed_interest: Decimal = ZERO
    points_deduction: Decimal = ZERO
    mortgage_insurance_deduction: Decimal = ZERO
    total_deduction: Decimal = ZERO
    qualifying_debt: Decimal = ZERO
    allowed_debt: Decimal = ZERO
    applicable_cap: Decimal = ZERO
    lines: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def line(self, label: str, amount: Decimal, *, note: str = "") -> None:
        self.lines.append(
            {"form": "Sch A", "label": label, "amount": str(cents(amount)), "note": note}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def compute_mortgage(
    loans: list[Loan],
    params: FederalParams,
    *,
    filing_status: str = "single",
    agi: Decimal = ZERO,
) -> MortgageResult:
    """Total deductible residence interest, points and mortgage insurance."""
    result = MortgageResult()
    status = params.normalise_status(filing_status)
    separate = status == "married_separately"
    cfg = "home_loans"

    grandfather_cutoff = _cutoff(params)
    acquisition_cap = params.amount(
        cfg, "acquisition_debt_cap_married_separately" if separate else "acquisition_debt_cap"
    )
    grandfathered_cap = params.amount(
        cfg, "grandfathered_debt_cap_married_separately" if separate else "grandfathered_debt_cap"
    )

    qualifying: list[tuple[Loan, Decimal, bool]] = []   # loan, balance, is grandfathered
    for loan in loans:
        interest = positive(loan.interest_paid)
        result.total_interest += interest
        if loan.used_for == USE_OTHER:
            # The classic: a HELOC spent on anything but the house.
            result.disallowed_interest += interest
            result.warnings.append(
                f"Interest of {interest:,.0f} on the "
                f"{'home equity loan' if loan.kind == 'home_equity' else 'loan'}"
                f"{' from ' + loan.lender if loan.lender else ''} is not deductible, because "
                "the money was not used to buy, build or substantially improve the home that "
                "secures it. Paying off a card or buying a car with it makes the interest "
                "personal interest, whatever the lender calls the loan."
            )
            continue
        started = loan.origination_date()
        old = bool(started and started <= grandfather_cutoff)
        qualifying.append((loan, positive(loan.balance), old))

    homes = sum(1 for loan, _, _ in qualifying if loan.is_main_home)
    limit_homes = int(params.get(cfg, "qualified_homes", default=2))
    if len({id(loan) for loan, _, _ in qualifying}) > limit_homes and homes <= 1:
        result.notes.append(
            f"Interest is deductible on at most {limit_homes} homes -- your main home and "
            "one other. A third property has to be a rental to deduct anything."
        )

    grandfathered_balance = sum((bal for _, bal, old in qualifying if old), ZERO)
    new_balance = sum((bal for _, bal, old in qualifying if not old), ZERO)
    total_balance = grandfathered_balance + new_balance
    result.qualifying_debt = cents(total_balance)

    # Grandfathered debt is measured against the old ceiling, and it eats into
    # the room available for newer debt rather than sitting alongside it.
    allowed_old = min(grandfathered_balance, grandfathered_cap)
    room_for_new = positive(acquisition_cap - allowed_old)
    allowed_new = min(new_balance, room_for_new)
    allowed = allowed_old + allowed_new
    result.allowed_debt = cents(allowed)
    result.applicable_cap = cents(grandfathered_cap if grandfathered_balance else acquisition_cap)

    if grandfathered_balance > ZERO:
        result.notes.append(
            f"{grandfathered_balance:,.0f} of your debt was taken on or before "
            f"{grandfather_cutoff:%d %B %Y}, so it keeps the older {grandfathered_cap:,.0f} "
            "ceiling. Keep the closing documents -- that date is worth money and you have "
            "to be able to prove it."
        )

    # Proration. Above the ceiling the interest is not lost, it is scaled.
    ratio = money(1)
    if total_balance > ZERO and allowed < total_balance:
        ratio = allowed / total_balance
        result.notes.append(
            f"Your mortgage balance of {total_balance:,.0f} is over the "
            f"{allowed:,.0f} that qualifies, so {ratio:.1%} of the interest is deductible "
            "and the rest is not. The deduction is prorated, not lost altogether."
        )

    deductible = ZERO
    points = ZERO
    insurance_base = ZERO
    for loan, _, _ in qualifying:
        deductible += positive(loan.interest_paid) * ratio
        points += _points(loan, params, result)
        insurance_base += positive(loan.mortgage_insurance)

    result.deductible_interest = cents(deductible)
    result.disallowed_interest = cents(
        result.disallowed_interest + positive(result.total_interest - deductible
                                              - result.disallowed_interest)
    )
    result.points_deduction = cents(points)
    result.mortgage_insurance_deduction = _insurance(
        insurance_base * ratio, params, result, status=status, agi=agi
    )
    result.total_interest = cents(result.total_interest)
    result.total_deduction = cents(
        result.deductible_interest + result.points_deduction
        + result.mortgage_insurance_deduction
    )

    if result.deductible_interest:
        result.line("Home mortgage interest (Form 1098 box 1)", result.deductible_interest)
    if result.points_deduction:
        result.line("Points (Form 1098 box 6)", result.points_deduction)
    if result.mortgage_insurance_deduction:
        result.line("Qualified mortgage insurance premiums",
                    result.mortgage_insurance_deduction)
    return result


def _cutoff(params: FederalParams) -> date:
    raw = str(params.get("home_loans", "grandfather_date", default="2017-12-15"))
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:  # pragma: no cover - the config ships a valid date
        return date(2017, 12, 15)


def _points(loan: Loan, params: FederalParams, result: MortgageResult) -> Decimal:
    """Points deductible this year: all of them, or this year's slice."""
    paid = positive(loan.points_paid)
    carried = positive(loan.unamortised_points_from_old_loan)
    total = ZERO

    if carried > ZERO:
        # Paying off a refinanced loan releases whatever is left of the points
        # that were being spread over it. Clients almost never claim this.
        total += carried
        result.notes.append(
            f"{carried:,.0f} of points left over from the loan you refinanced becomes "
            "fully deductible in the year that old loan is paid off. It is easy to miss "
            "because no 1098 reports it."
        )

    if paid <= ZERO:
        return total

    buying_main_home = (
        loan.is_main_home and not loan.is_refinance
        and loan.used_for in (USE_PURCHASE, USE_IMPROVE)
        and bool(params.get("home_loans", "points_deductible_in_full_on_purchase", default=True))
    )
    if buying_main_home:
        result.notes.append(
            f"The {paid:,.0f} of points you paid to buy your main home is deductible in "
            "full this year, rather than spread over the loan."
        )
        return total + paid

    months = max(1, int(loan.term_months or 360))
    this_year = min(12, max(0, int(loan.months_paid_this_year)))
    slice_ = paid * money(this_year) / money(months)
    result.notes.append(
        f"Points of {paid:,.0f} on a "
        f"{'refinance' if loan.is_refinance else 'second home'} are spread over the "
        f"{months // 12}-year term, so {slice_:,.0f} is deductible this year and the rest "
        "follows in later years. Keep the figure -- if you refinance again, what is left "
        "becomes deductible all at once."
    )
    return total + slice_


def _insurance(
    premiums: Decimal, params: FederalParams, result: MortgageResult, *,
    status: str, agi: Decimal,
) -> Decimal:
    """PMI, FHA MIP and VA funding fees -- when the year allows them at all."""
    premiums = positive(premiums)
    if premiums <= ZERO:
        return ZERO
    cfg = "home_loans"
    if not params.get(cfg, "mortgage_insurance_deductible", default=False):
        result.notes.append(
            f"Mortgage insurance of {premiums:,.0f} is not deductible for {params.year}. "
            "The deduction lapsed after 2021 and returns for tax year 2026, so the same "
            "premium is worth nothing this year and something next year."
        )
        return ZERO

    separate = status == "married_separately"
    start = params.amount(
        cfg,
        "mortgage_insurance_phase_out_start_married_separately" if separate
        else "mortgage_insurance_phase_out_start",
    )
    step = params.amount(cfg, "mortgage_insurance_phase_out_step", default=1000)
    rate = params.rate(cfg, "mortgage_insurance_phase_out_rate", default="0.10")

    excess = positive(money(agi) - start)
    if excess <= ZERO:
        result.notes.append(
            f"Mortgage insurance of {premiums:,.0f} is deductible as mortgage interest "
            f"for {params.year}."
        )
        return cents(premiums)

    # "10% for each $1,000, or fraction thereof, of AGI over the threshold" --
    # a proportion of the PREMIUM, not a flat dollar reduction, so this cannot
    # go through the general phase_out helper.
    steps = (excess / step).to_integral_value(rounding="ROUND_CEILING")
    kept = money(1) - steps * rate
    if kept <= ZERO:
        result.notes.append(
            f"Mortgage insurance of {premiums:,.0f} is fully phased out: the deduction "
            f"disappears once AGI reaches about {start + step * 10:,.0f}."
        )
        return ZERO
    allowed = cents(premiums * kept)
    result.notes.append(
        f"Mortgage insurance is reduced to {allowed:,.0f} of {premiums:,.0f}, because AGI "
        f"of {money(agi):,.0f} is over {start:,.0f}. It drops 10% for every $1,000 above, "
        f"and is gone entirely at {start + step * 10:,.0f}."
    )
    return allowed


# ===========================================================================
# Selling
# ===========================================================================
def home_sale_exclusion(
    params: FederalParams, *, filing_status: str = "single", gain: Decimal = ZERO,
    years_owned: Decimal | float = 0, years_lived_in: Decimal | float = 0,
) -> dict[str, Any]:
    """Section 121: up to $250,000 of gain excluded, $500,000 filing jointly."""
    status = params.normalise_status(filing_status)
    cap = params.by_status("home_loans", "home_sale_exclusion", status=status)
    needed = money(params.get("home_loans", "home_sale_ownership_years", default=2))
    lookback = money(params.get("home_loans", "home_sale_lookback_years", default=5))
    gain = positive(gain)

    qualifies = money(years_owned) >= needed and money(years_lived_in) >= needed
    excluded = min(gain, cap) if qualifies else ZERO
    notes = []
    if qualifies:
        notes.append(
            f"You owned and lived in the home for at least {needed} of the last "
            f"{lookback} years, so up to {cap:,.0f} of the gain is excluded."
        )
    else:
        notes.append(
            f"The exclusion needs {needed} years of ownership AND {needed} years of living "
            f"there within the last {lookback}. Short of that the gain is taxable, though "
            "a partial exclusion may apply for a move forced by work, health or an "
            "unforeseen event."
        )
    notes.append(
        "Improvements you paid for add to your basis and reduce the gain -- a new roof, an "
        "addition, a kitchen. Keep those receipts; repairs do not count, improvements do."
    )
    return {
        "exclusion_cap": str(cents(cap)),
        "gain": str(cents(gain)),
        "excluded": str(cents(excluded)),
        "taxable_gain": str(cents(positive(gain - excluded))),
        "qualifies": qualifies,
        "notes": notes,
    }
