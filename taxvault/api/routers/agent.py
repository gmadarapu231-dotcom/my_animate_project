"""The agent over HTTP, and the sandbox that proves it works.

Two surfaces:

  * `/api/agent/run` -- upload a client's documents and get back a return,
    a list of questions, a price and the filing gate. This is the product.
  * `/api/agent/sandbox/{scenario}` -- the same pipeline against synthetic
    documents built in-process. Unauthenticated and safe to leave on: it
    touches no client record, stores nothing, and the SSN on every synthetic
    form is from the 900-999 range the Social Security Administration has
    never issued. It exists so "does this work?" has an answer that is not a
    screenshot.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from taxvault.agent import Document, run_agent, sample_bundle, scenarios
from taxvault.agent.automation import act, pending, reconcile, run_all, run_for_client
from taxvault.agent.sandbox import SandboxUnavailable
from taxvault.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxvault.api.routers.billing import preparer
from taxvault.api.routers.documents import ALLOWED_CONTENT, MAX_UPLOAD_BYTES
from taxvault.auth import audit
from taxvault.config import UnsupportedTaxYear, federal, latest_year
from taxvault.db.models import Taxpayer
from taxvault.engines.commission import describe_models

router = APIRouter(prefix="/api/agent", tags=["agent"])

#: A sandbox run builds its own PDFs, so there is no upload to bound -- but a
#: caller can still ask for a lot of them. One scenario per request.
MAX_FILES = 12


@router.get("/scenarios")
def list_scenarios() -> dict[str, Any]:
    """The sandbox scenarios, with what each one is meant to prove."""
    return {
        "scenarios": scenarios(),
        "note": (
            "Synthetic documents built in-process. No client record is touched and "
            "nothing is stored. The Social Security number on every synthetic form "
            "is from the 900-999 range, which has never been issued."
        ),
    }


@router.get("/pricing")
def pricing() -> dict[str, Any]:
    """How the platform earns, including the model it will not offer."""
    return describe_models()


@router.post("/sandbox/{scenario}")
def run_sandbox(scenario: str, year: int | None = None,
                returns_this_period: int = 0, answer_everything: bool = False
                ) -> dict[str, Any]:
    """Run the whole pipeline against synthetic documents.

    `answer_everything` runs it twice: once to collect the questions, then
    again with them answered, which is how a return reaches
    `ready_for_signature`. Useful for showing the end state without having to
    invent answers by hand.
    """
    try:
        case, documents = sample_bundle(scenario, year=year)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SandboxUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    target = year or latest_year()
    common = {
        "tax_year": target,
        "taxpayer_name": case.taxpayer_name,
        "filing_status": case.filing_status,
        "resident_state": case.resident_state,
        "situation": dict(case.situation),
        "returns_this_period": returns_this_period,
    }
    run = run_agent(documents, **common)
    body = {"scenario": case.to_dict(), "run": run.to_dict()}

    if answer_everything:
        answered = {item.field for item in run.open_questions if item.field}
        signed = run_agent(
            documents, **common, answered=answered,
            client_reviewed=True, client_signed_8879=True, preparer_ptin="P00000000",
        )
        body["after_answers"] = signed.to_dict()
        body["note"] = (
            "The second run has every question answered and the authorisations in "
            "place, which is how a return reaches ready_for_signature. The one gate "
            "that stays open is the e-file transmitter, and that is an IRS "
            "authorisation rather than something software can satisfy."
        )
    return body


@router.post("/run")
async def run_for_client(
    request: Request,
    files: list[UploadFile] = File(...),
    tax_year: int | None = Form(default=None),
    filing_status: str = Form(default="single"),
    resident_state: str = Form(default=""),
    age: int = Form(default=40),
    returns_this_period: int = Form(default=0),
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Hand the agent a client's documents and get the whole return back.

    Nothing is stored by this route. It computes and returns; the documents go
    through `/api/documents` if they are to be kept, where they are encrypted
    at rest. Keeping the two separate means a client can see what the agent
    makes of a form before deciding to hand it over.
    """
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"One run takes at most {MAX_FILES} files. Split the upload.",
        )
    try:
        year = tax_year or latest_year()
        federal(year)
    except UnsupportedTaxYear as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    documents: list[Document] = []
    for upload in files:
        blob = await upload.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{upload.filename} is too large to accept.",
            )
        content_type = upload.content_type or ""
        if content_type and content_type not in ALLOWED_CONTENT:
            raise HTTPException(
                status_code=415,
                detail=f"{content_type} is not a file type this accepts.",
            )
        documents.append(Document(
            filename=upload.filename or "upload",
            content_type=content_type,
            blob=blob,
        ))

    run = run_agent(
        documents,
        tax_year=year,
        taxpayer_name=" ".join(
            part for part in (taxpayer.first_name, taxpayer.last_name) if part
        ),
        filing_status=filing_status,
        resident_state=resident_state or (taxpayer.resident_state or ""),
        situation={"age": age},
        returns_this_period=returns_this_period,
    )
    # The audit record carries the outcome and the document count, never the
    # figures: an audit log is not a place to keep a client's income.
    audit(session, "agent_run", account_id=account.id, actor=account.email,
          subject=f"taxpayer:{taxpayer.id}", ip_address=client_ip(request),
          tax_year=year, documents=len(documents), state=run.state,
          confidence=run.confidence)
    return run.to_dict()


@router.get("/captured")
def captured(year: int | None = None) -> dict[str, Any]:
    """Every sandbox scenario, run twice, for the Agent screen.

    Twice because the gap is the product: the first pass collects the
    questions, the second has them answered and reaches
    `ready_for_signature`. Unauthenticated and safe to leave on -- the
    documents are synthetic, nothing is stored, and every synthetic SSN is
    from the 900-999 range the Social Security Administration has never
    issued.

    The offline demo answers this from figures captured at build time; a
    running server computes it on the spot. Either way it is real engine
    output, which is why the screen can be trusted as a demonstration.
    """
    from taxvault.agent.sandbox import SCENARIOS, SandboxUnavailable

    target = year or latest_year()
    runs: dict[str, Any] = {}
    for key in SCENARIOS:
        try:
            runs[key] = run_sandbox(key, year=target, answer_everything=True)
        except HTTPException as exc:
            if exc.status_code == 503:  # reportlab absent
                raise
            continue
        except SandboxUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"scenarios": scenarios(), "runs": runs, "tax_year": target}


# ===========================================================================
# The automation: the queue, and the jobs that fill it
# ===========================================================================
class ActBody(BaseModel):
    action: str = Field(description="approve or dismiss")
    note: str = ""


class ReconcileRow(BaseModel):
    description: str
    amount: float
    bank_reference: str = ""


class ReconcileProposal(BaseModel):
    rows: list[ReconcileRow]


@router.get("/queue")
def work_queue(
    _preparer=Depends(preparer),
    kind: str = "",
    limit: int = 100,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """What the agent has done unprompted and wants a person to approve.

    Most urgent first. Every row carries the figures it was based on, so the
    decision does not need the work redoing to check it.
    """
    rows = pending(session, limit=limit, kind=kind)
    needs_human = [r for r in rows if r["requires_human"]]
    return {
        "tasks": rows,
        "count": len(rows),
        "needs_a_person": len(needs_human),
        "note": (
            "Nothing in this queue has happened. Approving a payment match "
            "books it; approving anything else carries out the proposal. "
            "Confirming money, a client's signature and transmitting to the "
            "IRS are never automatic, whatever the confidence."
        ),
    }


@router.post("/queue/{task_id}")
def act_on_task(
    task_id: int, body: ActBody, request: Request,
    account=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Approve or dismiss one proposal."""
    try:
        result = act(session, task_id=task_id, action=body.action,
                     actor=account.email, note=body.note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(session, f"agent_task_{body.action}d", account_id=account.id,
          actor=account.email, subject=f"agent_task:{task_id}",
          ip_address=client_ip(request), kind=result["kind"])
    return result


@router.post("/automation/run")
def run_automation(
    request: Request,
    account=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run every job now. Normally a scheduled worker does this.

    Safe to run as often as you like: a proposal already waiting is not
    queued twice, and stale ones are closed rather than piling up.
    """
    summary = run_all(session)
    audit(session, "automation_run", account_id=account.id, actor=account.email,
          subject="automation", ip_address=client_ip(request),
          queued=summary["queued"])
    return summary


@router.post("/automation/reconcile")
def propose_matches(
    body: ReconcileProposal, request: Request,
    account=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Match a bank export and queue the matches. Books nothing.

    The agent finds them; a person books them. That one click is the whole
    difference between automation and an accident.
    """
    from taxvault.engines.revenue import BankRow

    rows = [
        BankRow(description=row.description, amount=Decimal(str(row.amount)),
                bank_reference=row.bank_reference)
        for row in body.rows
    ]
    result = reconcile(session, rows, proposed_by=account.email)
    audit(session, "automation_reconcile", account_id=account.id,
          actor=account.email, subject="bank_export",
          ip_address=client_ip(request), rows=len(rows),
          queued=result["queued"])
    return result


@router.post("/automation/client/{taxpayer_id}")
def rerun_for_client(
    taxpayer_id: int, request: Request,
    tax_year: int | None = None,
    filing_status: str = "single",
    account=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Re-read every stored document for one client and recompute."""
    taxpayer = session.get(Taxpayer, taxpayer_id)
    if taxpayer is None:
        raise HTTPException(status_code=404, detail="No such client.")
    summary = run_for_client(
        session, taxpayer, tax_year=tax_year, filing_status=filing_status,
    )
    audit(session, "automation_client_run", account_id=account.id,
          actor=account.email, subject=f"taxpayer:{taxpayer_id}",
          ip_address=client_ip(request), ran=summary.get("ran"),
          state=summary.get("state"))
    return summary


@router.get("/receipts/pending")
def receipts_pending(
    _preparer=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Confirmed payments the client has not been told about yet."""
    from taxvault.agent.notify import awaiting_receipt

    rows = awaiting_receipt(session)
    return {"pending": rows, "count": len(rows)}


@router.get("/receipts/{declaration_id}/preview")
def preview_receipt(
    declaration_id: int,
    _preparer=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Exactly what the client will be sent, before it goes.

    A receipt names a figure and goes to somebody's inbox, so being able to
    read it first is not a nicety.
    """
    from taxvault.agent.notify import build_receipt
    from taxvault.db.models import PaymentDeclaration

    declaration = session.get(PaymentDeclaration, declaration_id)
    if declaration is None:
        raise HTTPException(status_code=404, detail="No such payment.")
    if declaration.status != "confirmed":
        raise HTTPException(
            status_code=400,
            detail=(
                "That payment is not confirmed yet, so there is nothing to "
                "confirm receipt of. Check it against the bank first."
            ),
        )
    return build_receipt(session, declaration).to_dict()


@router.post("/receipts/send")
def send_receipts(
    request: Request,
    dry_run: bool = False,
    account=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Send every outstanding receipt. `dry_run` previews without sending."""
    from taxvault.agent.notify import send_pending_receipts

    summary = send_pending_receipts(session, dry_run=dry_run)
    if not dry_run:
        audit(session, "receipts_sent", account_id=account.id,
              actor=account.email, subject="receipts",
              ip_address=client_ip(request), sent=summary["sent"],
              failed=summary["failed"])
    return summary
