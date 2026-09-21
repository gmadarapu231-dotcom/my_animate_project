"""Paying the tax: where the client is sent, and what this system refuses to do.

The system never takes a tax payment. A federal payment belongs on the IRS's
own channel and a state payment on the state's, so the last step is a handoff
carrying the exact values to enter. The two fields people get wrong on Direct
Pay are the reason for payment and the tax period, and a 2025 balance posted
to 2026 sits unapplied while the real balance accrues interest.

These tests also pin the refusals. Zelle is not an IRS payment channel, and a
product that quietly omits it leaves the client to guess; one that names it and
says why is protecting them from the commonest IRS-impersonation scam there is.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

import pytest

from taxvault.engines.handoff import (
    PURPOSE_BALANCE,
    PURPOSE_ESTIMATED,
    federal_handoff,
    refused_methods,
    scam_warning,
    service_fee_options,
    state_handoff,
)
from tests.test_tax_journey import W2_2025, auth, sign_in, verify_identity


# ---------------------------------------------------------------------------
# where the client is sent
# ---------------------------------------------------------------------------
def test_a_federal_balance_goes_to_irs_direct_pay():
    handoff = federal_handoff(3261.14, year=2025, as_of=date(2026, 3, 1))
    assert handoff.destination == "IRS Direct Pay"
    assert urlparse(handoff.url).netloc == "www.irs.gov"
    assert handoff.cost == "Free"


def test_the_direct_pay_entries_are_spelled_out():
    """These four fields are the whole point of the handoff."""
    handoff = federal_handoff(3261.14, year=2025, as_of=date(2026, 3, 1))
    entries = {f.label: f.value for f in handoff.fields}
    assert entries["Reason for Payment"] == "Balance Due"
    assert entries["Apply Payment To"] == "Income Tax - Form 1040"
    assert entries["Tax Period for Payment"] == "2025"
    assert entries["Amount"] == "3,261.14"


def test_the_tax_period_carries_the_warning_that_matters():
    handoff = federal_handoff(1000, year=2025, as_of=date(2026, 3, 1))
    period = next(f for f in handoff.fields if f.label == "Tax Period for Payment")
    assert "not the year you are paying in" in period.note


def test_an_estimated_payment_is_applied_to_1040es_not_the_return():
    handoff = federal_handoff(4700, year=2025, purpose=PURPOSE_ESTIMATED)
    entries = {f.label: f.value for f in handoff.fields}
    assert entries["Reason for Payment"] == "Estimated Tax"
    assert "1040ES" in entries["Apply Payment To"]


def test_a_late_balance_is_told_it_is_late():
    handoff = federal_handoff(5000, year=2025, as_of=date(2026, 11, 1))
    assert any("deadline" in w for w in handoff.warnings)
    assert any("reduces both" in w for w in handoff.warnings)


def test_paying_by_card_says_the_fee_buys_nothing():
    handoff = federal_handoff(5000, year=2025, method="card", as_of=date(2026, 3, 1))
    assert "card" in handoff.destination.lower()
    assert any("buys nothing" in w for w in handoff.warnings)


def test_every_irs_link_is_on_an_irs_domain():
    """A payment link that leaves irs.gov is how people get robbed."""
    handoff = federal_handoff(1000, year=2025)
    urls = [handoff.url] + [a["url"] for a in handoff.alternatives]
    for url in urls:
        assert urlparse(url).netloc in ("www.irs.gov", "www.eftps.gov"), url
        assert urlparse(url).scheme == "https"


# ---------------------------------------------------------------------------
# states
# ---------------------------------------------------------------------------
def test_a_state_balance_goes_to_that_state_not_the_irs():
    handoff = state_handoff(1147.08, code="CA", year=2025)
    assert handoff.destination == "California Franchise Tax Board"
    assert "ftb.ca.gov" in handoff.url
    assert any("does not pay your state" in w for w in handoff.warnings)


def test_every_taxing_state_has_somewhere_to_send_people():
    """All 42 taxing jurisdictions, not a sample: a missing one is a dead end."""
    from taxvault.config import states

    params = states(2025)
    for code in params.taxing_states():
        handoff = state_handoff(500, code=code, year=2025)
        assert handoff.url.startswith("https://"), f"{code} has no payment address"
        assert handoff.destination, code


def test_paying_a_no_tax_state_questions_itself():
    handoff = state_handoff(500, code="TX", year=2025)
    assert any("no income tax" in w for w in handoff.warnings)
    # Nothing to pay means nowhere to send them, and that is said rather than
    # rendered as a link to nowhere.
    assert handoff.url == ""
    assert any("revenue site" in w for w in handoff.warnings)


# ---------------------------------------------------------------------------
# what this system will not do
# ---------------------------------------------------------------------------
def test_zelle_is_named_as_refused_rather_than_left_out():
    refused = {r["method"]: r for r in refused_methods()}
    assert "zelle" in refused
    assert "does not accept Zelle" in refused["zelle"]["reason"]
    assert refused["zelle"]["instead"] == "irs_direct_pay"


@pytest.mark.parametrize("method", ["zelle", "venmo_cashapp_paypal", "cryptocurrency", "gift_card"])
def test_each_refusal_says_what_to_use_instead(method):
    refused = {r["method"]: r for r in refused_methods()}
    assert refused[method]["reason"] and refused[method]["instead"]


def test_the_scam_warning_names_the_actual_tactics():
    warning = scam_warning()
    for tactic in ("Zelle", "gift card", "never"):
        assert tactic.lower() in warning["body"].lower()
    assert urlparse(warning["url"]).netloc == "www.irs.gov"


def test_zelle_is_offered_for_the_preparation_fee():
    """It works for the fee. It is only the tax it cannot pay."""
    options = {o["method"]: o for o in service_fee_options()}
    assert "zelle" in options
    assert options["zelle"]["irreversible"] is True
    assert "cannot be recalled" in options["zelle"]["note"]


def test_the_recommended_fee_method_is_the_reversible_one():
    recommended = [o for o in service_fee_options() if o["recommended"]]
    assert len(recommended) == 1
    assert recommended[0]["irreversible"] is False


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def _owing(client):
    """A signed-in client with a balance to pay."""
    token = verify_identity(client, sign_in(client))
    client.post("/api/documents/w2/boxes", headers=auth(token),
                json={**W2_2025, "box2_federal_withheld": 4000})
    estimate = client.post("/api/estimates", headers=auth(token), json={
        "tax_year": 2025, "method": "regular",
        "situation": {"filing_status": "single", "resident_state": "CA"},
    }).json()
    assert float(estimate["totals"]["total_balance"]) > 0
    return token, estimate


def test_the_handoff_endpoint_returns_a_destination_and_the_entries(client):
    token, estimate = _owing(client)
    body = client.get("/api/payments/handoff", headers=auth(token), params={
        "amount": abs(float(estimate["federal"]["balance"])),
        "tax_year": 2025, "jurisdiction": "federal", "method": "irs_direct_pay",
    }).json()
    assert body["destination"] == "IRS Direct Pay"
    assert "irs.gov" in body["url"]
    assert len(body["fields"]) == 4
    assert body["not_accepted"], "the refusals belong in the response"
    assert "never" in body["disclaimer"]


def test_the_state_handoff_needs_a_state(client):
    token, _ = _owing(client)
    response = client.get("/api/payments/handoff", headers=auth(token),
                          params={"amount": 100, "jurisdiction": "state"})
    assert response.status_code == 400


def test_the_service_fee_endpoint_offers_zelle(client):
    token, _ = _owing(client)
    body = client.get("/api/payments/service-fee", headers=auth(token)).json()
    assert "zelle" in {o["method"] for o in body["options"]}
    assert "does not accept Zelle" in body["note"]


def test_a_payment_can_be_recorded_with_its_confirmation(client):
    token, estimate = _owing(client)
    response = client.post("/api/payments/record", headers=auth(token), json={
        "estimate_id": estimate["estimate_id"],
        "jurisdiction": "federal", "method": "irs_direct_pay",
        "amount": 8409.00, "confirmation_number": "IRS-2026-DP-884213",
        "paid_on": "2026-09-21",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recorded"] is True
    assert body["confirmation_number"] == "IRS-2026-DP-884213"
    assert "only evidence" in body["note"]


def test_recording_without_a_confirmation_number_says_so(client):
    token, estimate = _owing(client)
    body = client.post("/api/payments/record", headers=auth(token), json={
        "estimate_id": estimate["estimate_id"], "amount": 100,
    }).json()
    assert body["confirmation_number"] is None
    assert "no proof" in body["note"]


def test_a_zero_payment_is_refused(client):
    token, estimate = _owing(client)
    response = client.post("/api/payments/record", headers=auth(token), json={
        "estimate_id": estimate["estimate_id"], "amount": 0,
    })
    assert response.status_code == 400


def test_a_payment_cannot_be_recorded_against_another_account(client):
    token, estimate = _owing(client)
    other = verify_identity(
        client, sign_in(client, "other@example.com"),
        ssn="987-65-4320", email="other@example.com", mobile="4155550199",
    )
    response = client.post("/api/payments/record", headers=auth(other), json={
        "estimate_id": estimate["estimate_id"], "amount": 100,
    })
    assert response.status_code == 404


def test_recording_a_payment_is_audited(client, tax_db):
    from sqlalchemy import select

    from taxvault.db.models import AuditEvent

    token, estimate = _owing(client)
    client.post("/api/payments/record", headers=auth(token), json={
        "estimate_id": estimate["estimate_id"], "amount": 500,
        "confirmation_number": "ABC123",
    })
    events = tax_db.scalars(
        select(AuditEvent).where(AuditEvent.action == "payment_recorded")
    ).all()
    assert events and events[-1].detail["has_confirmation"] is True
