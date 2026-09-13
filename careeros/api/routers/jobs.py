"""Job discovery, analysis and search endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db, pipeline
from careeros.api.serializers import ats_view, job_card
from careeros.config import countries
from careeros.db.models import AtsAssessment, CareerDomain, Job, MatchAssessment, User
from careeros.pipeline import Pipeline
from careeros.search import QueryParser, run_search
from careeros.sources.jsonfile import JsonFileSource

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class IngestRequest(BaseModel):
    path: str
    limit: int | None = None


class SearchRequest(BaseModel):
    query: str
    limit: int = 50
    use_ai: bool = True


@router.get("")
def list_jobs(
    country: str | None = None,
    domain: str | None = None,
    bucket: str | None = None,
    limit: int = Query(100, le=500),
    include_archived: bool = False,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    pipe: Pipeline = Depends(pipeline),
) -> dict[str, Any]:
    """Ranked job list. Deadline-today jobs always come first."""
    jobs = pipe.ranked_jobs(include_archived=include_archived)
    if country:
        jobs = [j for j in jobs if j.country_code == country.upper()]
    if domain:
        jobs = [j for j in jobs if j.classification and j.classification.domain_id == domain]
    if bucket:
        jobs = [j for j in jobs if j.priority and j.priority.deadline_bucket == bucket]
    return {
        "count": len(jobs),
        "jobs": [job_card(session, j, user.id) for j in jobs[:limit]],
    }


@router.get("/facets")
def facets(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Dashboard filter options, built from what is actually in the database."""
    domains = session.scalars(select(CareerDomain).order_by(CareerDomain.label)).all()
    used = {
        row[0]
        for row in session.execute(
            select(Job.country_code).where(Job.archived.is_(False)).distinct()
        ).all()
    }
    return {
        "domains": [
            {"id": d.id, "label": d.label, "origin": d.origin, "job_count": d.job_count}
            for d in domains if d.job_count
        ],
        "countries": [
            {"code": p.code, "name": p.name}
            for p in countries().all() if p.code in used
        ],
        "all_countries": [{"code": p.code, "name": p.name} for p in countries().all()],
        "buckets": [
            {"id": b, "label": label}
            for b, label in (
                ("today", "🔴 Deadline today"),
                ("within_48h", "🟠 Within 48 hours"),
                ("within_7d", "🟡 Within 7 days"),
                ("future", "🟢 Future"),
                ("none", "⚪ No deadline"),
                ("expired", "⚫ Expired"),
            )
        ],
    }


@router.get("/{job_id}")
def get_job(
    job_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    card = job_card(session, job, user.id)
    ats = session.scalars(
        select(AtsAssessment).where(AtsAssessment.job_id == job.id).order_by(AtsAssessment.id.desc())
    ).first()
    matches = session.scalars(select(MatchAssessment).where(MatchAssessment.job_id == job.id)).all()
    card["ats"] = ats_view(ats)
    card["matches"] = [
        {
            "track_id": m.track_id,
            "match_score": m.match_score,
            "direct": m.direct_count,
            "related": m.related_count,
            "partial": m.partial_count,
            "missing": m.missing_count,
            "gaps": m.gaps,
            "skill_matches": m.skill_matches,
        }
        for m in matches
    ]
    card["description"] = job.description
    return card


@router.post("/ingest")
def ingest(
    payload: IngestRequest,
    pipe: Pipeline = Depends(pipeline),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    stats = pipe.ingest([JsonFileSource(payload.path)], limit=payload.limit)
    pipe.classify_jobs(stats=stats)
    pipe.assess_for_user(user, stats=stats)
    return stats.to_dict()


@router.post("/{job_id}/analyze")
def analyze(
    job_id: int,
    pipe: Pipeline = Depends(pipeline),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    pipe.classify_jobs([job])
    pipe.assess_for_user(user, jobs=[job])
    session.flush()
    return job_card(session, job, user.id)


@router.post("/search")
def search(
    payload: SearchRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Natural-language search: "H1B-friendly cybersecurity jobs in Texas"."""
    filters = QueryParser().parse(payload.query, use_ai=payload.use_ai)
    jobs = run_search(session, filters, user_id=user.id, limit=payload.limit)
    return {
        "query": payload.query,
        "filters": filters.to_dict(),
        "filters_description": filters.describe(),
        "count": len(jobs),
        "jobs": [job_card(session, j, user.id) for j in jobs],
    }
