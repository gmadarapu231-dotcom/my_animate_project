"""Application lifecycle tracking."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db
from careeros.db.models import Application, ApplicationEvent, Job, RejectionAnalysis, User
from careeros.enums import APPLICATION_FUNNEL, ApplicationStatus, AutomationMode

router = APIRouter(prefix="/api/applications", tags=["applications"])


class StatusUpdate(BaseModel):
    status: str
    detail: str | None = None


class NoteUpdate(BaseModel):
    notes: str


class PrepareRequest(BaseModel):
    job_id: int


@router.get("")
def list_applications(
    status: str | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    rows = session.execute(
        select(Application, Job).join(Job, Job.id == Application.job_id)
        .where(Application.user_id == user.id)
    ).all()
    if status:
        rows = [(a, j) for a, j in rows if a.status == status]
    return {
        "count": len(rows),
        "lifecycle": [s.value for s in APPLICATION_FUNNEL],
        "applications": [
            {
                "id": a.id,
                "job_id": j.id,
                "title": j.title,
                "company": j.company,
                "country": j.country_code,
                "status": a.status,
                "track_id": a.track_id,
                "applied_on": a.applied_on.isoformat() if a.applied_on else None,
                "last_activity_on": a.last_activity_on.isoformat() if a.last_activity_on else None,
                "priority": j.priority.overall if j.priority else None,
                "notes": a.notes,
            }
            for a, j in rows
        ],
    }


@router.get("/{application_id}")
def get_application(
    application_id: int, session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    app_row = session.get(Application, application_id)
    if app_row is None or app_row.user_id != user.id:
        raise HTTPException(404, "Application not found")
    events = session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.application_id == app_row.id)
        .order_by(ApplicationEvent.at)
    ).all()
    rejection = session.scalars(
        select(RejectionAnalysis).where(RejectionAnalysis.application_id == app_row.id)
    ).first()
    return {
        "id": app_row.id,
        "job_id": app_row.job_id,
        "status": app_row.status,
        "submission_mode": app_row.submission_mode,
        "answers": app_row.answers,
        "notes": app_row.notes,
        "events": [
            {
                "at": e.at.isoformat(), "from": e.from_status, "to": e.to_status,
                "source": e.source, "detail": e.detail,
            }
            for e in events
        ],
        "rejection": {
            "stage": rejection.stage,
            "explicit_reason": rejection.explicit_reason,
            "explicit_quote": rejection.explicit_quote,
            "possible_reasons": rejection.possible_reasons,
            "confidence": rejection.confidence,
        } if rejection else None,
    }


@router.post("/{application_id}/status")
def set_status(
    application_id: int,
    payload: StatusUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    app_row = session.get(Application, application_id)
    if app_row is None or app_row.user_id != user.id:
        raise HTTPException(404, "Application not found")
    try:
        target = ApplicationStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(
            422, f"Unknown status. Valid values: {[s.value for s in ApplicationStatus]}"
        ) from exc

    session.add(
        ApplicationEvent(
            application_id=app_row.id,
            from_status=app_row.status,
            to_status=target.value,
            source="user",
            detail=payload.detail,
        )
    )
    app_row.status = target.value
    app_row.last_activity_on = date.today()
    if target is ApplicationStatus.APPLIED and app_row.applied_on is None:
        app_row.applied_on = date.today()
    return {"id": app_row.id, "status": app_row.status}


@router.post("/{application_id}/notes")
def set_notes(
    application_id: int,
    payload: NoteUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    app_row = session.get(Application, application_id)
    if app_row is None or app_row.user_id != user.id:
        raise HTTPException(404, "Application not found")
    app_row.notes = payload.notes
    return {"id": app_row.id, "notes": app_row.notes}


@router.post("/prepare")
def prepare(
    payload: PrepareRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Assemble the application packet.

    Phase 1 stops here, at ASSISTED mode: CareerOS produces the packet and the
    suggested answers, and the human submits. Browser automation is Phase 3 and
    will never bypass CAPTCHA, MFA or any other access control.
    """
    job = session.get(Job, payload.job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    app_row = session.scalars(
        select(Application).where(Application.user_id == user.id, Application.job_id == job.id)
    ).first()
    if app_row is None:
        app_row = Application(user_id=user.id, job_id=job.id)
        session.add(app_row)
        session.flush()

    from careeros.db.models import CoverLetter, ResumeDocument

    resume = session.scalars(
        select(ResumeDocument).where(
            ResumeDocument.user_id == user.id, ResumeDocument.job_id == job.id
        ).order_by(ResumeDocument.version.desc())
    ).first()
    letter = session.scalars(
        select(CoverLetter).where(CoverLetter.user_id == user.id, CoverLetter.job_id == job.id)
        .order_by(CoverLetter.id.desc())
    ).first()

    blockers = []
    if resume is None:
        blockers.append("No tailored resume yet - POST /api/resumes/tailor first.")
    elif not resume.is_final:
        blockers.append("Tailored resume failed the factuality check and cannot be submitted.")

    answers = {
        "work_authorization": (
            "Answer from your Master Profile. CareerOS does not auto-answer immigration "
            "questions on your behalf."
        ),
        "years_of_experience": user.total_experience_years,
        "willing_to_relocate": user.open_to_relocation,
        "preferred_locations": user.preferred_locations,
    }
    app_row.answers = answers
    app_row.submission_mode = AutomationMode.ASSISTED.value
    if not blockers:
        app_row.status = ApplicationStatus.APPLICATION_READY.value

    return {
        "application_id": app_row.id,
        "job": {"id": job.id, "title": job.title, "company": job.company, "url": job.url},
        "resume_id": resume.id if resume else None,
        "resume_final": resume.is_final if resume else None,
        "cover_letter_id": letter.id if letter else None,
        "suggested_answers": answers,
        "submission_mode": app_row.submission_mode,
        "blockers": blockers,
        "status": app_row.status,
        "notice": (
            "ASSISTED mode: review every field, then submit yourself. CareerOS never "
            "bypasses CAPTCHA, MFA or bot protection, and never submits without you."
        ),
    }
