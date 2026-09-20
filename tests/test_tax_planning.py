"""Planning windows, unfiled years, payment pricing, and the crypto layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from taxvault.crypto import (
    CryptoError,
    decrypt_field,
    encrypt_field,
    normalise_ssn,
    open_ssn,
    redact,
    seal_ssn,
    ssn_index,
)
from taxvault.engines.compliance import estimate_penalties
from taxvault.engines.federal import TaxProfile, compute_federal
from taxvault.engines.payments import build_payment_options, estimated_payments_for_next_year
from taxvault.engines.planning import apply_strategies, build_strategies


def D(value):
    return Decimal(str(value))


BASE = dict(
    tax_year=2025, filing_status="married_jointly", wages=185000, federal_withheld=24000,
    resident_state="CA", children_under_17=2, age=44, spouse_age=42,
)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def test_strategies_are_priced_by_rerunning_the_whole_calculation(tax_env):
    profile = TaxProfile(**BASE)
    strategies = build_strategies(profile, as_of=date(2025, 11, 1), existing_401k=8000)
    deferral = next(s for s in strategies if s.id == "traditional_401k")

    applied = compute_federal(apply_strategies(profile, strategies, ["traditional_401k"]))
    baseline = compute_federal(profile)
    assert deferral.federal_saving == baseline.total_tax - applied.total_tax


def test_the_401k_window_shuts_at_year_end(tax_env):
    profile = TaxProfile(**BASE)
    in_year = build_strategies(profile, as_of=date(2025, 11, 1), existing_401k=8000)
    after = build_strategies(profile, as_of=date(2026, 2, 1), existing_401k=8000)

    assert next(s for s in in_year if s.id == "traditional_401k").still_available is True
    shut = next(s for s in after if s.id == "traditional_401k")
    assert shut.still_available is False
    assert shut.closes_on == date(2025, 12, 31)


def test_the_ira_window_stays_open_until_the_filing_deadline(tax_env):
    profile = TaxProfile(tax_year=2025, wages=60000, age=35)
    strategies = build_strategies(profile, as_of=date(2026, 2, 1), has_employer_plan=False)
    ira = next(s for s in strategies if s.id == "traditional_ira")
    assert ira.still_available is True
    assert ira.closes_on == date(2026, 4, 15)


def test_a_sep_ira_stays_open_to_the_extended_deadline(tax_env):
    profile = TaxProfile(tax_year=2025, self_employment_income=120000, qbi_income=120000)
    strategies = build_strategies(profile, as_of=date(2026, 8, 1))
    sep = next(s for s in strategies if s.id == "sep_ira")
    assert sep.still_available is True
    assert sep.closes_on == date(2026, 10, 15)


def test_an_ira_is_not_offered_when_income_puts_it_out_of_reach(tax_env):
    profile = TaxProfile(**{**BASE, "wages": 400000})
    strategies = build_strategies(profile, as_of=date(2025, 11, 1))
    assert not any(s.id == "traditional_ira" for s in strategies)


def test_informational_entries_never_outrank_a_real_move(tax_env):
    profile = TaxProfile(**BASE)
    strategies = build_strategies(profile, as_of=date(2025, 11, 1), existing_401k=8000)
    actionable = [i for i, s in enumerate(strategies) if s.actionable and s.still_available]
    informational = [i for i, s in enumerate(strategies) if not s.actionable]
    assert actionable and informational
    assert max(actionable) < min(informational)


def test_no_strategy_can_make_the_tax_worse(tax_env):
    profile = TaxProfile(**BASE)
    baseline = compute_federal(profile).total_tax
    strategies = build_strategies(profile, as_of=date(2025, 11, 1), existing_401k=8000)
    for strategy in strategies:
        if not strategy.actionable:
            continue
        after = compute_federal(apply_strategies(profile, strategies, [strategy.id]))
        assert after.total_tax <= baseline, strategy.id


def test_a_strategy_is_never_offered_beyond_what_the_client_earns(tax_env):
    profile = TaxProfile(tax_year=2025, wages=12000, age=30)
    strategies = build_strategies(profile, as_of=date(2025, 11, 1), existing_401k=0)
    deferral = next(s for s in strategies if s.id == "traditional_401k")
    assert deferral.amount <= D("12000")


# ---------------------------------------------------------------------------
# penalties and unfiled years
# ---------------------------------------------------------------------------
def test_a_year_that_owed_nothing_carries_no_penalty():
    result = estimate_penalties(0, due_date=date(2022, 4, 18), as_of=date(2026, 9, 20), year=2024)
    assert result["total"] == "0"
    assert "no failure-to-file penalty" in result["note"]


def test_failure_to_file_is_reduced_by_failure_to_pay_in_the_same_month():
    """3,100 unpaid: 5% x 5 months = 775, less 0.5% x 5 months = 77.50."""
    result = estimate_penalties(
        3100, due_date=date(2023, 4, 18), as_of=date(2026, 9, 20), year=2024
    )
    assert result["failure_to_file"] == "697.50"
    assert result["failure_to_pay"] == "651.00"  # 0.5% x 42 months, under the 25% cap


def test_the_failure_to_pay_penalty_stops_at_its_cap():
    result = estimate_penalties(
        10000, due_date=date(2010, 4, 15), as_of=date(2026, 9, 20), year=2024
    )
    assert result["failure_to_pay"] == "2500.00"  # 25% of 10,000


def test_filing_removes_the_larger_penalty():
    kwargs = dict(due_date=date(2023, 4, 18), as_of=date(2026, 9, 20), year=2024)
    unfiled = estimate_penalties(3100, filed=False, **kwargs)
    filed = estimate_penalties(3100, filed=True, **kwargs)
    assert filed["failure_to_file"] == "0.00"
    assert Decimal(filed["total"]) < Decimal(unfiled["total"])


def test_penalties_work_for_a_year_with_no_parameter_file():
    """Rates are statutory, so an old year borrows the nearest table."""
    result = estimate_penalties(
        1000, due_date=date(2019, 4, 15), as_of=date(2026, 9, 20), year=2019
    )
    assert Decimal(result["total"]) > 0


# ---------------------------------------------------------------------------
# payment
# ---------------------------------------------------------------------------
def test_a_plan_always_costs_more_than_paying_now():
    _, options = build_payment_options(9400, year=2025, as_of=date(2026, 3, 1))
    in_full = next(o for o in options if o.method == "irs_direct_pay")
    plan = next(o for o in options if o.method == "long_term_direct_debit_online")
    assert in_full.total_cost == D("9400.00")
    assert plan.total_cost > in_full.total_cost


def test_direct_debit_plans_halve_the_failure_to_pay_penalty():
    _, options = build_payment_options(9400, year=2025, as_of=date(2026, 3, 1))
    with_debit = next(o for o in options if o.method == "long_term_direct_debit_online")
    short = next(o for o in options if o.method == "short_term_extension")
    # Same balance, longer term, but a lower monthly penalty rate.
    assert with_debit.penalty / with_debit.instalments < short.penalty / short.instalments


def test_a_balance_over_the_limit_says_so_rather_than_offering_a_plan():
    _, options = build_payment_options(72000, year=2025, as_of=date(2026, 3, 1))
    long_term = [o for o in options if o.method.startswith("long_term")]
    assert long_term and all(not o.available for o in long_term)
    assert all("above the" in o.notes[0] for o in long_term)


def test_the_card_fee_is_shown_rather_than_buried():
    _, options = build_payment_options(9400, year=2025, as_of=date(2026, 3, 1))
    card = next(o for o in options if o.method == "card")
    assert card.processing_fee == D("173.90")  # 1.85%
    assert card.total_cost == D("9573.90")


def test_a_late_balance_already_carries_penalty_and_interest():
    _, on_time = build_payment_options(5000, year=2025, as_of=date(2026, 3, 1))
    _, late = build_payment_options(5000, year=2025, as_of=date(2026, 11, 1))
    assert next(o for o in on_time if o.method == "check").penalty == D("0")
    assert next(o for o in late if o.method == "check").penalty > D("0")


def test_the_safe_harbour_is_110_percent_above_150k_of_agi():
    high = estimated_payments_for_next_year(
        current_year_tax=28000, current_year_agi=190000, expected_withholding=12000, year=2025
    )
    low = estimated_payments_for_next_year(
        current_year_tax=11000, current_year_agi=90000, expected_withholding=4000, year=2025
    )
    assert high["multiplier"] == "1.1" and high["target_payments"] == "30800.00"
    assert low["multiplier"] == "1.0" and low["target_payments"] == "11000.00"
    assert high["quarterly_amount"] == "4700.00"


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------
def test_an_ssn_round_trips_but_only_in_its_own_row(tax_env):
    ciphertext, index, last4, kind = seal_ssn("123-45-6789", context="taxpayer:7")
    assert last4 == "6789" and kind == "ssn"
    assert open_ssn(ciphertext, context="taxpayer:7").digits == "123456789"
    with pytest.raises(CryptoError):
        open_ssn(ciphertext, context="taxpayer:8")


def test_the_blind_index_is_deterministic_and_not_the_ssn(tax_env):
    first = ssn_index("123-45-6789")
    assert first == ssn_index("123456789")
    assert "123456789" not in first
    assert first != ssn_index("123-45-6788")


def test_encryption_is_not_deterministic(tax_env):
    """Two encryptions of the same value must differ, or the ciphertext leaks
    equality -- which is exactly what the blind index is for instead."""
    a = encrypt_field("123456789", purpose="ssn", context="row:1")
    b = encrypt_field("123456789", purpose="ssn", context="row:1")
    assert a != b
    assert decrypt_field(a, purpose="ssn", context="row:1") == "123456789"


def test_tampered_ciphertext_is_refused(tax_env):
    ciphertext = encrypt_field("secret", purpose="ssn", context="row:1")
    head, key_id, nonce, body = ciphertext.split(".")
    flipped = body[:-4] + ("AAAA" if not body.endswith("AAAA") else "BBBB")
    with pytest.raises(CryptoError):
        decrypt_field(f"{head}.{key_id}.{nonce}.{flipped}", purpose="ssn", context="row:1")


def test_a_field_cannot_be_read_with_the_wrong_purpose(tax_env):
    ciphertext = encrypt_field("123456789", purpose="ssn", context="row:1")
    with pytest.raises(CryptoError):
        decrypt_field(ciphertext, purpose="bank", context="row:1")


@pytest.mark.parametrize("bad", ["000-12-3456", "666-12-3456", "900-12-3456",
                                 "123-00-4567", "123-45-0000", "12345678"])
def test_impossible_ssns_are_refused(bad):
    with pytest.raises(ValueError):
        normalise_ssn(bad)


@pytest.mark.parametrize("itin", ["912-70-1234", "955-94-5678", "999-50-0001"])
def test_itins_are_recognised_not_rejected(itin):
    assert normalise_ssn(itin).kind == "itin"


def test_redaction_catches_every_ssn_shape():
    text = "SSNs 123-45-6789, 123 45 6789 and 123456789 in one line"
    assert "6789" in redact(text)
    assert "123-45-6789" not in redact(text)
    assert "123456789" not in redact(text)
