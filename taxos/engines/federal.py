"""The federal calculation, in Form 1040 order.

Order matters and is not negotiable. AGI gates a dozen phase-outs, so it is
computed before anything that phases out; the deduction is compared *after* AGI
because the medical floor and the charitable limit are both AGI percentages;
QBI is limited by taxable income, so it comes after the deduction; and credits
split into non-refundable (which can only reduce tax to zero) and refundable
(which can pay out beyond it), because getting that backwards is the difference
between a $0 refund and a $6,000 one.

Preferential income -- qualified dividends and long-term gains -- is *stacked*
on top of ordinary income rather than taxed in isolation. A client with $40,000
of wages and $40,000 of long-term gain does not pay 0% on the gain just because
$40,000 is under the 0% breakpoint: the wages fill the bracket first. Getting
this wrong flatters the estimate by thousands, so it is implemented explicitly
in `_preferential_tax`.

Every line the calculation touches is recorded in `FederalResult.lines`, so the
UI can show the client the derivation instead of a bare number.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from taxos.config import FederalParams, federal
from taxos.money import ZERO, cents, marginal_rate, money, phase_out, positive, tax_on, whole


# ===========================================================================
# Input
# ===========================================================================
#: Fields of `TaxProfile` that are not money and must not be coerced.
_NON_AMOUNTS = frozenset({
    "tax_year", "filing_status", "resident_state", "age", "spouse_age",
    "children_under_17", "other_dependents", "education_credit_kind",
})


@dataclass
class TaxProfile:
    """Everything the calculation needs, in one place.

    Defaults are all zero/false so a caller supplies only what the client has:
    a wage earner with one W-2 sets four fields, and every phase-out, credit and
    surtax below simply resolves to nothing.
    """

    tax_year: int = 2025
    filing_status: str = "single"
    resident_state: str = ""

    # --- people ---
    age: int = 40
    spouse_age: int = 0
    is_blind: bool = False
    spouse_is_blind: bool = False
    children_under_17: int = 0
    other_dependents: int = 0
    can_be_claimed_by_another: bool = False

    # --- earned income ---
    wages: Decimal = ZERO
    federal_withheld: Decimal = ZERO
    tips: Decimal = ZERO                      # inside wages; drives the OBBBA deduction
    overtime_premium: Decimal = ZERO          # inside wages; the half-time premium only
    self_employment_income: Decimal = ZERO
    social_security_wages: Decimal = ZERO     # for excess-SS and Additional Medicare
    medicare_withheld: Decimal = ZERO

    # --- investment and other income ---
    taxable_interest: Decimal = ZERO
    tax_exempt_interest: Decimal = ZERO
    ordinary_dividends: Decimal = ZERO
    qualified_dividends: Decimal = ZERO       # subset of ordinary_dividends
    short_term_gains: Decimal = ZERO
    long_term_gains: Decimal = ZERO
    rental_income: Decimal = ZERO
    retirement_distributions: Decimal = ZERO
    social_security_benefits: Decimal = ZERO
    unemployment: Decimal = ZERO
    other_income: Decimal = ZERO

    # --- adjustments (above the line) ---
    hsa_contribution: Decimal = ZERO          # made directly, NOT via payroll
    traditional_ira: Decimal = ZERO
    student_loan_interest: Decimal = ZERO
    educator_expenses: Decimal = ZERO
    self_employed_health_insurance: Decimal = ZERO
    other_adjustments: Decimal = ZERO
    covered_by_retirement_plan: bool = False

    # --- itemised deductions ---
    state_local_income_tax: Decimal = ZERO
    property_tax: Decimal = ZERO
    mortgage_interest: Decimal = ZERO
    charitable_cash: Decimal = ZERO
    charitable_noncash: Decimal = ZERO
    medical_expenses: Decimal = ZERO
    other_itemised: Decimal = ZERO
    force_itemise: bool = False

    # --- OBBBA temporary deductions ---
    car_loan_interest: Decimal = ZERO

    # --- business ---
    qbi_income: Decimal = ZERO
    is_specified_service_business: bool = False

    # --- credits ---
    dependent_care_expenses: Decimal = ZERO
    dependent_care_benefits: Decimal = ZERO   # W-2 box 10, reduces the eligible base
    qualified_education_expenses: Decimal = ZERO
    education_credit_kind: str = "american_opportunity"  # or lifetime_learning
    retirement_contributions_for_savers: Decimal = ZERO
    foreign_tax_paid: Decimal = ZERO
    eitc_eligible: bool = True

    # --- payments already made ---
    estimated_payments: Decimal = ZERO
    excess_social_security: Decimal = ZERO
    prior_year_tax: Decimal = ZERO
    prior_year_agi: Decimal = ZERO

    def normalised(self, params: FederalParams) -> "TaxProfile":
        """Coerce every amount to Decimal and the status to its canonical name."""
        # Anything not in `_NON_AMOUNTS` is an amount, so a new field added to
        # this dataclass is coerced automatically rather than silently staying
        # a float. Booleans are skipped explicitly: `bool` is an `int` subclass.
        data = asdict(self)
        for key, value in data.items():
            if key in _NON_AMOUNTS or isinstance(value, bool):
                continue
            data[key] = money(value)
        data["filing_status"] = params.normalise_status(self.filing_status)
        return TaxProfile(**data)

    @property
    def is_married_joint(self) -> bool:
        return self.filing_status in ("married_jointly", "qualifying_surviving_spouse")


# ===========================================================================
# Output
# ===========================================================================
@dataclass
class FederalResult:
    tax_year: int
    filing_status: str
    total_income: Decimal = ZERO
    adjustments: Decimal = ZERO
    agi: Decimal = ZERO
    magi: Decimal = ZERO
    deduction_taken: Decimal = ZERO
    deduction_kind: str = "standard"
    standard_deduction: Decimal = ZERO
    itemised_deduction: Decimal = ZERO
    senior_deduction: Decimal = ZERO
    obbba_deductions: Decimal = ZERO
    qbi_deduction: Decimal = ZERO
    taxable_income: Decimal = ZERO
    ordinary_tax: Decimal = ZERO
    preferential_tax: Decimal = ZERO
    amt: Decimal = ZERO
    self_employment_tax: Decimal = ZERO
    additional_medicare_tax: Decimal = ZERO
    net_investment_income_tax: Decimal = ZERO
    nonrefundable_credits: Decimal = ZERO
    refundable_credits: Decimal = ZERO
    total_tax: Decimal = ZERO
    total_payments: Decimal = ZERO
    balance: Decimal = ZERO          # positive = owed, negative = refund
    effective_rate: Decimal = ZERO
    marginal_rate: Decimal = ZERO
    lines: list[dict[str, Any]] = field(default_factory=list)
    credits_detail: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def refund(self) -> Decimal:
        return positive(-self.balance)

    @property
    def owed(self) -> Decimal:
        return positive(self.balance)

    def line(self, label: str, amount: Decimal, *, form: str = "1040", note: str = "") -> None:
        self.lines.append(
            {"form": form, "label": label, "amount": str(cents(amount)), "note": note}
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["refund"] = str(self.refund)
        out["owed"] = str(self.owed)
        return out


# ===========================================================================
# Pieces
# ===========================================================================
def _standard_deduction(profile: TaxProfile, params: FederalParams) -> Decimal:
    status = profile.filing_status
    base = params.amount("standard_deduction", status)
    if profile.can_be_claimed_by_another:
        # A dependent's standard deduction is capped at earned income + $450,
        # floored at $1,350 -- the rule that catches every student return.
        earned = profile.wages + profile.self_employment_income
        base = min(base, max(money(1350), earned + money(450)))

    married = status in ("married_jointly", "married_separately", "qualifying_surviving_spouse")
    extra_each = params.amount(
        "standard_deduction", "additional_married" if married else "additional_unmarried"
    )
    conditions = 0
    if profile.age >= 65:
        conditions += 1
    if profile.is_blind:
        conditions += 1
    if profile.is_married_joint:
        if profile.spouse_age >= 65:
            conditions += 1
        if profile.spouse_is_blind:
            conditions += 1
    return cents(base + extra_each * conditions)


def _senior_deduction(profile: TaxProfile, params: FederalParams, agi: Decimal) -> Decimal:
    """OBBBA's $6,000-per-senior deduction, 2025-2028.

    It is allowed on top of the standard deduction *and* on top of itemising,
    which is unusual enough that clients routinely miss it.
    """
    per_person = params.amount("senior_deduction", "amount")
    if per_person <= ZERO or params.year > int(params.get("senior_deduction", "expires_after", default=0)):
        return ZERO
    seniors = (1 if profile.age >= 65 else 0)
    if profile.is_married_joint and profile.spouse_age >= 65:
        seniors += 1
    if not seniors:
        return ZERO
    threshold = params.by_status("senior_deduction", "thresholds", status=profile.filing_status)
    return phase_out(
        per_person * seniors,
        agi,
        threshold,
        rate=params.rate("senior_deduction", "phase_out_rate"),
        step=1,
    )


def _itemised(profile: TaxProfile, params: FederalParams, agi: Decimal) -> tuple[Decimal, list[str]]:
    notes: list[str] = []
    status = profile.filing_status

    salt_paid = profile.state_local_income_tax + profile.property_tax
    cap = params.amount(
        "deductions",
        "salt_cap_married_separately" if status == "married_separately" else "salt_cap",
    )
    threshold = params.amount("deductions", "salt_phase_down_threshold")
    if threshold > ZERO and agi > threshold:
        cap = phase_out(
            cap, agi, threshold,
            rate=params.rate("deductions", "salt_phase_down_rate"),
            step=1,
            floor_amount=params.amount("deductions", "salt_floor"),
        )
        notes.append(
            f"The SALT cap phases down above {threshold:,.0f} of income; "
            f"this return's cap is {cap:,.0f}."
        )
    salt = min(salt_paid, cap)
    if salt_paid > cap:
        notes.append(f"State and local tax of {salt_paid:,.0f} is capped at {cap:,.0f}.")

    floor = params.rate("deductions", "medical_agi_floor")
    medical = positive(profile.medical_expenses - agi * floor)
    if profile.medical_expenses > ZERO and medical <= ZERO:
        notes.append(
            f"Medical costs of {profile.medical_expenses:,.0f} fall under the "
            f"{floor:.1%} of AGI floor ({agi * floor:,.0f}), so none is deductible."
        )

    charity_limit = agi * params.rate("deductions", "charitable_cash_agi_limit")
    charity = min(profile.charitable_cash, charity_limit) + profile.charitable_noncash

    total = salt + profile.mortgage_interest + charity + medical + profile.other_itemised
    return cents(total), notes


def _obbba_deductions(profile: TaxProfile, params: FederalParams, agi: Decimal) -> tuple[Decimal, list[str]]:
    """Tips, overtime and car-loan interest: below the line, but no itemising needed."""
    notes: list[str] = []
    total = ZERO
    status = profile.filing_status

    tips_cap = params.amount("deductions", "tips_deduction_cap")
    if tips_cap > ZERO and profile.tips > ZERO:
        amount = phase_out(
            min(profile.tips, tips_cap), agi,
            params.by_status("deductions", "tips_phase_out", status=status),
            rate="0.10", step=1000,
        )
        if amount > ZERO:
            total += amount
            notes.append(f"Qualified tips deduction of {amount:,.0f} (no itemising required).")

    ot_cap = params.by_status("deductions", "overtime_deduction_cap", status=status)
    if ot_cap > ZERO and profile.overtime_premium > ZERO:
        amount = phase_out(
            min(profile.overtime_premium, ot_cap), agi,
            params.by_status("deductions", "overtime_phase_out", status=status),
            rate="0.10", step=1000,
        )
        if amount > ZERO:
            total += amount
            notes.append(f"Qualified overtime deduction of {amount:,.0f}.")

    car_cap = params.amount("deductions", "car_loan_interest_cap")
    if car_cap > ZERO and profile.car_loan_interest > ZERO:
        amount = phase_out(
            min(profile.car_loan_interest, car_cap), agi,
            params.by_status("deductions", "car_loan_phase_out", status=status),
            rate="0.20", step=1000,
        )
        if amount > ZERO:
            total += amount
            notes.append(f"Car loan interest deduction of {amount:,.0f} (US-assembled vehicles).")

    return cents(total), notes


def _preferential_tax(
    ordinary_taxable: Decimal, preferential: Decimal, params: FederalParams, status: str
) -> Decimal:
    """Tax on qualified dividends and long-term gain, stacked above ordinary income.

    The bands are measured on *total* taxable income, so ordinary income fills
    them first and the gain is taxed in whatever is left.
    """
    if preferential <= ZERO:
        return ZERO
    table = params.get("capital_gains", status, default={}) or {}
    zero_top = money(table.get("zero_up_to", 0))
    fifteen_top = money(table.get("fifteen_up_to", 0))
    rates = [money(r) for r in params.get("capital_gains", "rates", default=[0, "0.15", "0.20"])]

    remaining = preferential
    base = ordinary_taxable
    due = ZERO

    at_zero = min(remaining, positive(zero_top - base))
    due += at_zero * rates[0]
    remaining -= at_zero
    base += at_zero

    at_fifteen = min(remaining, positive(fifteen_top - base))
    due += at_fifteen * rates[1]
    remaining -= at_fifteen

    due += positive(remaining) * rates[2]
    return cents(due)


def _self_employment_tax(profile: TaxProfile, params: FederalParams) -> tuple[Decimal, Decimal]:
    """Returns (SE tax, the deductible half)."""
    if profile.self_employment_income <= ZERO:
        return ZERO, ZERO
    net = cents(profile.self_employment_income * params.rate("payroll", "self_employment_base_fraction"))
    if net < money(400):
        return ZERO, ZERO  # under $400, no SE tax is due
    wage_base = params.amount("payroll", "social_security_wage_base")
    # W-2 wages consume the Social Security base first.
    ss_room = positive(wage_base - profile.social_security_wages)
    ss_tax = min(net, ss_room) * params.rate("payroll", "social_security_rate") * 2
    medicare_tax = net * params.rate("payroll", "medicare_rate") * 2
    total = cents(ss_tax + medicare_tax)
    return total, cents(total / 2)


def _additional_medicare(profile: TaxProfile, params: FederalParams) -> Decimal:
    threshold = params.by_status("payroll", "additional_medicare_threshold", status=profile.filing_status)
    base = profile.wages + positive(
        profile.self_employment_income * params.rate("payroll", "self_employment_base_fraction")
    )
    return cents(positive(base - threshold) * params.rate("payroll", "additional_medicare_rate"))


def _niit(profile: TaxProfile, params: FederalParams, magi: Decimal) -> Decimal:
    investment = (
        profile.taxable_interest + profile.ordinary_dividends + profile.short_term_gains
        + profile.long_term_gains + profile.rental_income
    )
    if investment <= ZERO:
        return ZERO
    threshold = params.by_status("net_investment_income_tax", "thresholds", status=profile.filing_status)
    excess = positive(magi - threshold)
    return cents(min(investment, excess) * params.rate("net_investment_income_tax", "rate"))


def _taxable_social_security(profile: TaxProfile, params: FederalParams, other_income: Decimal) -> Decimal:
    """The provisional-income test: 0%, up to 50%, or up to 85% is taxable."""
    benefits = profile.social_security_benefits
    if benefits <= ZERO:
        return ZERO
    provisional = other_income + profile.tax_exempt_interest + benefits / 2
    joint = profile.is_married_joint
    first, second = (money(32000), money(44000)) if joint else (money(25000), money(34000))
    if provisional <= first:
        return ZERO
    if provisional <= second:
        return cents(min(benefits * money("0.5"), (provisional - first) * money("0.5")))
    base = (second - first) * money("0.5")
    return cents(min(
        benefits * money("0.85"),
        base + (provisional - second) * money("0.85"),
    ))


def _qbi_deduction(profile: TaxProfile, params: FederalParams, taxable_before_qbi: Decimal,
                   preferential: Decimal) -> tuple[Decimal, str]:
    if profile.qbi_income <= ZERO:
        return ZERO, ""
    rate = params.rate("qbi", "rate")
    threshold = params.by_status("qbi", "threshold", status=profile.filing_status)
    tentative = profile.qbi_income * rate
    # The overall limit: 20% of taxable income less net capital gain.
    ceiling = positive(taxable_before_qbi - preferential) * rate
    note = ""
    if profile.is_specified_service_business:
        phase_range = params.by_status("qbi", "phase_in_range", status=profile.filing_status)
        over = positive(taxable_before_qbi - threshold)
        if over >= phase_range:
            return ZERO, ("A specified service business gets no QBI deduction once taxable "
                          f"income passes {threshold + phase_range:,.0f}.")
        if over > ZERO:
            keep = (phase_range - over) / phase_range
            tentative *= keep
            note = f"QBI is phasing out for a service business: {keep:.0%} of it remains."
    return cents(min(tentative, ceiling)), note


# ===========================================================================
# Credits
# ===========================================================================
def _child_credits(profile: TaxProfile, params: FederalParams, agi: Decimal) -> tuple[Decimal, Decimal, dict[str, str]]:
    """Returns (non-refundable part, refundable ACTC, detail)."""
    detail: dict[str, str] = {}
    per_child = params.amount("credits", "child_tax_credit", "amount")
    per_other = params.amount("credits", "other_dependent_credit", "amount")
    gross = per_child * profile.children_under_17 + per_other * profile.other_dependents
    if gross <= ZERO:
        return ZERO, ZERO, detail

    threshold = params.by_status("credits", "child_tax_credit", "thresholds", status=profile.filing_status)
    allowed = phase_out(
        gross, agi, threshold,
        rate=params.rate("credits", "child_tax_credit", "phase_out_rate"),
        step=params.amount("credits", "child_tax_credit", "phase_out_step"),
    )
    if allowed < gross:
        detail["child_tax_credit_phaseout"] = (
            f"Reduced from {gross:,.0f} to {allowed:,.0f} because income passes {threshold:,.0f}."
        )
    detail["child_tax_credit"] = str(allowed)
    return allowed, ZERO, detail  # the refundable split happens once tax is known


def _actc(profile: TaxProfile, params: FederalParams, unused_credit: Decimal) -> Decimal:
    """The refundable slice of the child credit, on Schedule 8812."""
    if profile.children_under_17 <= 0 or unused_credit <= ZERO:
        return ZERO
    floor = params.amount("credits", "child_tax_credit", "earned_income_floor")
    rate = params.rate("credits", "child_tax_credit", "refundable_rate")
    cap_each = params.amount("credits", "child_tax_credit", "refundable_cap")
    earned = profile.wages + profile.self_employment_income
    from_earnings = positive(earned - floor) * rate
    ceiling = cap_each * profile.children_under_17
    return cents(min(unused_credit, from_earnings, ceiling))


def _eitc(profile: TaxProfile, params: FederalParams, agi: Decimal) -> tuple[Decimal, str]:
    if not profile.eitc_eligible or profile.filing_status == "married_separately":
        return ZERO, ""
    investment = (profile.taxable_interest + profile.ordinary_dividends
                  + profile.short_term_gains + profile.long_term_gains)
    limit = params.amount("credits", "earned_income_credit", "investment_income_limit")
    if investment > limit:
        earned_now = profile.wages + profile.self_employment_income
        # Only worth saying to someone who would otherwise have qualified.
        # Telling a $700k return why it missed the EITC is noise.
        near_range = money(70000)
        if agi <= near_range and earned_now > ZERO:
            return ZERO, (f"Investment income of {investment:,.0f} exceeds the EITC limit of "
                          f"{limit:,.0f}, so no earned income credit is available.")
        return ZERO, ""

    children = min(profile.children_under_17 + profile.other_dependents, 3)
    tiers = params.get("credits", "earned_income_credit", "tiers", default=[])
    tier = next((t for t in tiers if int(t["children"]) == children), None)
    if tier is None:
        return ZERO, ""
    earned = profile.wages + profile.self_employment_income
    if earned <= ZERO:
        return ZERO, ""
    if children == 0 and not (25 <= profile.age < 65):
        return ZERO, "The childless EITC requires an age between 25 and 64."

    credit = min(earned * money(tier["credit_rate"]), money(tier["max_credit"]))
    start = money(tier["phase_out_joint" if profile.is_married_joint else "phase_out_single"])
    measure = max(agi, earned)
    credit = positive(credit - positive(measure - start) * money(tier["phase_out_rate"]))
    return cents(credit), ""


def _dependent_care_credit(profile: TaxProfile, params: FederalParams, agi: Decimal) -> Decimal:
    expenses = profile.dependent_care_expenses
    if expenses <= ZERO:
        return ZERO
    dependents = max(1, profile.children_under_17 + profile.other_dependents)
    cap = params.amount(
        "credits", "dependent_care", "expense_cap_one" if dependents == 1 else "expense_cap_many"
    )
    # An employer dependent-care FSA reduces the expenses eligible for the credit.
    eligible = positive(min(expenses, cap) - profile.dependent_care_benefits)
    if eligible <= ZERO:
        return ZERO
    max_rate = params.rate("credits", "dependent_care", "max_rate")
    min_rate = params.rate("credits", "dependent_care", "min_rate")
    start = params.amount("credits", "dependent_care", "phase_down_start")
    steps = positive(agi - start) / money(2000)
    rate = max(min_rate, max_rate - money("0.01") * steps.to_integral_value(rounding="ROUND_CEILING"))
    return cents(eligible * rate)


def _education_credit(profile: TaxProfile, params: FederalParams, agi: Decimal) -> tuple[Decimal, Decimal]:
    """Returns (non-refundable part, refundable part)."""
    expenses = profile.qualified_education_expenses
    if expenses <= ZERO:
        return ZERO, ZERO
    kind = profile.education_credit_kind
    node = "american_opportunity" if kind == "american_opportunity" else "lifetime_learning"
    start = params.by_status("credits", node, "phase_out_start", status=profile.filing_status)
    end = params.by_status("credits", node, "phase_out_end", status=profile.filing_status)

    if kind == "american_opportunity":
        # 100% of the first $2,000 plus 25% of the next $2,000.
        credit = min(expenses, money(2000)) + min(positive(expenses - money(2000)), money(2000)) * money("0.25")
        credit = min(credit, params.amount("credits", node, "max_credit"))
    else:
        capped = min(expenses, params.amount("credits", node, "expense_cap"))
        credit = min(capped * params.rate("credits", node, "rate"),
                     params.amount("credits", node, "max_credit"))

    if agi >= end:
        return ZERO, ZERO
    if agi > start:
        credit *= (end - agi) / (end - start)

    if kind == "american_opportunity":
        refundable_share = params.rate("credits", node, "refundable_fraction")
        return cents(credit * (1 - refundable_share)), cents(credit * refundable_share)
    return cents(credit), ZERO


def _savers_credit(profile: TaxProfile, params: FederalParams, agi: Decimal) -> Decimal:
    contributions = profile.retirement_contributions_for_savers
    if contributions <= ZERO:
        return ZERO
    status = profile.filing_status
    key = ("married_jointly" if profile.is_married_joint
           else "head_of_household" if status == "head_of_household" else "single")
    bands = params.get("credits", "savers_credit", "agi_bands", key, default=None)
    if not bands:
        return ZERO
    rates = [money(r) for r in params.get("credits", "savers_credit", "rates", default=[])]
    cap = params.amount("credits", "savers_credit", "contribution_cap")
    if profile.is_married_joint:
        cap *= 2
    base = min(contributions, cap)
    for limit, rate in zip(bands, rates):
        if agi <= money(limit):
            return cents(base * rate)
    return ZERO


def _amt(profile: TaxProfile, params: FederalParams, result: FederalResult, agi: Decimal) -> Decimal:
    """Alternative Minimum Tax, for the cases that actually reach it.

    Since 2018 the AMT bites mainly on large ISO exercises and very high income
    with big SALT. This models the common path -- add back the SALT deduction
    and the standard deduction, apply the exemption and its phase-out -- and
    does not model ISO adjustments, which need Form 6251 data this system does
    not collect. When the result is close, `notes` says so rather than implying
    certainty.
    """
    salt_added_back = ZERO
    if result.deduction_kind == "itemised":
        salt_added_back = min(
            profile.state_local_income_tax + profile.property_tax,
            params.amount("deductions", "salt_cap"),
        )
        amti = agi - (result.itemised_deduction - salt_added_back)
    else:
        amti = agi  # the standard deduction is not allowed against AMTI

    exemption = params.by_status("amt", "exemption", status=profile.filing_status)
    start = params.by_status("amt", "phase_out_start", status=profile.filing_status)
    exemption = phase_out(
        exemption, amti, start, rate=params.rate("amt", "phase_out_rate"), step=1
    )
    base = positive(amti - exemption)
    from taxos.money import brackets_from
    tentative = tax_on(base, brackets_from(params.get("amt", "rates", default=[])))
    regular = result.ordinary_tax + result.preferential_tax
    return positive(tentative - regular)


# ===========================================================================
# The calculation
# ===========================================================================
def compute_federal(profile: TaxProfile, *, year: int | None = None) -> FederalResult:
    params = federal(year or profile.tax_year)
    profile = profile.normalised(params)
    status = profile.filing_status
    result = FederalResult(tax_year=params.year, filing_status=status)
    result.notes.append(f"Computed on {params.year} law ({params.source}).")

    # --- 1. total income ---------------------------------------------------
    ordinary_income_pieces = (
        profile.wages + profile.taxable_interest + profile.ordinary_dividends
        + profile.short_term_gains + profile.rental_income + profile.retirement_distributions
        + profile.unemployment + profile.other_income + profile.self_employment_income
    )
    taxable_ss = _taxable_social_security(profile, params, ordinary_income_pieces + profile.long_term_gains)
    total_income = cents(ordinary_income_pieces + profile.long_term_gains + taxable_ss)
    result.total_income = total_income
    result.line("Wages, salaries, tips (Box 1)", profile.wages)
    if profile.taxable_interest:
        result.line("Taxable interest", profile.taxable_interest)
    if profile.ordinary_dividends:
        result.line("Ordinary dividends", profile.ordinary_dividends,
                    note=f"of which {profile.qualified_dividends:,.0f} qualified")
    if profile.short_term_gains or profile.long_term_gains:
        result.line("Capital gains", profile.short_term_gains + profile.long_term_gains,
                    form="Sch D",
                    note=f"{profile.long_term_gains:,.0f} long-term at preferential rates")
    if profile.self_employment_income:
        result.line("Business income", profile.self_employment_income, form="Sch C")
    if taxable_ss:
        result.line("Taxable Social Security", taxable_ss,
                    note=f"of {profile.social_security_benefits:,.0f} received")
    result.line("Total income", total_income)

    # --- 2. adjustments -> AGI --------------------------------------------
    se_tax, se_deduction = _self_employment_tax(profile, params)
    student_loan = min(profile.student_loan_interest,
                       params.amount("deductions", "student_loan_interest_cap"))
    adjustments = cents(
        profile.hsa_contribution + profile.traditional_ira + student_loan
        + min(profile.educator_expenses, params.amount("deductions", "educator_expense_cap"))
        + profile.self_employed_health_insurance + profile.other_adjustments + se_deduction
    )
    agi = cents(positive(total_income - adjustments))
    result.adjustments, result.agi, result.magi = adjustments, agi, agi
    if adjustments:
        result.line("Adjustments to income", -adjustments, form="Sch 1")
    result.line("Adjusted gross income", agi)

    # --- 3. deduction ------------------------------------------------------
    standard = _standard_deduction(profile, params)
    itemised, itemised_notes = _itemised(profile, params, agi)
    senior = _senior_deduction(profile, params, agi)
    result.standard_deduction, result.itemised_deduction, result.senior_deduction = (
        standard, itemised, senior
    )
    itemise = profile.force_itemise or itemised > standard
    if status == "married_separately" and profile.force_itemise:
        result.notes.append(
            "Filing separately: if one spouse itemises, the other must too."
        )
    result.deduction_kind = "itemised" if itemise else "standard"
    base_deduction = itemised if itemise else standard
    result.notes.extend(itemised_notes)
    if not itemise and itemised > ZERO:
        result.notes.append(
            f"Itemising would give {itemised:,.0f} against a standard deduction of "
            f"{standard:,.0f}, so the standard deduction is taken."
        )

    obbba, obbba_notes = _obbba_deductions(profile, params, agi)
    result.obbba_deductions = obbba
    result.notes.extend(obbba_notes)
    if senior > ZERO:
        result.notes.append(
            f"Senior deduction of {senior:,.0f} applies on top of the "
            f"{result.deduction_kind} deduction."
        )

    deduction_total = cents(base_deduction + senior + obbba)
    result.deduction_taken = deduction_total
    result.line(
        f"{'Itemised' if itemise else 'Standard'} deduction", -base_deduction,
        form="Sch A" if itemise else "1040",
    )
    if senior:
        result.line("Senior deduction (OBBBA)", -senior)
    if obbba:
        result.line("Tips / overtime / car loan deduction (OBBBA)", -obbba)

    taxable_before_qbi = positive(agi - deduction_total)
    preferential = min(profile.qualified_dividends + positive(profile.long_term_gains),
                       taxable_before_qbi)

    # --- 4. QBI ------------------------------------------------------------
    qbi, qbi_note = _qbi_deduction(profile, params, taxable_before_qbi, preferential)
    result.qbi_deduction = qbi
    if qbi:
        result.line("Qualified business income deduction", -qbi, form="8995")
    if qbi_note:
        result.notes.append(qbi_note)

    taxable = positive(taxable_before_qbi - qbi)
    result.taxable_income = taxable
    result.line("Taxable income", taxable)

    # --- 5. tax ------------------------------------------------------------
    preferential = min(preferential, taxable)
    ordinary_taxable = positive(taxable - preferential)
    result.ordinary_tax = tax_on(ordinary_taxable, params.brackets(status))
    result.preferential_tax = _preferential_tax(ordinary_taxable, preferential, params, status)
    result.line("Tax on ordinary income", result.ordinary_tax)
    if result.preferential_tax or preferential:
        result.line("Tax on qualified dividends and long-term gain", result.preferential_tax,
                    note="stacked above ordinary income")

    result.amt = _amt(profile, params, result, agi)
    if result.amt > ZERO:
        result.line("Alternative minimum tax", result.amt, form="6251")
        result.notes.append(
            "AMT is estimated without Form 6251 preference items (ISO exercises, "
            "depletion, private-activity bonds). Confirm before filing."
        )

    tax_before_credits = cents(result.ordinary_tax + result.preferential_tax + result.amt)

    # --- 6. non-refundable credits ----------------------------------------
    child_credit, _, credit_detail = _child_credits(profile, params, agi)
    education_nonref, education_ref = _education_credit(profile, params, agi)
    care_credit = _dependent_care_credit(profile, params, agi)
    savers = _savers_credit(profile, params, agi)
    foreign = profile.foreign_tax_paid

    wanted = cents(child_credit + education_nonref + care_credit + savers + foreign)
    applied = min(wanted, tax_before_credits)
    result.nonrefundable_credits = applied
    result.credits_detail = credit_detail
    for label, amount in (
        ("Child tax credit", child_credit), ("Education credit", education_nonref),
        ("Child and dependent care credit", care_credit), ("Saver's credit", savers),
        ("Foreign tax credit", foreign),
    ):
        if amount > ZERO:
            result.credits_detail[label] = str(cents(amount))
    # The child credit unused against tax comes back as the refundable ACTC, so
    # it is computed before deciding how much credit is genuinely lost.
    unused_child_credit = positive(child_credit - min(child_credit, tax_before_credits))
    actc = _actc(profile, params, unused_child_credit)

    lost = positive(wanted - applied - actc)
    if lost > ZERO:
        result.notes.append(
            f"{lost:,.0f} of non-refundable credit cannot be used: these credits reduce tax "
            "to zero but are not paid out. Shifting income into this year, or a Roth "
            "conversion, would put it to work."
        )

    # --- 7. other taxes ----------------------------------------------------
    result.self_employment_tax = se_tax
    result.additional_medicare_tax = positive(
        _additional_medicare(profile, params) - ZERO
    )
    result.net_investment_income_tax = _niit(profile, params, agi)
    if se_tax:
        result.line("Self-employment tax", se_tax, form="Sch SE")
    if result.additional_medicare_tax:
        result.line("Additional Medicare tax", result.additional_medicare_tax, form="8959")
    if result.net_investment_income_tax:
        result.line("Net investment income tax", result.net_investment_income_tax, form="8960")

    result.total_tax = cents(
        positive(tax_before_credits - applied)
        + se_tax + result.additional_medicare_tax + result.net_investment_income_tax
    )
    result.line("Total tax", result.total_tax)

    # --- 8. refundable credits and payments -------------------------------
    eitc, eitc_note = _eitc(profile, params, agi)
    if eitc_note:
        result.notes.append(eitc_note)
    result.refundable_credits = cents(eitc + actc + education_ref)
    for label, amount in (
        ("Earned income credit", eitc), ("Additional child tax credit", actc),
        ("Refundable education credit", education_ref),
    ):
        if amount > ZERO:
            result.credits_detail[label] = str(cents(amount))

    result.total_payments = cents(
        profile.federal_withheld + profile.estimated_payments
        + profile.excess_social_security + result.refundable_credits
    )
    result.line("Federal income tax withheld (Box 2)", profile.federal_withheld)
    if profile.estimated_payments:
        result.line("Estimated tax payments", profile.estimated_payments)
    if profile.excess_social_security:
        result.line("Excess Social Security withheld", profile.excess_social_security, form="Sch 3")
    if result.refundable_credits:
        result.line("Refundable credits", result.refundable_credits)
    result.line("Total payments", result.total_payments)

    # --- 9. the answer -----------------------------------------------------
    result.balance = cents(result.total_tax - result.total_payments)
    result.line(
        "Amount owed" if result.balance > ZERO else "Refund",
        abs(result.balance),
    )
    if agi > ZERO:
        result.effective_rate = (result.total_tax / agi).quantize(Decimal("0.0001"))
    result.marginal_rate = marginal_rate(taxable, params.brackets(status))
    return result
