"""Texas, and the eight other states that levy no income tax.

A filer in one of these states is not a filer with a missing state return.
They have no state income tax to deduct, which changes what Schedule A can
carry, and their W-2 has blank state boxes, which is correct rather than
suspicious. Both of those were wrong before these tests existed.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from taxvault.config import states
from taxvault.engines.estimate import run_estimate
from taxvault.engines.federal import TaxProfile
from taxvault.forms.w2 import W2

pytest_plugins = ["tests.conftest_taxvault"]

NO_TAX = ["AK", "FL", "NH", "NV", "SD", "TN", "TX", "WA", "WY"]


def texan(**kwargs) -> TaxProfile:
    base = dict(tax_year=2025, filing_status="married_jointly", resident_state="TX",
                wages=D(180000), federal_withheld=D(24000))
    base.update(kwargs)
    return TaxProfile(**base)


# ------------------------------------------------------------ the state itself
def test_texas_levies_nothing_on_wages():
    result = run_estimate(texan(), method="regular")
    texas = next(s for s in result.states if s.code == "TX")
    assert texas.total_tax == D(0)
    assert texas.balance == D(0)
    assert result.state_tax == D(0)


def test_the_whole_bill_is_federal_in_texas():
    result = run_estimate(texan(), method="regular")
    assert result.total_balance == result.federal.balance


def test_every_no_tax_state_is_still_known_to_the_engine():
    """A state with no income tax still needs a name, a note and a filing
    answer. Being absent from the table is how "unknown state" errors reach
    a client who simply lives in Florida."""
    table = states(2025)
    assert sorted(table.no_tax_states()) == NO_TAX
    for code in NO_TAX:
        entry = table.get(code)
        assert entry["type"] == "none"
        assert entry["name"] and entry["note"]


def test_washington_still_taxes_a_large_long_term_gain():
    """"No income tax" is not "no tax". Washington's 7% excise on long-term
    gains above its own standard deduction is real money and has caught
    people who moved there for the wage treatment."""
    profile = TaxProfile(tax_year=2025, filing_status="single", resident_state="WA",
                         wages=D(120000), federal_withheld=D(18000),
                         long_term_gains=D(600000))
    result = run_estimate(profile, method="regular")
    washington = next(s for s in result.states if s.code == "WA")
    assert washington.total_tax > D(0), "the capital gains excise was not charged"


# ------------------------------------------------------------- the W-2 boxes
def test_a_texas_w2_with_blank_state_boxes_raises_no_warning():
    """Boxes 16 and 17 are blank on every W-2 issued in these states. Warning
    about it told nine states' worth of clients to check a correct form,
    which is how people learn to ignore warnings."""
    form = W2.from_dict({
        "tax_year": 2025, "employer_name": "Lone Star Logistics",
        "employer_ein": "74-1234567", "employee_first_name": "Dana",
        "employee_last_name": "Reed", "box1_wages": 93500,
        "box2_federal_withheld": 11200, "box3_social_security_wages": 93500,
        "box5_medicare_wages": 93500,
        "states": [{"state": "TX", "state_wages": 0, "state_withheld": 0}],
    })
    box16 = [f for f in form.validate() if f["box"] == "16"]
    assert box16 == [], box16


def test_a_california_w2_with_blank_state_boxes_still_warns():
    """The suppression must be about the state, not about the zero."""
    form = W2.from_dict({
        "tax_year": 2025, "employer_name": "Acme", "employer_ein": "12-3456789",
        "employee_first_name": "Dana", "employee_last_name": "Reed",
        "box1_wages": 93500, "box2_federal_withheld": 11200,
        "box3_social_security_wages": 93500, "box5_medicare_wages": 93500,
        "states": [{"state": "CA", "state_wages": 0, "state_withheld": 0}],
    })
    box16 = [f for f in form.validate() if f["box"] == "16"]
    assert len(box16) == 1
    assert box16[0]["severity"] == "warning"


# ------------------------------------------------- the sales-tax election
def test_a_texan_may_deduct_sales_tax():
    """IRC 164(b)(5). With no income tax to deduct it is the only SALT a
    Texas filer has besides property tax, and taking it as zero hands them a
    smaller deduction than the law allows."""
    without = run_estimate(texan(property_tax=D(9800), mortgage_interest=D(21000),
                                 charitable_cash=D(4000)), method="regular")
    with_sales = run_estimate(texan(property_tax=D(9800), mortgage_interest=D(21000),
                                    charitable_cash=D(4000),
                                    state_local_sales_tax=D(3100)), method="regular")
    assert with_sales.federal.itemised_deduction > without.federal.itemised_deduction
    assert (with_sales.federal.itemised_deduction
            - without.federal.itemised_deduction) == D(3100)
    assert with_sales.federal.total_tax < without.federal.total_tax


def test_income_tax_and_sales_tax_are_never_both_deducted():
    """The statute is an election between the two, not a sum."""
    both = run_estimate(
        TaxProfile(tax_year=2025, filing_status="married_jointly", resident_state="CA",
                   wages=D(180000), federal_withheld=D(24000),
                   state_local_income_tax=D(9000), state_local_sales_tax=D(3100),
                   property_tax=D(8000), mortgage_interest=D(21000)),
        method="regular")
    only_income = run_estimate(
        TaxProfile(tax_year=2025, filing_status="married_jointly", resident_state="CA",
                   wages=D(180000), federal_withheld=D(24000),
                   state_local_income_tax=D(9000),
                   property_tax=D(8000), mortgage_interest=D(21000)),
        method="regular")
    assert both.federal.itemised_deduction == only_income.federal.itemised_deduction


def test_the_larger_of_the_two_is_taken():
    profile = dict(tax_year=2025, filing_status="married_jointly", resident_state="CA",
                   wages=D(180000), federal_withheld=D(24000),
                   property_tax=D(8000), mortgage_interest=D(21000))
    income_bigger = run_estimate(
        TaxProfile(**profile, state_local_income_tax=D(9000),
                   state_local_sales_tax=D(3100)), method="regular")
    sales_bigger = run_estimate(
        TaxProfile(**profile, state_local_income_tax=D(3100),
                   state_local_sales_tax=D(9000)), method="regular")
    assert (income_bigger.federal.itemised_deduction
            == sales_bigger.federal.itemised_deduction)


def test_the_return_says_which_election_was_made():
    result = run_estimate(texan(property_tax=D(9800), mortgage_interest=D(21000),
                                state_local_sales_tax=D(3100)), method="regular")
    assert any("sales tax" in note for note in result.federal.notes), result.federal.notes


def test_sales_tax_is_added_back_for_amt_like_any_other_salt():
    """Whatever SALT was deducted comes back for AMT income.

    Reading only the income-tax field would under-state AMT income for a
    Texas filer by the whole sales-tax deduction, so the add-back is tested
    where it lives rather than through an income level where the SALT cap
    makes the two indistinguishable anyway.
    """
    from taxvault.config import federal as federal_params
    from taxvault.engines.federal import _amt

    params = federal_params(2025)
    shared = dict(tax_year=2025, filing_status="married_jointly",
                  wages=D(700000), federal_withheld=D(200000),
                  property_tax=D(9000), mortgage_interest=D(30000))

    as_income = run_estimate(TaxProfile(**shared, resident_state="CA",
                                        state_local_income_tax=D(18000)),
                             method="regular")
    as_sales = run_estimate(TaxProfile(**shared, resident_state="TX",
                                       state_local_sales_tax=D(18000)),
                            method="regular")
    assert as_income.federal.deduction_kind == "itemised"

    # Same SALT, declared two ways: the AMT add-back must not care which.
    income_amt = _amt(TaxProfile(**shared, resident_state="CA",
                                 state_local_income_tax=D(18000)),
                      params, as_income.federal, as_income.federal.agi)
    sales_amt = _amt(TaxProfile(**shared, resident_state="TX",
                                state_local_sales_tax=D(18000)),
                     params, as_sales.federal, as_sales.federal.agi)
    assert income_amt == sales_amt

    # And it must not be silently treated as nothing.
    ignored = _amt(TaxProfile(**shared, resident_state="TX"),
                   params, as_sales.federal, as_sales.federal.agi)
    assert sales_amt <= ignored, (
        "adding SALT back raises AMT income, so ignoring the sales tax can "
        "only ever understate it"
    )


# --------------------------------------------------------------- the agent
def test_the_agent_asks_a_texan_about_sales_tax():
    """Nothing on any form reports sales tax, so it is a question or it is
    nothing."""
    from taxvault.agent import Document, run_agent
    from taxvault.agent.sandbox import w2_pdf

    pdf = w2_pdf(employer="Lone Star Logistics", ein="74-1234567", first="Dana",
                 last="Reed", year=2025, wages=240000, withheld=38000,
                 state="TX", state_wages=0, state_withheld=0)
    run = run_agent(
        [Document(filename="w2.pdf", content_type="application/pdf", blob=pdf)],
        tax_year=2025, taxpayer_name="Dana Reed", filing_status="married_jointly",
        resident_state="TX",
        situation={"property_tax": 14000, "mortgage_interest": 26000,
                   "charitable_cash": 5000},
    )
    asked = [item for item in run.review if item.field == "state_local_sales_tax"]
    assert asked, [item.question for item in run.review]
    assert "TX" in asked[0].why


def test_the_agent_does_not_ask_a_californian_about_sales_tax():
    """California takes income tax, so the income-tax deduction is the one
    that applies and the question would be noise."""
    from taxvault.agent import Document, run_agent
    from taxvault.agent.sandbox import w2_pdf

    pdf = w2_pdf(employer="Acme", ein="12-3456789", first="Dana", last="Reed",
                 year=2025, wages=240000, withheld=38000, state="CA",
                 state_wages=240000, state_withheld=19000)
    run = run_agent(
        [Document(filename="w2.pdf", content_type="application/pdf", blob=pdf)],
        tax_year=2025, taxpayer_name="Dana Reed", filing_status="married_jointly",
        resident_state="CA",
        situation={"property_tax": 14000, "mortgage_interest": 26000},
    )
    assert not [i for i in run.review if i.field == "state_local_sales_tax"]


def test_the_question_is_not_asked_when_it_could_not_matter():
    """A Texas filer nowhere near itemising does not need to go hunting for
    receipts to change nothing."""
    from taxvault.agent import Document, run_agent
    from taxvault.agent.sandbox import w2_pdf

    pdf = w2_pdf(employer="Lone Star Logistics", ein="74-1234567", first="Dana",
                 last="Reed", year=2025, wages=42000, withheld=3200,
                 state="TX", state_wages=0, state_withheld=0)
    run = run_agent(
        [Document(filename="w2.pdf", content_type="application/pdf", blob=pdf)],
        tax_year=2025, taxpayer_name="Dana Reed", filing_status="single",
        resident_state="TX",
    )
    assert not [i for i in run.review if i.field == "state_local_sales_tax"]


# --------------------------------------------------------- through the API
def test_a_texan_gets_an_estimate_and_a_document(client):
    started = client.post("/api/auth/sign-in", json={"email": "tex@example.com"})
    token = client.post("/api/auth/verify", json={
        "email": "tex@example.com",
        "code": started.json()["development_code"]}).json()["token"]
    head = {"Authorization": f"Bearer {token}"}
    sent = client.post("/api/auth/mobile/start", json={"mobile": "2145550117"},
                       headers=head)
    client.post("/api/auth/mobile/verify",
                json={"code": sent.json()["development_code"]}, headers=head)
    token = client.post("/api/auth/identity", headers=head, json={
        "ssn": "123-45-6789", "email": "tex@example.com", "mobile": "2145550117",
        "first_name": "Dana", "last_name": "Reed", "resident_state": "TX",
    }).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    added = client.post("/api/documents/w2/boxes", headers=auth, json={
        "tax_year": 2025, "employer_name": "Lone Star Logistics",
        "employer_ein": "74-1234567", "employee_first_name": "Dana",
        "employee_last_name": "Reed", "box1_wages": 93500,
        "box2_federal_withheld": 11200, "box3_social_security_wages": 93500,
        "box5_medicare_wages": 93500,
        "states": [{"state": "TX", "state_wages": 0, "state_withheld": 0}],
    })
    assert added.status_code == 200, added.text
    assert not [w for w in added.json().get("warnings", []) if w.get("box") == "16"]

    run = client.post("/api/estimates", headers=auth, json={
        "tax_year": 2025, "method": "regular",
        "situation": {"state_local_sales_tax": 2400, "property_tax": 9800},
    })
    assert run.status_code == 200, run.text
    estimate = run.json()
    assert estimate["totals"]["state_tax"] == "0.00"

    document = client.get(
        f"/api/estimates/{estimate['estimate_id']}/document?format=pdf", headers=auth)
    assert document.status_code == 200
    assert document.content[:5] == b"%PDF-"


# ------------------------------------------------------------- the document
def _document_for(profile):
    from taxvault.reports.estimate import build_estimate_document

    result = run_estimate(profile, method="regular")
    return build_estimate_document(result.to_dict(), client_name="Dana Reed")


def test_the_document_says_texas_takes_nothing_instead_of_printing_zeros():
    """Four rows of $0.00 under a heading tell a client nothing. One
    sentence does."""
    doc = _document_for(texan(property_tax=D(11400), mortgage_interest=D(19800)))
    state = next(s for s in doc.sections if s.title == "State tax")
    assert len(state.rows) == 1
    assert "no state income tax" in state.rows[0].label.lower()
    assert state.rows[0].note


def test_the_bottom_line_does_not_repeat_the_zeros():
    doc = _document_for(texan(property_tax=D(11400), mortgage_interest=D(19800)))
    bottom = next(s for s in doc.sections if s.title == "Where this leaves you")
    assert not [r for r in bottom.rows if "state" in r.label.lower()]
    assert "no state income tax where you live" in bottom.blurb


def test_washington_with_an_excise_bill_still_gets_the_full_table():
    """"No income tax" is not "nothing to report". A filer who owes
    Washington's capital gains excise needs the figures, not a reassuring
    sentence."""
    doc = _document_for(TaxProfile(
        tax_year=2025, filing_status="single", resident_state="WA",
        wages=D(120000), federal_withheld=D(18000), long_term_gains=D(600000)))
    state = next(s for s in doc.sections if s.title == "State tax")
    assert len(state.rows) > 1
    assert any("washington" in (r.label or "").lower() for r in state.rows)
    bottom = next(s for s in doc.sections if s.title == "Where this leaves you")
    assert [r for r in bottom.rows if "state" in r.label.lower()]
