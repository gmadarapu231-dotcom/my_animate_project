"""The estimate as a document the client takes away.

Until this existed the estimate was a screen and nothing else. These tests
hold the two things that make it a deliverable rather than a printout: it says
what it is on every page, and it says the same thing next year as it said
today.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from taxvault.reports.estimate import (
    DISCLAIMER,
    build_estimate_document,
    render_html,
    render_pdf,
)

pytest_plugins = ["tests.conftest_taxvault"]


PAYLOAD = {
    "tax_year": 2025,
    "method": "regular",
    "filing_status": "married_jointly",
    "resident_state": "CA",
    "headline": "Estimated balance of 1,293 to pay",
    "federal": {
        "lines": [
            {"form": "1040", "label": "Wages, salaries, tips (Box 1)", "amount": "93500.00"},
            {"form": "1040", "label": "Tax-exempt interest", "amount": "0.00"},
            {"form": "Sch D", "label": "Net short-term gain or loss", "amount": "0.00"},
            {"form": "1040", "label": "Total income", "amount": "93500.00"},
            {"form": "1040", "label": "Standard deduction", "amount": "-15750.00"},
            {"form": "1040", "label": "Amount owed", "amount": "819.00"},
        ],
        "total_income": "93500.00",
        "agi": "93500.00",
        "adjustments": "0.00",
        "deduction_kind": "standard",
        "deduction_taken": "15750.00",
        "standard_deduction": "15750.00",
        "itemised_deduction": "0.00",
        "taxable_income": "77750.00",
        "ordinary_tax": "12019.00",
        "total_tax": "12019.00",
        "total_payments": "11200.00",
        "effective_rate": 0.1285,
        "marginal_rate": 0.22,
        "notes": ["Computed on 2025 law."],
        "warnings": [],
    },
    "states": [{
        "code": "CA", "name": "California", "total_tax": "4573.64",
        "withheld": "4100.00", "balance": "473.64", "credits": "149",
        "lines": [{"label": "State wages / income", "amount": "93500.00"},
                  {"label": "Exemption credit", "amount": "-149.00"}],
        "notes": [],
    }],
    "totals": {
        "federal_tax": "12019.00", "state_tax": "4573.64",
        "federal_balance": "819.00", "state_balance": "473.64",
        "total_balance": "1292.64",
    },
    "strategies": [], "applied": [],
}


def _pdf_text(blob: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(blob)).pages)


def document(**kwargs):
    return build_estimate_document(PAYLOAD, client_name="Dana Reed",
                                   ssn_last4="6789", estimate_id=7, **kwargs)


# ---------------------------------------------------------------- content
def test_the_headline_is_the_figure_the_client_came_for():
    doc = document()
    assert doc.headline_label == "Estimated balance to pay"
    assert doc.headline_amount == "$1,292.64"
    assert doc.headline_is_refund is False


def test_a_refund_is_labelled_as_one_and_not_as_a_negative_balance():
    payload = {**PAYLOAD, "totals": {**PAYLOAD["totals"], "total_balance": "-2400.00"}}
    doc = build_estimate_document(payload, client_name="Dana Reed")
    assert doc.headline_label == "Estimated refund"
    assert doc.headline_amount == "$2,400.00"
    assert doc.headline_is_refund is True


def test_the_income_section_stops_before_the_computation():
    """`federal.lines` is the whole 1040 spine. Rendering all of it under
    "Income" printed the deduction and the balance twice more further down."""
    doc = document()
    income = next(s for s in doc.sections if s.title == "Income")
    labels = [row.label for row in income.rows]
    assert "Wages, salaries, tips (Box 1)" in labels
    assert "Standard deduction" not in labels
    assert "Amount owed" not in labels


def test_a_kind_of_income_the_client_does_not_have_is_left_out():
    doc = document()
    income = next(s for s in doc.sections if s.title == "Income")
    assert "Tax-exempt interest" not in [row.label for row in income.rows]


def test_a_subtraction_never_renders_as_a_dollar_sign_followed_by_a_minus():
    doc = document()
    everything = [row.amount or "" for section in doc.sections for row in section.rows]
    assert not [a for a in everything if a.startswith("$-")]


def test_nothing_is_taken_away_at_minus_zero():
    """"−$0.00" is not a quantity, and it makes a reader stop and squint."""
    payload = {**PAYLOAD,
               "federal": {**PAYLOAD["federal"], "total_payments": "0.00"}}
    doc = build_estimate_document(payload, client_name="Dana Reed")
    everything = [row.amount or "" for section in doc.sections for row in section.rows]
    assert "−$0.00" not in everything


def test_the_state_credit_is_listed_once():
    """The state's own line list already carries its credits; adding the
    scalar field on top listed the exemption credit twice."""
    doc = document()
    state = next(s for s in doc.sections if s.title == "State tax")
    assert sum("credit" in row.label.lower() for row in state.rows) == 1


def test_the_filing_status_is_spelled_out():
    assert document().filing_status == "Married filing jointly"


def test_the_ssn_is_masked_and_stays_ascii():
    """A bullet has no glyph in reportlab's standard fonts, so a prettier
    mask rendered as "--6789" on the one copy the client keeps."""
    doc = document()
    assert doc.ssn_masked == "SSN ending 6789"
    assert doc.ssn_masked.isascii()
    assert "123" not in doc.ssn_masked


# ---------------------------------------------------------------- renderers
def test_the_html_is_a_whole_page_and_escapes_what_it_is_given():
    doc = build_estimate_document(PAYLOAD, client_name='Dana "quote" <script>')
    page = render_html(doc)
    assert page.startswith("<!doctype html>")
    assert "<script>" not in page.split("</head>", 1)[1]
    assert "&lt;script&gt;" in page


def test_both_renderers_carry_the_same_figures():
    doc = document()
    page, text = render_html(doc), _pdf_text(render_pdf(doc))
    for figure in ("$93,500.00", "$77,750.00", "$12,019.00", "$1,292.64"):
        assert figure in page, figure
        assert figure in text, figure


def test_every_page_says_it_is_not_a_filed_return():
    """A client who files this away believing it was sent does not pay."""
    doc = document()
    reader = PdfReader(io.BytesIO(render_pdf(doc)))
    for page in reader.pages:
        assert "not a filed tax return" in page.extract_text()
    assert DISCLAIMER in render_html(doc)


def test_the_filename_says_the_year_and_who_it_is_for():
    assert document().filename() == "2025-estimate-DanaReed.pdf"


# ---------------------------------------------------------------- the API
def _sign_in(client, email="dana@example.com", *, ssn="123-45-6789",
             mobile="4155550132", first="Dana", last="Reed"):
    """A verified taxpayer. Two of these need two identities: the same SSN on
    two accounts is refused, which is correct and not what these tests are
    about."""
    started = client.post("/api/auth/sign-in", json={"email": email})
    token = client.post("/api/auth/verify", json={
        "email": email, "code": started.json()["development_code"]}).json()["token"]
    head = {"Authorization": f"Bearer {token}"}
    sent = client.post("/api/auth/mobile/start", json={"mobile": mobile}, headers=head)
    client.post("/api/auth/mobile/verify",
                json={"code": sent.json()["development_code"]}, headers=head)
    done = client.post("/api/auth/identity", headers=head, json={
        "ssn": ssn, "email": email, "mobile": mobile,
        "first_name": first, "last_name": last, "resident_state": "CA",
    })
    assert done.status_code == 200, done.text
    return {"Authorization": f"Bearer {done.json()['token']}"}


@pytest.fixture
def saved_estimate(client):
    auth = _sign_in(client)
    added = client.post("/api/documents/w2/boxes", headers=auth, json={
        "tax_year": 2025, "employer_name": "Acme Corporation",
        "employee_first_name": "Dana", "employee_last_name": "Reed",
        "employer_ein": "12-3456789",
        "box1_wages": 93500, "box2_federal_withheld": 11200,
        "box3_social_security_wages": 93500, "box5_medicare_wages": 93500,
        "states": [{"state": "CA", "state_wages": 93500, "state_withheld": 4100}],
    })
    assert added.status_code == 200, added.text
    run = client.post("/api/estimates", headers=auth,
                      json={"tax_year": 2025, "method": "regular"})
    assert run.status_code == 200, run.text
    return auth, run.json()


def test_an_estimate_can_be_downloaded_as_a_pdf(saved_estimate):
    auth, estimate = saved_estimate
    response = client_get(auth, estimate, "pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"
    assert "attachment" in response.headers["content-disposition"]
    assert "2025-estimate-DanaReed.pdf" in response.headers["content-disposition"]


def test_the_html_form_can_be_opened_inline(saved_estimate):
    auth, estimate = saved_estimate
    response = client_get(auth, estimate, "html", inline=True)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"].startswith("inline")
    assert "Estimated tax summary" in response.text


def test_a_tax_document_is_never_left_in_a_shared_cache(saved_estimate):
    auth, estimate = saved_estimate
    response = client_get(auth, estimate, "pdf")
    assert "no-store" in response.headers["cache-control"]


def test_the_document_shows_what_the_client_was_told_not_a_fresh_run(
        client, saved_estimate):
    """The point of a document is that it does not move. Changing the inputs
    afterwards must not change paper already handed over."""
    auth, estimate = saved_estimate
    before = _pdf_text(client_get(auth, estimate, "pdf").content)
    assert "$93,500.00" in before

    # A second W-2 turns up. The old estimate's document must not absorb it.
    client.post("/api/documents/w2/boxes", headers=auth, json={
        "tax_year": 2025, "employer_name": "Second Job Inc",
        "employee_first_name": "Dana", "employee_last_name": "Reed",
        "employer_ein": "98-7654321",
        "box1_wages": 40000, "box2_federal_withheld": 5000,
        "box3_social_security_wages": 40000, "box5_medicare_wages": 40000,
    })
    after = _pdf_text(client_get(auth, estimate, "pdf").content)
    assert after == before
    assert "$133,500.00" not in after


def test_somebody_elses_estimate_is_not_downloadable(client, saved_estimate):
    _, estimate = saved_estimate
    intruder = _sign_in(client, email="mallory@example.com", ssn="987-65-4321",
                        mobile="4155550199", first="Mallory", last="Quinn")
    response = client.get(f"/api/estimates/{estimate['estimate_id']}/document",
                          headers=intruder)
    assert response.status_code == 404
    assert "Dana" not in response.text


def test_an_unknown_format_is_refused_rather_than_guessed(saved_estimate):
    auth, estimate = saved_estimate
    response = client_get(auth, estimate, "docx")
    assert response.status_code == 422


# A tiny indirection so each test above reads as one line.
_CLIENT = {}


@pytest.fixture(autouse=True)
def _capture_client(client):
    _CLIENT["c"] = client
    yield
    _CLIENT.clear()


def client_get(auth, estimate, fmt, inline=False):
    query = f"?format={fmt}" + ("&disposition=inline" if inline else "")
    return _CLIENT["c"].get(
        f"/api/estimates/{estimate['estimate_id']}/document{query}", headers=auth)
