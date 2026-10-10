"""Résumé upload and parsing, assembly, tailoring, the factuality gate, cover letters."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db, optional_user, pipeline
from careeros.auth import AuthMode, auth_mode
from careeros.db.models import CoverLetter, FactualityReport, Job, ResumeDocument, User
from careeros.engines.resume import ResumeBuilder, render_text
from careeros.enums import ResumeKind
from careeros.pipeline import Pipeline
from careeros.services import (
    load_certifications,
    load_contact,
    load_education,
    load_evidence_records,
)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


class TailorRequest(BaseModel):
    job_id: int
    use_ai: bool = True


class CoverLetterRequest(BaseModel):
    job_id: int
    use_ai: bool = True


class IntakeTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    commit: bool = True
    overwrite_profile: bool = False


#: A résumé, even a 3-page 300-dpi scan, is well under this.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _owner_for_intake(request: Request, session: Session, parsed_email: str | None, name: str | None) -> User:
    """The signed-in account, else the local profile, else one opened from the résumé.

    The last case is the first-run path on an open (single-person) server:
    there is no profile yet, and the résumé is how one gets made. On a server
    that requires sign-in, `current_user` has already refused.
    """
    user = optional_user(request, session)
    if user is not None:
        return user
    if auth_mode() is AuthMode.REQUIRED:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    if not parsed_email:
        raise HTTPException(
            status_code=422,
            detail=(
                "No profile on this server yet, and no email address was found in the "
                "résumé to open one with. Add your email to the résumé header, or sign in first."
            ),
        )
    user = User(
        email=parsed_email.strip().lower(),
        full_name=name or parsed_email.split("@", 1)[0],
        links={},
        preferred_locations=[],
        industries=[],
    )
    session.add(user)
    session.flush()
    return user


def _intake_response(report: dict[str, Any], user: User) -> dict[str, Any]:
    return {**report, "user_id": user.id, "email": user.email}


@router.post("/upload")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    commit: bool = Form(True),
    overwrite_profile: bool = Form(False),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Upload a résumé (.pdf .docx .txt, or a scan/photo read by OCR) and parse it.

    The profile's descriptive fields are filled in, and every bullet lands as
    *unverified* evidence: confirm the accurate ones before a tailored résumé
    can cite them.
    """
    from careeros.resume_intake import ExtractionError, intake_text, parse_resume
    from careeros.resume_intake.extract import extract_upload

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"That file is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        extraction = extract_upload(data, file.filename or "")
    except ExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc

    parsed = parse_resume(extraction.text)
    user = _owner_for_intake(request, session, parsed.email, parsed.full_name)
    label = (file.filename or "upload").replace("\\", "/").rsplit("/", 1)[-1][:120]
    report = intake_text(
        session, user, extraction.text, source=f"résumé: {label}",
        commit=commit, overwrite_profile=overwrite_profile,
    )
    report["file"] = {
        "name": label,
        "bytes": len(data),
        "characters": len(extraction.text),
        "method": extraction.method,
    }
    report["text"] = extraction.text
    return _intake_response(report, user)


@router.post("/intake-text")
def intake_pasted_text(
    payload: IntakeTextRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """The same as an upload, for résumé text pasted in directly."""
    from careeros.resume_intake import intake_text, normalise, parse_resume

    text = normalise(payload.text)
    if len(text) < 80:
        raise HTTPException(422, "That is too little text to be a résumé.")
    parsed = parse_resume(text)
    user = _owner_for_intake(request, session, parsed.email, parsed.full_name)
    report = intake_text(
        session, user, text, source="résumé: pasted text",
        commit=payload.commit, overwrite_profile=payload.overwrite_profile,
    )
    report["file"] = {"name": "pasted text", "bytes": len(payload.text), "characters": len(text), "method": "text"}
    return _intake_response(report, user)


def _resume_payload(session: Session, doc: ResumeDocument) -> dict[str, Any]:
    report = session.scalars(
        select(FactualityReport).where(FactualityReport.resume_id == doc.id)
        .order_by(FactualityReport.id.desc())
    ).first()
    return {
        "id": doc.id,
        "name": doc.name,
        "kind": doc.kind,
        "version": doc.version,
        "job_id": doc.job_id,
        "track_id": doc.track_id,
        "storage_path": doc.storage_path,
        "is_final": doc.is_final,
        "sections": doc.sections,
        "rendered_text": doc.rendered_text,
        "notes": doc.tailoring_notes,
        "factuality": {
            "passed": report.passed,
            "unsupported_count": report.unsupported_count,
            "summary": report.summary,
            "claims": report.claims,
        } if report else None,
    }


@router.get("")
def list_resumes(
    kind: str | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    stmt = select(ResumeDocument).where(ResumeDocument.user_id == user.id)
    if kind:
        stmt = stmt.where(ResumeDocument.kind == kind)
    docs = session.scalars(stmt.order_by(ResumeDocument.id.desc())).all()
    return {
        "count": len(docs),
        "resumes": [
            {
                "id": d.id, "name": d.name, "kind": d.kind, "version": d.version,
                "job_id": d.job_id, "is_final": d.is_final, "storage_path": d.storage_path,
            }
            for d in docs
        ],
    }


@router.get("/{resume_id}")
def get_resume(
    resume_id: int, session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    doc = session.get(ResumeDocument, resume_id)
    if doc is None or doc.user_id != user.id:
        raise HTTPException(404, "Resume not found")
    return _resume_payload(session, doc)


@router.post("/master")
def build_master(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    """(Re)build the master resume from the full evidence database."""
    evidence = load_evidence_records(session, user.id)
    if not evidence:
        raise HTTPException(400, "No evidence recorded - load a profile first.")
    certs = load_certifications(session, user.id)
    education = load_education(session, user.id)
    contact = load_contact(session, user)

    builder = ResumeBuilder()
    resume = builder.build(
        contact=contact,
        evidence=evidence,
        certifications=certs,
        education=education,
        total_years=float(user.total_experience_years or 0.0),
        kind=ResumeKind.MASTER,
        name="Master Resume",
    )
    previous = session.scalars(
        select(ResumeDocument).where(
            ResumeDocument.user_id == user.id, ResumeDocument.kind == ResumeKind.MASTER.value
        ).order_by(ResumeDocument.version.desc())
    ).first()
    doc = ResumeDocument(
        user_id=user.id,
        kind=ResumeKind.MASTER.value,
        name=resume.name,
        version=(previous.version + 1) if previous else 1,
        storage_path=f"/resumes/master/master-v{(previous.version + 1) if previous else 1}.txt",
        sections=[s.to_dict() for s in resume.sections],
        rendered_text=render_text(resume, contact),
        is_final=True,
        tailoring_notes=resume.notes,
    )
    session.add(doc)
    session.flush()
    return _resume_payload(session, doc)


@router.post("/tailor")
def tailor(
    payload: TailorRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    pipe: Pipeline = Depends(pipeline),
) -> dict[str, Any]:
    """Tailor a resume to one job and run the factuality gate.

    A resume with any unsupported claim comes back `is_final: false` with the
    offending claims listed. That is the gate, not a warning.
    """
    job = session.get(Job, payload.job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    try:
        doc, _report = pipe.tailor_for_job(user, job, use_ai=payload.use_ai)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.flush()
    return _resume_payload(session, doc)


@router.post("/cover-letter")
def cover_letter(
    payload: CoverLetterRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    pipe: Pipeline = Depends(pipeline),
) -> dict[str, Any]:
    """Cover letter drawn only from evidence that matched this job."""
    job = session.get(Job, payload.job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.classification is None:
        raise HTTPException(409, "Job has not been classified yet")

    classification = pipe.classification_from_row(job.classification)
    evidence = load_evidence_records(session, user.id)
    from careeros.services import build_profile_index

    profile = build_profile_index(session, user, pipe.matcher)
    match = pipe.matcher.match(classification, profile, job_title=job.title)
    claimable = [m for m in match.matches if m.claimable]
    used_ids = sorted({i for m in claimable for i in m.evidence_ids})
    by_id = {e.id: e for e in evidence}
    highlights = [by_id[i].text for i in used_ids[:3] if i in by_id]

    body = pipe.provider.text(
        system=(
            "Write a concise cover letter (max 250 words) using ONLY the supplied evidence. "
            "Never claim a skill, tool, certification or outcome that is not in it. Do not "
            "mention visa status, salary or availability."
        ),
        prompt=(
            f"ROLE: {job.title} at {job.company}\n"
            f"DOMAIN: {classification.domain_label}\n"
            f"TOP POSTING TERMS: {', '.join(classification.ats_keywords[:15])}\n\n"
            "EVIDENCE:\n" + "\n".join(f"- {h}" for h in highlights)
        ),
        max_tokens=900,
    ) if getattr(pipe.provider, "available", False) else None

    if not body:
        intro = f"Dear Hiring Team,\n\nI am writing to apply for the {job.title} role at {job.company}."
        bullets = "\n".join(f"- {h}" for h in highlights)
        gaps_note = (
            "\n\nI have not worked directly with "
            + ", ".join(match.gaps[:3])
            + ", and would be glad to discuss how my adjacent experience applies."
            if match.gaps else ""
        )
        body = (
            f"{intro}\n\nRelevant experience:\n{bullets}{gaps_note}\n\n"
            f"Thank you for your consideration.\n\n{user.full_name}"
        )

    row = CoverLetter(user_id=user.id, job_id=job.id, body=body, evidence_ids=used_ids)
    session.add(row)
    session.flush()
    return {
        "id": row.id,
        "job_id": job.id,
        "body": row.body,
        "evidence_ids": used_ids,
        "unclaimed_gaps": match.gaps,
        "approved": row.approved,
    }
