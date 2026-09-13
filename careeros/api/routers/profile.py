"""Master profile, work authorization, career tracks and the evidence database."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db
from careeros.config import countries
from careeros.db.models import CareerTrack, EvidenceItem, User, WorkAuthorization
from careeros.enums import EvidenceKind, VerificationState
from careeros.profile_io import export_profile, load_profile
from careeros.services import (
    load_certifications,
    load_education,
    load_evidence_records,
)

router = APIRouter(prefix="/api/profile", tags=["profile"])


class LoadProfileRequest(BaseModel):
    path: str


class EvidenceIn(BaseModel):
    text: str
    kind: str = EvidenceKind.RESPONSIBILITY.value
    employer_id: int | None = None
    role_title: str | None = None
    project: str | None = None
    technologies: list[str] = []
    skills: list[str] = []
    metrics: dict[str, Any] = {}
    strength: float = 0.5
    verified: bool = False


@router.get("")
def get_profile(
    session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    auth = session.scalars(
        select(WorkAuthorization).where(WorkAuthorization.user_id == user.id)
    ).all()
    tracks = session.scalars(
        select(CareerTrack).where(CareerTrack.user_id == user.id).order_by(CareerTrack.priority)
    ).all()
    return {
        "personal": {
            "full_name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "country": user.home_country,
            "city": user.home_city,
            "region": user.home_region,
            "preferred_locations": user.preferred_locations,
            "remote_preference": user.remote_preference,
            "open_to_relocation": user.open_to_relocation,
            "links": user.links,
        },
        "professional": {
            "current_title": user.current_title,
            "total_experience_years": user.total_experience_years,
            "industries": user.industries,
            "certifications": load_certifications(session, user.id),
            "education": load_education(session, user.id),
        },
        "automation_mode": user.automation_mode,
        "work_authorization": [
            {
                "country": a.country_code,
                "country_name": (countries().get(a.country_code) or None)
                and countries().require(a.country_code).name,
                "status": a.status_id,
                "status_label": countries().require(a.country_code).status(a.status_id).label
                if countries().get(a.country_code) else a.status_id,
                "valid_until": a.valid_until.isoformat() if a.valid_until else None,
                "needs_sponsorship": a.needs_sponsorship,
                "employment_preferences": a.employment_preferences,
            }
            for a in auth
        ],
        "career_tracks": [
            {
                "id": t.id,
                "name": t.name,
                "domain": t.domain_id,
                "active": t.active,
                "priority": t.priority,
                "preferred_titles": t.preferred_titles,
                "core_skills": t.core_skills,
                "min_match_score": t.min_match_score,
                "countries": t.countries,
            }
            for t in tracks
        ],
        "evidence_count": len(load_evidence_records(session, user.id)),
    }


@router.post("/load")
def load(payload: LoadProfileRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    user = load_profile(session, payload.path)
    return {"loaded": True, "user_id": user.id, "email": user.email}


@router.get("/export")
def export(session: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    return export_profile(session, user)


@router.get("/evidence")
def list_evidence(
    kind: str | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """The Career Evidence Database - the substrate every resume is built from."""
    rows = load_evidence_records(session, user.id)
    if kind:
        rows = [r for r in rows if r.kind == kind]
    return {
        "count": len(rows),
        "evidence": [
            {
                "id": r.id,
                "kind": r.kind,
                "text": r.text,
                "employer": r.employer,
                "role_title": r.role_title,
                "project": r.project,
                "technologies": list(r.technologies),
                "skills": list(r.skills),
                "metrics": r.metrics,
                "verified": r.verified,
                "strength": r.strength,
                "start_date": r.start_date.isoformat() if r.start_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
            }
            for r in rows
        ],
    }


@router.post("/evidence")
def add_evidence(
    payload: EvidenceIn,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    item = EvidenceItem(
        user_id=user.id,
        kind=payload.kind,
        text=payload.text,
        employer_id=payload.employer_id,
        role_title=payload.role_title,
        project=payload.project,
        technologies=payload.technologies,
        skills=payload.skills,
        metrics=payload.metrics,
        strength=payload.strength,
        verification=(
            VerificationState.VERIFIED.value if payload.verified
            else VerificationState.UNVERIFIED.value
        ),
        source="api",
    )
    session.add(item)
    session.flush()
    return {"id": item.id}


@router.post("/evidence/{evidence_id}/verify")
def verify_evidence(
    evidence_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    item = session.get(EvidenceItem, evidence_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "Evidence not found")
    item.verification = VerificationState.VERIFIED.value
    return {"id": item.id, "verification": item.verification}
