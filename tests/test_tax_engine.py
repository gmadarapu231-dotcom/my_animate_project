"""The arithmetic, pinned to figures computed by hand.

Expected values here were worked out from the published rate schedules rather
than captured from a previous run of this code. A test that records whatever
the code did today only proves the code has not changed; these prove it is
right.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from taxos.engines.federal import TaxProfile, compute_federal
from taxos.engines.state import compute_state, compute_states
from taxos.money import brackets_from, money, phase_out, tax_on


def D(value):
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# bracket arithmetic
# ---------------------------------------------------------------------------
def test_progressive_tax_taxes_only_the_slice_in_each_band():
    schedule = brackets_from([{"up_to": 100, "rate": 10}, {"up_to": 200, "rate": 20}, {"rate": 30}])
    assert tax_on(50, schedule) == D("5.00")     # 50 @ 10%
    assert tax_on(150, schedule) == D("20.00")   # 10 + 50 @ 20%
    assert tax_on(300, schedule) == D("60.00")   # 10 + 20 + 100 @ 30%


def test_a_schedule_must_end_unbounded():
    with pytest.raises(ValueError):
        brackets_from([{"up_to": 100, "rate": 10}, {"up_to": 200, "rate": 20}])


def test_stepped_phase_out_counts_a_partial_step_as_a_whole_one():
    # $50 per $1,000 "or fraction thereof": $200 over is still a full step.
    assert phase_out(2000, 200200, 200000, rate=5, step=1000) == D("1950.00")


# ---------------------------------------------------------------------------
# federal
# ---------------------------------------------------------------------------
def test_single_wage_earner_2025():
    """$95,000 wages, $15,750 standard deduction -> $79,250 taxable.

    11,925 @ 10%  =  1,192.50
    36,550 @ 12%  =  4,386.00
    30,775 @ 22%  =  6,770.50
                    ---------
                    12,349.00
    """
    result = compute_federal(TaxProfile(tax_year=2025, wages=95000, federal_withheld=11800))
    assert result.standard_deduction == D("15750.00")
    assert result.taxable_income == D("79250.00")
    assert result.total_tax == D("12349.00")
    assert result.balance == D("549.00")
    assert result.marginal_rate == D("0.22")


def test_long_term_gain_is_stacked_on_top_of_wages_not_taxed_alone():
    """$40k wages + $40k gain. The wages fill the 0% band first.

    Ordinary taxable 24,250 -> 1,192.50 + 1,479.00 = 2,671.50
    Gain: 24,100 fits under the 48,350 breakpoint at 0%; 15,900 at 15% = 2,385.
    """
    result = compute_federal(
        TaxProfile(tax_year=2025, wages=40000, long_term_gains=40000)
    )
    assert result.ordinary_tax == D("2671.50")
    assert result.preferential_tax == D("2385.00")


def test_a_gain_alone_under_the_breakpoint_is_untaxed():
    result = compute_federal(TaxProfile(tax_year=2025, long_term_gains=60000))
    assert result.taxable_income == D("44250.00")
    assert result.total_tax == D("0.00")


def test_child_tax_credit_is_2200_in_2025_and_2000_in_2024():
    for year, expected in ((2025, D("4400.00")), (2024, D("4000.00"))):
        result = compute_federal(TaxProfile(
            tax_year=year, filing_status="married_jointly", wages=145000,
            children_under_17=2,
        ))
        assert result.credits_detail["Child tax credit"] == str(expected), year


def test_child_tax_credit_phases_out_above_the_threshold():
    result = compute_federal(TaxProfile(
        tax_year=2025, filing_status="married_jointly", wages=430000,
        children_under_17=2, social_security_wages=176100,
    ))
    # 30,000 over the 400,000 threshold = 30 steps x $50 = $1,500 off $4,400.
    assert result.credits_detail["Child tax credit"] == "2900.00"


def test_refundable_child_credit_pays_out_beyond_zero_tax():
    result = compute_federal(TaxProfile(
        tax_year=2025, filing_status="head_of_household", wages=28000,
        federal_withheld=900, children_under_17=2, age=32,
    ))
    assert result.total_tax == D("0.00")
    # ACTC caps at $1,700 per child.
    assert result.credits_detail["Additional child tax credit"] == "3400.00"
    assert result.refund > D("10000")


def test_self_employment_tax_and_its_deductible_half():
    """$120,000 profit: 92.35% of it is the SE base, taxed at 15.3%."""
    result = compute_federal(TaxProfile(tax_year=2025, self_employment_income=120000))
    assert result.self_employment_tax == D("16955.46")
    # Half of the SE tax comes off income, so AGI is 120,000 - 8,477.73.
    assert result.agi == D("111522.27")


def test_wages_consume_the_social_security_base_before_self_employment_income():
    """Someone already at the wage base owes only Medicare on side income."""
    result = compute_federal(TaxProfile(
        tax_year=2025, wages=200000, social_security_wages=176100,
        self_employment_income=20000,
    ))
    base = D("20000") * D("0.9235")
    assert result.self_employment_tax == (base * D("0.029")).quantize(D("0.01"))


def test_niit_and_additional_medicare_apply_above_the_thresholds():
    result = compute_federal(TaxProfile(
        tax_year=2025, filing_status="married_jointly", wages=600000,
        social_security_wages=176100, taxable_interest=20000, long_term_gains=60000,
        ordinary_dividends=20000, qualified_dividends=15000,
    ))
    # 3.8% of the lesser of investment income (100,000) and MAGI over 250,000.
    assert result.net_investment_income_tax == D("3800.00")
    # 0.9% on wages above 250,000.
    assert result.additional_medicare_tax == D("3150.00")


def test_salt_cap_phases_down_at_high_income_but_never_below_the_floor():
    result = compute_federal(TaxProfile(
        tax_year=2025, filing_status="married_jointly", wages=700000,
        state_local_income_tax=45000, property_tax=18000, mortgage_interest=28000,
        charitable_cash=20000, force_itemise=True,
    ))
    # 40,000 cap less 30% of the 200,000 over the threshold = floor of 10,000.
    assert result.itemised_deduction == D("58000.00")  # 10k SALT + 28k + 20k


def test_salt_cap_was_only_10000_in_2024():
    result = compute_federal(TaxProfile(
        tax_year=2024, filing_status="married_jointly", wages=250000,
        state_local_income_tax=25000, property_tax=12000, mortgage_interest=20000,
        force_itemise=True,
    ))
    assert result.itemised_deduction == D("30000.00")  # 10k capped + 20k


def test_social_security_benefits_are_taxed_on_provisional_income():
    result = compute_federal(TaxProfile(
        tax_year=2025, filing_status="married_jointly", age=68, spouse_age=67,
        social_security_benefits=40000, retirement_distributions=55000,
    ))
    # Provisional 75,000 -> 6,000 + 85% of 31,000 = 32,350 taxable of 40,000.
    assert result.agi == D("87350.00")
    # Standard 31,500 + two 65+ additions of 1,600 + two senior deductions.
    assert result.senior_deduction == D("12000.00")
    assert result.deduction_taken == D("46700.00")


def test_low_benefits_alone_are_not_taxable_at_all():
    result = compute_federal(TaxProfile(tax_year=2025, social_security_benefits=20000))
    assert result.agi == D("0.00")
    assert result.total_tax == D("0.00")


def test_obbba_tips_and_overtime_are_deductible_without_itemising():
    result = compute_federal(TaxProfile(
        tax_year=2025, wages=52000, tips=22000, overtime_premium=6000,
    ))
    assert result.obbba_deductions == D("28000.00")
    assert result.deduction_kind == "standard"
    assert result.taxable_income == D("8250.00")


def test_those_deductions_did_not_exist_in_2024():
    result = compute_federal(TaxProfile(
        tax_year=2024, wages=52000, tips=22000, overtime_premium=6000,
    ))
    assert result.obbba_deductions == D("0.00")


def test_a_dependent_s_standard_deduction_is_capped_by_earned_income():
    result = compute_federal(TaxProfile(
        tax_year=2025, wages=3000, can_be_claimed_by_another=True, age=19,
    ))
    assert result.standard_deduction == D("3450.00")  # 3,000 earned + 450


def test_qbi_deduction_is_limited_by_taxable_income():
    result = compute_federal(TaxProfile(
        tax_year=2025, self_employment_income=120000, qbi_income=120000,
    ))
    # 20% of QBI is 24,000, but 20% of taxable income before QBI is less.
    assert result.qbi_deduction == D("19154.45")


def test_a_service_business_loses_qbi_above_the_phase_out():
    result = compute_federal(TaxProfile(
        tax_year=2025, wages=400000, self_employment_income=0, qbi_income=100000,
        is_specified_service_business=True,
    ))
    assert result.qbi_deduction == D("0.00")
    assert any("specified service business" in n for n in result.notes)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["AK", "FL", "NH", "NV", "SD", "TN", "TX", "WA", "WY"])
def test_no_tax_states_charge_nothing_on_wages(code):
    result = compute_state(code, state_income=250000, filing_status="single")
    assert result.kind == "none"
    assert result.tax == D("0")
    assert result.notes


def test_washington_still_taxes_a_large_long_term_gain():
    result = compute_state("WA", state_income=95000, long_term_gains=500000)
    # 7% of the gain above the 276,000 allowance.
    assert result.tax == D("15680.00")


def test_withholding_to_a_no_tax_state_is_flagged_as_recoverable():
    result = compute_state("TX", state_income=95000, withheld=1200)
    assert result.refund == D("1200.00")
    assert any("levies no" in n for n in result.notes)


def test_a_flat_state_is_rate_times_taxable_income():
    result = compute_state("PA", state_income=100000, filing_status="single",
                           include_local=False)
    assert result.tax == D("3070.00")


def test_colorado_starts_from_federal_taxable_income():
    result = compute_state("CO", state_income=95000, federal_taxable_income=79250)
    assert result.taxable_income == D("79250.00")
    assert result.tax == D("3487.00")


def test_massachusetts_adds_the_millionaires_surtax():
    result = compute_state("MA", state_income=1200000, filing_status="single")
    assert result.tax == D("59780.00")
    assert result.surtax == D("4498.00")


def test_joint_brackets_double_in_california_but_not_in_virginia():
    from taxos.config import states

    params = states(2025)
    assert params.brackets("CA", "married_jointly")[0].ceiling == D("21512")  # 10,756 x 2
    assert params.brackets("VA", "married_jointly")[0].ceiling == D("3000")   # unchanged


def test_new_york_uses_its_own_joint_table():
    from taxos.config import states

    assert states(2025).brackets("NY", "married_jointly")[0].ceiling == D("17150")


def test_reciprocity_refunds_the_work_state_in_full(): 
    results = compute_states(
        [{"state": "PA", "wages": 103000, "withheld": 3162}],
        resident_state="NJ", filing_status="single", resident_income=103000,
    )
    work = next(r for r in results if r.code == "PA")
    assert work.tax == D("0")
    assert work.refund == D("3162.00")
    assert any("reciprocity" in n for n in work.notes)


def test_the_home_state_credits_tax_paid_to_a_work_state():
    results = compute_states(
        [{"state": "NJ", "wages": 60000, "withheld": 2000},
         {"state": "NY", "wages": 40000, "withheld": 2200}],
        resident_state="NY", filing_status="single", resident_income=100000,
    )
    home = next(r for r in results if r.code == "NY" and r.is_resident)
    work = next(r for r in results if r.code == "NJ")
    assert work.total_tax > D("0")
    assert home.credits >= work.total_tax
    assert any("other states" in n for n in home.notes)


def test_every_jurisdiction_computes_without_error():
    """51 jurisdictions, one income. Catches a typo in any rate table."""
    from taxos.config import states

    for code in states(2025).codes():
        result = compute_state(code, state_income=85000, filing_status="single",
                               federal_taxable_income=69250, federal_deduction=15750)
        assert result.tax >= D("0"), code
        # Nobody's state tax should exceed a third of their income.
        assert result.total_tax < D("28000"), f"{code} looks wrong: {result.total_tax}"
