"""Job/application payloads shared by the API and the dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.config import countries
from careeros.db.models import (
    Application,
    AtsAssessment,
    CareerTrack,
    EligibilityAssessment,
    Job,
)
from careeros.engines.salary import SalaryNormalizer
from careeros.enums import DeadlineBucket


def salary_view(job: Job) -> dict[str, Any]:
    """Original string first, normalised annual figure alongside it."""
    pack = countries().get(job.country_code)
    parsed = SalaryNormalizer(pack).parse(job.salary_raw) if pack else None
    return {
        "raw": job.salary_raw,
        "display": parsed.display(pack.currency_symbol) if parsed and pack else (job.salary_raw or "Not disclosed"),
        "currency": job.salary_currency,
        "annual_min": job.salary_min,
        "annual_max": job.salary_max,
        "period": job.salary_period,
        "components": job.salary_components or [],
    }


def job_card(session: Session, job: Job, user_id: int | None = None) -> dict[str, Any]:
    """Everything the job card in the dashboard renders."""
    classification = job.classification
    priority = job.priority
    bucket = DeadlineBucket(priority.deadline_bucket) if priority else DeadlineBucket.NONE
    pack = countries().get(job.country_code)

    eligibility = None
    if user_id is not None:
        eligibility = session.scalars(
            select(EligibilityAssessment).where(
                EligibilityAssessment.user_id == user_id, EligibilityAssessment.job_id == job.id
            )
        ).first()

    ats = session.scalars(
        select(AtsAssessment).where(AtsAssessment.job_id == job.id).order_by(AtsAssessment.id.desc())
    ).first()

    application = None
    if user_id is not None:
        application = session.scalars(
            select(Application).where(Application.user_id == user_id, Application.job_id == job.id)
        ).first()

    track = session.get(CareerTrack, priority.best_track_id) if priority and priority.best_track_id else None

    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "url": job.url,
        "source": job.source,
        "country": job.country_code,
        "country_name": pack.name if pack else job.country_code,
        "location": pack.format_location(job.city, job.region) if pack else (job.city or ""),
        "work_arrangement": job.work_arrangement,
        "employment_type": job.employment_type,
        "salary": salary_view(job),
        "posted_on": job.posted_on.isoformat() if job.posted_on else None,
        "deadline_on": job.deadline_on.isoformat() if job.deadline_on else None,
        "applicant_count": job.applicant_count,
        "archived": job.archived,
        "classification": {
            "domain_id": classification.domain_id,
            "domain_label": classification.domain_label,
            "function": classification.function,
            "specialization": classification.specialization,
            "industry": classification.industry,
            "seniority": classification.seniority,
            "min_experience_years": classification.min_experience_years,
            "education_requirement": classification.education_requirement,
            "required_skills": classification.required_skills,
            "preferred_skills": classification.preferred_skills,
            "required_certifications": classification.required_certifications,
            "soft_skills": classification.soft_skills,
            "ats_keywords": classification.ats_keywords,
            "confidence": classification.confidence,
            "method": classification.method,
            "rationale": classification.rationale,
        } if classification else None,
        "eligibility": {
            "verdict": eligibility.verdict,
            "score": eligibility.score,
            "status": eligibility.user_status_id,
            "employment_type_match": eligibility.employment_type_match,
            "reasons": eligibility.reasons,
            "evidence": eligibility.evidence,
            "disclaimer": eligibility.disclaimer,
        } if eligibility else None,
        "scores": {
            "match": priority.match_score if priority else None,
            "eligibility": priority.eligibility_score if priority else None,
            "urgency": priority.urgency_score if priority else None,
            "competition": priority.competition_score if priority else None,
            "priority": priority.overall if priority else None,
            "ats": ats.overall if ats else None,
        },
        "priority": {
            "overall": priority.overall,
            "bucket": bucket.value,
            "flag": f"{bucket.emoji} {bucket.label}",
            "emoji": bucket.emoji,
            "days_to_deadline": priority.days_to_deadline,
            "explanation": priority.explanation,
            "best_track": track.name if track else None,
        } if priority else None,
        "application": {
            "id": application.id,
            "status": application.status,
            "applied_on": application.applied_on.isoformat() if application.applied_on else None,
            "notes": application.notes,
        } if application else None,
    }


def ats_view(row: AtsAssessment | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "overall": row.overall,
        "keyword_match": row.keyword_match,
        "required_skills_match": row.required_skills_match,
        "preferred_skills_match": row.preferred_skills_match,
        "experience_match": row.experience_match,
        "title_match": row.title_match,
        "education_match": row.education_match,
        "certification_match": row.certification_match,
        "formatting_score": row.formatting_score,
        "matched_keywords": row.matched_keywords,
        "missing_keywords": row.missing_keywords,
        "suggestions": row.suggestions,
    }
