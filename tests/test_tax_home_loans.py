"""Home loans: the debt ceiling, the use test, points and PMI."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.config import federal
from taxvault.engines.federal import TaxProfile, compute_federal
from taxvault.engines.mortgage import (
    USE_IMPROVE,
    USE_OTHER,
    USE_PURCHASE,
    Loan,
    compute_mortgage,
    home_sale_exclusion,
)
from taxvault.forms.f1098 import parse_1098_text, to_loan


def D(value):
    return Decimal(str(value))


@pytest.fixture
def p2026():
    return federal(2026)


@pytest.fixture
def p2025():
    return federal(2025)


# ---------------------------------------------------------------------------
# the debt ceiling
# ---------------------------------------------------------------------------
def test_interest_under_the_ceiling_is_fully_deductible(p2026):
    result = compute_mortgage(
        [Loan(balance=D(400000), interest_paid=D(16000), origination="2021-05-01")], p2026
    )
    assert result.deductible_interest == D("16000.00")


def test_interest_above_the_ceiling_is_prorated_not_lost(p2026):
    """$900,000 of debt against a $750,000 ceiling: 83.33% of the interest."""
    result = compute_mortgage(
        [Loan(balance=D(900000), interest_paid=D(36000), origination="2022-01-10")], p2026
    )
    assert result.deductible_interest == D("30000.00")
    assert result.disallowed_interest == D("6000.00")


def test_a_pre_2018_loan_keeps_the_million_dollar_ceiling(p2026):
    """The same $900,000 balance, taken in 2016, is fully deductible."""
    result = compute_mortgage(
        [Loan(balance=D(900000), interest_paid=D(36000), origination="2016-04-01")], p2026
    )
    assert result.deductible_interest == D("36000.00")
    assert result.applicable_cap == D("1000000.00")


def test_the_grandfather_date_is_a_cliff(p2026):
    """15 December 2017 keeps the old cap; 16 December does not."""
    just_in = compute_mortgage(
        [Loan(balance=D(900000), interest_paid=D(36000), origination="2017-12-15")], p2026
    )
    just_out = compute_mortgage(
        [Loan(balance=D(900000), interest_paid=D(36000), origination="2017-12-16")], p2026
    )
    assert just_in.deductible_interest == D("36000.00")
    assert just_out.deductible_interest == D("30000.00")


def test_grandfathered_debt_eats_the_room_for_newer_debt(p2026):
    """$600,000 from 2016 plus $300,000 from 2021.

    The old loan uses its $600,000; the new one is limited to $750,000 less
    that, so $150,000 of it qualifies and the total base is $750,000.
    """
    result = compute_mortgage(
        [Loan(balance=D(600000), interest_paid=D(24000), origination="2016-03-01"),
         Loan(balance=D(300000), interest_paid=D(12000), origination="2021-06-01",
              is_main_home=False)],
        p2026,
    )
    assert result.allowed_debt == D("750000.00")
    assert result.deductible_interest == D("30000.00")


def test_married_separately_gets_half_the_ceiling(p2026):
    result = compute_mortgage(
        [Loan(balance=D(750000), interest_paid=D(30000), origination="2020-01-01")],
        p2026, filing_status="married_separately",
    )
    assert result.allowed_debt == D("375000.00")
    assert result.deductible_interest == D("15000.00")


# ---------------------------------------------------------------------------
# what the money was used for
# ---------------------------------------------------------------------------
def test_a_heloc_spent_on_anything_else_is_not_deductible(p2026):
    result = compute_mortgage(
        [Loan(balance=D(50000), interest_paid=D(4000), kind="home_equity",
              used_for=USE_OTHER)],
        p2026,
    )
    assert result.deductible_interest == D("0")
    assert result.disallowed_interest == D("4000.00")
    assert any("not used to buy, build or substantially improve" in w
               for w in result.warnings)


def test_a_heloc_spent_on_the_house_is_deductible(p2026):
    result = compute_mortgage(
        [Loan(balance=D(50000), interest_paid=D(4000), kind="home_equity",
              used_for=USE_IMPROVE, origination="2023-02-01")],
        p2026,
    )
    assert result.deductible_interest == D("4000.00")


def test_a_qualifying_heloc_still_counts_against_the_same_ceiling(p2026):
    """$700,000 mortgage plus a $100,000 improvement HELOC is over the cap."""
    result = compute_mortgage(
        [Loan(balance=D(700000), interest_paid=D(28000), origination="2020-01-01"),
         Loan(balance=D(100000), interest_paid=D(6000), kind="home_equity",
              used_for=USE_IMPROVE, origination="2023-01-01")],
        p2026,
    )
    assert result.allowed_debt == D("750000.00")
    # 750/800 of 34,000
    assert result.deductible_interest == D("31875.00")


# ---------------------------------------------------------------------------
# points
# ---------------------------------------------------------------------------
def test_points_on_buying_a_main_home_come_off_in_full(p2026):
    result = compute_mortgage(
        [Loan(balance=D(400000), interest_paid=D(16000), points_paid=D(6000),
              origination="2026-03-01", used_for=USE_PURCHASE)],
        p2026,
    )
    assert result.points_deduction == D("6000.00")


def test_points_on_a_refinance_are_spread_over_the_term(p2026):
    """$3,600 over 30 years, ten months paid: 3,600 * 10/360."""
    result = compute_mortgage(
        [Loan(balance=D(400000), interest_paid=D(16000), points_paid=D(3600),
              origination="2026-03-01", is_refinance=True, months_paid_this_year=10)],
        p2026,
    )
    assert result.points_deduction == D("100.00")


def test_leftover_points_from_a_paid_off_loan_come_off_at_once(p2026):
    result = compute_mortgage(
        [Loan(balance=D(400000), interest_paid=D(16000), origination="2026-03-01",
              is_refinance=True, unamortised_points_from_old_loan=D(2400))],
        p2026,
    )
    assert result.points_deduction == D("2400.00")


# ---------------------------------------------------------------------------
# mortgage insurance
# ---------------------------------------------------------------------------
def test_mortgage_insurance_is_not_deductible_for_2025(p2025):
    result = compute_mortgage(
        [Loan(balance=D(300000), interest_paid=D(12000), mortgage_insurance=D(1800),
              origination="2024-01-01")],
        p2025, agi=D(80000),
    )
    assert result.mortgage_insurance_deduction == D("0")


def test_mortgage_insurance_is_deductible_again_for_2026(p2026):
    result = compute_mortgage(
        [Loan(balance=D(300000), interest_paid=D(12000), mortgage_insurance=D(1800),
              origination="2024-01-01")],
        p2026, agi=D(80000),
    )
    assert result.mortgage_insurance_deduction == D("1800.00")


def test_mortgage_insurance_phases_out_ten_percent_per_thousand(p2026):
    """AGI $103,000 is $3,000 over: three steps, so 30% is lost."""
    result = compute_mortgage(
        [Loan(balance=D(300000), interest_paid=D(12000), mortgage_insurance=D(2000),
              origination="2024-01-01")],
        p2026, agi=D(103000),
    )
    assert result.mortgage_insurance_deduction == D("1400.00")


def test_mortgage_insurance_is_gone_entirely_above_the_range(p2026):
    result = compute_mortgage(
        [Loan(balance=D(300000), interest_paid=D(12000), mortgage_insurance=D(2000),
              origination="2024-01-01")],
        p2026, agi=D(150000),
    )
    assert result.mortgage_insurance_deduction == D("0")


def test_a_partial_thousand_counts_as_a_whole_step(p2026):
    """$100 over the threshold still costs a full 10%."""
    result = compute_mortgage(
        [Loan(balance=D(300000), interest_paid=D(12000), mortgage_insurance=D(2000),
              origination="2024-01-01")],
        p2026, agi=D(100100),
    )
    assert result.mortgage_insurance_deduction == D("1800.00")


# ---------------------------------------------------------------------------
# through the 1040
# ---------------------------------------------------------------------------
def test_mortgage_interest_is_worth_nothing_below_the_standard_deduction():
    """The normal outcome, and the app says so rather than implying a saving."""
    result = compute_federal(TaxProfile(
        tax_year=2026, filing_status="married_jointly", wages=D(140000),
        loans=[Loan(balance=D(250000), interest_paid=D(9000), origination="2021-01-01")],
    ))
    assert result.deduction_kind == "standard"
    assert any("worth nothing extra this year" in n for n in result.notes)


def test_the_debt_ceiling_reaches_the_itemised_total():
    """A client over the ceiling itemises a smaller number than box 1."""
    result = compute_federal(TaxProfile(
        tax_year=2026, filing_status="married_jointly", wages=D(400000),
        state_local_income_tax=D(20000), property_tax=D(12000),
        loans=[Loan(balance=D(1500000), interest_paid=D(60000), origination="2022-01-01")],
    ))
    assert result.deduction_kind == "itemised"
    # 750/1500 of 60,000 = 30,000, plus 32,000 of SALT under the 40,400 cap.
    assert result.itemised_deduction == D("62000.00")


def test_the_scalar_field_still_works_without_loan_detail():
    result = compute_federal(TaxProfile(
        tax_year=2026, filing_status="married_jointly", wages=D(400000),
        state_local_income_tax=D(20000), property_tax=D(12000),
        mortgage_interest=D(30000),
    ))
    assert result.itemised_deduction == D("62000.00")


# ---------------------------------------------------------------------------
# Form 1098 and selling
# ---------------------------------------------------------------------------
def test_a_1098_becomes_a_loan_the_engine_can_price(p2026):
    text = (
        "Form 1098 Mortgage Interest Statement 2026\n"
        "1 Mortgage interest received from payer(s)/borrower(s)  18,432.55\n"
        "2 Outstanding mortgage principal  612,000.00\n"
        "3 Mortgage origination date  03/14/2016\n"
        "5 Mortgage insurance premiums  1,860.00\n"
    )
    form, confidence, _ = parse_1098_text(text)
    assert form.mortgage_interest == D("18432.55")
    assert form.best_origination() == "2016-03-14"
    assert confidence == 1.0
    result = compute_mortgage([to_loan(form)], p2026, agi=D(90000))
    assert result.deductible_interest == D("18432.55")
    assert result.mortgage_insurance_deduction == D("1860.00")


def test_a_1098_without_a_balance_says_the_ceiling_cannot_be_applied():
    form, _, warnings = parse_1098_text("1 Mortgage interest received  12,000.00")
    assert any("ceiling cannot be applied" in w for w in warnings)


def test_the_home_sale_exclusion_needs_two_of_five_years(p2026):
    kept = home_sale_exclusion(p2026, filing_status="married_jointly", gain=D(380000),
                               years_owned=6, years_lived_in=4)
    assert kept["excluded"] == "380000.00"
    assert kept["taxable_gain"] == "0.00"

    short = home_sale_exclusion(p2026, filing_status="married_jointly", gain=D(380000),
                                years_owned=1, years_lived_in=1)
    assert short["excluded"] == "0.00"
    assert short["taxable_gain"] == "380000.00"


def test_a_large_gain_is_only_excluded_up_to_the_cap(p2026):
    result = home_sale_exclusion(p2026, filing_status="single", gain=D(400000),
                                 years_owned=9, years_lived_in=9)
    assert result["excluded"] == "250000.00"
    assert result["taxable_gain"] == "150000.00"
