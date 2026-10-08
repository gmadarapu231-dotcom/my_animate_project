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

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from taxvault.agent import Document, run_agent, sample_bundle, scenarios
from taxvault.agent.sandbox import SandboxUnavailable
from taxvault.api.deps import client_ip, current_taxpayer, get_db, verified_account
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
