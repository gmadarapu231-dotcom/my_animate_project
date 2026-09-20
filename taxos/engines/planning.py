"""Planning: what the client could still do, and what each move is worth.

Every strategy here is evaluated by *re-running the whole calculation* with the
change applied and taking the difference. No strategy claims "your marginal
rate times the contribution", because that shortcut is wrong wherever a
phase-out sits: a $5,000 traditional IRA at $85,000 of income can be worth far
more than 22% of $5,000 if it drags AGI back under a credit threshold, and it
can be worth nothing if the client is already at zero tax. Measuring beats
estimating, and it costs one extra pass over a calculation that takes
microseconds.

The other thing this module insists on is **whether a move is still possible**.
Most planning tools present a list of ideas without saying that the 401(k)
window shut on 31 December. Each strategy carries the date it closes, so the
screen separates "you can still do this for the year being filed" from "this is
for next year now".
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from taxos.config import FederalParams, federal
from taxos.enums import PlanningStrategy
from taxos.engines.federal import TaxProfile, compute_federal
from taxos.engines.state import compute_state
from taxos.money import ZERO, cents, money, positive

#: When the door closes on a kind of move, relative to the tax year.
WINDOW_YEAR_END = "year_end"              # 31 Dec of the tax year
WINDOW_FILING = "filing_deadline"          # 15 Apr after the tax year
WINDOW_EXTENDED = "extended_deadline"      # 15 Oct with an extension
WINDOW_ELECTION = "election"               # a choice made on the return itself


@dataclass
class Strategy:
    """One planning move, priced."""

    id: str
    label: str
    category: str
    summary: str
    how_it_works: str
    window: str
    closes_on: date | None = None
    amount: Decimal = ZERO             # what the client would contribute or change
    headroom: Decimal = ZERO           # how much more is allowed
    federal_saving: Decimal = ZERO
    state_saving: Decimal = ZERO
    cash_required: Decimal = ZERO
    still_available: bool = True
    #: False for entries that inform rather than offer a move the client can
    #: make. Residency and withholding belong on the screen, but ranking them
    #: above a real, fundable action because the headline number is bigger
    #: would be misleading.
    actionable: bool = True
    confidence: str = "high"
    caveats: list[str] = field(default_factory=list)

    @property
    def total_saving(self) -> Decimal:
        return cents(self.federal_saving + self.state_saving)

    @property
    def return_on_cash(self) -> Decimal:
        """Tax saved per dollar of cash the client has to part with.

        A 401(k) contribution is not free -- the money is locked up -- so a
        strategy that saves $500 for $2,000 of cash is ranked behind one that
        saves $300 for nothing.
        """
        if self.cash_required <= ZERO:
            return money(999)  # costs nothing: always worth doing
        return (self.total_saving / self.cash_required).quantize(Decimal("0.001"))

    def to_dict(self) -> dict[str, Any]:
        out = {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }
        out["closes_on"] = self.closes_on.isoformat() if self.closes_on else None
        out["total_saving"] = str(self.total_saving)
        out["return_on_cash"] = str(self.return_on_cash)
        return out


def window_closes(window: str, tax_year: int, params: FederalParams) -> date | None:
    if window == WINDOW_YEAR_END:
        return date(tax_year, 12, 31)
    if window == WINDOW_FILING:
        return date.fromisoformat(params.due_date) if params.due_date else None
    if window == WINDOW_EXTENDED:
        return date.fromisoformat(params.extended_due_date) if params.extended_due_date else None
    return None  # an election lives as long as the return can be amended


# ---------------------------------------------------------------------------
# the measuring machinery
# ---------------------------------------------------------------------------
def _with(profile: TaxProfile, **changes: Any) -> TaxProfile:
    clone = copy.deepcopy(profile)
    for key, value in changes.items():
        setattr(clone, key, value)
    return clone


def _state_tax(profile: TaxProfile, federal_result: Any, state_income: Decimal) -> Decimal:
    """State tax for a scenario, or zero where the client is in a no-tax state."""
    code = (profile.resident_state or "").strip().upper()
    if not code:
        return ZERO
    try:
        result = compute_state(
            code,
            state_income=state_income,
            filing_status=profile.filing_status,
            dependents=profile.children_under_17 + profile.other_dependents,
            federal_taxable_income=federal_result.taxable_income,
            federal_deduction=federal_result.deduction_taken,
            year=profile.tax_year,
        )
    except LookupError:
        return ZERO
    return result.total_tax


def _price(baseline_federal: Decimal, baseline_state: Decimal, profile: TaxProfile,
           *, state_income_delta: Decimal = ZERO) -> tuple[Decimal, Decimal]:
    """Run the scenario and return (federal saving, state saving)."""
    result = compute_federal(profile)
    state_income = positive(profile.wages + profile.self_employment_income + state_income_delta)
    state_tax = _state_tax(profile, result, state_income)
    return (
        cents(baseline_federal - result.total_tax),
        cents(baseline_state - state_tax),
    )


# ---------------------------------------------------------------------------
# the strategies
# ---------------------------------------------------------------------------
def build_strategies(
    profile: TaxProfile,
    *,
    as_of: date | None = None,
    existing_401k: Decimal | float | str = 0,
    existing_hsa: Decimal | float | str = 0,
    has_hdhp: bool = False,
    hdhp_family: bool = False,
    has_employer_plan: bool = True,
    self_employed: bool | None = None,
) -> list[Strategy]:
    """Every move worth pricing for this client, best value first."""
    params = federal(profile.tax_year)
    today = as_of or date.today()
    profile = profile.normalised(params)
    base_result = compute_federal(profile)
    base_state = _state_tax(profile, base_result, profile.wages + profile.self_employment_income)
    base_federal = base_result.total_tax
    if self_employed is None:
        self_employed = profile.self_employment_income > ZERO

    limits = params.get("contribution_limits", default={})
    strategies: list[Strategy] = []

    def add(strategy: Strategy) -> None:
        strategy.closes_on = window_closes(strategy.window, profile.tax_year, params)
        strategy.still_available = strategy.closes_on is None or today <= strategy.closes_on
        strategies.append(strategy)

    # --- 1. 401(k) ---------------------------------------------------------
    if has_employer_plan and profile.wages > ZERO:
        base_limit = money(limits.get("elective_deferral_401k", 0))
        catch_up = money(limits.get("catch_up_401k", 0))
        if profile.age in limits.get("super_catch_up_ages", []):
            catch_up = money(limits.get("super_catch_up_401k", catch_up))
        ceiling = base_limit + (catch_up if profile.age >= 50 else ZERO)
        headroom = positive(ceiling - money(existing_401k))
        # Never suggest contributing more than the client actually earns.
        headroom = min(headroom, positive(profile.wages))
        if headroom > ZERO:
            fed, st = _price(base_federal, base_state,
                             _with(profile, wages=profile.wages - headroom),
                             state_income_delta=ZERO)
            add(Strategy(
                id=PlanningStrategy.TRADITIONAL_401K.value,
                label="Increase pre-tax 401(k) contributions",
                category="retirement",
                summary=f"Contribute {headroom:,.0f} more before the year ends.",
                how_it_works=(
                    "A pre-tax deferral comes out of Box 1 wages, so it reduces income "
                    "at the top marginal rate and can also drop AGI below a phase-out. "
                    "It does not reduce Social Security or Medicare wages."
                ),
                window=WINDOW_YEAR_END, amount=headroom, headroom=headroom,
                federal_saving=fed, state_saving=st, cash_required=headroom,
                caveats=[
                    f"The {params.year} limit is {ceiling:,.0f}"
                    + (" including catch-up." if profile.age >= 50 else "."),
                    "Contributions must come out of payroll by 31 December, so the "
                    "practical deadline is the last pay run of the year.",
                ] + ([
                    "Pennsylvania and New Jersey tax 401(k) contributions, so the "
                    "state saving there is nil."
                ] if (profile.resident_state or "").upper() in ("PA", "NJ") else []),
            ))

    # --- 2. HSA ------------------------------------------------------------
    if has_hdhp:
        cap = money(limits.get("hsa_family" if hdhp_family else "hsa_self_only", 0))
        if profile.age >= 55:
            cap += money(limits.get("hsa_catch_up", 0))
        headroom = positive(cap - money(existing_hsa))
        if headroom > ZERO:
            fed, st = _price(base_federal, base_state,
                             _with(profile, hsa_contribution=profile.hsa_contribution + headroom))
            add(Strategy(
                id=PlanningStrategy.HSA.value,
                label="Top up the Health Savings Account",
                category="health",
                summary=f"Contribute {headroom:,.0f} more, deductible even without itemising.",
                how_it_works=(
                    "An HSA is the only account that is deductible going in, grows "
                    "untaxed, and comes out untaxed for medical costs. Contributed "
                    "through payroll it also escapes Social Security and Medicare tax."
                ),
                window=WINDOW_FILING, amount=headroom, headroom=headroom,
                federal_saving=fed, state_saving=st, cash_required=headroom,
                caveats=[
                    "Requires a high-deductible health plan for the months claimed.",
                    "California and New Jersey do not recognise HSAs, so no state saving there.",
                ],
            ))

    # --- 3. Traditional IRA ------------------------------------------------
    ira_cap = money(limits.get("ira", 0)) + (money(limits.get("ira_catch_up", 0)) if profile.age >= 50 else ZERO)
    earned = profile.wages + profile.self_employment_income
    ira_headroom = min(positive(ira_cap - profile.traditional_ira), positive(earned))
    if ira_headroom > ZERO:
        band_key = "covered_joint" if profile.is_married_joint else "covered_single"
        band = limits.get("ira_deduction_phase_out", {}).get(band_key, [])
        deductible = True
        caveats = ["The deadline is the filing deadline, not year end, so this is "
                   "one of the few moves still open after 31 December."]
        if has_employer_plan and band:
            start, end = money(band[0]), money(band[1])
            if base_result.agi >= end:
                deductible = False
                caveats.append(
                    f"Income of {base_result.agi:,.0f} is above the {end:,.0f} limit for a "
                    "deductible IRA while covered by a workplace plan. A non-deductible "
                    "contribution still works as a backdoor Roth."
                )
            elif base_result.agi > start:
                caveats.append(
                    f"The deduction is partly phased out between {start:,.0f} and {end:,.0f}."
                )
        if deductible:
            fed, st = _price(base_federal, base_state,
                             _with(profile, traditional_ira=profile.traditional_ira + ira_headroom))
            add(Strategy(
                id=PlanningStrategy.TRADITIONAL_IRA.value,
                label="Contribute to a traditional IRA",
                category="retirement",
                summary=f"Up to {ira_headroom:,.0f}, and the window is still open after year end.",
                how_it_works=(
                    "A deductible IRA contribution reduces AGI directly, which is more "
                    "valuable than a deduction because AGI gates the credits."
                ),
                window=WINDOW_FILING, amount=ira_headroom, headroom=ira_headroom,
                federal_saving=fed, state_saving=st, cash_required=ira_headroom,
                caveats=caveats,
            ))

    # --- 4. Self-employed retirement --------------------------------------
    if self_employed and profile.self_employment_income > ZERO:
        # A SEP-IRA takes 25% of net self-employment earnings, ~20% of net profit.
        sep = cents(min(profile.self_employment_income * money("0.20"), money(70000)))
        if sep > ZERO:
            fed, st = _price(
                base_federal, base_state,
                _with(profile, other_adjustments=profile.other_adjustments + sep,
                      qbi_income=positive(profile.qbi_income - sep)),
            )
            add(Strategy(
                id="sep_ira",
                label="Open a SEP-IRA or solo 401(k)",
                category="retirement",
                summary=f"Shelter about {sep:,.0f} of self-employment profit.",
                how_it_works=(
                    "A SEP-IRA takes roughly 20% of net self-employment profit and can "
                    "be opened and funded right up to the extended filing deadline -- "
                    "the single largest deduction still available after year end."
                ),
                window=WINDOW_EXTENDED, amount=sep, headroom=sep,
                federal_saving=fed, state_saving=st, cash_required=sep,
                caveats=[
                    "A SEP contribution also reduces qualified business income, so it "
                    "trims the QBI deduction -- the saving shown is already net of that.",
                    "A solo 401(k) usually shelters more at lower profit levels but must "
                    "have been established by 31 December.",
                ],
            ))

    # --- 5. Itemise instead of the standard deduction ---------------------
    if base_result.itemised_deduction > ZERO and base_result.deduction_kind == "standard":
        gap = base_result.standard_deduction - base_result.itemised_deduction
        extra_charity = cents(positive(gap) + money(1000))
        fed, st = _price(
            base_federal, base_state,
            _with(profile, charitable_cash=profile.charitable_cash + extra_charity, force_itemise=True),
        )
        if fed + st > ZERO:
            add(Strategy(
                id=PlanningStrategy.BUNCH_CHARITABLE.value,
                label="Bunch charitable giving into one year",
                category="deductions",
                summary=(
                    f"Itemised deductions are {base_result.itemised_deduction:,.0f} against a "
                    f"{base_result.standard_deduction:,.0f} standard deduction. Giving "
                    f"{extra_charity:,.0f} more this year clears the bar."
                ),
                how_it_works=(
                    "Deductions below the standard deduction are worth nothing. Moving two "
                    "or three years of giving into one year -- a donor-advised fund makes "
                    "this practical -- gets the benefit in that year and the standard "
                    "deduction in the others."
                ),
                window=WINDOW_YEAR_END, amount=extra_charity,
                headroom=extra_charity, federal_saving=fed, state_saving=st,
                cash_required=extra_charity,
                caveats=["Only worth doing if the client was going to give anyway: it "
                         "costs a dollar to save a fraction of one."],
            ))

    # --- 6. Filing status ---------------------------------------------------
    if profile.filing_status in ("married_jointly", "married_separately"):
        alternative = ("married_separately" if profile.filing_status == "married_jointly"
                       else "married_jointly")
        fed, st = _price(base_federal, base_state, _with(profile, filing_status=alternative))
        if fed + st > ZERO:
            add(Strategy(
                id=PlanningStrategy.FILING_STATUS_SWITCH.value,
                label=f"Compare filing {alternative.replace('_', ' ')}",
                category="election",
                summary=f"Filing {alternative.replace('_', ' ')} appears to cost less this year.",
                how_it_works=(
                    "Joint filing wins for most couples, but not all: large medical costs "
                    "or a student-loan repayment tied to income can make separate filing "
                    "cheaper overall. This compares the two on the same figures."
                ),
                window=WINDOW_ELECTION, federal_saving=fed, state_saving=st,
                confidence="medium",
                caveats=[
                    "Filing separately forfeits the earned income credit, the education "
                    "credits, and most of the child and dependent care credit.",
                    "This comparison treats all income as the filer's; a real separate "
                    "return splits income between the spouses, so confirm before electing.",
                ],
            ))

    # --- 7. Dependent care FSA ---------------------------------------------
    if profile.dependent_care_expenses > ZERO and profile.dependent_care_benefits <= ZERO:
        cap = money(limits.get("dependent_care_fsa", 0))
        amount = min(cap, profile.dependent_care_expenses)
        fed, st = _price(
            base_federal, base_state,
            _with(profile, wages=positive(profile.wages - amount),
                  dependent_care_benefits=amount),
        )
        if fed + st > ZERO:
            add(Strategy(
                id=PlanningStrategy.DEPENDENT_CARE_FSA.value,
                label="Use a dependent care FSA",
                category="benefits",
                summary=f"Route {amount:,.0f} of childcare through payroll instead of paying after tax.",
                how_it_works=(
                    "A dependent care FSA escapes income tax AND the 7.65% payroll tax, "
                    "which the childcare credit does not. Above roughly $43,000 of income "
                    "the FSA beats the credit."
                ),
                window=WINDOW_YEAR_END, amount=amount, headroom=amount,
                federal_saving=fed, state_saving=st, cash_required=ZERO,
                caveats=[
                    "FSA money is use-it-or-lose-it within the plan year.",
                    "Every dollar through the FSA reduces the expenses eligible for the "
                    "dependent care credit, which is why the saving shown is the net figure.",
                ],
            ))

    # --- 8. Saver's credit --------------------------------------------------
    savers_bands = params.get("credits", "savers_credit", "agi_bands", default={})
    key = ("married_jointly" if profile.is_married_joint
           else "head_of_household" if profile.filing_status == "head_of_household" else "single")
    band = savers_bands.get(key) or []
    if band and base_result.agi <= money(band[-1]) and profile.retirement_contributions_for_savers <= ZERO:
        cap = money(params.get("credits", "savers_credit", "contribution_cap", default=2000))
        contribution = min(cap, positive(earned))
        if contribution > ZERO:
            fed, st = _price(
                base_federal, base_state,
                _with(profile,
                      traditional_ira=profile.traditional_ira + contribution,
                      retirement_contributions_for_savers=contribution),
            )
            if fed > ZERO:
                add(Strategy(
                    id=PlanningStrategy.SAVERS_CREDIT.value,
                    label="Claim the Saver's Credit",
                    category="credits",
                    summary=f"Income is inside the Saver's Credit range; {contribution:,.0f} of "
                            "retirement saving earns a credit on top of the deduction.",
                    how_it_works=(
                        "The Saver's Credit pays 10-50% of the first $2,000 contributed to a "
                        "retirement account, on top of any deduction. It is the highest-return "
                        "move available at low and middle incomes and is missed constantly."
                    ),
                    window=WINDOW_FILING, amount=contribution, headroom=contribution,
                    federal_saving=fed, state_saving=st, cash_required=contribution,
                    caveats=["It is non-refundable: it can only reduce tax to zero.",
                             "Full-time students and anyone claimed as a dependent are excluded."],
                ))

    # --- 9. Capital loss harvesting ----------------------------------------
    gains = profile.short_term_gains + profile.long_term_gains
    if gains > ZERO:
        offset = min(gains, money(3000) + gains)
        fed, st = _price(
            base_federal, base_state,
            _with(profile,
                  short_term_gains=positive(profile.short_term_gains - min(profile.short_term_gains, offset)),
                  long_term_gains=positive(profile.long_term_gains - positive(offset - profile.short_term_gains))),
        )
        if fed + st > ZERO:
            add(Strategy(
                id=PlanningStrategy.TAX_LOSS_HARVEST.value,
                label="Harvest capital losses against this year's gains",
                category="investments",
                summary=f"Realising losses could offset up to {offset:,.0f} of gain.",
                how_it_works=(
                    "Losses offset gains dollar for dollar, and up to $3,000 of net loss "
                    "offsets ordinary income. Short-term gains are worth offsetting first: "
                    "they are taxed at the full marginal rate."
                ),
                window=WINDOW_YEAR_END, amount=offset, federal_saving=fed, state_saving=st,
                confidence="medium",
                caveats=[
                    "The wash-sale rule disallows the loss if a substantially identical "
                    "security is bought within 30 days either side of the sale.",
                    "Only worth doing where losses genuinely exist in the portfolio; this "
                    "prices the opportunity, it does not confirm the losses are there.",
                ],
            ))

    # --- 10. Withholding for next year --------------------------------------
    if base_result.balance > money(1000):
        add(Strategy(
            id=PlanningStrategy.WITHHOLDING_TUNE.value,
            label="Fix withholding for next year",
            category="cashflow",
            summary=f"This year ends with {base_result.balance:,.0f} owed. A new Form W-4 avoids "
                    "a repeat, and the underpayment penalty with it.",
            how_it_works=(
                "The penalty for underpaying is charged as if it were interest, quarter by "
                "quarter. Withholding is treated as paid evenly through the year no matter "
                "when it happened, so a W-4 change late in the year can still repair an "
                "earlier shortfall -- an estimated payment cannot."
            ),
            window=WINDOW_ELECTION, federal_saving=ZERO, state_saving=ZERO,
            actionable=False, confidence="high",
            caveats=["This avoids a penalty rather than reducing tax, so it shows no saving."],
        ))
    elif base_result.refund > money(5000):
        add(Strategy(
            id=PlanningStrategy.WITHHOLDING_TUNE.value,
            label="Reduce over-withholding",
            category="cashflow",
            summary=f"A refund of {base_result.refund:,.0f} is an interest-free loan to the IRS "
                    f"of about {base_result.refund / 12:,.0f} a month.",
            how_it_works=(
                "A large refund means too much was withheld. Adjusting the W-4 moves that "
                "money into each pay packet instead of waiting a year for it."
            ),
            window=WINDOW_ELECTION, federal_saving=ZERO, state_saving=ZERO,
            actionable=False,
            caveats=["Some clients prefer a refund as forced saving. This is a cashflow "
                     "choice, not a tax saving."],
        ))

    # --- 11. Residency -------------------------------------------------------
    from taxos.config import states as _states
    code = (profile.resident_state or "").upper()
    if code and base_state > money(2000):
        try:
            jurisdiction = _states(profile.tax_year).get(code)
            if jurisdiction["type"] != "none":
                add(Strategy(
                    id=PlanningStrategy.RESIDENCY.value,
                    label="State residency is costing real money",
                    category="residency",
                    summary=f"{jurisdiction['name']} takes {base_state:,.0f} this year. "
                            "Nine states take nothing.",
                    how_it_works=(
                        "Alaska, Florida, Nevada, New Hampshire, South Dakota, Tennessee, "
                        "Texas, Washington and Wyoming levy no income tax on wages. For a "
                        "remote worker this is worth pricing -- but residency is a question "
                        "of domicile and day count, not of a mailing address."
                    ),
                    window=WINDOW_ELECTION, federal_saving=ZERO, state_saving=base_state,
                    actionable=False, confidence="low",
                    caveats=[
                        "This is the full-year figure, shown for scale. It is not a saving "
                        "available by election.",
                        "High-tax states audit departure aggressively; a genuine move means "
                        "moving, and the former state can claim tax on days still worked there.",
                    ],
                ))
        except LookupError:
            pass

    # Still-open actions first, biggest saving first; informational entries
    # last whatever their headline number.
    strategies.sort(
        key=lambda s: (not s.still_available, not s.actionable, -s.total_saving, -s.return_on_cash)
    )
    return strategies


def apply_strategies(profile: TaxProfile, strategies: list[Strategy], chosen: list[str]) -> TaxProfile:
    """Fold the selected moves into a profile so the optimised return can be run.

    Only the moves that actually change an input are folded in; an election such
    as tuning withholding changes no figure on this year's return.
    """
    selected = {s.id: s for s in strategies if s.id in set(chosen)}
    plan = copy.deepcopy(profile)
    for key, strategy in selected.items():
        if key == PlanningStrategy.TRADITIONAL_401K.value:
            plan.wages = positive(plan.wages - strategy.amount)
        elif key == PlanningStrategy.HSA.value:
            plan.hsa_contribution += strategy.amount
        elif key == PlanningStrategy.TRADITIONAL_IRA.value:
            plan.traditional_ira += strategy.amount
        elif key == "sep_ira":
            plan.other_adjustments += strategy.amount
            plan.qbi_income = positive(plan.qbi_income - strategy.amount)
        elif key == PlanningStrategy.BUNCH_CHARITABLE.value:
            plan.charitable_cash += strategy.amount
            plan.force_itemise = True
        elif key == PlanningStrategy.DEPENDENT_CARE_FSA.value:
            plan.wages = positive(plan.wages - strategy.amount)
            plan.dependent_care_benefits += strategy.amount
        elif key == PlanningStrategy.SAVERS_CREDIT.value:
            plan.traditional_ira += strategy.amount
            plan.retirement_contributions_for_savers += strategy.amount
        elif key == PlanningStrategy.FILING_STATUS_SWITCH.value:
            plan.filing_status = (
                "married_separately" if plan.filing_status == "married_jointly"
                else "married_jointly"
            )
        elif key == PlanningStrategy.TAX_LOSS_HARVEST.value:
            take = min(plan.short_term_gains, strategy.amount)
            plan.short_term_gains = positive(plan.short_term_gains - take)
            plan.long_term_gains = positive(plan.long_term_gains - positive(strategy.amount - take))
    return plan
