"""The fee, the client's money, and paying their tax for them.

The separation between the practice's money and the client's is the thing
these tests exist to defend. Everything else here is arithmetic; commingling
client funds is what ends practices.
"""

from __future__ import annotations

import csv
import io

import pytest

from taxvault.engines.fees import quote
from tests.test_tax_journey import W2_2025, auth, sign_in, verify_identity


# ---------------------------------------------------------------------------
# what the practice charges
# ---------------------------------------------------------------------------
def test_a_simple_return_costs_the_minimum():
    result = quote(agi=52000, w2_count=1, state_count=1)
    assert result.total >= 30
    assert result.total <= 120


def test_a_complex_return_costs_much_more_but_stays_capped():
    result = quote(agi=2400000, rental_income=300000, state_count=4,
                   self_employment_income=500000, capital_gains=800000,
                   itemised=True, dependents=3, prior_year_returns=2)
    assert result.total <= 2500
    assert result.total > 1000


def test_the_quote_is_always_itemised():
    """A client shown one number cannot tell a fair price from an arbitrary one."""
    result = quote(agi=185000, itemised=True, w2_count=2, state_count=2, dependents=2)
    labels = [line.label for line in result.lines]
    assert any("base" in label for label in labels)
    assert any("additional W-2" in label for label in labels)
    assert any("additional state" in label for label in labels)
    assert any("dependant" in label for label in labels)
    # The lines have to add up to the total.
    assert sum(line.amount for line in result.lines) == result.total


def test_the_income_band_scales_the_base_but_never_the_add_ons():
    """A second W-2 is the same work whoever it belongs to."""
    modest = quote(agi=60000, w2_count=3, itemised=True)
    wealthy = quote(agi=800000, w2_count=3, itemised=True)
    w2_line = lambda q: next(l.amount for l in q.lines if "additional W-2" in l.label)
    assert w2_line(modest) == w2_line(wealthy)
    assert wealthy.total > modest.total


def test_a_very_low_income_simple_return_is_free():
    result = quote(agi=15000, w2_count=1, state_count=1)
    assert result.total == 0
    assert any(line.kind == "discount" for line in result.lines)


def test_eitc_due_diligence_is_charged_because_it_is_required_work():
    with_eitc = quote(agi=38000, dependents=2, claims_eitc=True)
    without = quote(agi=38000, dependents=2, claims_eitc=False)
    assert with_eitc.total > without.total


def test_an_extension_is_free():
    """Delaying an extension costs clients money, so it should not cost them a fee."""
    from taxvault.config import fees

    assert fees()["add_ons"]["extension"]["amount"] == 0


def test_the_fee_is_never_taken_from_a_refund():
    result = quote(agi=90000)
    assert "never deducted from your refund" in " ".join(result.notes)


# ---------------------------------------------------------------------------
# the two buckets
# ---------------------------------------------------------------------------
def _client(client, email="dana@example.com", ssn="123-45-6789", mobile="4155550132"):
    return verify_identity(client, sign_in(client, email), ssn=ssn, email=email, mobile=mobile)


def test_fee_money_and_tax_money_are_separate_ledgers(client):
    """The single most important property in this module."""
    token = _client(client)
    priced = client.post("/api/billing/quote", headers=auth(token),
                         json={"tax_year": 2025}).json()

    client.post("/api/billing/fee/paid", headers=auth(token), json={
        "quote_id": priced["quote_id"], "method": "zelle",
        "amount": float(priced["total"]) or 30.0, "reference": "ZL-8841",
    })
    client.post("/api/billing/funds", headers=auth(token), json={
        "amount": 3261.14, "tax_year": 2025, "method": "zelle", "reference": "ZL-8842",
    })

    ledger = client.get("/api/billing/ledger", headers=auth(token)).json()
    assert float(ledger["held_for_tax"]) == 3261.14
    assert float(ledger["fees_received"]) > 0
    # They are different numbers in different buckets, not one pot.
    assert ledger["held_for_tax"] != ledger["fees_received"]
    buckets = {e["bucket"] for e in ledger["entries"]}
    assert buckets == {"tax", "fee"}


def test_the_ledger_shows_a_running_balance(client):
    token = _client(client)
    for amount in (1000, 500, 761.14):
        client.post("/api/billing/funds", headers=auth(token),
                    json={"amount": amount, "tax_year": 2025})
    ledger = client.get("/api/billing/ledger", headers=auth(token)).json()
    assert float(ledger["held_for_tax"]) == 2261.14
    newest = ledger["entries"][0]
    assert float(newest["balance_after"]) == 2261.14


def test_a_zero_payment_is_refused(client):
    token = _client(client)
    response = client.post("/api/billing/funds", headers=auth(token), json={"amount": 0})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------
def test_nothing_is_authorised_without_the_client_agreeing(client):
    """A default of yes would make the whole record worthless."""
    token = _client(client)
    response = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 3261.14, "tax_year": 2025, "agreed": False,
    })
    assert response.status_code == 400
    assert "agree to it" in response.json()["detail"]


def test_the_authorisation_keeps_what_the_client_agreed_to(client):
    token = _client(client)
    body = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 3261.14, "tax_year": 2025, "agreed": True,
    }).json()
    assert "$3,261.14" in body["statement"]
    assert "the IRS" in body["statement"]
    assert "2025" in body["statement"]
    assert "withdraw this instruction" in body["statement"]


def test_an_unfunded_authorisation_says_what_is_missing(client):
    token = _client(client)
    client.post("/api/billing/funds", headers=auth(token),
                json={"amount": 1000, "tax_year": 2025})
    body = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 3261.14, "tax_year": 2025, "agreed": True,
    }).json()
    assert body["funded"] is False
    assert float(body["shortfall"]) == pytest.approx(2261.14)
    assert "Nothing is paid until" in body["note"]


def test_only_one_live_authorisation_per_year_and_jurisdiction(client):
    token = _client(client)
    first = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 1000, "tax_year": 2025, "agreed": True,
    })
    assert first.status_code == 200
    second = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 2000, "tax_year": 2025, "agreed": True,
    })
    assert second.status_code == 400
    assert "already on file" in second.json()["detail"]


def test_an_instruction_can_be_withdrawn_while_the_money_is_still_here(client):
    token = _client(client)
    created = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 1000, "tax_year": 2025, "agreed": True,
    }).json()
    withdrawn = client.post(
        f"/api/billing/authorizations/{created['id']}/withdraw", headers=auth(token),
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "revoked"

    listing = client.get("/api/billing/authorizations", headers=auth(token)).json()
    assert listing["authorizations"][0]["can_withdraw"] is False


def test_one_client_cannot_withdraw_anothers_instruction(client):
    token = _client(client)
    created = client.post("/api/billing/authorize", headers=auth(token), json={
        "amount": 1000, "tax_year": 2025, "agreed": True,
    }).json()
    other = _client(client, "sam@example.com", "987-65-4320", "4155550199")
    response = client.post(
        f"/api/billing/authorizations/{created['id']}/withdraw", headers=auth(other),
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the practice's side
# ---------------------------------------------------------------------------
def _preparer(client, tax_db):
    """An account with the preparer role."""
    from sqlalchemy import select

    from taxvault.db.models import Account

    token = _client(client, "firm@example.com", "456-78-9012", "4155550111")
    account = tax_db.scalars(
        select(Account).where(Account.email == "firm@example.com")
    ).first()
    account.role = "preparer"
    tax_db.commit()
    return token


def test_a_client_cannot_see_the_practice_wide_queue(client):
    token = _client(client)
    assert client.get("/api/billing/remittance/pending", headers=auth(token)).status_code == 403


def test_the_queue_shows_who_is_funded_and_who_is_not(client, tax_db):
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025})
    client.post("/api/billing/authorize", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025, "agreed": True})

    unfunded = _client(client, "b@example.com", "222-33-4444", "4155550133")
    client.post("/api/billing/authorize", headers=auth(unfunded),
                json={"amount": 900, "tax_year": 2025, "agreed": True})

    queue = client.get("/api/billing/remittance/pending", headers=auth(firm)).json()
    # Keyed by amount, not name: the test fixtures deliberately share a name,
    # and a queue keyed on names would hide exactly that collision.
    states = {row["amount"]: row["funded"] for row in queue["pending"]}
    assert states["2000.00"] is True
    assert states["900.00"] is False
    assert queue["checklist"], "the compliance checklist travels with the queue"


def test_an_unfunded_client_is_skipped_rather_than_fronted(client, tax_db):
    """The practice never lends a client their own tax payment."""
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025})
    client.post("/api/billing/authorize", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025, "agreed": True})

    unfunded = _client(client, "b@example.com", "222-33-4444", "4155550133")
    client.post("/api/billing/authorize", headers=auth(unfunded),
                json={"amount": 900, "tax_year": 2025, "agreed": True})

    batch = client.post("/api/billing/remittance/batch?tax_year=2025",
                        headers=auth(firm)).json()
    assert batch["item_count"] == 1
    assert float(batch["total"]) == 2000.0
    assert len(batch["skipped"]) == 1
    assert "Collect the balance" in batch["skipped"][0]["reason"]


def test_remitting_empties_the_clients_trust_balance(client, tax_db):
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025})
    client.post("/api/billing/authorize", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025, "agreed": True})

    client.post("/api/billing/remittance/batch?tax_year=2025", headers=auth(firm))
    ledger = client.get("/api/billing/ledger", headers=auth(funded)).json()
    assert float(ledger["held_for_tax"]) == 0.0
    assert any(e["direction"] == "disbursed" for e in ledger["entries"])


def test_the_batch_file_carries_no_full_social_security_numbers(client, tax_db):
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025})
    client.post("/api/billing/authorize", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025, "agreed": True})
    batch = client.post("/api/billing/remittance/batch?tax_year=2025",
                        headers=auth(firm)).json()

    response = client.get(f"/api/billing/remittance/batch/{batch['reference']}/file",
                          headers=auth(firm))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "111223333" not in response.text
    assert "111-22-3333" not in response.text

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["tax_form"] == "1040"
    assert rows[0]["tax_period"] == "202512"
    assert rows[0]["payment_amount"] == "2000.00"
    assert rows[0]["taxpayer_identification"].startswith("***-**-")


def test_a_batched_payment_can_no_longer_be_withdrawn(client, tax_db):
    """Once the money is in a batch, recovering it is the IRS's business."""
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025})
    created = client.post("/api/billing/authorize", headers=auth(funded),
                          json={"amount": 2000, "tax_year": 2025, "agreed": True}).json()
    client.post("/api/billing/remittance/batch?tax_year=2025", headers=auth(firm))

    response = client.post(
        f"/api/billing/authorizations/{created['id']}/withdraw", headers=auth(funded),
    )
    assert response.status_code == 409
    assert "already gone into a batch" in response.json()["detail"]


def test_a_batch_records_its_confirmations(client, tax_db):
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 2000, "tax_year": 2025})
    created = client.post("/api/billing/authorize", headers=auth(funded),
                          json={"amount": 2000, "tax_year": 2025, "agreed": True}).json()
    batch = client.post("/api/billing/remittance/batch?tax_year=2025",
                        headers=auth(firm)).json()

    submitted = client.post(
        f"/api/billing/remittance/batch/{batch['reference']}/submitted",
        headers=auth(firm), json={"confirmations": {str(created["id"]): "EFTPS-77120"}},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"

    listing = client.get("/api/billing/authorizations", headers=auth(funded)).json()
    row = listing["authorizations"][0]
    assert row["status"] == "remitted"
    assert row["confirmation_number"] == "EFTPS-77120"


def test_a_batch_cannot_be_submitted_twice(client, tax_db):
    firm = _preparer(client, tax_db)
    funded = _client(client, "a@example.com", "111-22-3333", "4155550122")
    client.post("/api/billing/funds", headers=auth(funded),
                json={"amount": 500, "tax_year": 2025})
    client.post("/api/billing/authorize", headers=auth(funded),
                json={"amount": 500, "tax_year": 2025, "agreed": True})
    batch = client.post("/api/billing/remittance/batch?tax_year=2025",
                        headers=auth(firm)).json()
    url = f"/api/billing/remittance/batch/{batch['reference']}/submitted"
    assert client.post(url, headers=auth(firm), json={}).status_code == 200
    assert client.post(url, headers=auth(firm), json={}).status_code == 409


def test_the_compliance_checklist_names_the_real_obligations(client, tax_db):
    firm = _preparer(client, tax_db)
    body = client.get("/api/billing/remittance/checklist", headers=auth(firm)).json()
    items = " ".join(entry["item"] + entry["why"] for entry in body["checklist"])
    assert "EFTPS Batch Provider" in items
    assert "trust account" in items.lower()
    assert "money transmission" in items.lower()
    assert "can be enforced in code" in body["note"]


def test_every_money_movement_is_audited(client, tax_db):
    from sqlalchemy import select

    from taxvault.db.models import AuditEvent

    token = _client(client)
    client.post("/api/billing/funds", headers=auth(token),
                json={"amount": 1000, "tax_year": 2025})
    client.post("/api/billing/authorize", headers=auth(token),
                json={"amount": 1000, "tax_year": 2025, "agreed": True})

    actions = {
        row.action for row in tax_db.scalars(select(AuditEvent)).all()
    }
    assert "tax_funds_received" in actions
    assert "remittance_authorized" in actions
