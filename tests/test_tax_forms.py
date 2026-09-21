"""The 1099 family and Form 1098: parsing, upload and autofill."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.forms.f1098 import parse_1098_text
from taxvault.forms.f1099 import (
    detect_form_kind,
    kinds_present,
    parse_1099b_text,
    parse_1099div_text,
    parse_1099r_text,
)


def D(value):
    return Decimal(str(value))


CONSOLIDATED = """2026 Consolidated Form 1099
Meridian Brokerage LLC

Form 1099-DIV
1a Total ordinary dividends                    6,480.00
1b Qualified dividends                         5,905.00
2a Total capital gain distributions            1,240.00
2b Unrecap. Sec. 1250 gain                         0.00
4 Federal income tax withheld                      0.00
7 Foreign tax paid                                86.00

Form 1099-B  Proceeds From Broker and Barter Exchange Transactions
Total Short-Term proceeds                     41,300.00
Short-Term cost or other basis                47,900.00
Short-Term gain or loss                       -6,600.00
Total Long-Term proceeds                      88,200.00
Long-Term cost or other basis                 62,050.00
Long-Term gain or loss                        26,150.00
1g Wash sale loss disallowed                     910.00
"""

PENSION = """2026 Form 1099-R
Distributions From Pensions, Annuities, Retirement or Profit-Sharing Plans
PAYER'S name  Harbour Retirement Services
1 Gross distribution                          38,000.00
2a Taxable amount                             38,000.00
2b Taxable amount not determined
4 Federal income tax withheld                  7,600.00
5 Employee contributions                           0.00
7 Distribution code(s)  1
"""

STATEMENT = """Form 1098  Mortgage Interest Statement  2026
RECIPIENT'S/LENDER'S name
Cascade Mutual Bank
1 Mortgage interest received from payer(s)/borrower(s)     18,432.55
2 Outstanding mortgage principal                          612,000.00
3 Mortgage origination date                               03/14/2016
5 Mortgage insurance premiums                               1,860.00
6 Points paid on purchase of principal residence                0.00
9 Number of properties securing the mortgage                       1
"""


# ---------------------------------------------------------------------------
# which form is this
# ---------------------------------------------------------------------------
def test_each_form_is_recognised():
    assert detect_form_kind(PENSION)[0] == "1099_r"
    assert detect_form_kind(STATEMENT)[0] == "1098"


def test_a_consolidated_statement_reports_every_form_inside_it():
    kinds = set(kinds_present(CONSOLIDATED))
    assert "1099_b" in kinds and "1099_div" in kinds


# ---------------------------------------------------------------------------
# 1099-B
# ---------------------------------------------------------------------------
def test_the_section_totals_come_off_a_consolidated_1099b():
    form, confidence, warnings = parse_1099b_text(CONSOLIDATED)
    assert form.short_term_gain == D("-6600.00")
    assert form.long_term_gain == D("26150.00")
    assert form.wash_sale_disallowed == D("910.00")
    assert confidence == 1.0
    assert any("wash-sale" in w for w in warnings)


def test_a_gain_is_derived_from_proceeds_and_basis_when_not_stated():
    text = (
        "Form 1099-B 2026\n"
        "Total Short-Term proceeds  10,000.00\n"
        "Short-Term cost or other basis  7,500.00\n"
    )
    form, _, _ = parse_1099b_text(text)
    assert form.short_term_gain == D("2500.00")


def test_noncovered_positions_are_called_out():
    text = "Form 1099-B 2026\nBasis not reported to the IRS\nTotal Short-Term proceeds 900.00\n"
    form, _, warnings = parse_1099b_text(text)
    assert form.basis_not_reported is True
    assert any("treated as ZERO" in w for w in warnings)


# ---------------------------------------------------------------------------
# 1099-DIV
# ---------------------------------------------------------------------------
def test_the_dividend_boxes_are_read():
    form, confidence, _ = parse_1099div_text(CONSOLIDATED)
    assert form.ordinary_dividends == D("6480.00")
    assert form.qualified_dividends == D("5905.00")
    assert form.capital_gain_distributions == D("1240.00")
    assert form.foreign_tax_paid == D("86.00")
    assert confidence == 1.0


def test_qualified_cannot_exceed_ordinary():
    """Box 1b is a subset of box 1a, and a read that says otherwise is wrong."""
    text = ("Form 1099-DIV 2026\n1a Total ordinary dividends 1,000.00\n"
            "1b Qualified dividends 4,000.00\n")
    form, _, warnings = parse_1099div_text(text)
    assert any("cannot exceed" in w for w in warnings)


def test_a_return_of_capital_is_explained_rather_than_taxed():
    text = ("Form 1099-DIV 2026\n1a Total ordinary dividends 500.00\n"
            "3 Nondividend distributions 1,200.00\n")
    form, _, _ = parse_1099div_text(text)
    findings = {f["box"]: f for f in form.validate()}
    assert "3" in findings
    assert "reduces your cost basis" in findings["3"]["message"]


# ---------------------------------------------------------------------------
# 1099-R
# ---------------------------------------------------------------------------
def test_the_1099r_boxes_and_code_are_read():
    form, confidence, _ = parse_1099r_text(PENSION)
    assert form.gross_distribution == D("38000.00")
    assert form.taxable_amount == D("38000.00")
    assert form.distribution_code == "1"
    assert form.federal_withheld == D("7600.00")
    assert confidence == 1.0


def test_code_one_is_flagged_as_not_settling_the_question():
    _, _, warnings = parse_1099r_text(PENSION)
    assert any("Form 5329" in w for w in warnings)


def test_taxable_amount_not_determined_is_explained():
    form, _, warnings = parse_1099r_text(PENSION)
    assert form.taxable_not_determined is True
    assert any("Form 8606" in w for w in warnings)


def test_a_missing_distribution_code_is_refused_rather_than_guessed():
    form, _, warnings = parse_1099r_text("Form 1099-R 2026\n1 Gross distribution 5,000.00\n")
    assert form.distribution_code == ""
    assert any("cannot be guessed" in w for w in warnings)


def test_net_unrealised_appreciation_is_surfaced():
    text = ("Form 1099-R 2026\n1 Gross distribution 200,000.00\n"
            "6 Net unrealized appreciation 85,000.00\n7 Distribution code(s) 7\n")
    form, _, _ = parse_1099r_text(text)
    messages = " ".join(f["message"] for f in form.validate())
    assert "long-term capital gains rates" in messages


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def _ready(client):
    from tests.test_tax_journey import sign_in, verify_identity

    return verify_identity(client, sign_in(client))


def test_a_pasted_1099r_reaches_the_estimate(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    posted = client.post("/api/documents/form/text", headers=auth(token),
                         json={"text": PENSION, "tax_year": 2026})
    assert posted.status_code == 200, posted.text
    assert posted.json()["kinds"] == ["1099_r"]

    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2026, "situation": {"filing_status": "single", "age": 44},
    })
    assert estimate.status_code == 200, estimate.text
    federal = estimate.json()["federal"]
    # $38,000 of ordinary income, and the 10% additional tax on top.
    assert Decimal(federal["total_income"]) == D("38000.00")
    assert Decimal(federal["early_withdrawal_penalty"]) == D("3800.00")
    assert Decimal(federal["total_payments"]) >= D("7600.00")


def test_a_consolidated_statement_stores_both_forms_and_nets_them(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    posted = client.post("/api/documents/form/text", headers=auth(token),
                         json={"text": CONSOLIDATED, "tax_year": 2026})
    assert posted.status_code == 200, posted.text
    assert set(posted.json()["kinds"]) == {"1099_b", "1099_div"}

    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2026,
        "situation": {"filing_status": "single", "age": 44, "other_income": 60000},
    })
    assert estimate.status_code == 200, estimate.text
    capital = estimate.json()["federal"]["capital"]
    # -6,600 short (+910 wash) against 26,150 long plus a 1,240 distribution.
    assert capital["short_term_net"] == "-5690.00"
    assert capital["long_term_net"] == "27390.00"
    assert capital["ordinary_component"] == "0"
    assert capital["preferential_component"] == "21700.00"


def test_a_1098_reaches_the_deduction(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    posted = client.post("/api/documents/form/text", headers=auth(token),
                         json={"text": STATEMENT, "tax_year": 2026})
    assert posted.status_code == 200, posted.text

    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2026,
        "situation": {"filing_status": "single", "age": 44, "other_income": 200000},
    })
    assert estimate.status_code == 200, estimate.text
    mortgage = estimate.json()["federal"]["mortgage"]
    assert mortgage["deductible_interest"] == "18432.55"
    # AGI is far above the phase-out, so the premiums are worth nothing.
    assert mortgage["mortgage_insurance_deduction"] == "0"


def test_the_same_form_cannot_be_uploaded_twice(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    client.post("/api/documents/form/text", headers=auth(token),
                json={"text": PENSION, "tax_year": 2026})
    again = client.post("/api/documents/form/text", headers=auth(token),
                        json={"text": PENSION, "tax_year": 2026})
    assert again.status_code == 409
    assert "already been uploaded" in again.json()["detail"]


def test_something_that_is_not_a_tax_form_is_refused(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    response = client.post("/api/documents/form/text", headers=auth(token),
                           json={"text": "Dear customer, your statement is ready to view."})
    assert response.status_code == 400
    assert "does not look like" in response.json()["detail"]


# ---------------------------------------------------------------------------
# reference
# ---------------------------------------------------------------------------
def test_the_retirement_reference_lists_the_limits_and_exceptions(client):
    body = client.get("/api/reference/retirement?year=2026").json()
    assert body["limits"]["elective_deferral"] == "24500"
    assert body["limits"]["super_catch_up"] == "11250"
    codes = {e["code"] for e in body["penalty_exceptions"]}
    assert {"rule_of_55", "disability", "sepp", "birth_or_adoption"} <= codes
    # 40 rows of divisors have no business in a UI payload.
    assert "uniform_lifetime_table" not in body["rules"]


def test_the_rmd_endpoint_applies_the_table(client):
    body = client.get("/api/reference/rmd?birth_year=1953&balance=500000&year=2026").json()
    assert body["required"] is True
    assert body["amount"] == "18867.92"


def test_the_home_loan_reference_says_whether_pmi_counts_this_year(client):
    assert client.get("/api/reference/home-loans?year=2025").json()[
        "mortgage_insurance_deductible"] is False
    assert client.get("/api/reference/home-loans?year=2026").json()[
        "mortgage_insurance_deductible"] is True


def test_2026_is_a_supported_year(client):
    assert 2026 in client.get("/api/reference/years").json()["supported"]
