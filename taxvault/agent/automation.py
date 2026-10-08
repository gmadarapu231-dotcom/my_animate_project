"""The agent, running unprompted, queueing what needs a person.

A tax practice's year is not a series of requests. Documents arrive in
February and nobody notices until April; a client answers a question three
weeks late and the estimate is never re-run; a fee goes unpaid and the return
goes out anyway; a planning move expires on 31 December while everyone is on
holiday. None of that is hard work. All of it is work nobody does, because
doing it means remembering to look.

These jobs look. Each one runs on a schedule, does the whole mechanical part,
and leaves a row in `agent_task` with the figures already worked out and a
draft already written. By the time anyone opens the queue the thinking is
done and what is left is a decision.

**What is deliberately not automated**, and why it is not an oversight:

  * **Confirming money against a bank statement.** The agent proposes the
    match; a person books it. Automating this on a string match means that
    one day a client's tax payment, memo copied from an earlier transfer,
    becomes the practice's revenue. `reconcile` therefore never passes
    `auto_confirm=True`.
  * **A client's review and their Form 8879.** Required, and the taxpayer is
    responsible for the return's contents whoever prepared it.
  * **Transmitting anything.** Needs an EFIN, which is an authorisation your
    firm holds.

Everything else -- reading, computing, checking, drafting, chasing, watching
the calendar -- happens without being asked.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from taxvault.config import federal, latest_year, supported_years
from taxvault.db.models import (
    AgentTask,
    Estimate,
    FeeQuoteRecord,
    PaymentDeclaration,
    TaxDocument,
    Taxpayer,
)
from taxvault.money import ZERO, cents, money, positive

logger = logging.getLogger(__name__)

OPEN, APPROVED, DISMISSED, EXPIRED, DONE = (
    "open", "approved", "dismissed", "expired", "done"
)

#: Why a person must be the one to act. Recorded on the task so the reason
#: travels with it rather than living only in this docstring.
HUMAN_BANK = (
    "A bank statement is the only witness that money arrived, and it answers "
    "to the practice, not to this software."
)
HUMAN_SIGNATURE = (
    "The taxpayer is responsible for the return's contents whoever prepared "
    "it, so they have to see it and sign Form 8879."
)
HUMAN_EFILE = (
    "Transmitting needs an EFIN from IRS e-Services. That is an authorisation "
    "the firm holds, not a feature software can grant itself."
)

#: How long a proposal stays worth acting on before its figures are stale.
STALE_DAYS = {
    "payment_match": 30, "fee_unpaid": 14, "question_outstanding": 21,
    "estimate_ready": 14, "deadline": 60, "document_unreadable": 30,
    "document_check": 30, "review_ready": 14, "receipt_failed": 30,
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ===========================================================================
# The queue
# ===========================================================================
@dataclass
class Proposal:
    """One thing the agent wants to do, with what it based that on."""

    kind: str
    title: str
    proposal: str = ""
    taxpayer_id: int | None = None
    tax_year: int | None = None
    priority: int = 5
    confidence: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)
    requires_human: str = ""

    def fingerprint(self) -> str:
        """Identifies this proposal so a second run does not queue it twice.

        Deliberately excludes the figures: a nightly job that re-queues "this
        client owes a fee" every night because the amount moved by a cent is a
        job nobody reads the output of.
        """
        seed = f"{self.kind}|{self.taxpayer_id}|{self.tax_year}|{self.title}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def queue(session: Session, proposal: Proposal) -> AgentTask | None:
    """Add a proposal, unless an identical one is already waiting.

    Returns None when it was a duplicate, so a caller can count what is
    genuinely new rather than what it looked at.
    """
    fingerprint = proposal.fingerprint()
    existing = session.scalars(
        select(AgentTask).where(
            AgentTask.fingerprint == fingerprint,
            AgentTask.status.in_((OPEN, APPROVED)),
        )
    ).first()
    if existing is not None:
        return None

    days = STALE_DAYS.get(proposal.kind, 21)
    task = AgentTask(
        kind=proposal.kind, taxpayer_id=proposal.taxpayer_id,
        tax_year=proposal.tax_year, title=proposal.title,
        proposal=proposal.proposal or None, evidence=proposal.evidence,
        confidence=round(float(proposal.confidence), 3),
        priority=proposal.priority,
        requires_human=proposal.requires_human or None,
        stale_after=_now() + timedelta(days=days),
        fingerprint=fingerprint,
    )
    session.add(task)
    session.flush()
    return task


def pending(session: Session, *, limit: int = 100,
            kind: str = "") -> list[dict[str, Any]]:
    """The queue, most urgent first."""
    query = select(AgentTask).where(AgentTask.status == OPEN)
    if kind:
        query = query.where(AgentTask.kind == kind)
    rows = session.scalars(
        query.order_by(AgentTask.priority, AgentTask.at).limit(limit)
    ).all()
    return [_serialise(row) for row in rows]


def act(session: Session, *, task_id: int, action: str, actor: str,
        note: str = "") -> dict[str, Any]:
    """Approve or dismiss a proposal. Records who, because somebody did."""
    if action not in ("approve", "dismiss"):
        raise ValueError(f"{action!r} is not approve or dismiss")
    if not (actor or "").strip():
        raise ValueError("an action has to name who took it")
    task = session.get(AgentTask, task_id)
    if task is None:
        raise LookupError("no such task")
    if task.status != OPEN:
        raise ValueError(f"that task is already {task.status}")

    task.status = APPROVED if action == "approve" else DISMISSED
    task.acted_at = _now()
    task.acted_by = actor
    task.outcome = note or None
    session.flush()
    return _serialise(task)


def expire_stale(session: Session) -> int:
    """Close proposals whose figures are too old to act on."""
    rows = session.scalars(
        select(AgentTask).where(
            AgentTask.status == OPEN,
            AgentTask.stale_after.is_not(None),
            AgentTask.stale_after < _now(),
        )
    ).all()
    for task in rows:
        task.status = EXPIRED
        task.outcome = "The figures behind this were too old to act on."
    session.flush()
    return len(rows)


def _serialise(task: AgentTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "kind": task.kind,
        "status": task.status,
        "priority": task.priority,
        "taxpayer_id": task.taxpayer_id,
        "tax_year": task.tax_year,
        "title": task.title,
        "proposal": task.proposal or "",
        "evidence": task.evidence or {},
        "confidence": float(task.confidence or 0),
        "requires_human": task.requires_human or "",
        "at": task.at.isoformat() if task.at else None,
        "stale_after": task.stale_after.isoformat() if task.stale_after else None,
        "acted_by": task.acted_by or "",
        "outcome": task.outcome or "",
    }


# ===========================================================================
# The jobs
# ===========================================================================
@dataclass
class JobResult:
    name: str
    looked_at: int = 0
    queued: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_all(session: Session, *, as_of: date | None = None) -> dict[str, Any]:
    """Every job, in the order a practice would want them.

    Deadlines first, because a date nobody can move beats everything else.
    """
    today = as_of or date.today()
    expired = expire_stale(session)
    results = [
        watch_deadlines(session, as_of=today),
        review_new_documents(session),
        chase_questions(session),
        chase_unpaid_fees(session, as_of=today),
        flag_awaiting_confirmation(session),
        tell_clients_their_money_arrived(session),
    ]
    return {
        "ran_at": _now().isoformat(),
        "expired": expired,
        "jobs": [job.to_dict() for job in results],
        "queued": sum(job.queued for job in results),
        "open_tasks": len(pending(session, limit=1000)),
    }


def tell_clients_their_money_arrived(session: Session) -> JobResult:
    """Send a receipt for every confirmed payment nobody has mentioned yet.

    The one part of the money flow that is safe to automate. Confirming a
    payment is a decision about a bank statement; telling the client is a
    statement about something that already happened, and leaving it to
    whoever remembers is how a client who paid last Tuesday phones on Friday
    to ask whether it arrived.
    """
    from taxvault.agent.notify import awaiting_receipt, send_pending_receipts

    result = JobResult(name="send_receipts")
    waiting = awaiting_receipt(session)
    result.looked_at = len(waiting)
    if not waiting:
        return result

    summary = send_pending_receipts(session)
    result.notes.append(
        f"{summary['sent']} receipt(s) sent, {summary['failed']} could not go out"
    )
    for receipt in summary["receipts"]:
        if receipt["failed"]:
            result.notes.append(
                f"  {receipt['reference']}: " + "; ".join(receipt["failed"])
            )
            # Nobody told the client, so a person needs to know that.
            if queue(session, Proposal(
                kind="receipt_failed", taxpayer_id=None,
                priority=3,
                title=f"Could not tell the client about {receipt['amount']}",
                proposal=(
                    f"The payment on reference {receipt['reference']} is "
                    "confirmed and booked, but the receipt could not be "
                    "delivered: " + "; ".join(receipt["failed"]) + ". The client "
                    "does not know their money arrived."
                ),
                evidence=receipt,
            )):
                result.queued += 1
    return result


# ----------------------------------------------------------- the calendar
def watch_deadlines(session: Session, *, as_of: date) -> JobResult:
    """Dates that cost money, surfaced while they can still be acted on.

    A planning move that expires on 31 December is worth nothing on 1 January,
    and the difference between those two days is whether anybody looked.
    """
    result = JobResult(name="watch_deadlines")
    taxpayers = session.scalars(select(Taxpayer)).all()
    result.looked_at = len(taxpayers)
    if not taxpayers:
        return result

    year = latest_year()
    params = federal(year)

    # Estimated payment dates for the current year.
    for raw in params.get("estimated_payment_due_dates", default=[]) or []:
        try:
            due = date.fromisoformat(str(raw))
        except ValueError:
            continue
        days = (due - as_of).days
        if 0 <= days <= 21:
            if queue(session, Proposal(
                kind="deadline", tax_year=year, priority=2,
                title=f"Estimated tax payment due {due:%d %B %Y}",
                proposal=(
                    f"The {due:%d %B} instalment is {days} day(s) away. Anyone "
                    "with self-employment or investment income who is relying "
                    "on withholding alone will be charged an underpayment "
                    "penalty, and that penalty is computed quarter by quarter "
                    "-- paying the whole lot in April does not undo it."
                ),
                evidence={"due_date": due.isoformat(), "days_away": days},
            )):
                result.queued += 1

    # The filing deadline and the extension.
    for label, iso, lead in (
        ("Filing deadline", params.due_date, 30),
        ("Extended filing deadline", params.extended_due_date, 30),
    ):
        if not iso:
            continue
        try:
            due = date.fromisoformat(iso)
        except ValueError:
            continue
        days = (due - as_of).days
        if 0 <= days <= lead:
            if queue(session, Proposal(
                kind="deadline", tax_year=params.year, priority=1,
                title=f"{label} for {params.year}: {due:%d %B %Y}",
                proposal=(
                    f"{days} day(s) left. An extension buys six more months to "
                    "FILE and none to PAY -- interest and the failure-to-pay "
                    "penalty run from the original date either way."
                ),
                evidence={"due_date": due.isoformat(), "days_away": days},
            )):
                result.queued += 1

    # Year-end: the planning moves that close with the calendar.
    year_end = date(as_of.year, 12, 31)
    days_left = (year_end - as_of).days
    if 0 <= days_left <= 45:
        if queue(session, Proposal(
            kind="deadline", tax_year=as_of.year, priority=2,
            title=f"Year-end planning closes in {days_left} day(s)",
            proposal=(
                "Anything that has to happen inside the tax year closes on 31 "
                "December: a 401(k) deferral, harvesting a loss, realising gain "
                "inside the 0% band, a charitable gift, and -- most valuably -- "
                "getting income under the health-subsidy cliff. An IRA and an "
                "HSA can still be funded up to the filing deadline; none of the "
                "others can."
            ),
            evidence={"days_left": days_left},
        )):
            result.queued += 1
    return result


# ------------------------------------------------------- new documents in
def review_new_documents(session: Session) -> JobResult:
    """A document arrived. Re-read it, re-run the return, say what changed.

    The common failure this removes: a client uploads a second W-2 in March,
    nobody re-runs the estimate, and the return goes out on February's
    figures.
    """
    result = JobResult(name="review_new_documents")
    rows = session.scalars(
        select(TaxDocument).where(TaxDocument.status != "rejected")
        .order_by(TaxDocument.id.desc()).limit(500)
    ).all()
    result.looked_at = len(rows)

    by_client: dict[tuple[int, int], list[TaxDocument]] = {}
    for document in rows:
        by_client.setdefault((document.taxpayer_id, document.tax_year), []).append(
            document
        )

    for (taxpayer_id, year), documents in by_client.items():
        # Two different problems that were being reported as one. A document
        # nothing could be read from needs a different file; a document that
        # read perfectly but failed a cross-check needs somebody to look at
        # the figure. Telling a preparer to chase a client for a PDF they
        # already sent, because box 3 disagreed with box 4, is worse than
        # saying nothing.
        unreadable = [d for d in documents if float(d.parse_confidence or 0) == 0]
        flagged = [
            d for d in documents
            if d.status == "needs_review" and float(d.parse_confidence or 0) > 0
        ]

        for document in unreadable:
            if queue(session, Proposal(
                kind="document_unreadable", taxpayer_id=taxpayer_id,
                tax_year=year, priority=3,
                title=f"{document.original_filename or document.kind} could not be read",
                proposal=(
                    "Nothing could be pulled off this document, which almost "
                    "always means it is a scan or a photograph. There is no OCR "
                    "here on purpose: a guessed figure on a tax return is worse "
                    "than a blank one, because a blank gets asked about. Ask the "
                    "client for the original PDF, or type the boxes in."
                ),
                evidence={"document_id": document.id, "kind": document.kind,
                          "confidence": float(document.parse_confidence or 0)},
                confidence=1.0,
            )):
                result.queued += 1

        for document in flagged:
            findings = [
                w for w in (document.parse_warnings or [])
                if isinstance(w, dict) and w.get("severity") == "error"
            ] or [
                w for w in (document.parse_warnings or [])
                if isinstance(w, dict) and w.get("severity") == "warning"
            ]
            headline = (findings[0].get("message", "") if findings
                        else "A cross-check on this form did not agree.")
            if queue(session, Proposal(
                kind="document_check", taxpayer_id=taxpayer_id, tax_year=year,
                priority=3,
                title=(f"{document.original_filename or document.kind}: "
                       "a figure needs checking"),
                proposal=(
                    f"The form read cleanly at "
                    f"{float(document.parse_confidence or 0):.0%}, so this is not "
                    f"a reading problem -- it is the figures. {headline} Check it "
                    "against the paper form before the return goes out."
                ),
                evidence={
                    "document_id": document.id, "kind": document.kind,
                    "confidence": float(document.parse_confidence or 0),
                    "findings": [
                        {"box": w.get("box", ""), "severity": w.get("severity", ""),
                         "message": w.get("message", "")}
                        for w in findings[:4]
                    ],
                },
                confidence=1.0,
            )):
                result.queued += 1

        # Is the latest estimate older than the latest document?
        newest = max((d.created_at for d in documents if d.created_at), default=None)
        latest_estimate = session.scalars(
            select(Estimate).where(
                Estimate.taxpayer_id == taxpayer_id, Estimate.tax_year == year
            ).order_by(Estimate.id.desc())
        ).first()
        stale = (
            latest_estimate is None
            or (newest and latest_estimate.created_at and newest > latest_estimate.created_at)
        )
        if stale and not unreadable:
            if queue(session, Proposal(
                kind="estimate_ready", taxpayer_id=taxpayer_id, tax_year=year,
                priority=4,
                title=f"{len(documents)} document(s) on file for {year}, estimate not current",
                proposal=(
                    "A document arrived after the last estimate was run, so the "
                    "figures on file are out of date. The agent has re-read "
                    "everything; approve to re-run the return and bring the "
                    "questions with it."
                ),
                evidence={
                    "documents": len(documents),
                    "kinds": sorted({d.kind for d in documents}),
                    "last_estimate_id": latest_estimate.id if latest_estimate else None,
                },
            )):
                result.queued += 1
    return result


# ---------------------------------------------------- unanswered questions
def chase_questions(session: Session) -> JobResult:
    """A question nobody answered is a return nobody can file."""
    result = JobResult(name="chase_questions")
    rows = session.scalars(
        select(AgentTask).where(
            AgentTask.kind == "question_outstanding", AgentTask.status == OPEN
        )
    ).all()
    result.looked_at = len(rows)
    cutoff = _now() - timedelta(days=7)
    for task in rows:
        if task.at and task.at < cutoff:
            result.notes.append(
                f"task {task.id} has been waiting {(_now() - task.at).days} days"
            )
    if result.notes:
        result.notes.insert(0, (
            f"{len(result.notes)} question(s) unanswered for over a week. Each "
            "one is holding a return."
        ))
    return result


# ------------------------------------------------------------ unpaid fees
def chase_unpaid_fees(session: Session, *, as_of: date) -> JobResult:
    """Quoted, not paid. The fee is due before the return goes out."""
    result = JobResult(name="chase_unpaid_fees")
    rows = session.scalars(
        select(FeeQuoteRecord).where(FeeQuoteRecord.paid_at.is_(None))
    ).all()
    result.looked_at = len(rows)
    for quote in rows:
        total = money(quote.total)
        if total <= ZERO:
            continue  # a pro-bono return has nothing to chase
        age = (as_of - quote.created_at.date()).days if quote.created_at else 0
        if age < 3:
            continue
        if queue(session, Proposal(
            kind="fee_unpaid", taxpayer_id=quote.taxpayer_id,
            tax_year=quote.tax_year, priority=3 if age > 14 else 5,
            title=f"{total:,.2f} quoted {age} day(s) ago and unpaid",
            proposal=(
                f"The {quote.tax_year} fee of {total:,.2f} was quoted {age} "
                "day(s) ago and no payment has been confirmed. Fees are "
                "collected before a return is filed, so this is work that "
                "should not go out yet. Approve to send the client their "
                "payment reference again."
            ),
            evidence={"quote_id": quote.id, "total": str(total),
                      "tier": quote.tier, "age_days": age},
        )):
            result.queued += 1
    return result


# ------------------------------------------ money a client says they sent
def flag_awaiting_confirmation(session: Session) -> JobResult:
    """Declared payments waiting on a bank check. Never auto-confirmed."""
    result = JobResult(name="flag_awaiting_confirmation")
    rows = session.scalars(
        select(PaymentDeclaration).where(PaymentDeclaration.status == "declared")
    ).all()
    result.looked_at = len(rows)
    for declaration in rows:
        age = (_now() - declaration.at).days if declaration.at else 0
        if queue(session, Proposal(
            kind="payment_match", taxpayer_id=declaration.taxpayer_id,
            tax_year=declaration.tax_year,
            priority=2 if age >= 2 else 4,
            title=(f"{money(declaration.amount):,.2f} declared, "
                   f"{'waiting ' + str(age) + ' day(s)' if age else 'today'}"),
            proposal=(
                f"The client says they sent {money(declaration.amount):,.2f} by "
                f"{declaration.method} with reference {declaration.reference}. "
                "Check it against the bank and confirm it. Until then it is not "
                "revenue and the return should not go out."
            ),
            evidence={"declaration_id": declaration.id,
                      "reference": declaration.reference,
                      "amount": str(money(declaration.amount)),
                      "method": declaration.method, "age_days": age},
            requires_human=HUMAN_BANK,
        )):
            result.queued += 1
    return result


# ===========================================================================
# Reconciliation, proposed not booked
# ===========================================================================
def reconcile(session: Session, rows: list[Any], *, proposed_by: str) -> dict[str, Any]:
    """Match a bank export and QUEUE the matches for approval.

    Never confirms. `reconcile_bank_rows` can auto-confirm and this does not
    pass that flag, on purpose: a rule that books money on a string match will
    one day book a client's tax payment as the practice's revenue, and the
    saving from skipping one click does not come close to the cost of that.
    """
    from taxvault.engines.revenue import reconcile_bank_rows

    result = reconcile_bank_rows(
        session, rows, confirmed_by=proposed_by, auto_confirm=False
    )
    queued = 0
    for match in result.matched:
        if queue(session, Proposal(
            kind="payment_match", taxpayer_id=match.get("taxpayer_id"),
            priority=2,
            title=f"Bank credit matches {match['reference']}",
            proposal=(
                f"A credit of {money(match['amount']):,.2f} in the bank carries "
                f"reference {match['reference']}, which matches an open request "
                f"for {money(match['expected']):,.2f}. Approve to book it as "
                "revenue."
            ),
            evidence=match,
            requires_human=HUMAN_BANK,
        )):
            queued += 1
    for row in result.mismatched:
        if queue(session, Proposal(
            kind="payment_match", taxpayer_id=row.get("taxpayer_id"),
            priority=2,
            title=f"Bank credit does not match {row.get('reference', 'a request')}",
            proposal=(
                f"{row.get('reason', 'The amounts differ.')} A short payment is a "
                "conversation with the client, not an arithmetic adjustment, so "
                "nothing has been booked."
            ),
            evidence=row,
            requires_human=HUMAN_BANK,
        )):
            queued += 1
    return {**result.to_dict(), "queued": queued, "auto_confirmed": False}


# ===========================================================================
# Running the agent for one client, and queueing what it asks
# ===========================================================================
def run_for_client(
    session: Session, taxpayer: Taxpayer, *, tax_year: int | None = None,
    filing_status: str = "single", situation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-read every document on file, recompute, and queue the questions.

    This is what `estimate_ready` approves into. It does the whole mechanical
    pass and turns each thing the agent cannot know into a queued question
    with the figure it would move attached.
    """
    from taxvault.agent import Document, run_agent
    from taxvault.crypto import decrypt_field

    year = tax_year or latest_year()
    rows = session.scalars(
        select(TaxDocument).where(
            TaxDocument.taxpayer_id == taxpayer.id,
            TaxDocument.tax_year == year,
            TaxDocument.status != "rejected",
        )
    ).all()

    documents: list[Document] = []
    for row in rows:
        if not row.raw_blob:
            continue
        try:
            plain = decrypt_field(
                row.raw_blob, purpose="document",
                context=f"taxpayer:{taxpayer.id}",
            )
        except Exception:
            logger.warning("could not reopen document %s", row.id)
            continue
        content_type = row.content_type or ""
        blob = (
            plain.encode("utf-8") if content_type.startswith("text/")
            else bytes.fromhex(plain)
        )
        documents.append(Document(
            filename=row.original_filename or f"{row.kind}.pdf",
            content_type=content_type, blob=blob,
            declared_kind=row.kind if row.kind != "w2" else "",
        ))

    if not documents:
        return {"ran": False, "reason": "no stored documents could be reopened"}

    run = run_agent(
        documents, tax_year=year,
        taxpayer_name=" ".join(
            part for part in (taxpayer.first_name, taxpayer.last_name) if part
        ),
        filing_status=filing_status,
        resident_state=taxpayer.resident_state or "",
        situation=dict(situation or {}),
    )

    queued = 0
    for item in run.open_questions:
        if queue(session, Proposal(
            kind="question_outstanding", taxpayer_id=taxpayer.id, tax_year=year,
            priority=2 if item.severity == "blocker" else 4,
            title=item.question[:190],
            proposal=item.why,
            evidence={"field": item.field, "moves": item.moves,
                      "form": item.form, "severity": item.severity},
            confidence=run.confidence,
            requires_human=(
                HUMAN_SIGNATURE if item.severity == "confirm" else ""
            ),
        )):
            queued += 1

    if run.state == "ready_for_signature":
        if queue(session, Proposal(
            kind="review_ready", taxpayer_id=taxpayer.id, tax_year=year,
            priority=3,
            title=f"{year} return ready for the client to review and sign",
            proposal=(
                "Every question is answered and the figures are checked. Send it "
                "to the client to read and sign Form 8879. Nothing goes to the "
                "IRS from here -- that needs an EFIN."
            ),
            evidence={"state": run.state,
                      "confidence": round(run.confidence, 3),
                      "balance": (run.estimate or {}).get("totals", {})
                      .get("total_balance")},
            confidence=run.confidence,
            requires_human=HUMAN_SIGNATURE,
        )):
            queued += 1

    return {
        "ran": True,
        "state": run.state,
        "confidence": round(run.confidence, 3),
        "documents": len(documents),
        "questions_queued": queued,
        "estimate": run.estimate,
        "gate": run.gate.to_dict(),
    }
