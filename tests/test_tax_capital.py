"""Schedule D: netting, the $3,000 limit, and carryforward character.

Every expected figure here was worked out from the statute and the published
rate schedule rather than captured from a run of this code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.config import federal
from taxvault.engines.capital import CapitalInput, compute_capital
from taxvault.engines.federal import TaxProfile, compute_federal


def D(value):
    return Decimal(str(value))


@pytest.fixture
def p2026():
    return federal(2026)


# ---------------------------------------------------------------------------
# netting and character
# ---------------------------------------------------------------------------
def test_both_positive_keep_their_own_character(p2026):
    result = compute_capital(CapitalInput(short_term=D(5000), long_term=D(9000)), p2026)
    assert result.ordinary_component == D("5000.00")
    assert result.preferential_component == D("9000.00")
    assert result.loss_deduction == D("0")


def test_a_short_term_loss_leaves_long_term_gain_long_term(p2026):
    """$4,000 short-term loss against $10,000 long-term gain.

    The survivor is $6,000 and it is LONG-term -- it does not become some
    blend. Getting this wrong taxes $6,000 at 22-37% instead of 15%.
    """
    result = compute_capital(CapitalInput(short_term=D(-4000), long_term=D(10000)), p2026)
    assert result.ordinary_component == D("0")
    assert result.preferential_component == D("6000.00")


def test_a_long_term_loss_leaves_short_term_gain_short_term(p2026):
    result = compute_capital(CapitalInput(short_term=D(10000), long_term=D(-4000)), p2026)
    assert result.ordinary_component == D("6000.00")
    assert result.preferential_component == D("0")


def test_capital_gain_distributions_are_always_long_term(p2026):
    """A fund's December distribution is long-term however long you held it."""
    result = compute_capital(
        CapitalInput(capital_gain_distributions=D(2500)), p2026
    )
    assert result.preferential_component == D("2500.00")
    assert result.ordinary_component == D("0")


# ---------------------------------------------------------------------------
# the annual limit
# ---------------------------------------------------------------------------
def test_net_loss_is_capped_at_three_thousand_a_year(p2026):
    result = compute_capital(CapitalInput(short_term=D(-12000), long_term=D(-3000)), p2026)
    assert result.loss_deduction == D("3000.00")
    assert result.total_carryforward == D("12000.00")


def test_the_allowance_is_spent_on_short_term_loss_first(p2026):
    """IRC 1212(b): the $3,000 comes off the short-term loss first.

    $12,000 short-term and $3,000 long-term: the $3,000 allowance is taken
    entirely from the short-term side, leaving $9,000 short-term and the full
    $3,000 long-term to carry. Short-term loss is the more valuable of the
    two, so spending it first is worth arguing about -- but it is the rule.
    """
    result = compute_capital(CapitalInput(short_term=D(-12000), long_term=D(-3000)), p2026)
    assert result.carryforward_short == D("9000.00")
    assert result.carryforward_long == D("3000.00")


def test_married_separately_gets_half_the_allowance(p2026):
    result = compute_capital(
        CapitalInput(short_term=D(-10000)), p2026, filing_status="married_separately"
    )
    assert result.loss_deduction == D("1500.00")
    assert result.carryforward_short == D("8500.00")


def test_a_small_loss_is_fully_deductible(p2026):
    result = compute_capital(CapitalInput(long_term=D(-1200)), p2026)
    assert result.loss_deduction == D("1200.00")
    assert result.total_carryforward == D("0")


def test_a_carryforward_keeps_its_character_when_it_lands(p2026):
    """Last year's long-term loss offsets this year's long-term gain first."""
    result = compute_capital(
        CapitalInput(long_term=D(10000), carryforward_long=D(4000)), p2026
    )
    assert result.long_term_net == D("6000.00")
    assert result.preferential_component == D("6000.00")


# ---------------------------------------------------------------------------
# wash sales
# ---------------------------------------------------------------------------
def test_a_wash_sale_adds_the_loss_back(p2026):
    """A $5,000 short-term loss with $2,000 disallowed nets to $3,000."""
    result = compute_capital(
        CapitalInput(short_term=D(-5000), wash_sale_disallowed=D(2000)), p2026
    )
    assert result.short_term_net == D("-3000.00")
    assert result.loss_deduction == D("3000.00")
    assert any("wash-sale" in note for note in result.notes)


def test_a_wash_sale_never_turns_a_loss_into_a_gain(p2026):
    result = compute_capital(
        CapitalInput(short_term=D(-1000), wash_sale_disallowed=D(9000)), p2026
    )
    assert result.short_term_net == D("0")


# ---------------------------------------------------------------------------
# special rates
# ---------------------------------------------------------------------------
def test_collectibles_cannot_exceed_the_gain_that_survived_netting(p2026):
    """A loss elsewhere reduces the 28% slice too."""
    result = compute_capital(
        CapitalInput(long_term=D(5000), collectibles_gain=D(8000)), p2026
    )
    assert result.collectibles_gain == D("5000.00")


def test_a_low_bracket_client_never_pays_28_percent_on_collectibles():
    """The 28% rate is a ceiling, not a flat rate.

    $30,000 of wages and a $5,000 collectibles gain: taxable income is well
    inside the 12% band, so the gain is taxed at 12%, not 28%.
    """
    result = compute_federal(TaxProfile(
        tax_year=2026, wages=D(30000), collectibles_gain=D(5000), long_term_gains=D(5000),
    ))
    # 12% of 5,000 = 600, against 1,400 if the 28% ceiling were applied flatly.
    assert result.preferential_tax == D("600.00")


# ---------------------------------------------------------------------------
# through the 1040
# ---------------------------------------------------------------------------
def test_a_net_loss_reduces_agi_by_three_thousand():
    """$80,000 of wages and a $20,000 net capital loss -> $77,000 of AGI."""
    plain = compute_federal(TaxProfile(tax_year=2026, wages=D(80000)))
    with_loss = compute_federal(TaxProfile(
        tax_year=2026, wages=D(80000), short_term_gains=D(-20000),
    ))
    assert plain.agi - with_loss.agi == D("3000.00")
    assert with_loss.capital["carryforward_short"] == "17000.00"


def test_long_term_gain_is_stacked_above_ordinary_income():
    """$50,000 wages and $60,000 long-term gain, single, 2026.

    Taxable income 50,000 + 60,000 - 16,100 = 93,900. Ordinary taxable is
    50,000 - 16,100 = 33,900, so the 0% band (to 49,450) has 15,550 of room;
    the remaining 44,450 of gain is taxed at 15% = 6,667.50.
    """
    result = compute_federal(TaxProfile(
        tax_year=2026, wages=D(50000), long_term_gains=D(60000),
    ))
    assert result.taxable_income == D("93900.00")
    assert result.preferential_tax == D("6667.50")


def test_niit_counts_the_net_gain_not_the_gross():
    """A loss in the same year reduces net investment income."""
    gross = compute_federal(TaxProfile(
        tax_year=2026, filing_status="single", wages=D(240000), long_term_gains=D(40000),
    ))
    netted = compute_federal(TaxProfile(
        tax_year=2026, filing_status="single", wages=D(240000),
        long_term_gains=D(40000), short_term_gains=D(-15000),
    ))
    assert netted.net_investment_income_tax < gross.net_investment_income_tax


def test_the_simple_fields_and_the_structured_input_agree():
    """A client who types two numbers gets the same answer as one who uploads."""
    simple = compute_federal(TaxProfile(
        tax_year=2026, wages=D(90000), short_term_gains=D(4000), long_term_gains=D(11000),
    ))
    structured = compute_federal(TaxProfile(
        tax_year=2026, wages=D(90000),
        capital=CapitalInput(short_term=D(4000), long_term=D(11000)),
    ))
    assert simple.total_tax == structured.total_tax
