"""Salary: normalise internally, preserve the original."""

from __future__ import annotations

import pytest

from careeros.engines.salary import parse_salary


@pytest.mark.parametrize(
    "raw,country,annual_min,annual_max",
    [
        ("$120,000 - $150,000 per year", "US", 120_000, 150_000),
        ("$65/hr W2", "US", 135_200, None),          # the "2" in W2 is not money
        ("$140K", "US", 140_000, None),
        ("$130k-$160k", "US", 130_000, 160_000),
        ("₹18 LPA CTC", "IN", 1_800_000, None),
        ("₹25,00,000/year", "IN", 2_500_000, None),
        ("12 LPA", "IN", 1_200_000, None),
        ("Rs 90,000 per month", "IN", 1_080_000, None),
        ("₹15-22 LPA", "IN", 1_500_000, 2_200_000),
    ],
)
def test_normalisation(raw, country, annual_min, annual_max):
    parsed = parse_salary(raw, country)
    assert parsed.annual_min == pytest.approx(annual_min)
    assert (parsed.annual_max is None) == (annual_max is None)
    if annual_max is not None:
        assert parsed.annual_max == pytest.approx(annual_max)


def test_original_is_always_preserved():
    parsed = parse_salary("₹18 LPA CTC", "IN")
    assert parsed.original == "₹18 LPA CTC"
    assert "18 LPA CTC" in parsed.display("₹")
    assert parsed.components == ["ctc"]        # CTC is not the same as base


def test_components_are_kept_apart():
    parsed = parse_salary("₹15-22 LPA (base + variable)", "IN")
    assert set(parsed.components) == {"base", "variable"}


def test_undisclosed_salary_is_not_invented():
    parsed = parse_salary("Competitive salary and 401k", "US")
    assert parsed.annual_min is None
    assert parsed.note == "no numeric amount found"


def test_irrelevant_numbers_are_filtered():
    parsed = parse_salary("$120,000-$150,000 + 10% bonus, 3 days onsite", "US")
    assert parsed.annual_min == 120_000
    assert parsed.annual_max == 150_000
