"""The Premium Tax Credit: Form 8962, the cliff, and the 2026 reversion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.config import federal
from taxvault.engines.federal import TaxProfile, compute_federal
from taxvault.engines.health import (
    MarketplaceCoverage,
    applicable_percentage,
    compute_premium_tax_credit,
    poverty_line,
)
from taxvault.forms.f1095 import parse_1095a_text


def D(value):
    return Decimal(str(value))


POLICY = [MarketplaceCoverage(annual_premium=D(14400), benchmark_premium=D(15600),
                              advance_credit=D(11000))]


@pytest.fixture
def p2025():
    return federal(2025)


@pytest.fixture
def p2026():
    return federal(2026)


# ---------------------------------------------------------------------------
# the poverty line
# ---------------------------------------------------------------------------
def test_the_poverty_line_uses_the_prior_year_guidelines(p2025, p2026):
    """2024 guidelines for 2025 coverage; 2025 guidelines for 2026."""
    assert poverty_line(p2025, household_size=1) == D("15060.00")
    assert poverty_line(p2026, household_size=1) == D("15650.00")


def test_each_extra_person_adds_the_increment(p2026):
    assert poverty_line(p2026, household_size=4) == D("32150.00")   # 15,650 + 3 x 5,500


def test_alaska_and_hawaii_have_their_own_higher_figures(p2026):
    """Using the contiguous figure in Alaska moves a household a whole band."""
    contiguous = poverty_line(p2026, household_size=2)
    assert poverty_line(p2026, household_size=2, state_code="AK") > contiguous
    assert poverty_line(p2026, household_size=2, state_code="HI") > contiguous


# ---------------------------------------------------------------------------
# the applicable percentage
# ---------------------------------------------------------------------------
def test_2025_asks_nothing_of_a_household_under_150_percent(p2025):
    assert applicable_percentage(p2025, D(120)) == D(0)


def test_2026_asks_for_2_1_percent_even_at_the_bottom(p2026):
    """The enhanced subsidies lapsed, so the 0% floor is gone."""
    assert applicable_percentage(p2026, D(120)) == D("0.0210")


def test_the_percentage_is_interpolated_inside_a_band(p2026):
    """175% of the poverty line sits halfway between the 150% and 200% rates."""
    low = applicable_percentage(p2026, D(150))
    high = applicable_percentage(p2026, D(200))
    middle = applicable_percentage(p2026, D(175))
    assert low < middle < high
    assert abs(middle - (low + high) / 2) < D("0.0005")


def test_2025_has_no_cliff_and_caps_at_8_5_percent(p2025):
    assert applicable_percentage(p2025, D(600)) == D("0.085")


# ---------------------------------------------------------------------------
# the credit
# ---------------------------------------------------------------------------
def test_a_household_that_earned_less_than_estimated_gets_money_back(p2025):
    """$62,000, household of 3: 240% of the poverty line, 3.6% contribution."""
    result = compute_premium_tax_credit(
        POLICY, p2025, household_income=D(62000), household_size=3,
        filing_status="married_jointly",
    )
    assert result.income_as_pct_of_fpl == D("240.12")
    assert result.applicable_percentage == D("0.0360")
    assert result.allowed_credit == D("13365.02")
    assert result.net_credit == D("2365.02")
    assert result.repayment == D(0)


def test_the_same_household_owes_in_2026_because_the_rate_rose(p2026):
    """Identical figures, one year later: 2,365 back becomes 237 owed."""
    result = compute_premium_tax_credit(
        POLICY, p2026, household_income=D(62000), household_size=3,
        filing_status="married_jointly",
    )
    assert result.applicable_percentage == D("0.0780")
    assert result.net_credit == D(0)
    assert result.repayment == D("236.94")


def test_the_credit_is_measured_against_the_benchmark_not_what_was_paid(p2025):
    """A cheap plan with a high benchmark still gets the benchmark's credit."""
    cheap = [MarketplaceCoverage(annual_premium=D(9000), benchmark_premium=D(15600),
                                 advance_credit=D(8000))]
    result = compute_premium_tax_credit(
        cheap, p2025, household_income=D(62000), household_size=3,
        filing_status="married_jointly",
    )
    # 15,600 less the 2,235 contribution is 13,365, but capped at what the
    # plan actually cost: the credit never exceeds the premium.
    assert result.allowed_credit == D("9000.00")


# ---------------------------------------------------------------------------
# the cliff
# ---------------------------------------------------------------------------
def test_2026_takes_the_whole_credit_one_dollar_over_400_percent(p2026):
    line = poverty_line(p2026, household_size=3)
    under = compute_premium_tax_credit(
        POLICY, p2026, household_income=line * 4 - D(1), household_size=3,
        filing_status="married_jointly",
    )
    over = compute_premium_tax_credit(
        POLICY, p2026, household_income=line * 4 + D(1), household_size=3,
        filing_status="married_jointly",
    )
    assert under.eligible is True
    assert over.eligible is False
    assert over.over_cliff is True
    # One dollar of income costs the entire advance, uncapped.
    assert over.repayment == D("11000.00")
    assert over.repayment_cap is None


def test_2025_has_no_cliff_at_the_same_income(p2025):
    line = poverty_line(p2025, household_size=3)
    result = compute_premium_tax_credit(
        POLICY, p2025, household_income=line * 5, household_size=3,
        filing_status="married_jointly",
    )
    assert result.eligible is True
    assert result.over_cliff is False


def test_approaching_the_cliff_is_warned_about_while_it_can_be_fixed(p2026):
    line = poverty_line(p2026, household_size=3)
    result = compute_premium_tax_credit(
        POLICY, p2026, household_income=line * D("3.9"), household_size=3,
        filing_status="married_jointly",
    )
    assert result.eligible is True
    assert any("disappears entirely" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# repayment caps
# ---------------------------------------------------------------------------
def test_repayment_is_capped_below_400_percent(p2026):
    """A big advance against a small entitlement: the cap holds it down."""
    generous = [MarketplaceCoverage(annual_premium=D(18000),
                                    benchmark_premium=D(18000),
                                    advance_credit=D(17000))]
    result = compute_premium_tax_credit(
        generous, p2026, household_income=D(70000), household_size=3,
        filing_status="married_jointly",
    )
    assert result.repayment_before_cap > result.repayment
    assert result.repayment == result.repayment_cap


def test_a_single_filer_has_half_the_repayment_cap(p2026):
    """$40,000 for one person is 256% of the poverty line: the 200-300% band."""
    generous = [MarketplaceCoverage(annual_premium=D(12000),
                                    benchmark_premium=D(12000),
                                    advance_credit=D(11500))]
    single = compute_premium_tax_credit(
        generous, p2026, household_income=D(40000), household_size=1,
        filing_status="single",
    )
    joint = compute_premium_tax_credit(
        generous, p2026, household_income=D(40000), household_size=1,
        filing_status="married_jointly",
    )
    assert single.repayment_cap == D("975")
    assert joint.repayment_cap == D("1950")
    assert joint.repayment_cap == single.repayment_cap * 2


# ---------------------------------------------------------------------------
# through the 1040
# ---------------------------------------------------------------------------
def test_a_net_credit_is_refundable():
    """It is paid even where no tax is owed, so it can exceed the withholding."""
    result = compute_federal(TaxProfile(
        tax_year=2025, filing_status="married_jointly", wages=D(62000),
        children_under_17=1, federal_withheld=D(0), marketplace=POLICY,
    ))
    assert Decimal(result.premium_tax_credit["net_credit"]) > D(0)
    assert result.refundable_credits >= Decimal(result.premium_tax_credit["net_credit"])
    assert result.balance < D(0)


def test_a_repayment_adds_to_the_tax_owed():
    result = compute_federal(TaxProfile(
        tax_year=2026, filing_status="married_jointly", wages=D(112000),
        children_under_17=1, federal_withheld=D(9000), marketplace=POLICY,
    ))
    assert result.premium_tax_credit_repayment == D("11000.00")
    assert any("NO premium tax credit" in w for w in result.warnings)


def test_household_income_adds_back_tax_exempt_interest():
    """A household living on municipal interest is not a low-income household."""
    plain = compute_federal(TaxProfile(
        tax_year=2026, filing_status="single", wages=D(40000), marketplace=POLICY,
        household_size=1,
    ))
    with_munis = compute_federal(TaxProfile(
        tax_year=2026, filing_status="single", wages=D(40000),
        tax_exempt_interest=D(20000), marketplace=POLICY, household_size=1,
    ))
    assert (Decimal(with_munis.premium_tax_credit["household_income"])
            > Decimal(plain.premium_tax_credit["household_income"]))


def test_no_1095a_means_no_form_8962():
    result = compute_federal(TaxProfile(tax_year=2026, wages=D(60000)))
    assert result.premium_tax_credit == {}
    assert result.premium_tax_credit_repayment == D(0)


# ---------------------------------------------------------------------------
# the form
# ---------------------------------------------------------------------------
SAMPLE = """Form 1095-A  Health Insurance Marketplace Statement  2026
Marketplace-assigned policy number
POL-99214
CA Marketplace
Part III Coverage Information
Month                    A Monthly premium   B SLCSP premium   C Advance payment
January                        1,200.00          1,300.00           916.67
Annual Totals                 14,400.00         15,600.00        11,000.00
Covered individual Dana Reed
Covered individual Sam Reed
"""


def test_the_three_columns_are_read():
    form, confidence, warnings = parse_1095a_text(SAMPLE)
    assert form.annual_premium == D("14400.00")
    assert form.benchmark_premium == D("15600.00")
    assert form.advance_credit == D("11000.00")
    assert form.policy_number == "POL-99214"
    assert confidence == 1.0
    assert warnings == []


def test_a_missing_benchmark_is_an_error_not_a_zero():
    """Treating column B as zero silently wipes out the entire credit."""
    form, _, warnings = parse_1095a_text(
        SAMPLE.replace("15,600.00", "0.00").replace("1,300.00", "0.00")
    )
    assert form.benchmark_premium == D(0)
    assert any("second-lowest-cost silver" in w for w in warnings)
    severities = {f["severity"] for f in form.validate()}
    assert "error" in severities


def test_a_parsed_form_becomes_engine_input():
    form, _, _ = parse_1095a_text(SAMPLE)
    coverage = form.to_coverage()
    assert coverage.benchmark_premium == D("15600.00")
    assert coverage.policy_number == "POL-99214"


# ---------------------------------------------------------------------------
# reference
# ---------------------------------------------------------------------------
def test_the_limits_endpoint_publishes_the_cliff(client):
    assert client.get("/api/reference/limits?year=2025").json()[
        "premium_tax_credit"]["income_cap_fpl"] == 0
    assert client.get("/api/reference/limits?year=2026").json()[
        "premium_tax_credit"]["income_cap_fpl"] == 400
