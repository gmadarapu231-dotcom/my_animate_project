"""The agent running unprompted, and the queue it fills.

The design these test: the agent does the WORK and queues the DECISION.
Three things never leave the queue automatically whatever the confidence --
confirming money against a bank, a client's signature, and transmitting to
the IRS -- and that is enforced here, not just documented.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from taxvault.agent.automation import (
    APPROVED,
    DISMISSED,
    EXPIRED,
    HUMAN_BANK,
    OPEN,
    Proposal,
    act,
    chase_unpaid_fees,
    expire_stale,
    flag_awaiting_confirmation,
    pending,
    queue,
    reconcile,
    review_new_documents,
    run_all,
    watch_deadlines,
)
from taxvault.db.models import Account, AgentTask, FeeQuoteRecord, Taxpayer

pytest.importorskip("reportlab", reason="the sandbox builds real PDFs")


def D(value):
    return Decimal(str(value))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def client_row(tax_db):
    account = Account(email="client@example.com", role="client")
    tax_db.add(account)
    tax_db.flush()
    taxpayer = Taxpayer(account_id=account.id, first_name="Priya",
                        last_name="Raman", ssn_index="idx", resident_state="CA")
    tax_db.add(taxpayer)
    tax_db.flush()
    return tax_db, taxpayer


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------
def test_a_proposal_is_queued_with_its_evidence(tax_db):
    task = queue(tax_db, Proposal(
        kind="deadline", title="Something is due", proposal="Do the thing",
        evidence={"days_away": 7}, priority=2,
    ))
    assert task is not None
    assert task.status == OPEN
    assert task.evidence == {"days_away": 7}
    assert task.stale_after is not None


def test_the_same_proposal_is_not_queued_twice(client_row):
    """A nightly job that re-queues the same thing is a job nobody reads."""
    tax_db, taxpayer = client_row
    first = queue(tax_db, Proposal(kind="fee_unpaid", title="200.00 unpaid",
                                   taxpayer_id=taxpayer.id, tax_year=2026))
    second = queue(tax_db, Proposal(kind="fee_unpaid", title="200.00 unpaid",
                                    taxpayer_id=taxpayer.id, tax_year=2026))
    assert first is not None
    assert second is None
    assert len(pending(tax_db)) == 1


def test_the_queue_is_ordered_by_urgency(tax_db):
    queue(tax_db, Proposal(kind="fee_unpaid", title="a nudge", priority=5))
    queue(tax_db, Proposal(kind="deadline", title="a deadline", priority=1))
    titles = [row["title"] for row in pending(tax_db)]
    assert titles == ["a deadline", "a nudge"]


def test_approving_records_who_did_it(tax_db):
    task = queue(tax_db, Proposal(kind="deadline", title="x"))
    result = act(tax_db, task_id=task.id, action="approve",
                 actor="preparer@practice.example", note="done")
    assert result["status"] == APPROVED
    assert result["acted_by"] == "preparer@practice.example"


def test_an_action_has_to_name_somebody(tax_db):
    """"The system approved it" is not an answer to "who approved this"."""
    task = queue(tax_db, Proposal(kind="deadline", title="x"))
    with pytest.raises(ValueError, match="name who"):
        act(tax_db, task_id=task.id, action="approve", actor="  ")


def test_a_task_cannot_be_acted_on_twice(tax_db):
    task = queue(tax_db, Proposal(kind="deadline", title="x"))
    act(tax_db, task_id=task.id, action="dismiss", actor="a@b.example")
    with pytest.raises(ValueError, match="already dismissed"):
        act(tax_db, task_id=task.id, action="approve", actor="a@b.example")


def test_stale_proposals_are_closed_not_left_to_pile_up(tax_db):
    task = queue(tax_db, Proposal(kind="fee_unpaid", title="old"))
    task.stale_after = _now() - timedelta(days=1)
    tax_db.flush()
    assert expire_stale(tax_db) == 1
    assert tax_db.get(AgentTask, task.id).status == EXPIRED
    assert pending(tax_db) == []


# ---------------------------------------------------------------------------
# what must never be automatic
# ---------------------------------------------------------------------------
def test_a_declared_payment_is_queued_and_never_confirmed(client_row):
    """The agent proposes the match; a person books it."""
    from taxvault.engines.remittance import BUCKET_FEE
    from taxvault.engines.revenue import confirmed_total, request_fee_payment

    session, taxpayer = client_row
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=D(95), add_ons=D(0), discount=D(0), total=D(215))
    session.add(row)
    session.flush()
    request_fee_payment(session, taxpayer, quote=row)

    result = flag_awaiting_confirmation(session)
    assert result.queued == 1
    task = pending(session, kind="payment_match")[0]
    assert task["requires_human"] == HUMAN_BANK
    # Nothing was booked.
    assert confirmed_total(session, taxpayer_id=taxpayer.id,
                           bucket=BUCKET_FEE) == D("0.00")


def test_reconciliation_proposes_and_books_nothing(client_row):
    """auto_confirm is never passed, whatever the match quality."""
    from taxvault.engines.remittance import BUCKET_FEE
    from taxvault.engines.revenue import (
        BankRow,
        confirmed_total,
        request_fee_payment,
    )

    session, taxpayer = client_row
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=D(95), add_ons=D(0), discount=D(0), total=D(215))
    session.add(row)
    session.flush()
    instruction = request_fee_payment(session, taxpayer, quote=row)

    result = reconcile(
        session,
        [BankRow(description=f"ZELLE MEMO {instruction.reference}",
                 amount=D("215.00"))],
        proposed_by="preparer@practice.example",
    )
    assert result["auto_confirmed"] is False
    assert result["queued"] == 1
    assert confirmed_total(session, taxpayer_id=taxpayer.id,
                           bucket=BUCKET_FEE) == D("0.00")
    task = pending(session, kind="payment_match")[0]
    assert task["requires_human"] == HUMAN_BANK


def test_a_short_payment_is_queued_as_a_mismatch(client_row):
    from taxvault.engines.revenue import BankRow, request_fee_payment

    session, taxpayer = client_row
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=D(95), add_ons=D(0), discount=D(0), total=D(215))
    session.add(row)
    session.flush()
    instruction = request_fee_payment(session, taxpayer, quote=row)

    result = reconcile(
        session,
        [BankRow(description=instruction.reference, amount=D("180.00"))],
        proposed_by="preparer@practice.example",
    )
    assert result["matched"] == []
    assert len(result["mismatched"]) == 1
    task = pending(session, kind="payment_match")[0]
    assert "conversation" in task["proposal"]


# ---------------------------------------------------------------------------
# the jobs
# ---------------------------------------------------------------------------
def test_an_unpaid_fee_is_chased_but_not_on_the_first_day(client_row):
    session, taxpayer = client_row
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=D(95), add_ons=D(0), discount=D(0), total=D(215))
    session.add(row)
    session.flush()

    today = chase_unpaid_fees(session, as_of=date.today())
    assert today.queued == 0, "chased a fee quoted the same day"

    later = chase_unpaid_fees(session, as_of=date.today() + timedelta(days=10))
    assert later.queued == 1
    assert "215.00" in pending(session, kind="fee_unpaid")[0]["title"]


def test_a_pro_bono_return_is_not_chased_for_nothing(client_row):
    """The price list gives a simple low-income return away on purpose."""
    session, taxpayer = client_row
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="simple",
                         base=D(0), add_ons=D(0), discount=D(0), total=D(0))
    session.add(row)
    session.flush()
    result = chase_unpaid_fees(session, as_of=date.today() + timedelta(days=30))
    assert result.queued == 0


def test_deadlines_are_surfaced_while_they_can_still_be_acted_on(client_row):
    """A planning move that expires on 31 December is worth nothing on 1 Jan."""
    session, _ = client_row
    result = watch_deadlines(session, as_of=date(2026, 12, 5))
    assert result.queued >= 1
    titles = " ".join(row["title"] for row in pending(session, kind="deadline"))
    assert "Year-end planning" in titles


def test_a_deadline_long_past_is_not_surfaced(client_row):
    session, _ = client_row
    result = watch_deadlines(session, as_of=date(2026, 6, 1))
    titles = " ".join(row["title"] for row in pending(session, kind="deadline"))
    assert "Year-end planning" not in titles


def test_an_unreadable_document_is_separated_from_one_that_merely_needs_review(
    client_row,
):
    """These were reported as one thing, and they are not the same thing.

    Telling a preparer to chase a client for a PDF they already sent, because
    box 3 disagreed with box 4, is worse than saying nothing.
    """
    from taxvault.db.models import TaxDocument

    session, taxpayer = client_row
    session.add(TaxDocument(
        taxpayer_id=taxpayer.id, tax_year=2026, kind="w2", status="needs_review",
        source="upload", parse_confidence=1.0, original_filename="good.pdf",
        parse_warnings=[{"severity": "error", "box": "3",
                         "message": "Box 3 disagrees with Box 4."}],
    ))
    session.add(TaxDocument(
        taxpayer_id=taxpayer.id, tax_year=2026, kind="1099_int",
        status="needs_review", source="upload", parse_confidence=0.0,
        original_filename="photo.pdf", parse_warnings=[],
    ))
    session.flush()

    review_new_documents(session)
    kinds = {row["kind"] for row in pending(session)}
    assert "document_check" in kinds, "a readable form was not flagged for checking"
    assert "document_unreadable" in kinds, "a scan was not flagged as unreadable"

    checked = [r for r in pending(session) if r["kind"] == "document_check"][0]
    assert "not a reading problem" in checked["proposal"]
    assert checked["evidence"]["findings"][0]["box"] == "3"

    unreadable = [r for r in pending(session) if r["kind"] == "document_unreadable"][0]
    assert "scan or a photograph" in unreadable["proposal"]


def test_run_all_is_safe_to_run_repeatedly(client_row):
    session, taxpayer = client_row
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=D(95), add_ons=D(0), discount=D(0), total=D(215))
    row.created_at = _now() - timedelta(days=10)
    session.add(row)
    session.flush()

    first = run_all(session)
    second = run_all(session)
    assert first["queued"] >= 1
    assert second["queued"] == 0, "a second run queued duplicates"
    assert second["open_tasks"] == first["open_tasks"]


# ---------------------------------------------------------------------------
# through the API
# ---------------------------------------------------------------------------
def _preparer_token(client, tax_db):
    from sqlalchemy import select

    from tests.test_tax_journey import sign_in, verify_identity

    token = verify_identity(client, sign_in(client, "firm@example.com"),
                            ssn="456-78-9012", email="firm@example.com",
                            mobile="4155550111")
    account = tax_db.scalars(
        select(Account).where(Account.email == "firm@example.com")
    ).first()
    account.role = "preparer"
    tax_db.commit()
    return token


def test_a_client_cannot_see_the_work_queue(client):
    from tests.test_tax_journey import auth, sign_in, verify_identity

    token = verify_identity(client, sign_in(client))
    assert client.get("/api/agent/queue", headers=auth(token)).status_code == 403


def test_a_preparer_can_run_the_jobs_and_read_the_queue(client, tax_db):
    from tests.test_tax_journey import auth

    token = _preparer_token(client, tax_db)
    ran = client.post("/api/agent/automation/run", headers=auth(token))
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert "jobs" in body and body["jobs"]

    queue_body = client.get("/api/agent/queue", headers=auth(token)).json()
    assert "tasks" in queue_body
    assert "never automatic" in queue_body["note"]


def test_a_preparer_can_approve_from_the_queue(client, tax_db):
    from tests.test_tax_journey import auth

    token = _preparer_token(client, tax_db)
    task = queue(tax_db, Proposal(kind="deadline", title="Something is due",
                                  proposal="Do it"))
    tax_db.commit()
    response = client.post(f"/api/agent/queue/{task.id}",
                           headers=auth(token), json={"action": "approve"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == APPROVED
    assert response.json()["acted_by"] == "firm@example.com"


def test_an_unknown_task_is_a_404(client, tax_db):
    from tests.test_tax_journey import auth

    token = _preparer_token(client, tax_db)
    assert client.post("/api/agent/queue/99999", headers=auth(token),
                       json={"action": "approve"}).status_code == 404


def test_a_bad_action_is_refused(client, tax_db):
    from tests.test_tax_journey import auth

    token = _preparer_token(client, tax_db)
    task = queue(tax_db, Proposal(kind="deadline", title="x"))
    tax_db.commit()
    assert client.post(f"/api/agent/queue/{task.id}", headers=auth(token),
                       json={"action": "delete"}).status_code == 400


# ---------------------------------------------------------------------------
# telling the client their money arrived
# ---------------------------------------------------------------------------
def _confirmed_payment(session, taxpayer, *, amount=D(379), bucket="fee"):
    from taxvault.engines.revenue import confirm_payment, request_fee_payment

    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=amount, add_ons=D(0), discount=D(0), total=amount)
    session.add(row)
    session.flush()
    instruction = request_fee_payment(session, taxpayer, quote=row)
    declaration, _ = confirm_payment(
        session, declaration_id=instruction.declaration_id,
        confirmed_by="preparer@practice.example", bank_reference="CONF88213",
    )
    return declaration


def test_a_receipt_says_what_arrived_and_what_happens_next(client_row, monkeypatch):
    from taxvault.agent.notify import build_receipt

    monkeypatch.setenv("TAXVAULT_PRACTICE_NAME", "Test Tax Associates")
    session, taxpayer = client_row
    declaration = _confirmed_payment(session, taxpayer)
    receipt = build_receipt(session, declaration)

    assert "379.00" in receipt.body
    assert declaration.reference in receipt.body
    assert "Test Tax Associates" in receipt.subject
    # The thing clients worry about, said plainly.
    assert "deducted from your refund" in receipt.body
    # And what happens next, because that is the next question.
    assert "read and sign" in receipt.body
    # The anti-phishing line, because a receipt is a phishing template.
    assert "never ask you for your Social Security number" in receipt.body


def test_the_sms_carries_the_figure_and_the_reference(client_row):
    from taxvault.agent.notify import build_receipt

    session, taxpayer = client_row
    declaration = _confirmed_payment(session, taxpayer)
    receipt = build_receipt(session, declaration)
    assert "379.00" in receipt.sms
    assert declaration.reference in receipt.sms
    assert len(receipt.sms) < 320, "an SMS this long arrives as three messages"


def test_a_receipt_for_tax_money_says_it_is_being_held_not_earned(client_row):
    """Client money and the practice's fee are different things to be told."""
    from taxvault.agent.notify import build_receipt
    from taxvault.engines.revenue import confirm_payment, new_reference
    from taxvault.db.models import PaymentDeclaration

    session, taxpayer = client_row
    declaration = PaymentDeclaration(
        taxpayer_id=taxpayer.id, bucket="tax", amount=D(9000), tax_year=2026,
        method="zelle", reference=new_reference(tax_year=2026, bucket="tax"),
        status="declared",
    )
    session.add(declaration)
    session.flush()
    confirm_payment(session, declaration_id=declaration.id,
                    confirmed_by="preparer@practice.example")
    receipt = build_receipt(session, declaration)
    assert "holding for you" in receipt.body
    assert "kept" in receipt.body and "separate" in receipt.body
    assert "withdraw that instruction" in receipt.body


def test_a_receipt_is_sent_once(client_row):
    """Being told twice that your 379 arrived reads like it was taken twice."""
    from taxvault.agent.notify import send_pending_receipts

    session, taxpayer = client_row
    declaration = _confirmed_payment(session, taxpayer)
    # No channels configured in tests, so nothing is delivered -- but the
    # once-only guard is what this checks, so force the flag.
    declaration.receipt_sent_at = _now()
    declaration.receipt_channels = "email"
    session.flush()

    summary = send_pending_receipts(session)
    assert summary["looked_at"] == 0, "a receipt already sent was queued again"


def test_an_undeliverable_receipt_is_reported_not_swallowed(client_row):
    """The money is booked and the client does not know. Somebody must."""
    from taxvault.agent.automation import tell_clients_their_money_arrived

    session, taxpayer = client_row
    _confirmed_payment(session, taxpayer)
    result = tell_clients_their_money_arrived(session)
    assert result.looked_at == 1
    # Nothing is configured in a test environment, so it cannot go out.
    assert any("could not go out" in note for note in result.notes)
    flagged = pending(session, kind="receipt_failed")
    assert flagged, "an undelivered receipt left no trace"
    assert "does not know their money arrived" in flagged[0]["proposal"]


def test_a_dry_run_previews_without_sending(client_row):
    from taxvault.agent.notify import send_pending_receipts

    session, taxpayer = client_row
    declaration = _confirmed_payment(session, taxpayer)
    summary = send_pending_receipts(session, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["sent"] == 0
    assert summary["receipts"][0]["body"]
    assert declaration.receipt_sent_at is None, "a dry run marked it as sent"


def test_an_unverified_mobile_is_not_texted_a_figure(client_row):
    """An unverified number is a number somebody typed, maybe wrongly."""
    from taxvault.agent.notify import build_receipt

    session, taxpayer = client_row
    account = session.get(Account, taxpayer.account_id)
    account.mobile_e164 = "+14155550132"
    account.mobile_verified_at = None
    session.flush()
    declaration = _confirmed_payment(session, taxpayer)
    assert build_receipt(session, declaration).to_mobile == ""

    account.mobile_verified_at = _now()
    session.flush()
    assert build_receipt(session, declaration).to_mobile == "+14155550132"


def test_a_receipt_cannot_be_previewed_before_the_money_is_confirmed(client, tax_db):
    """There is nothing to confirm receipt of until it is in the bank."""
    from taxvault.engines.revenue import request_fee_payment
    from tests.test_tax_journey import auth

    token = _preparer_token(client, tax_db)
    taxpayer = tax_db.scalars(select(Taxpayer)).first()
    row = FeeQuoteRecord(taxpayer_id=taxpayer.id, tax_year=2026, tier="standard",
                         base=D(215), add_ons=D(0), discount=D(0), total=D(215))
    tax_db.add(row)
    tax_db.flush()
    instruction = request_fee_payment(tax_db, taxpayer, quote=row)
    tax_db.commit()

    response = client.get(
        f"/api/agent/receipts/{instruction.declaration_id}/preview",
        headers=auth(token),
    )
    assert response.status_code == 400
    assert "not confirmed yet" in response.json()["detail"]


def test_a_client_cannot_send_receipts(client):
    from tests.test_tax_journey import auth, sign_in, verify_identity

    token = verify_identity(client, sign_in(client))
    assert client.post("/api/agent/receipts/send",
                       headers=auth(token)).status_code == 403
