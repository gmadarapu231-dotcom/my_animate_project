"""401(k) rules: limits going in, tax coming out, and RMDs."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.config import federal
from taxvault.engines.federal import TaxProfile, compute_federal
from taxvault.engines.retirement import (
    ContributionPlan,
    Distribution,
    check_contributions,
    compute_distributions,
    compute_rmd,
    penalty_exceptions,
    required_beginning_age,
)


def D(value):
    return Decimal(str(value))


@pytest.fixture
def p2026():
    return federal(2026)


# ---------------------------------------------------------------------------
# contributions
# ---------------------------------------------------------------------------
def test_the_2026_elective_deferral_limit(p2026):
    check = check_contributions(ContributionPlan(age=40, traditional_401k=D(10000)), p2026)
    assert check.total_elective_limit == D("24500.00")
    assert check.room_left == D("14500.00")


def test_fifty_and_over_get_the_catch_up(p2026):
    check = check_contributions(ContributionPlan(age=52), p2026)
    assert check.catch_up_limit == D("8000.00")
    assert check.total_elective_limit == D("32500.00")


def test_sixty_to_sixty_three_get_the_larger_catch_up(p2026):
    """SECURE 2.0: $11,250 instead of $8,000, for four years only."""
    check = check_contributions(ContributionPlan(age=61), p2026)
    assert check.catch_up_limit == D("11250.00")
    assert check.total_elective_limit == D("35750.00")


def test_the_larger_catch_up_stops_at_sixty_four(p2026):
    """A real cliff: the limit falls by $3,250 on a birthday."""
    at_63 = check_contributions(ContributionPlan(age=63), p2026).total_elective_limit
    at_64 = check_contributions(ContributionPlan(age=64), p2026).total_elective_limit
    assert at_63 - at_64 == D("3250.00")


def test_an_excess_deferral_is_flagged_with_the_correction_deadline(p2026):
    check = check_contributions(ContributionPlan(age=35, traditional_401k=D(28000)), p2026)
    assert check.excess_deferral == D("3500.00")
    assert any("15 April" in w for w in check.warnings)


def test_high_earners_must_make_the_catch_up_roth(p2026):
    check = check_contributions(
        ContributionPlan(age=55, traditional_401k=D(30000), prior_year_wages=D(200000)), p2026
    )
    assert check.catch_up_must_be_roth is True


def test_a_modest_earner_keeps_a_pre_tax_catch_up(p2026):
    check = check_contributions(
        ContributionPlan(age=55, traditional_401k=D(30000), prior_year_wages=D(90000)), p2026
    )
    assert check.catch_up_must_be_roth is False


def test_only_the_pre_tax_side_reduces_this_year_s_income(p2026):
    check = check_contributions(
        ContributionPlan(age=40, traditional_401k=D(9000), roth_401k=D(6000)), p2026
    )
    assert check.deferred == D("15000.00")
    assert check.pre_tax_deduction == D("9000.00")


def test_employee_and_employer_together_hit_the_415c_limit(p2026):
    check = check_contributions(
        ContributionPlan(age=40, traditional_401k=D(24500), employer_contribution=D(60000),
                         compensation=D(300000)),
        p2026,
    )
    assert check.annual_additions == D("84500.00")
    assert check.excess_additions > D(0)


def test_a_deferral_is_not_deducted_twice():
    """A pre-tax deferral is already out of W-2 box 1.

    Attaching the plan detail must not move AGI -- if it did, the client would
    get the deduction twice.
    """
    without = compute_federal(TaxProfile(tax_year=2026, wages=D(100000)))
    with_plan = compute_federal(TaxProfile(
        tax_year=2026, wages=D(100000),
        retirement_plan=ContributionPlan(age=40, traditional_401k=D(20000)),
    ))
    assert without.agi == with_plan.agi
    assert without.total_tax == with_plan.total_tax


# ---------------------------------------------------------------------------
# distributions
# ---------------------------------------------------------------------------
def test_an_early_distribution_carries_ten_percent(p2026):
    result = compute_distributions(
        [Distribution(gross=D(20000), code="1", age_at_distribution=45)], p2026
    )
    assert result.taxable == D("20000.00")
    assert result.penalty == D("2000.00")


def test_a_normal_distribution_carries_no_penalty(p2026):
    result = compute_distributions(
        [Distribution(gross=D(40000), code="7", age_at_distribution=66)], p2026
    )
    assert result.taxable == D("40000.00")
    assert result.penalty == D("0")


def test_a_direct_rollover_is_not_income(p2026):
    result = compute_distributions(
        [Distribution(gross=D(150000), code="G", age_at_distribution=48)], p2026
    )
    assert result.taxable == D("0")
    assert result.penalty == D("0")


def test_a_capped_exception_only_waives_the_penalty_up_to_the_cap(p2026):
    """Birth or adoption: $5,000 is excepted, the rest is not."""
    result = compute_distributions(
        [Distribution(gross=D(20000), code="1", age_at_distribution=33,
                      penalty_exception="birth_or_adoption")],
        p2026,
    )
    assert result.penalty == D("1500.00")   # 10% of the 15,000 over the cap
    assert result.taxable == D("20000.00")  # income tax still due on all of it


def test_an_ira_only_exception_does_not_apply_to_a_401k(p2026):
    result = compute_distributions(
        [Distribution(gross=D(10000), code="1", age_at_distribution=40, plan_kind="401k",
                      penalty_exception="first_home")],
        p2026,
    )
    assert result.penalty == D("1000.00")
    assert any("different kind of account" in n for n in result.notes)


def test_a_qualified_roth_distribution_is_tax_free(p2026):
    result = compute_distributions(
        [Distribution(gross=D(50000), code="Q", is_roth=True, roth_years=9,
                      age_at_distribution=63)],
        p2026,
    )
    assert result.taxable == D("0")


def test_an_unqualified_roth_distribution_taxes_only_the_earnings(p2026):
    """Three years in: contributions come out free, growth does not."""
    result = compute_distributions(
        [Distribution(gross=D(30000), code="B", is_roth=True, roth_years=3,
                      roth_basis=D(22000), age_at_distribution=61)],
        p2026,
    )
    assert result.taxable == D("8000.00")


def test_a_simple_within_two_years_is_penalised_at_twenty_five_percent(p2026):
    result = compute_distributions(
        [Distribution(gross=D(10000), code="S", age_at_distribution=40, plan_kind="simple")],
        p2026,
    )
    assert result.penalty == D("2500.00")


def test_the_penalty_reaches_total_tax_and_the_withholding_reaches_payments():
    result = compute_federal(TaxProfile(
        tax_year=2026, wages=D(60000), federal_withheld=D(6000),
        distributions=[Distribution(gross=D(25000), code="1", age_at_distribution=44,
                                    federal_withheld=D(5000))],
    ))
    assert result.early_withdrawal_penalty == D("2500.00")
    assert result.total_payments >= D("11000.00")


def test_penalty_exceptions_can_be_filtered_by_plan(p2026):
    ira_only = {e["code"] for e in penalty_exceptions(p2026, "ira")}
    plan_only = {e["code"] for e in penalty_exceptions(p2026, "401k")}
    assert "first_home" in ira_only and "first_home" not in plan_only
    assert "rule_of_55" in plan_only and "rule_of_55" not in ira_only


# ---------------------------------------------------------------------------
# required minimum distributions
# ---------------------------------------------------------------------------
def test_the_required_age_depends_on_birth_year(p2026):
    assert required_beginning_age(1955, p2026) == 73
    assert required_beginning_age(1960, p2026) == 75
    assert required_beginning_age(1975, p2026) == 75


def test_the_rmd_is_the_balance_over_the_table_divisor(p2026):
    """Age 73 in 2026, $500,000 at last year end: 500,000 / 26.5."""
    result = compute_rmd(p2026, birth_year=1953, prior_year_balance=D(500000))
    assert result.required is True
    assert result.divisor == D("26.5")
    assert result.amount == D("18867.92")


def test_the_first_year_warns_about_stacking_two_into_one(p2026):
    result = compute_rmd(p2026, birth_year=1953, prior_year_balance=D(500000))
    assert result.first_year is True
    assert any("two in the same tax year" in n for n in result.notes)


def test_a_missed_rmd_is_a_twenty_five_percent_excise_reducible_to_ten(p2026):
    result = compute_rmd(p2026, birth_year=1950, prior_year_balance=D(400000), taken=D(0))
    assert result.shortfall == result.amount
    assert result.excise == (result.amount * D("0.25")).quantize(D("0.01"))
    assert result.excise_if_corrected == (result.amount * D("0.10")).quantize(D("0.01"))


def test_a_roth_401k_has_no_lifetime_rmd(p2026):
    result = compute_rmd(p2026, birth_year=1945, prior_year_balance=D(900000),
                         is_roth_401k=True)
    assert result.required is False
    assert any("no required minimum" in n for n in result.notes)


def test_still_working_defers_that_plan_s_rmd_but_says_so(p2026):
    result = compute_rmd(p2026, birth_year=1950, prior_year_balance=D(300000),
                         still_working_for_plan_sponsor=True)
    assert result.required is False
    assert any("does NOT cover an IRA" in n for n in result.notes)


def test_a_five_percent_owner_cannot_use_the_still_working_exception(p2026):
    result = compute_rmd(p2026, birth_year=1950, prior_year_balance=D(300000),
                         still_working_for_plan_sponsor=True, owns_five_percent=True)
    assert result.required is True
