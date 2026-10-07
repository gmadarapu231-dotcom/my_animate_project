"""Getting paid: the reference, the confirmation gate, and the firm's account."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxvault.engines.fees import quote
from taxvault.engines.revenue import (
    BankRow,
    DECLARED,
    CONFIRMED,
    REJECTED,
    confirm_payment,
    confirmed_total,
    declare_payment,
    find_reference,
    firm_account,
    new_reference,
    outstanding_requests,
    reconcile_bank_rows,
    reference_is_valid,
    reject_payment,
    request_fee_payment,
)
from taxvault.engines.remittance import BUCKET_FEE, BUCKET_TAX, RemittanceError


def D(value):
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# the fee engine bug this flow exposed
# ---------------------------------------------------------------------------
def test_an_unknown_income_is_not_a_low_income():
    """A quote taken before the return was computed priced it at nothing.

    The pro-bono discount is means-tested. Defaulting an absent AGI to zero
    made the means test pass on no evidence, so a $199,680 return was quoted
    free. A means test must require evidence, not the absence of it.
    """
    unknown = quote(w2_count=1, planning_session=True)
    assert unknown.total > D(0)
    assert unknown.income_band == "income not yet known"
    assert any("indicative price" in note for note in unknown.notes)


def test_a_genuinely_low_income_return_is_still_free():
    """The policy is deliberate and must survive the fix above."""
    assert quote(agi=D(18000), w2_count=1, dependents=1).total == D(0)
    assert quote(agi=D(0), w2_count=1).total == D(0)


def test_a_high_income_return_is_priced_on_the_work():
    priced = quote(agi=D(199680), w2_count=1, planning_session=True,
                   capital_gains=D(21700), investment_income=D(6480))
    assert priced.total > D(400)
    assert priced.tier == "investment"


# ---------------------------------------------------------------------------
# the reference
# ---------------------------------------------------------------------------
def test_a_reference_carries_a_check_character():
    reference = new_reference(tax_year=2026)
    assert reference_is_valid(reference)
    mistyped = reference[:-1] + ("A" if reference[-1] != "A" else "B")
    assert not reference_is_valid(mistyped)


def test_a_reference_avoids_the_characters_people_mistype():
    """No I, O, 0 or 1: a mistyped reference that is still valid credits the
    wrong client."""
    for _ in range(40):
        body = new_reference(tax_year=2026).split("-", 3)[3]
        assert not (set("IO01") & set(body))


def test_a_reference_is_found_inside_a_noisy_bank_line():
    reference = new_reference(tax_year=2026)
    line = f"ZELLE FROM PRIYA RAMAN ON 10/07 MEMO {reference} CONF 88213"
    assert find_reference(line) == reference


def test_an_invalid_reference_in_a_bank_line_is_not_matched():
    assert find_reference("ZELLE MEMO TV-FEE-26-AAAAAA-A CONF 1") in ("", )


# ---------------------------------------------------------------------------
# request, declare, confirm
# ---------------------------------------------------------------------------
@pytest.fixture
def engagement(tax_db):
    """A taxpayer with an accepted quote, ready to be asked for money."""
    from taxvault.db.models import Account, FeeQuoteRecord, Taxpayer

    account = Account(email="client@example.com", role="client")
    tax_db.add(account)
    tax_db.flush()
    taxpayer = Taxpayer(account_id=account.id, first_name="Priya", last_name="Raman",
                        ssn_index="idx", resident_state="CA")
    tax_db.add(taxpayer)
    tax_db.flush()
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="investment",
                         base=D(185), add_ons=D(194), discount=D(0), total=D(379))
    tax_db.add(row)
    tax_db.flush()
    return tax_db, taxpayer, row


def test_a_request_produces_instructions_and_a_reference(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(
        session, taxpayer, quote=row, pay_to="billing@practice.example"
    )
    assert instruction.amount == D("379.00")
    assert reference_is_valid(instruction.reference)
    assert any("memo" in step for step in instruction.steps)
    assert instruction.declaration_id is not None


def test_a_declaration_moves_no_money(engagement):
    """This is the whole point of the two-step: a claim is not a credit."""
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    declare_payment(session, taxpayer,
                    declaration_id=instruction.declaration_id)
    assert confirmed_total(session, taxpayer_id=taxpayer.id,
                           bucket=BUCKET_FEE) == D("0.00")


def test_only_confirmation_writes_to_the_ledger(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    declare_payment(session, taxpayer, declaration_id=instruction.declaration_id)
    declaration, entry = confirm_payment(
        session, declaration_id=instruction.declaration_id,
        confirmed_by="preparer@practice.example", bank_reference="CONF88213",
    )
    assert declaration.status == CONFIRMED
    assert entry.bucket == BUCKET_FEE
    assert confirmed_total(session, taxpayer_id=taxpayer.id,
                           bucket=BUCKET_FEE) == D("379.00")


def test_a_confirmation_has_to_name_who_checked_the_bank(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    with pytest.raises(RemittanceError, match="name the person"):
        confirm_payment(session, declaration_id=instruction.declaration_id,
                        confirmed_by="  ")


def test_confirming_twice_is_refused(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    confirm_payment(session, declaration_id=instruction.declaration_id,
                    confirmed_by="preparer@practice.example")
    with pytest.raises(RemittanceError, match="already been confirmed"):
        confirm_payment(session, declaration_id=instruction.declaration_id,
                        confirmed_by="preparer@practice.example")


def test_confirming_marks_the_quote_paid(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    confirm_payment(session, declaration_id=instruction.declaration_id,
                    confirmed_by="preparer@practice.example")
    assert row.paid_at is not None
    assert row.payment_reference == instruction.reference


def test_a_rejection_closes_the_claim_without_deleting_it(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    declaration = reject_payment(
        session, declaration_id=instruction.declaration_id,
        rejected_by="preparer@practice.example", reason="no credit in the bank",
    )
    assert declaration.status == REJECTED
    assert confirmed_total(session, taxpayer_id=taxpayer.id, bucket=BUCKET_FEE) == D(0)


def test_a_confirmed_payment_cannot_be_rejected(engagement):
    """The ledger is append-only: a reversal is an entry, not an edit."""
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    confirm_payment(session, declaration_id=instruction.declaration_id,
                    confirmed_by="preparer@practice.example")
    with pytest.raises(RemittanceError, match="append-only"):
        reject_payment(session, declaration_id=instruction.declaration_id,
                       rejected_by="preparer@practice.example")


def test_asking_again_once_paid_is_refused(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    confirm_payment(session, declaration_id=instruction.declaration_id,
                    confirmed_by="preparer@practice.example")
    with pytest.raises(RemittanceError, match="already paid in full"):
        request_fee_payment(session, taxpayer, quote=row)


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------
def test_a_bank_row_is_matched_by_its_reference(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    result = reconcile_bank_rows(
        session,
        [BankRow(description=f"ZELLE FROM P RAMAN MEMO {instruction.reference}",
                 amount=D("379.00"), bank_reference="CONF1")],
        confirmed_by="preparer@practice.example",
    )
    assert len(result.matched) == 1
    assert result.matched[0]["reference"] == instruction.reference


def test_matches_are_proposed_not_booked_by_default(engagement):
    """A rule that books on a string match will one day book the wrong thing."""
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    reconcile_bank_rows(
        session,
        [BankRow(description=instruction.reference, amount=D("379.00"))],
        confirmed_by="preparer@practice.example",
    )
    assert confirmed_total(session, taxpayer_id=taxpayer.id, bucket=BUCKET_FEE) == D(0)


def test_auto_confirm_books_it_when_explicitly_asked(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    reconcile_bank_rows(
        session,
        [BankRow(description=instruction.reference, amount=D("379.00"))],
        confirmed_by="preparer@practice.example", auto_confirm=True,
    )
    assert confirmed_total(session, taxpayer_id=taxpayer.id,
                           bucket=BUCKET_FEE) == D("379.00")


def test_a_short_payment_is_a_mismatch_not_a_part_payment(engagement):
    """A short payment is a conversation, not an arithmetic adjustment."""
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    result = reconcile_bank_rows(
        session,
        [BankRow(description=instruction.reference, amount=D("300.00"))],
        confirmed_by="preparer@practice.example", auto_confirm=True,
    )
    assert result.matched == []
    assert len(result.mismatched) == 1
    assert confirmed_total(session, taxpayer_id=taxpayer.id, bucket=BUCKET_FEE) == D(0)


def test_a_row_with_no_reference_is_reported_not_guessed(engagement):
    session, taxpayer, row = engagement
    request_fee_payment(session, taxpayer, quote=row)
    result = reconcile_bank_rows(
        session, [BankRow(description="ZELLE FROM SOMEBODY", amount=D("379.00"))],
        confirmed_by="preparer@practice.example",
    )
    assert result.matched == []
    assert len(result.unmatched) == 1
    assert "no TaxVault reference" in result.unmatched[0]["reason"]


# ---------------------------------------------------------------------------
# the practice's account
# ---------------------------------------------------------------------------
def test_the_firm_account_counts_only_confirmed_money(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    declare_payment(session, taxpayer, declaration_id=instruction.declaration_id)

    before = firm_account(session)
    assert before.fees_collected == D("0.00")
    assert before.fees_declared_awaiting_confirmation == D("379.00")
    assert before.declarations_awaiting == 1

    confirm_payment(session, declaration_id=instruction.declaration_id,
                    confirmed_by="preparer@practice.example")
    after = firm_account(session)
    assert after.fees_collected == D("379.00")
    assert after.returns_paid == 1
    assert after.net_to_practice == D("379.00")


def test_client_tax_money_is_never_counted_as_revenue(engagement):
    """The distinction that ends practices when it is not kept."""
    from taxvault.engines.remittance import record_funds

    session, taxpayer, row = engagement
    record_funds(session, taxpayer, amount=D(9000), bucket=BUCKET_TAX,
                 direction="received", tax_year=2026)
    account = firm_account(session)
    assert account.fees_collected == D("0.00")
    assert account.tax_held_in_trust == D("9000.00")
    assert any("NOT revenue" in note for note in account.notes)


def test_a_platform_plan_shows_what_the_practice_keeps(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    confirm_payment(session, declaration_id=instruction.declaration_id,
                    confirmed_by="preparer@practice.example")
    own = firm_account(session)
    platform = firm_account(session, platform_model="per_return")
    assert own.platform_cost == D("0.00")
    assert platform.platform_cost == D("12.00")
    assert platform.net_to_practice == own.net_to_practice - D("12.00")


def test_the_confirmation_queue_lists_what_is_waiting(engagement):
    session, taxpayer, row = engagement
    instruction = request_fee_payment(session, taxpayer, quote=row)
    declare_payment(session, taxpayer, declaration_id=instruction.declaration_id)
    waiting = outstanding_requests(session)
    assert len(waiting) == 1
    assert waiting[0]["reference"] == instruction.reference
    assert waiting[0]["amount"] == "379.00"


# ---------------------------------------------------------------------------
# who may do what
# ---------------------------------------------------------------------------
def _ready(client):
    from tests.test_tax_journey import sign_in, verify_identity

    return verify_identity(client, sign_in(client))


def test_a_client_cannot_mark_their_own_fee_paid(client):
    """It was reachable by the client, so revenue was whatever clients said.

    Recording money writes to the trust ledger, and a client is not a witness
    to their own payment arriving in the practice's bank.
    """
    from tests.test_tax_journey import auth

    token = _ready(client)
    quoted = client.post("/api/billing/quote", headers=auth(token),
                         json={"tax_year": 2026, "w2_count": 1}).json()
    response = client.post("/api/billing/fee/paid", headers=auth(token),
                           json={"quote_id": quoted["quote_id"], "amount": 379.0})
    assert response.status_code == 403
    assert "preparer" in response.json()["detail"]


def test_a_client_cannot_declare_tax_funds_received(client):
    """Otherwise the trust balance is inflated and a batch pays out money
    that was never received."""
    from tests.test_tax_journey import auth

    token = _ready(client)
    response = client.post("/api/billing/funds", headers=auth(token),
                           json={"amount": 9000.0, "tax_year": 2026})
    assert response.status_code == 403


def test_a_client_cannot_confirm_a_payment(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    response = client.post("/api/billing/payments/confirm", headers=auth(token),
                           json={"declaration_id": 1})
    assert response.status_code == 403


def test_a_client_cannot_see_the_firm_account(client):
    from tests.test_tax_journey import auth

    token = _ready(client)
    assert client.get("/api/billing/firm/account",
                      headers=auth(token)).status_code == 403


def test_a_client_can_ask_for_and_declare_their_own_payment(client):
    """The client's half of the flow, which moves no money."""
    from tests.test_tax_journey import auth

    token = _ready(client)
    quoted = client.post("/api/billing/quote", headers=auth(token),
                         json={"tax_year": 2026, "w2_count": 1}).json()
    requested = client.post("/api/billing/payments/request", headers=auth(token),
                            json={"quote_id": quoted["quote_id"]})
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert reference_is_valid(body["reference"])

    declared = client.post("/api/billing/payments/declare", headers=auth(token),
                           json={"declaration_id": body["declaration_id"]})
    assert declared.status_code == 200, declared.text
    assert declared.json()["status"] == DECLARED

    # And the ledger has not moved.
    ledger = client.get("/api/billing/ledger", headers=auth(token)).json()
    assert ledger["fees_received"] == "0.00"
