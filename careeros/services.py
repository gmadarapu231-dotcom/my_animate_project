"""Loaders that bridge the ORM and the (ORM-free) engines.

The engines take plain dataclasses on purpose: they are pure functions of their
inputs and can be unit-tested without a database. This module is the only place
that knows both sides.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.db.models import (
    Certification,
    Education,
    Employer,
    EvidenceItem,
    User,
    WorkAuthorization,
)
from careeros.engines.matching import EvidenceRef, ProfileIndex, SkillMatcher
from careeros.engines.resume import ContactInfo, EvidenceRecord
from careeros.engines.textutil import EDU_RANK, normalize
from careeros.enums import VerificationState


def load_user(session: Session, user_id: int | None = None) -> User | None:
    if user_id is not None:
        return session.get(User, user_id)
    return session.scalars(select(User).order_by(User.id)).first()


def _employer_map(session: Session, user_id: int) -> dict[int, Employer]:
    rows = session.scalars(select(Employer).where(Employer.user_id == user_id)).all()
    return {e.id: e for e in rows}


def load_evidence_records(session: Session, user_id: int) -> list[EvidenceRecord]:
    employers = _employer_map(session, user_id)
    items = session.scalars(
        select(EvidenceItem).where(EvidenceItem.user_id == user_id).order_by(EvidenceItem.id)
    ).all()
    out: list[EvidenceRecord] = []
    for item in items:
        employer = employers.get(item.employer_id) if item.employer_id else None
        out.append(
            EvidenceRecord(
                id=item.id,
                kind=item.kind,
                text=item.text,
                employer=employer.name if employer else None,
                role_title=item.role_title or (employer.titles[0] if employer and employer.titles else None),
                project=item.project,
                technologies=tuple(item.technologies or ()),
                skills=tuple(item.skills or ()),
                start_date=item.start_date or (employer.start_date if employer else None),
                end_date=item.end_date if item.end_date else (employer.end_date if employer else None),
                metrics=dict(item.metrics or {}),
                verified=item.verification == VerificationState.VERIFIED.value,
                strength=float(item.strength or 0.5),
            )
        )
    return out


def load_evidence_refs(session: Session, user_id: int) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            id=r.id,
            text=r.text,
            skills=r.skills,
            technologies=r.technologies,
            role_title=r.role_title,
            employer=r.employer,
            strength=r.strength,
            verified=r.verified,
        )
        for r in load_evidence_records(session, user_id)
    ]


def load_certifications(session: Session, user_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(Certification).where(Certification.user_id == user_id)).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "issuer": c.issuer,
            "issued_on": c.issued_on.isoformat() if c.issued_on else None,
            "expires_on": c.expires_on.isoformat() if c.expires_on else None,
            "skills": c.skills or [],
        }
        for c in rows
    ]


def load_education(session: Session, user_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(Education).where(Education.user_id == user_id)).all()
    return [
        {
            "id": e.id,
            "degree": e.degree,
            "field": e.field,
            "institution": e.institution,
            "completed_on": e.completed_on.year if e.completed_on else None,
        }
        for e in rows
    ]


def education_rank(education: Sequence[dict[str, Any]]) -> int:
    best = 0
    for row in education:
        deg = normalize(row.get("degree", ""))
        for level, rank in EDU_RANK.items():
            if level in deg:
                best = max(best, rank)
        # Spelled-out degrees.
        for needle, level in (
            ("bachelor", "bachelors"), ("master", "masters"), ("mba", "masters"),
            ("doctor", "phd"), ("ph.d", "phd"), ("associate", "associates"),
        ):
            if needle in deg:
                best = max(best, EDU_RANK[level])
    return best


def education_level(education: Sequence[dict[str, Any]]) -> str | None:
    rank = education_rank(education)
    for level, value in EDU_RANK.items():
        if value == rank:
            return level
    return None


def load_contact(session: Session, user: User) -> ContactInfo:
    return ContactInfo(
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        location=", ".join(p for p in (user.home_city, user.home_region) if p) or None,
        links=dict(user.links or {}),
    )


def build_profile_index(
    session: Session,
    user: User,
    matcher: SkillMatcher | None = None,
) -> ProfileIndex:
    matcher = matcher or SkillMatcher()
    refs = load_evidence_refs(session, user.id)
    certs = [c["name"] for c in load_certifications(session, user.id)]
    edu = load_education(session, user.id)
    return matcher.build_profile(
        refs,
        certifications=certs,
        total_experience_years=float(user.total_experience_years or 0.0),
        education_rank=education_rank(edu),
    )


def work_auth_for(session: Session, user_id: int, country_code: str) -> WorkAuthorization | None:
    return session.scalars(
        select(WorkAuthorization).where(
            WorkAuthorization.user_id == user_id,
            WorkAuthorization.country_code == country_code.upper(),
        )
    ).first()
