"""Uploading and reviewing tax documents.

Three ways in, because clients arrive with three different things:

* **boxes**  -- typed straight in from the form in their hand. Always exact.
* **text**   -- pasted from a payroll portal or an OCR pass. Parsed, scored,
                and routed to review when the score is low.
* **file**   -- the original PDF or image, stored encrypted. A scanned image is
                accepted and kept, but it is *not* claimed to be read: the
                confidence comes back at zero with a warning, because silently
                estimating from a form nobody read is the worst failure this
                system could have.

Every upload is validated immediately and the findings come back with it, so a
mistyped box is caught at the point it can still be fixed cheaply.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from taxvault.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxvault.auth import audit
from taxvault.crypto import encrypt_field
from taxvault.db.models import TaxDocument, Taxpayer
from taxvault.enums import DocumentKind, DocumentStatus
from taxvault.forms.extract import extract_text, pair_orphan_amounts
from taxvault.forms.identity_match import compare_names, compare_ssn
from taxvault.forms.w2 import W2, parse_w2_text
from taxvault.forms.w2_layout import read_w2_layout

router = APIRouter(prefix="/api/documents", tags=["documents"])

#: Refuse anything larger. A W-2 PDF is tens of kilobytes; a 25MB upload is
#: either a mistake or an attempt to fill the disk.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT = {
    "application/pdf", "image/jpeg", "image/png", "image/heic", "image/tiff",
    "text/plain", "text/csv",
}


class W2Boxes(BaseModel):
    tax_year: int
    employer_name: str = ""
    employee_first_name: str = ""
    employee_last_name: str = ""
    employer_ein: str = ""
    box1_wages: float | str = 0.0
    box2_federal_withheld: float | str = 0.0
    box3_social_security_wages: float | str = 0.0
    box4_social_security_withheld: float | str = 0.0
    box5_medicare_wages: float | str = 0.0
    box6_medicare_withheld: float | str = 0.0
    box7_social_security_tips: float | str = 0.0
    box8_allocated_tips: float | str = 0.0
    box10_dependent_care: float | str = 0.0
    box11_nonqualified: float | str = 0.0
    box12: dict[str, float | str] = Field(default_factory=dict)
    box13: dict[str, bool] = Field(default_factory=dict)
    box14: dict[str, float | str] = Field(default_factory=dict)
    states: list[dict[str, Any]] = Field(default_factory=list)


class W2Text(BaseModel):
    tax_year: int | None = None
    text: str


def check_identity(form: W2, taxpayer: Taxpayer) -> list[dict[str, str]]:
    """Does this W-2 belong to the person on the account?

    The IRS matches the name and Social Security number on a return against
    Social Security Administration records, and a mismatch is the commonest
    cause of an e-file rejection. Catching it as the W-2 goes in costs a minute;
    catching it after a filing bounces costs a season.

    Findings only. Whether "Reed" and "Reed-Santos" are one person is not a
    question this code can settle -- but it is one the client can.
    """
    findings: list[dict[str, str]] = []

    name = compare_names(
        registered_first=taxpayer.first_name or "",
        registered_last=taxpayer.last_name or "",
        form_first=form.employee_first_name,
        form_last=form.employee_last_name,
    )
    if name.verdict not in ("exact", "unknown"):
        findings.append({"severity": name.severity, "box": "e",
                         "message": name.message, "check": "name"})
    elif name.verdict == "unknown" and (taxpayer.first_name or taxpayer.last_name):
        findings.append({"severity": "info", "box": "e",
                         "message": name.message, "check": "name"})

    if form.employee_ssn:
        ssn = compare_ssn(
            form.employee_ssn,
            registered_index=taxpayer.ssn_index or "",
            registered_last4=taxpayer.ssn_last4 or "",
        )
        if not ssn.matches and ssn.verdict != "unknown":
            findings.append({"severity": ssn.severity, "box": "a",
                             "message": ssn.message, "check": "ssn"})
    return findings


def _store(
    session: Session, taxpayer: Taxpayer, form: W2, *,
    source: str, confidence: float, warnings: list[str],
    filename: str = "", content_type: str = "", blob: bytes | None = None,
) -> TaxDocument:
    findings = form.validate(year=form.tax_year) + check_identity(form, taxpayer)
    errors = [f for f in findings if f["severity"] == "error"]
    status = (
        DocumentStatus.NEEDS_REVIEW.value if errors or confidence < 0.75
        else DocumentStatus.PARSED.value
    )
    # The number read off the form has served its purpose in `check_identity`;
    # it is not part of what a document stores.
    form.employee_ssn = ""
    document = TaxDocument(
        taxpayer_id=taxpayer.id,
        tax_year=form.tax_year,
        kind=DocumentKind.W2.value,
        status=status,
        source=source,
        employer_name=form.employer_name or None,
        state_code=form.primary_state or None,
        payload=form.to_dict(),
        original_filename=filename or None,
        content_type=content_type or None,
        parse_confidence=confidence,
        parse_warnings=warnings + findings,
        checksum=form.fingerprint(),
    )
    if form.employer_ein:
        document.employer_ein_last4 = form.employer_ein[-4:]
    if blob is not None:
        # The original is kept because an audit asks for it, and it carries the
        # employer EIN and the client's address -- so it is kept encrypted.
        document.raw_blob = encrypt_field(
            blob.decode("utf-8", errors="replace") if content_type.startswith("text/")
            else blob.hex(),
            purpose="document", context=f"taxpayer:{taxpayer.id}",
        )
        document.checksum = hashlib.sha256(blob).hexdigest()

    duplicate = session.scalars(
        select(TaxDocument).where(
            TaxDocument.taxpayer_id == taxpayer.id,
            TaxDocument.checksum == document.checksum,
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This looks like a document already uploaded for {duplicate.tax_year} "
                f"({duplicate.employer_name or 'unnamed employer'}). Uploading the same "
                "W-2 twice would double the wages on the estimate."
            ),
        )
    session.add(document)
    session.flush()
    return document


def _serialise(document: TaxDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "tax_year": document.tax_year,
        "kind": document.kind,
        "status": document.status,
        "source": document.source,
        "employer_name": document.employer_name,
        "employee_name": " ".join(
            part for part in (
                (document.payload or {}).get("employee_first_name", ""),
                (document.payload or {}).get("employee_last_name", ""),
            ) if part
        ),
        "employer_ein_last4": document.employer_ein_last4,
        "state_code": document.state_code,
        "payload": document.payload,
        "parse_confidence": float(document.parse_confidence or 0),
        "warnings": document.parse_warnings,
        "identity_checks": [
            w for w in (document.parse_warnings or []) if w.get("check")
        ],
        "original_filename": document.original_filename,
        "uploaded_at": document.created_at.isoformat() if document.created_at else None,
    }


@router.get("")
def list_documents(
    tax_year: int | None = None,
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(TaxDocument).where(TaxDocument.taxpayer_id == taxpayer.id)
    if tax_year:
        query = query.where(TaxDocument.tax_year == tax_year)
    documents = session.scalars(query.order_by(TaxDocument.tax_year.desc(),
                                               TaxDocument.id.desc())).all()
    return {
        "documents": [_serialise(d) for d in documents],
        "years": sorted({d.tax_year for d in documents}, reverse=True),
        "needs_review": [
            _serialise(d) for d in documents if d.status == DocumentStatus.NEEDS_REVIEW.value
        ],
    }


@router.post("/w2/boxes")
def add_w2_boxes(
    body: W2Boxes, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enter a W-2 by typing the boxes. The most reliable route."""
    form = W2.from_dict(body.model_dump())
    document = _store(session, taxpayer, form, source="manual", confidence=1.0, warnings=[])
    audit(session, "document_added", account_id=account.id, actor=account.email,
          subject=f"document:{document.id}", ip_address=client_ip(request),
          kind="w2", tax_year=form.tax_year)
    return _serialise(document)


@router.post("/w2/text")
def add_w2_text(
    body: W2Text, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Paste W-2 text and have the boxes read out of it."""
    form, confidence, warnings = parse_w2_text(body.text)
    if body.tax_year:
        form.tax_year = body.tax_year
    if not form.tax_year:
        raise HTTPException(
            status_code=400,
            detail="Which tax year is this W-2 for? It could not be read from the text.",
        )
    document = _store(session, taxpayer, form, source="upload",
                      confidence=confidence, warnings=[{"severity": "warning", "box": "",
                                                        "message": w} for w in warnings])
    audit(session, "document_added", account_id=account.id, actor=account.email,
          subject=f"document:{document.id}", ip_address=client_ip(request),
          kind="w2", tax_year=form.tax_year, confidence=confidence)
    return _serialise(document)


@router.post("/w2/file")
async def add_w2_file(
    request: Request,
    file: UploadFile = File(...),
    tax_year: int = Form(...),
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Upload the original W-2 file, and read it where it can be read.

    A PDF from a payroll portal carries its text, so the boxes come straight
    out of it -- which is the common case and the one worth getting right. A
    scan or a photograph carries pixels instead, and reading those needs OCR
    this server does not run: the file is stored encrypted, the confidence
    comes back at zero, and the response says plainly that it was not read
    rather than quietly contributing nothing to an estimate that looks
    complete.
    """
    content_type = (file.content_type or "").split(";")[0].strip()
    if content_type and content_type not in ALLOWED_CONTENT:
        raise HTTPException(
            status_code=415,
            detail=f"{content_type} is not an accepted document type. Upload a PDF, "
                   "an image of the form, or plain text.",
        )
    blob = await file.read()
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"That file is {len(blob) / 1e6:.1f}MB. The limit is "
                   f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
        )
    if not blob:
        raise HTTPException(status_code=400, detail="That file is empty.")

    extraction = extract_text(blob, content_type, file.filename or "")
    warnings: list[dict[str, str]] = list(extraction.notes)
    strategy = "none"
    form, confidence = W2(), 0.0

    if extraction.readable:
        # Read the page's geometry first. A W-2 is a grid, and a flattened text
        # dump loses it: Box 16 and Box 17 sit next to each other on the page
        # but far apart in the dump, so a flat read quietly reports no state
        # tax at all. Reading position gets the right figure out of the right
        # box, and also finds the names, which a flat read cannot place.
        if extraction.method == "pdf_text":
            form, confidence, layout_notes = read_w2_layout(blob)
            strategy = "layout"
            warnings += layout_notes

        # Fall back to the flat parse when the geometry was unreadable, and
        # keep whichever read found more of the boxes that matter.
        if confidence < 1.0:
            flat, flat_confidence, text_warnings = parse_w2_text(extraction.text)
            if flat_confidence > confidence:
                form, confidence, strategy = flat, flat_confidence, "text"
                warnings += [{"severity": "warning", "box": "", "message": w}
                             for w in text_warnings]

        if confidence < 0.75:
            repaired = pair_orphan_amounts(extraction.text)
            if repaired:
                salvaged, salvaged_confidence, _ = parse_w2_text(repaired)
                if salvaged_confidence > confidence:
                    # A repair, not a read: the confidence is cut to match.
                    form, confidence = salvaged, round(salvaged_confidence * 0.8, 3)
                    strategy = "repaired"
                    warnings.append({
                        "severity": "warning", "box": "",
                        "message": (
                            "The boxes were matched by position in the form rather than "
                            "read from their labels, so check every figure against your "
                            "W-2 before relying on this estimate."
                        ),
                    })

    form.tax_year = tax_year

    document = _store(
        session, taxpayer, form, source="upload", confidence=confidence,
        warnings=warnings, filename=file.filename or "",
        content_type=content_type, blob=blob,
    )
    audit(session, "document_uploaded", account_id=account.id, actor=account.email,
          subject=f"document:{document.id}", ip_address=client_ip(request),
          filename=file.filename, bytes=len(blob), content_type=content_type,
          extraction=extraction.method, readable=extraction.readable,
          strategy=strategy)
    payload = _serialise(document)
    payload["extraction"] = {**extraction.to_dict(), "strategy": strategy}
    return payload


@router.patch("/{document_id}")
def update_document(
    document_id: int, body: W2Boxes, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Correct a parsed document by hand, which clears the review flag."""
    document = session.get(TaxDocument, document_id)
    if document is None or document.taxpayer_id != taxpayer.id:
        raise HTTPException(status_code=404, detail="No such document.")
    form = W2.from_dict(body.model_dump())
    findings = form.validate(year=form.tax_year) + check_identity(form, taxpayer)
    document.payload = form.to_dict()
    document.tax_year = form.tax_year
    document.employer_name = form.employer_name or document.employer_name
    document.state_code = form.primary_state or document.state_code
    document.parse_warnings = findings
    document.parse_confidence = 1.0
    document.status = (
        DocumentStatus.NEEDS_REVIEW.value
        if any(f["severity"] == "error" for f in findings)
        else DocumentStatus.CONFIRMED.value
    )
    session.flush()
    audit(session, "document_corrected", account_id=account.id, actor=account.email,
          subject=f"document:{document.id}", ip_address=client_ip(request))
    return _serialise(document)


@router.delete("/{document_id}")
def delete_document(
    document_id: int, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    document = session.get(TaxDocument, document_id)
    if document is None or document.taxpayer_id != taxpayer.id:
        raise HTTPException(status_code=404, detail="No such document.")
    session.delete(document)
    audit(session, "document_deleted", account_id=account.id, actor=account.email,
          subject=f"document:{document_id}", ip_address=client_ip(request))
    return {"deleted": document_id}
