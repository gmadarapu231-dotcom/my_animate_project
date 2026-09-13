"""Import/export of the Master Profile and Career Evidence Database (YAML).

The profile file is the user's source of truth and is meant to be readable and
diffable. Import is idempotent -- re-running it updates rather than duplicates,
keyed on natural identifiers (employer name, evidence text).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.db.models import (
    CareerDomain,
    CareerTrack,
    Certification,
    Education,
    Employer,
    EvidenceItem,
    User,
    WorkAuthorization,
)
from careeros.enums import EvidenceKind, VerificationState


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None


def load_profile(session: Session, path: str | Path) -> User:
    """Create or update the user, tracks, employers, evidence and credentials."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    personal = data.get("personal", {})

    user = session.scalars(select(User).where(User.email == personal["email"])).first()
    if user is None:
        user = User(email=personal["email"], full_name=personal.get("full_name", ""))
        session.add(user)

    user.full_name = personal.get("full_name", user.full_name)
    user.phone = personal.get("phone")
    user.links = personal.get("links", {}) or {}
    user.home_country = (personal.get("country") or "US").upper()
    user.home_city = personal.get("city")
    user.home_region = personal.get("region")
    user.preferred_locations = personal.get("preferred_locations", []) or []
    user.remote_preference = personal.get("remote_preference", "any")
    user.open_to_relocation = bool(personal.get("open_to_relocation", False))

    professional = data.get("professional", {})
    user.current_title = professional.get("current_title")
    user.total_experience_years = float(professional.get("total_experience_years", 0) or 0)
    user.industries = professional.get("industries", []) or []
    session.flush()

    _load_work_auth(session, user, data.get("work_authorization", []) or [])
    employer_ids = _load_employers(session, user, data.get("employers", []) or [])
    _load_evidence(session, user, data.get("evidence", []) or [], employer_ids)
    _load_certifications(session, user, professional.get("certifications", []) or [])
    _load_education(session, user, professional.get("education", []) or [])
    _load_tracks(session, user, data.get("career_tracks", []) or [])
    session.flush()
    return user


def _load_work_auth(session: Session, user: User, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        country = (row.get("country") or "US").upper()
        item = session.scalars(
            select(WorkAuthorization).where(
                WorkAuthorization.user_id == user.id, WorkAuthorization.country_code == country
            )
        ).first() or WorkAuthorization(user_id=user.id, country_code=country)
        item.status_id = row.get("status", "unknown")
        item.valid_until = _as_date(row.get("valid_until"))
        item.needs_sponsorship = bool(row.get("needs_sponsorship", False))
        item.employment_preferences = row.get("employment_preferences", []) or []
        item.notes = row.get("notes")
        if item.id is None:
            session.add(item)


def _load_employers(session: Session, user: User, rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        item = session.scalars(
            select(Employer).where(Employer.user_id == user.id, Employer.name == row["name"])
        ).first() or Employer(user_id=user.id, name=row["name"])
        item.industry = row.get("industry")
        item.location = row.get("location")
        item.country_code = (row.get("country") or user.home_country).upper()
        item.employment_type = row.get("employment_type")
        item.start_date = _as_date(row.get("start_date"))
        item.end_date = _as_date(row.get("end_date"))
        item.titles = row.get("titles", []) or []
        if item.id is None:
            session.add(item)
        session.flush()
        out[row["name"]] = item.id
    return out


def _load_evidence(
    session: Session, user: User, rows: list[dict[str, Any]], employer_ids: dict[str, int]
) -> None:
    for row in rows:
        text = row["text"]
        item = session.scalars(
            select(EvidenceItem).where(EvidenceItem.user_id == user.id, EvidenceItem.text == text)
        ).first() or EvidenceItem(user_id=user.id, text=text)
        item.kind = row.get("kind", EvidenceKind.RESPONSIBILITY.value)
        item.employer_id = employer_ids.get(row.get("employer", ""))
        item.project = row.get("project")
        item.role_title = row.get("role_title")
        item.technologies = row.get("technologies", []) or []
        item.skills = row.get("skills", []) or []
        item.domains = row.get("domains", []) or []
        item.start_date = _as_date(row.get("start_date"))
        item.end_date = _as_date(row.get("end_date"))
        item.metrics = row.get("metrics", {}) or {}
        item.verification = (
            VerificationState.VERIFIED.value if row.get("verified", False)
            else VerificationState.UNVERIFIED.value
        )
        item.source = row.get("source", "profile.yaml")
        item.strength = float(row.get("strength", 0.5))
        item.tags = row.get("tags", []) or []
        if item.id is None:
            session.add(item)


def _load_certifications(session: Session, user: User, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        item = session.scalars(
            select(Certification).where(Certification.user_id == user.id, Certification.name == row["name"])
        ).first() or Certification(user_id=user.id, name=row["name"])
        item.issuer = row.get("issuer")
        item.issued_on = _as_date(row.get("issued_on"))
        item.expires_on = _as_date(row.get("expires_on"))
        item.credential_id = row.get("credential_id")
        item.skills = row.get("skills", []) or []
        item.verification = (
            VerificationState.VERIFIED.value if row.get("verified", True)
            else VerificationState.UNVERIFIED.value
        )
        if item.id is None:
            session.add(item)


def _load_education(session: Session, user: User, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        item = session.scalars(
            select(Education).where(
                Education.user_id == user.id,
                Education.degree == row["degree"],
                Education.institution == row["institution"],
            )
        ).first() or Education(user_id=user.id, degree=row["degree"], institution=row["institution"])
        item.field = row.get("field")
        item.country_code = (row.get("country") or user.home_country).upper()
        item.started_on = _as_date(row.get("started_on"))
        item.completed_on = _as_date(row.get("completed_on"))
        if item.id is None:
            session.add(item)


def _load_tracks(session: Session, user: User, rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows):
        domain_id = row.get("domain", "other")
        if session.get(CareerDomain, domain_id) is None:
            session.add(
                CareerDomain(id=domain_id, label=row.get("name", domain_id), origin="user")
            )
            session.flush()
        item = session.scalars(
            select(CareerTrack).where(CareerTrack.user_id == user.id, CareerTrack.name == row["name"])
        ).first() or CareerTrack(user_id=user.id, name=row["name"], domain_id=domain_id)
        item.domain_id = domain_id
        item.active = bool(row.get("active", True))
        item.priority = int(row.get("priority", index + 1))
        item.preferred_titles = row.get("preferred_titles", []) or []
        item.preferred_industries = row.get("preferred_industries", []) or []
        item.core_skills = row.get("core_skills", []) or []
        item.min_match_score = float(row.get("min_match_score", 60))
        item.salary_floor = row.get("salary_floor")
        item.salary_currency = row.get("salary_currency")
        item.countries = [c.upper() for c in (row.get("countries", []) or [])]
        if item.id is None:
            session.add(item)


def export_profile(session: Session, user: User) -> dict[str, Any]:
    """Round-trip the database back into the YAML shape."""
    from careeros.services import load_certifications, load_education, load_evidence_records

    employers = session.scalars(select(Employer).where(Employer.user_id == user.id)).all()
    tracks = session.scalars(select(CareerTrack).where(CareerTrack.user_id == user.id)).all()
    auth = session.scalars(select(WorkAuthorization).where(WorkAuthorization.user_id == user.id)).all()
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
        "work_authorization": [
            {
                "country": a.country_code,
                "status": a.status_id,
                "needs_sponsorship": a.needs_sponsorship,
                "employment_preferences": a.employment_preferences,
            }
            for a in auth
        ],
        "employers": [
            {
                "name": e.name,
                "industry": e.industry,
                "location": e.location,
                "country": e.country_code,
                "start_date": e.start_date.isoformat() if e.start_date else None,
                "end_date": e.end_date.isoformat() if e.end_date else None,
                "titles": e.titles,
            }
            for e in employers
        ],
        "career_tracks": [
            {
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
        "evidence": [
            {
                "kind": r.kind,
                "text": r.text,
                "employer": r.employer,
                "role_title": r.role_title,
                "technologies": list(r.technologies),
                "skills": list(r.skills),
                "metrics": r.metrics,
                "verified": r.verified,
                "strength": r.strength,
            }
            for r in load_evidence_records(session, user.id)
        ],
    }
