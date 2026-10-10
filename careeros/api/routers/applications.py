"""Application lifecycle tracking."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db, pipeline
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


_NOT_YET_APPLIED = {
    ApplicationStatus.DISCOVERED.value,
    ApplicationStatus.SAVED.value,
    ApplicationStatus.ANALYZED.value,
    ApplicationStatus.RESUME_READY.value,
    ApplicationStatus.APPLICATION_READY.value,
}


class ApplyRequest(BaseModel):
    job_id: int
    #: False (the default) assembles everything and reports what *would* be
    #: sent. True actually submits through the API or email channel.
    send: bool = False
    use_ai: bool = True
    #: Extra question -> answer pairs the user typed in for this posting.
    answers: dict[str, str] = {}


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


@router.post("/apply")
def apply(
    payload: ApplyRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    pipe=Depends(pipeline),
) -> dict[str, Any]:
    """Apply to one job: tailor if needed, pick the channel, and submit.

    * **api** boards (Greenhouse, Lever, ...) and **email** postings are sent
      for you when `send` is true -- after the same checks the autopilot uses.
    * Everything else (company career sites, LinkedIn, Workday, ...) comes back
      as an **assisted** hand-off: the finished packet plus the URL to submit
      it at. Mark it applied afterwards with `POST /{id}/status`.

    Nothing here bypasses a CAPTCHA, a login wall or bot protection.
    """
    from careeros.apply import channels as channel_module
    from careeros.apply.guardrails import HARD_BLOCK_VERDICTS, cited_evidence_ids
    from careeros.apply.mail import gmail_sender
    from careeros.apply.packet import build as build_packet
    from careeros.apply.submit import submit
    from careeros.autopilot import (
        _assessments,
        _latest_resume,
        _record,
        _verified_evidence,
    )
    from careeros.db.models import AtsAssessment, CoverLetter

    job = session.get(Job, payload.job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    # 1. Make sure the job is understood and scored for this person.
    if job.classification is None:
        pipe.classify_jobs([job])
        pipe.assess_for_user(user, jobs=[job])
        session.flush()

    # 2. A tailored résumé, built now if there is none yet.
    resume = _latest_resume(session, user, job.id)
    tailored_now = False
    if resume is None:
        try:
            resume, _report = pipe.tailor_for_job(user, job, use_ai=payload.use_ai)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.flush()
        tailored_now = True

    factuality = resume.factuality[-1] if getattr(resume, "factuality", None) else None
    eligibility, _match, _priority = _assessments(session, user, job.id)
    channel = channel_module.detect(
        url=job.url, description=job.description, source=job.source, company=job.company
    )

    blockers: list[str] = []
    warnings: list[str] = []
    if not resume.is_final:
        blockers.append(
            "The tailored résumé failed the factuality check, so it cannot be sent. "
            "Open it to see the unsupported claims."
        )
    if eligibility is not None and eligibility.verdict in HARD_BLOCK_VERDICTS:
        blockers.append(
            "The posting's work-authorization terms are not compatible with your profile."
        )
    verified = _verified_evidence(session, user)
    if not (cited_evidence_ids(factuality) & verified):
        warnings.append(
            "Every claim on this résumé rests on evidence you have not confirmed yet. "
            "Review it in Profile → Evidence before sending."
        )
        if channel.tier.automatable and payload.send:
            blockers.append(
                "Automatic sending needs at least one confirmed evidence item on the résumé."
            )

    existing = session.scalars(
        select(Application).where(Application.user_id == user.id, Application.job_id == job.id)
    ).first()
    already_applied = bool(existing and existing.status not in _NOT_YET_APPLIED)
    if already_applied and payload.send:
        blockers.append(f"Already applied (status: {existing.status}).")

    cover = session.scalars(
        select(CoverLetter).where(CoverLetter.user_id == user.id, CoverLetter.job_id == job.id)
        .order_by(CoverLetter.id.desc())
    ).first()
    ats = session.scalars(
        select(AtsAssessment).where(
            AtsAssessment.job_id == job.id, AtsAssessment.resume_id == resume.id
        )
    ).first()
    packet = build_packet(
        user=user, job=job, channel=channel, resume=resume, cover_letter=cover,
        eligibility=eligibility, ats=ats, extra_answers=payload.answers,
    )

    result = None
    application = existing
    if not blockers:
        result = submit(packet, dry_run=not payload.send, send=gmail_sender())
        # A preview never moves an application that is already past "applied".
        if not already_applied:
            application = _record(session, user, job, resume, result, source="user")

    if application is None:
        application = Application(user_id=user.id, job_id=job.id, resume_id=resume.id)
        session.add(application)
        session.flush()

    return {
        "application_id": application.id,
        "status": application.status,
        "job": {"id": job.id, "title": job.title, "company": job.company, "url": job.url},
        "channel": channel.to_dict(),
        "resume_id": resume.id,
        "resume_final": resume.is_final,
        "tailored_now": tailored_now,
        "cover_letter_id": cover.id if cover else None,
        "packet": packet.to_dict(),
        "blockers": blockers,
        "warnings": warnings,
        "submission": result.to_dict() if result else None,
        "sent": bool(result and result.ok and not result.dry_run and result.tier != "assisted"),
        "next_step": _next_step(blockers, result, channel, payload.send),
    }


def _next_step(blockers: list[str], result: Any, channel: Any, send: bool) -> str:
    if blockers:
        return "Fix the blockers above, then apply again."
    if result is None:
        return ""
    if result.tier == "assisted" or result.downgraded_to == "assisted":
        where = channel.apply_url or "the posting"
        return (
            f"Submit it yourself at {where} using the packet below, "
            "then mark the application as applied."
        )
    if result.dry_run:
        return "Preview only. Apply again with send=true to submit it."
    if result.ok:
        return "Submitted. Replies will show up in your email sync."
    return result.message
