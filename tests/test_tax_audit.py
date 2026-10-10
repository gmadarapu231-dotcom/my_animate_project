"""The agent checking its own arithmetic.

An audit that cannot fail is decoration, so most of these tests break a
return on purpose and assert the check catches it. The one that matters
most is the internally consistent error: a return whose withholding was
inflated AND whose balance was adjusted to match adds up perfectly, and
only the source documents give it away.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from taxvault.agent.audit import audit_return, tax_from_brackets
from taxvault.engines.estimate import run_estimate
from taxvault.engines.federal import TaxProfile
from taxvault.forms.w2 import W2

pytest_plugins = ["tests.conftest_taxvault"]

WAGES, WITHHELD, STATE_TAX = D("37847.06"), D("4238.29"), D("1849.21")


def _return():
    profile = TaxProfile(tax_year=2025, filing_status="single", resident_state="KY",
                         wages=WAGES, federal_withheld=WITHHELD)
    return run_estimate(
        profile,
        wage_lines=[{"state": "KY", "wages": WAGES, "withheld": STATE_TAX}],
        method="regular")


def _w2():
    return W2.from_dict({
        "tax_year": 2025, "employer_name": "New Tech Company",
        "box1_wages": float(WAGES), "box2_federal_withheld": float(WITHHELD),
        "box3_social_security_wages": float(WAGES),
        "box5_medicare_wages": float(WAGES),
        "states": [{"state": "KY", "state_wages": float(WAGES),
                    "state_withheld": float(STATE_TAX)}],
    })


def _audit(mutate=None, **kwargs):
    estimate, form = _return(), _w2()
    if mutate:
        mutate(estimate, form)
    return audit_return(estimate, w2s=[form], **kwargs)


# ------------------------------------------------------- it is independent
def test_the_bracket_table_is_walked_by_hand():
    """Two bodies of code reading the same table and agreeing is evidence.
    One agreeing with itself is not, so this is written separately from the
    engine on purpose."""
    # 2025 single: 10% to 11,925, then 12% to 48,475.
    expected = D("11925") * D("0.10") + (D("22097.06") - D("11925")) * D("0.12")
    actual = tax_from_brackets(D("22097.06"), year=2025, status="single")
    assert abs(actual - expected) <= D("0.01"), (actual, expected)


def test_a_correct_return_passes_every_check():
    report = _audit()
    assert report.clean, [c.name for c in report.failures]
    assert report.ran >= 10


# ------------------------------------------------------------ it can fail
def _fails(report, name_fragment):
    return any(name_fragment.lower() in c.name.lower() for c in report.failures)


def test_a_tax_figure_that_is_not_on_the_rate_table_is_caught():
    report = _audit(lambda e, w: setattr(e.federal, "ordinary_tax",
                                         e.federal.ordinary_tax - D(500)))
    assert _fails(report, "bracket table")


def test_wages_that_do_not_match_the_w2_are_caught():
    report = _audit(lambda e, w: setattr(w, "wages", D("30000")))
    assert _fails(report, "Wages equal the W-2s")


def test_inflated_withholding_is_caught_even_when_the_return_still_adds_up():
    """The one that matters. Claiming withholding nobody withheld is the
    commonest shape of refund fraud and the penalty falls on the preparer.
    A return that inflates it and adjusts the balance to match is internally
    perfect, so only the documents catch it."""
    def tidy(estimate, _form):
        estimate.federal.total_payments = D("9000")
        estimate.federal.balance = estimate.federal.total_tax - D("9000")

    report = _audit(tidy)
    assert _fails(report, "every dollar of withholding")
    failure = next(c for c in report.failures
                   if "withholding" in c.name.lower())
    assert "4,238.29" in failure.expected
    assert "9,000.00" in failure.actual


def test_withholding_from_another_form_is_not_treated_as_unexplained():
    """A pension's withholding is real money paid in. Failing it would make
    the check cry wolf on every retiree."""
    def extra(estimate, _form):
        estimate.federal.total_payments = WITHHELD + D("1500")
        estimate.federal.balance = estimate.federal.total_tax - estimate.federal.total_payments

    report = _audit(extra, other_payments=D("1500"),
                    other_payments_detail="withheld on a 1099-R")
    assert not _fails(report, "every dollar of withholding"), [
        c.detail for c in report.failures]


def test_a_dropped_state_return_is_caught():
    """The shape of the Idaho fault: the state quietly not computed."""
    report = _audit(lambda e, w: e.states.clear())
    assert _fails(report, "Every state on the W-2s")


def test_an_agi_that_does_not_follow_from_the_income_is_caught():
    report = _audit(lambda e, w: setattr(e.federal, "agi", e.federal.agi + D(2500)))
    assert _fails(report, "Adjusted gross income")


def test_a_balance_that_does_not_follow_from_the_tax_is_caught():
    report = _audit(lambda e, w: setattr(e.federal, "balance", D("-9999")))
    assert _fails(report, "Federal balance")


def test_taking_less_than_the_larger_deduction_is_caught():
    report = _audit(lambda e, w: setattr(e.federal, "deduction_taken", D("8000")))
    assert _fails(report, "larger deduction")


# ------------------------------------------------- it declines to guess
def test_preferential_income_skips_the_bracket_check_rather_than_failing_it():
    """Gains stack on top of ordinary income. Reimplementing that stacking
    badly here would fail every investor's return with confidence, which is
    worse than saying nothing."""
    profile = TaxProfile(tax_year=2025, filing_status="single", resident_state="CA",
                         wages=D(120000), federal_withheld=D(18000),
                         long_term_gains=D(40000))
    report = audit_return(run_estimate(profile, method="regular"))
    bracket = next(c for c in report.checks if "bracket table" in c.name)
    assert bracket.status == "skipped"
    assert "stacking" in bracket.detail
    assert report.clean


def test_self_employment_tax_skips_the_rate_comparison():
    """Self-employment tax sits outside the income-tax brackets, so the
    effective rate can exceed the marginal rate quite correctly."""
    profile = TaxProfile(tax_year=2025, filing_status="single", resident_state="TX",
                         self_employment_income=D(102000))
    report = audit_return(run_estimate(profile, method="regular"))
    rate = next(c for c in report.checks if "effective rate" in c.name)
    assert rate.status == "skipped"


# ------------------------------------------------------- through the agent
def test_the_agent_audits_every_return_it_computes():
    from taxvault.agent import run_agent, sample_bundle
    from taxvault.agent.sandbox import SCENARIOS

    for key in SCENARIOS:
        case, documents = sample_bundle(key, year=2026)
        run = run_agent(documents, tax_year=2026, taxpayer_name=case.taxpayer_name,
                        filing_status=case.filing_status,
                        resident_state=case.resident_state,
                        situation=dict(case.situation))
        step = next((s for s in run.steps if s.name == "audit"), None)
        assert step is not None, f"{key} ran no audit"
        assert step.status == "ok", f"{key}: {step.detail}"
        assert run.audit and run.audit["clean"], f"{key}: {run.audit}"


def test_a_planning_run_is_audited_as_filed_not_as_planned():
    """A planning run deliberately moves wages into a 401(k), so its figures
    are a projection and do not tie to the W-2 -- they are not meant to."""
    from taxvault.agent import run_agent, sample_bundle

    case, documents = sample_bundle("simple", year=2026)
    run = run_agent(documents, tax_year=2026, taxpayer_name=case.taxpayer_name,
                    filing_status=case.filing_status,
                    resident_state=case.resident_state,
                    situation=dict(case.situation))
    assert run.audit["clean"], run.audit
    assert any("as filed, not as planned" in c["name"] for c in run.audit["checks"])


def test_a_return_that_does_not_tie_blocks_the_agent():
    """A failure here is a blocker, not a note: if the return does not tie to
    the forms it came from, nothing else about it is worth discussing."""
    from taxvault.agent.pipeline import AgentRun, _audit

    estimate = _return()
    estimate.federal.total_payments = D("9000")
    estimate.federal.balance = estimate.federal.total_tax - D("9000")

    run = AgentRun(tax_year=2025)
    run._result = estimate
    _audit(run, {"w2": [_w2()], "1099_r": []}, TaxProfile(tax_year=2025))

    step = next(s for s in run.steps if s.name == "audit")
    assert step.status == "blocked"
    assert run.blockers, "a return that does not tie did not block"
    assert "does not tie" in run.blockers[0].question
