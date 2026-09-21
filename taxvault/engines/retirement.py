"""401(k), 403(b) and IRA rules: what goes in, what comes out, what it costs.

A retirement account is a different animal from a brokerage account and the
difference is the whole point of it. Inside the account, nothing is a taxable
event: dividends are not reported, a sale produces no 1099-B, and a position
held eleven months is no worse than one held eleven years. None of it reaches
Schedule D. The tax arrives once, at the door -- going in for a Roth, coming
out for a traditional.

That trade has a sting that clients consistently miss: a traditional 401(k)
converts everything inside it to ORDINARY income on the way out. Growth that
would have been a long-term capital gain at 15% in a brokerage account comes
out of a 401(k) at your marginal rate, which can be 37%. The account is still
usually the right choice -- decades of untaxed compounding and a deduction now
generally beat it -- but "my 401(k) gets capital gains rates" is wrong, and
expensively so.

This module covers three things, which are three different calculations:

  * **Contributions** -- the limit for the year, who gets a catch-up, and
    whether an excess deferral needs correcting before it is taxed twice.
  * **Distributions** -- what a 1099-R means, when the 10% additional tax
    applies, and which exceptions waive it (the income tax is still due).
  * **Required minimum distributions** -- when they start, how much, and the
    excise tax for missing one.

Every age and rate here is statutory and lives in `shared.yaml`; only the
dollar limits move with inflation, and those live in each year's file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from taxvault.config import FederalParams
from taxvault.money import ZERO, cents, money, positive

#: Codes whose taxable amount is a Roth distribution needing the 5-year test.
_ROTH_CODES = frozenset({"B", "H", "Q", "T", "J"})


# ===========================================================================
# Contributions
# ===========================================================================
@dataclass
class ContributionPlan:
    """What the client is putting into an employer plan this year."""

    age: int = 40
    traditional_401k: Decimal = ZERO       # pre-tax elective deferral
    roth_401k: Decimal = ZERO              # after-tax elective deferral
    employer_contribution: Decimal = ZERO  # match and profit sharing
    compensation: Decimal = ZERO           # plan-year pay
    prior_year_wages: Decimal = ZERO       # decides the Roth catch-up mandate
    plan_kind: str = "401k"                # or 403b, simple


@dataclass
class ContributionCheck:
    elective_limit: Decimal = ZERO         # base, before any catch-up
    catch_up_limit: Decimal = ZERO         # extra allowed for this client's age
    total_elective_limit: Decimal = ZERO
    deferred: Decimal = ZERO
    room_left: Decimal = ZERO
    excess_deferral: Decimal = ZERO
    annual_additions: Decimal = ZERO
    annual_additions_limit: Decimal = ZERO
    excess_additions: Decimal = ZERO
    pre_tax_deduction: Decimal = ZERO      # what actually reduces this year's income
    catch_up_must_be_roth: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def check_contributions(plan: ContributionPlan, params: FederalParams) -> ContributionCheck:
    """The elective deferral limit for this client, and whether they passed it."""
    check = ContributionCheck()
    limits = "contribution_limits"

    if plan.plan_kind == "simple":
        base = params.amount(limits, "simple_deferral")
        catch_up_base = params.amount(limits, "simple_catch_up")
    else:
        base = params.amount(limits, "elective_deferral_401k")
        catch_up_base = params.amount(limits, "catch_up_401k")

    check.elective_limit = base

    # Catch-up. SECURE 2.0 gives 60-to-63-year-olds a larger one, and it drops
    # back to the ordinary catch-up at 64 -- a cliff clients do not expect.
    super_ages = params.get(limits, "super_catch_up_ages", default=[]) or []
    if plan.age >= 50:
        if plan.plan_kind != "simple" and int(plan.age) in [int(a) for a in super_ages]:
            check.catch_up_limit = params.amount(limits, "super_catch_up_401k")
            check.notes.append(
                f"At {plan.age} you get the higher SECURE 2.0 catch-up of "
                f"{check.catch_up_limit:,.0f} rather than {catch_up_base:,.0f}. It applies "
                "only for ages 60 to 63 and drops back at 64."
            )
        else:
            check.catch_up_limit = catch_up_base
            check.notes.append(
                f"Being 50 or over, you can add a catch-up of {check.catch_up_limit:,.0f} "
                "on top of the standard limit."
            )

    check.total_elective_limit = cents(base + check.catch_up_limit)
    deferred = positive(plan.traditional_401k) + positive(plan.roth_401k)
    check.deferred = cents(deferred)
    check.room_left = cents(positive(check.total_elective_limit - deferred))
    check.excess_deferral = cents(positive(deferred - check.total_elective_limit))

    if check.excess_deferral > ZERO:
        check.warnings.append(
            f"You have deferred {deferred:,.0f} against a limit of "
            f"{check.total_elective_limit:,.0f}. The excess of "
            f"{check.excess_deferral:,.0f} must be taken back out by 15 April next year. "
            "Miss that and it is taxed twice -- once in the year you earned it and again "
            "when it eventually comes out."
        )
    elif check.room_left > ZERO:
        check.notes.append(
            f"You have {check.room_left:,.0f} of room left in the plan this year."
        )

    # The limit is per PERSON across every 401(k) and 403(b) they are in, which
    # only bites on a mid-year job change -- neither payroll department can see
    # the other's deferrals.
    check.notes.append(
        f"The {check.total_elective_limit:,.0f} limit is yours, not each employer's. If you "
        "changed jobs this year, add up the deferrals on every W-2 -- neither payroll "
        "system can see the other."
    )

    # Roth catch-up mandate (SECURE 2.0).
    threshold = params.amount(limits, "roth_catch_up_wage_threshold")
    if (check.catch_up_limit > ZERO and threshold > ZERO
            and positive(plan.prior_year_wages) > threshold):
        check.catch_up_must_be_roth = True
        check.notes.append(
            f"Because last year's wages from this employer passed {threshold:,.0f}, your "
            "catch-up has to go in as Roth. It is still a catch-up -- it just does not "
            "reduce this year's taxable income."
        )

    # IRC 415(c): everything credited to the account, employee and employer,
    # with the catch-up sitting outside the limit.
    additions_cap = params.amount(limits, "annual_additions_401k")
    if additions_cap > ZERO:
        additions = cents(deferred + positive(plan.employer_contribution))
        check.annual_additions = additions
        check.annual_additions_limit = cents(additions_cap + check.catch_up_limit)
        comp = positive(plan.compensation)
        if comp > ZERO:
            check.annual_additions_limit = min(
                check.annual_additions_limit, max(comp, check.total_elective_limit)
            )
        check.excess_additions = cents(positive(additions - check.annual_additions_limit))
        if check.excess_additions > ZERO:
            check.warnings.append(
                f"Employee and employer contributions together come to {additions:,.0f}, "
                f"over the {check.annual_additions_limit:,.0f} annual additions limit. The "
                "plan has to correct this; it is the plan's job, but it is your money."
            )

    # Only the pre-tax side reduces income. This is the figure the calculation
    # actually uses, and it is why a Roth deferral shows no saving this year.
    pre_tax = positive(plan.traditional_401k)
    if check.excess_deferral > ZERO:
        # An excess that is not returned stays taxable, so it buys no deduction.
        pre_tax = positive(pre_tax - check.excess_deferral)
    check.pre_tax_deduction = cents(pre_tax)

    comp_cap = params.amount(limits, "compensation_limit")
    if comp_cap > ZERO and positive(plan.compensation) > comp_cap:
        check.notes.append(
            f"Only the first {comp_cap:,.0f} of pay counts towards an employer match, so a "
            "match stated as a percentage stops growing above that."
        )
    return check


# ===========================================================================
# Distributions
# ===========================================================================
@dataclass
class Distribution:
    """One Form 1099-R."""

    gross: Decimal = ZERO                 # box 1
    taxable: Decimal | None = None        # box 2a; None means "same as gross"
    code: str = "7"                       # box 7
    federal_withheld: Decimal = ZERO      # box 4
    state_withheld: Decimal = ZERO
    is_roth: bool = False                 # designated Roth account
    roth_years: int = 0                   # years since the first Roth contribution
    roth_basis: Decimal = ZERO            # box 5: contributions already taxed
    age_at_distribution: float = 0.0
    rolled_over: Decimal = ZERO           # moved to another plan within 60 days
    penalty_exception: str = ""           # a code from `penalty_exceptions`
    plan_kind: str = "401k"               # 401k, 403b, ira, simple
    payer: str = ""


@dataclass
class DistributionResult:
    gross: Decimal = ZERO
    taxable: Decimal = ZERO
    rolled_over: Decimal = ZERO
    penalty: Decimal = ZERO
    federal_withheld: Decimal = ZERO
    state_withheld: Decimal = ZERO
    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def compute_distributions(
    items: list[Distribution], params: FederalParams, *, agi_for_medical: Decimal = ZERO
) -> DistributionResult:
    """Total up 1099-R income and the 10% additional tax, row by row."""
    result = DistributionResult()
    codes = params.get("retirement_distributions", "distribution_codes", default={}) or {}
    exceptions = {
        row["code"]: row
        for row in (params.get("retirement_distributions", "penalty_exceptions", default=[]) or [])
    }
    penalty_rate = params.rate("retirement_distributions", "early_withdrawal_penalty")
    early_age = float(params.get("retirement_distributions", "early_withdrawal_age", default=59.5))
    roth_years_needed = int(params.get("retirement_distributions", "roth_qualified_years", default=5))

    for item in items:
        gross = positive(item.gross)
        if gross <= ZERO:
            continue
        code = (item.code or "7").strip().upper()[:1]
        rule = codes.get(code, {})
        rolled = min(positive(item.rolled_over), gross)
        label = rule.get("label", "Distribution")
        row_notes: list[str] = []

        taxable = gross if item.taxable is None else positive(item.taxable)
        taxable = min(taxable, gross)

        # A rollover is not income. Direct trustee-to-trustee is code G or H;
        # an indirect one the client did themselves still counts if it landed
        # inside 60 days, which the 1099-R cannot know.
        if not rule.get("taxable", True):
            taxable = ZERO
        if rolled > ZERO:
            taxable = positive(taxable - rolled)
            row_notes.append(
                f"{rolled:,.0f} was rolled into another plan or IRA, so it is not income. "
                "It still has to be reported -- the IRS has the 1099-R either way."
            )

        # A designated Roth account: qualified means five years AND 59 1/2
        # (or death or disability). Short of that, only the earnings are taxed.
        if item.is_roth or code in _ROTH_CODES:
            if item.roth_years >= roth_years_needed and (
                item.age_at_distribution >= early_age or code in ("Q", "H")
            ):
                taxable = ZERO
                row_notes.append(
                    f"Qualified Roth distribution: the account has been open "
                    f"{item.roth_years} years and you are past {early_age}, so none of it "
                    "is taxable -- the growth comes out free."
                )
            else:
                basis = min(positive(item.roth_basis), gross)
                taxable = positive(gross - rolled - basis)
                if item.roth_years < roth_years_needed:
                    row_notes.append(
                        f"The Roth account is {item.roth_years} years old, short of the "
                        f"{roth_years_needed} needed. Your own contributions "
                        f"({basis:,.0f}) still come out tax-free; only the earnings are "
                        "taxed."
                    )

        # The 10% additional tax, on the TAXABLE amount only.
        penalty = ZERO
        if rule.get("penalty") and taxable > ZERO:
            rate = money(rule.get("penalty_rate", penalty_rate))
            exempt_amount = ZERO
            chosen = exceptions.get((item.penalty_exception or "").strip().lower())
            if chosen and _exception_applies(chosen, item.plan_kind):
                cap = chosen.get("cap")
                exempt_amount = taxable if cap is None else min(taxable, money(cap))
                row_notes.append(
                    f"{chosen['label']} waives the {rate:.0%} additional tax"
                    + (f" on the first {money(cap):,.0f}." if cap is not None else ".")
                    + " Income tax is still due on the whole distribution -- the exception "
                    "is only to the penalty."
                )
            elif chosen:
                row_notes.append(
                    f"{chosen['label']} does not apply to a {item.plan_kind} -- that "
                    "exception is for a different kind of account."
                )
            penalty = cents(positive(taxable - exempt_amount) * rate)
            if penalty > ZERO:
                result.warnings.append(
                    f"{label} of {gross:,.0f} carries a {rate:.0%} additional tax of "
                    f"{penalty:,.0f} on top of the income tax, because you are under "
                    f"{early_age}."
                )

        result.gross += gross
        result.taxable += taxable
        result.rolled_over += rolled
        result.penalty += penalty
        result.federal_withheld += positive(item.federal_withheld)
        result.state_withheld += positive(item.state_withheld)
        result.rows.append({
            "payer": item.payer,
            "code": code,
            "label": label,
            "gross": str(cents(gross)),
            "taxable": str(cents(taxable)),
            "penalty": str(cents(penalty)),
            "withheld": str(cents(positive(item.federal_withheld))),
            "notes": row_notes,
        })
        result.notes.extend(row_notes)

    for key in ("gross", "taxable", "rolled_over", "penalty", "federal_withheld", "state_withheld"):
        setattr(result, key, cents(getattr(result, key)))

    if result.taxable > ZERO:
        result.notes.append(
            "A traditional plan distribution is ordinary income at your marginal rate. It "
            "does not get capital gains treatment, however long the money was invested."
        )
    if result.federal_withheld > ZERO:
        result.notes.append(
            f"{result.federal_withheld:,.0f} was withheld and counts as tax already paid. "
            "The default 20% on a plan distribution is often short of the real rate, so "
            "check the balance below before spending it."
        )
    return result


def _exception_applies(exception: dict[str, Any], plan_kind: str) -> bool:
    plans = [str(p).lower() for p in exception.get("plans", ["all"])]
    return "all" in plans or (plan_kind or "").lower() in plans


def penalty_exceptions(params: FederalParams, plan_kind: str = "") -> list[dict[str, Any]]:
    """The exceptions a client can pick from, for the UI."""
    rows = params.get("retirement_distributions", "penalty_exceptions", default=[]) or []
    out = []
    for row in rows:
        if plan_kind and not _exception_applies(row, plan_kind):
            continue
        entry = {"code": row["code"], "label": row["label"],
                 "plans": row.get("plans", ["all"])}
        if row.get("cap") is not None:
            entry["cap"] = str(money(row["cap"]))
        out.append(entry)
    return out


# ===========================================================================
# Required minimum distributions
# ===========================================================================
@dataclass
class RmdResult:
    required: bool = False
    age_required: int = 0
    age_this_year: int = 0
    divisor: Decimal = ZERO
    amount: Decimal = ZERO
    taken: Decimal = ZERO
    shortfall: Decimal = ZERO
    excise: Decimal = ZERO
    excise_if_corrected: Decimal = ZERO
    first_year: bool = False
    deadline: str = ""
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in asdict(self).items()
        }


def required_beginning_age(birth_year: int, params: FederalParams) -> int:
    """73, or 75 for anyone born in 1960 or later. SECURE 2.0 moved it twice."""
    cutoff = int(params.get("retirement_distributions", "rmd_age_birth_year_cutoff", default=1960))
    if birth_year and birth_year >= cutoff:
        return int(params.get("retirement_distributions", "rmd_age_born_1960_or_later", default=75))
    return int(params.get("retirement_distributions", "rmd_age", default=73))


def compute_rmd(
    params: FederalParams,
    *,
    birth_year: int,
    prior_year_balance: Decimal = ZERO,
    taken: Decimal = ZERO,
    is_roth_401k: bool = False,
    still_working_for_plan_sponsor: bool = False,
    owns_five_percent: bool = False,
) -> RmdResult:
    """Whether an RMD is due this year, how much, and the cost of missing it."""
    result = RmdResult()
    tax_year = params.year
    if not birth_year:
        return result

    age = tax_year - birth_year
    result.age_this_year = age
    result.age_required = required_beginning_age(birth_year, params)

    if is_roth_401k and not params.get("retirement_distributions", "roth_401k_lifetime_rmd",
                                       default=False):
        result.notes.append(
            "A Roth 401(k) has no required minimum distribution during your lifetime -- "
            "SECURE 2.0 removed it from 2024. You can leave it alone."
        )
        return result

    if age < result.age_required:
        result.notes.append(
            f"Required minimum distributions start at {result.age_required} for you, which "
            f"is {result.age_required - age} years away."
        )
        return result

    if still_working_for_plan_sponsor and not owns_five_percent:
        result.notes.append(
            "You are still working for the employer whose plan this is and do not own 5% "
            "of the business, so this plan's RMD waits until you retire. It does NOT "
            "cover an IRA or an old employer's plan -- those still have to be taken."
        )
        return result

    table = params.get("retirement_distributions", "uniform_lifetime_table", default={}) or {}
    divisor = table.get(age) or table.get(str(age))
    if divisor is None:
        # Past the end of the published table, the last divisor is the floor.
        keys = sorted(int(k) for k in table)
        divisor = table[keys[-1]] if keys else None
    if not divisor:
        return result

    result.required = True
    result.divisor = money(str(divisor))
    balance = positive(prior_year_balance)
    result.amount = cents(balance / result.divisor) if result.divisor else ZERO
    result.taken = cents(positive(taken))
    result.shortfall = cents(positive(result.amount - result.taken))
    result.first_year = age == result.age_required

    if result.first_year:
        result.deadline = str(
            params.get("retirement_distributions", "first_rmd_deadline", default="")
        )
        result.notes.append(
            f"This is your first required year. You may delay this one until "
            f"{result.deadline} -- but then you take two in the same tax year, "
            "and two distributions stacked into one year can push you into a higher "
            "bracket, raise the tax on your Social Security, and lift your Medicare "
            "premium two years later."
        )
    else:
        result.deadline = f"31 December {tax_year}"

    result.notes.append(
        f"Your required minimum is {result.amount:,.0f}: the {balance:,.0f} balance at the "
        f"end of last year divided by {result.divisor} from the IRS Uniform Lifetime Table."
    )

    if result.shortfall > ZERO:
        rate = params.rate("retirement_distributions", "rmd_excise_rate")
        corrected = params.rate("retirement_distributions", "rmd_excise_rate_if_corrected")
        window = int(params.get("retirement_distributions", "rmd_correction_window_years",
                                default=2))
        result.excise = cents(result.shortfall * rate)
        result.excise_if_corrected = cents(result.shortfall * corrected)
        result.warnings.append(
            f"You are {result.shortfall:,.0f} short of the required minimum. That is a "
            f"{rate:.0%} excise tax of {result.excise:,.0f}, reported on Form 5329 -- but "
            f"it drops to {corrected:.0%} ({result.excise_if_corrected:,.0f}) if you take "
            f"the missed amount within {window} years and file the form. Take it now."
        )
    return result
