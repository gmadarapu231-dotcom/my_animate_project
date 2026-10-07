"""The Premium Tax Credit: reconciling a marketplace health subsidy.

This is the single most consequential form most clients have never heard of.
If anyone in the household bought health insurance on a marketplace and took
the subsidy as a monthly discount -- which almost everyone does -- then the
subsidy was an ADVANCE, paid on an estimate of this year's income made last
year. Form 8962 settles up. Earn less than the estimate and the rest comes
back as a refundable credit; earn more and some of it is repaid.

The mechanics, in the order they matter:

  1. Household income as a percentage of the federal poverty line. Note the
     poverty line is the PRIOR year's: 2025 guidelines for 2026 coverage.
  2. An applicable percentage from that, interpolated within a band.
  3. The required contribution: that percentage of household income.
  4. The credit: the benchmark plan's premium (the second-lowest-cost silver
     plan, which is on Form 1095-A column B) minus the required contribution.
  5. Against the advance already paid (column C), giving a credit or a
     repayment.

**2026 is the year this gets much worse for some people.** The enhanced
subsidies from the American Rescue Plan, extended through 2025, lapse. Two
reversions:

  * The 400% cliff returns. One dollar of income over 400% of the poverty line
    costs the ENTIRE credit, not a tapered part of it. For a family of four on
    a mid-priced plan that can be more than $20,000 over one dollar -- which
    makes a deductible IRA or HSA contribution in December worth many multiples
    of what it costs. `cliff_warning` exists to say so while there is still
    time.
  * The contribution percentage rises from a 0%-8.5% range to 2.10%-9.96%, so
    even households far below the cliff pay more.

Repayment is capped below 400% of the poverty line and uncapped above it. That
asymmetry is the thing to watch: a client who guessed low on income and landed
over 400% repays every dollar of the advance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from taxvault.config import FederalParams
from taxvault.money import ZERO, cents, money, positive

#: States with their own poverty guidelines. Everywhere else is "contiguous".
ALASKA, HAWAII = "AK", "HI"


@dataclass
class MarketplaceCoverage:
    """Form 1095-A, as the marketplace issues it.

    One per policy. A household that changed plans mid-year gets two, and the
    monthly columns are what make them add up -- but the annual totals in the
    bottom row are what Form 8962 uses when coverage ran all year, which is
    the common case.
    """

    #: Column A: what the plan actually cost.
    annual_premium: Decimal = ZERO
    #: Column B: the second-lowest-cost silver plan for this household. This
    #: is the figure the credit is measured against, NOT what they paid.
    benchmark_premium: Decimal = ZERO
    #: Column C: the advance credit already paid to the insurer.
    advance_credit: Decimal = ZERO
    months_covered: int = 12
    marketplace_state: str = ""
    policy_number: str = ""


@dataclass
class PremiumTaxCreditResult:
    household_income: Decimal = ZERO
    household_size: int = 1
    poverty_line: Decimal = ZERO
    income_as_pct_of_fpl: Decimal = ZERO
    applicable_percentage: Decimal = ZERO
    required_contribution: Decimal = ZERO
    benchmark_premium: Decimal = ZERO
    premiums_paid: Decimal = ZERO
    #: What the client was entitled to for the year.
    allowed_credit: Decimal = ZERO
    #: What was already paid to the insurer on their behalf.
    advance_credit: Decimal = ZERO
    #: Positive: a refundable credit. Never negative.
    net_credit: Decimal = ZERO
    #: Positive: owed back. Capped below 400% of the poverty line.
    repayment: Decimal = ZERO
    repayment_before_cap: Decimal = ZERO
    repayment_cap: Decimal | None = None
    eligible: bool = True
    over_cliff: bool = False
    lines: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def line(self, label: str, amount: Decimal, note: str = "") -> None:
        self.lines.append(
            {"form": "8962", "label": label, "amount": str(cents(amount)), "note": note}
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["repayment_cap"] = (
            None if self.repayment_cap is None else str(cents(self.repayment_cap))
        )
        return out


def poverty_line(params: FederalParams, *, household_size: int,
                 state_code: str = "") -> Decimal:
    """The federal poverty line for this household.

    Alaska and Hawaii have their own, higher, figures -- about 25% and 15%
    above the contiguous states -- and using the wrong one moves a household
    across a subsidy band.
    """
    table = params.get("premium_tax_credit", "poverty_line", default={}) or {}
    code = (state_code or "").strip().upper()
    key = "alaska" if code == ALASKA else "hawaii" if code == HAWAII else "contiguous"
    row = table.get(key, table.get("contiguous", {}))
    first = money(row.get("first_person", 0))
    each = money(row.get("each_additional", 0))
    size = max(1, int(household_size))
    return cents(first + each * (size - 1))


def applicable_percentage(params: FederalParams, pct_of_fpl: Decimal) -> Decimal:
    """The share of income a household is expected to put towards the benchmark.

    Interpolated inside each band, which is how the statute writes it: a
    household at 175% of the poverty line sits halfway between the 150% and
    200% rates, not at one end.
    """
    bands = params.get("premium_tax_credit", "applicable_percentage", default=[]) or []
    for band in bands:
        low = money(band["fpl_from"])
        high = band.get("fpl_to")
        rate_from = money(band["rate_from"])
        rate_to = money(band["rate_to"])
        if high is None:
            if pct_of_fpl >= low:
                return rate_to
            continue
        top = money(high)
        if low <= pct_of_fpl < top:
            if rate_from == rate_to or top == low:
                return rate_from
            share = (pct_of_fpl - low) / (top - low)
            return rate_from + (rate_to - rate_from) * share
    # Above every band: the last band's top rate, or nothing where a cliff
    # applies (the caller checks the cliff before getting here).
    return money(bands[-1]["rate_to"]) if bands else ZERO


def compute_premium_tax_credit(
    policies: list[MarketplaceCoverage],
    params: FederalParams,
    *,
    household_income: Decimal,
    household_size: int = 1,
    filing_status: str = "single",
    state_code: str = "",
) -> PremiumTaxCreditResult:
    """Form 8962, start to finish."""
    result = PremiumTaxCreditResult(
        household_income=cents(positive(household_income)),
        household_size=max(1, int(household_size)),
    )
    if not policies:
        return result

    result.benchmark_premium = cents(
        sum((positive(p.benchmark_premium) for p in policies), ZERO)
    )
    result.premiums_paid = cents(
        sum((positive(p.annual_premium) for p in policies), ZERO)
    )
    result.advance_credit = cents(
        sum((positive(p.advance_credit) for p in policies), ZERO)
    )

    line = poverty_line(params, household_size=household_size, state_code=state_code)
    result.poverty_line = line
    if line <= ZERO:  # pragma: no cover - the config ships a table
        return result

    pct = (result.household_income / line * 100).quantize(Decimal("0.01"))
    result.income_as_pct_of_fpl = pct
    result.line("Household income as a percentage of the poverty line", pct,
                note=f"{result.household_income:,.0f} against a poverty line of "
                     f"{line:,.0f} for {result.household_size}")

    # --- the cliff ---------------------------------------------------------
    cap = money(params.get("premium_tax_credit", "income_cap_fpl", default=0))
    # Compared in DOLLARS, not in the rounded percentage. The statute measures
    # household income against 400% of the poverty line; testing the percentage
    # rounded to two places lets a dollar or two over the line read as under
    # it, and on this credit a dollar is worth the whole thing.
    cliff_income = cents(line * cap / 100) if cap > ZERO else ZERO
    if cap > ZERO and result.household_income > cliff_income:
        result.eligible = False
        result.over_cliff = True
        result.allowed_credit = ZERO
        result.repayment_before_cap = result.advance_credit
        result.repayment = result.advance_credit
        result.repayment_cap = None
        result.warnings.append(
            f"Household income is {pct:,.0f}% of the federal poverty line, over the "
            f"{cap:,.0f}% limit, so NO premium tax credit is allowed for "
            f"{params.year} -- and the whole {result.advance_credit:,.0f} advance has "
            "to be repaid. There is no cap on the repayment above this line. "
            f"Income of {cliff_income:,.0f} or less would have kept the "
            "credit, so anything that reduces income for the year -- a deductible "
            "IRA, an HSA contribution, a deferral -- is worth far more than it costs."
        )
        result.line("Premium tax credit allowed", ZERO, note="over the income limit")
        result.line("Advance credit to repay", result.repayment)
        return result

    # --- the credit --------------------------------------------------------
    rate = applicable_percentage(params, pct)
    result.applicable_percentage = rate.quantize(Decimal("0.0001"))
    result.required_contribution = cents(result.household_income * rate)
    result.allowed_credit = cents(
        positive(result.benchmark_premium - result.required_contribution)
    )
    # The credit can never exceed what the plan actually cost: a cheap plan
    # with a high benchmark does not pay the client the difference.
    if result.premiums_paid > ZERO:
        result.allowed_credit = min(result.allowed_credit, result.premiums_paid)

    result.line("Benchmark plan premium for the year", result.benchmark_premium,
                note="second-lowest-cost silver plan, 1095-A column B")
    result.line(f"Your expected contribution ({rate:.2%} of income)",
                -result.required_contribution)
    result.line("Premium tax credit allowed", result.allowed_credit)
    result.line("Advance credit already paid to the insurer",
                -result.advance_credit, note="1095-A column C")

    difference = result.allowed_credit - result.advance_credit
    if difference >= ZERO:
        result.net_credit = cents(difference)
        if result.net_credit > ZERO:
            result.notes.append(
                f"You were entitled to {result.allowed_credit:,.0f} and "
                f"{result.advance_credit:,.0f} was paid in advance, so "
                f"{result.net_credit:,.0f} comes back as a refundable credit -- it is "
                "paid even if you owe no tax."
            )
        result.line("Net premium tax credit", result.net_credit)
    else:
        result.repayment_before_cap = cents(-difference)
        limit = _repayment_cap(params, pct, filing_status)
        result.repayment_cap = limit
        result.repayment = (
            result.repayment_before_cap if limit is None
            else min(result.repayment_before_cap, limit)
        )
        result.line("Excess advance credit to repay", result.repayment,
                    note="added to the tax you owe")
        if limit is not None and result.repayment_before_cap > limit:
            result.notes.append(
                f"The advance was {result.repayment_before_cap:,.0f} more than you were "
                f"entitled to, but repayment is capped at {limit:,.0f} at "
                f"{pct:,.0f}% of the poverty line, so that is what is owed."
            )
        else:
            result.warnings.append(
                f"{result.repayment:,.0f} of the advance credit has to be repaid: your "
                "income came out higher than the figure the marketplace used. Tell the "
                "marketplace when your income changes during the year and this does not "
                "build up."
            )

    _advise(result, params, pct, cap)
    return result


def _repayment_cap(params: FederalParams, pct: Decimal,
                   filing_status: str) -> Decimal | None:
    """The most that has to be repaid. None means uncapped."""
    rows = params.get("premium_tax_credit", "repayment_limit", default=[]) or []
    single = filing_status in ("single", "married_separately")
    for row in rows:
        top = row.get("fpl_to")
        if top is None or pct < money(top):
            value = row.get("single" if single else "other")
            return None if value is None else money(value)
    return None


def _advise(result: PremiumTaxCreditResult, params: FederalParams,
            pct: Decimal, cap: Decimal) -> None:
    """The one warning worth interrupting someone for."""
    if cap <= ZERO:
        if not params.get("premium_tax_credit", "enhanced_subsidies", default=False):
            return
        result.notes.append(
            f"For {params.year} there is no income limit on this credit: above 400% of "
            "the poverty line it is capped at 8.5% of income rather than withdrawn. "
            "That ends after 2025 -- from 2026 one dollar over 400% costs the whole "
            "credit."
        )
        return

    # Within 25 percentage points of the cliff is close enough that a bonus, a
    # Roth conversion or a capital gain could cross it.
    if cap > ZERO and cap - 25 <= pct <= cap:
        room = cents(result.poverty_line * cap / 100 - result.household_income)
        result.warnings.append(
            f"You are at {pct:,.0f}% of the federal poverty line and the credit "
            f"disappears entirely above {cap:,.0f}%. That is {room:,.0f} of income "
            f"away, and crossing it costs the whole {result.allowed_credit:,.0f} "
            "credit, not a part of it. Before taking a bonus, a Roth conversion or a "
            "capital gain this year, work out what it does to this figure."
        )
