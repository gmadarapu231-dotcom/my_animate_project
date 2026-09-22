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


@router.get("/{job_id}/ats")
def ats_report(
    job_id: int,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """The posting's own keywords, and where the résumé stands on each.

    Two scores where a tailored résumé exists: the baseline from the master
    résumé and the tailored one, so the lift is a measured number. The keyword
    verdicts say which terms the résumé carries, which were added in the
    posting's wording because evidence supports them, and which are genuine
    gaps that no amount of rewording can close.
    """
    from careeros.db.models import ResumeDocument
    from careeros.engines.ats_align import AtsAligner, KeywordVerdict
    from careeros.enums import ResumeKind
    from careeros.services import load_evidence_records

    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.classification is None:
        raise HTTPException(409, "This job has not been classified yet. Run an assessment first.")

    keywords = list(job.classification.ats_keywords or [])

    rows = session.scalars(
        select(AtsAssessment).where(AtsAssessment.job_id == job.id).order_by(AtsAssessment.id)
    ).all()
    tailored = session.scalars(
        select(ResumeDocument)
        .where(
            ResumeDocument.user_id == user.id,
            ResumeDocument.job_id == job.id,
            ResumeDocument.kind == ResumeKind.TAILORED.value,
        )
        .order_by(ResumeDocument.version.desc())
    ).first()

    tailored_row = next((r for r in rows if tailored and r.resume_id == tailored.id), None)
    baseline_row = next((r for r in rows if r is not tailored_row), None)

    evidence = load_evidence_records(session, user.id)
    plan = None
    if tailored is not None:
        from careeros.engines.resume import Resume, ResumeBlock, ResumeEntry, ResumeSection

        # Re-decide against the stored résumé rather than re-tailoring: this is
        # a report, and it must describe the document that would actually go out.
        rebuilt = Resume(name=tailored.name, kind=ResumeKind.TAILORED)
        for section in tailored.sections or []:
            rebuilt.sections.append(
                ResumeSection(
                    name=section.get("name", ""),
                    kind=section.get("kind", ""),
                    entries=[
                        ResumeEntry(
                            heading=entry.get("heading"),
                            subheading=entry.get("subheading"),
                            meta=entry.get("meta"),
                            blocks=[
                                ResumeBlock(
                                    text=block.get("text", ""),
                                    evidence_ids=list(block.get("evidence_ids") or []),
                                )
                                for block in entry.get("blocks") or []
                            ],
                        )
                        for entry in section.get("entries") or []
                    ],
                )
            )
        plan = AtsAligner().align(rebuilt, evidence, keywords, apply=False).to_dict()

    lift = None
    if baseline_row and tailored_row:
        lift = {
            "overall": round(tailored_row.overall - baseline_row.overall, 1),
            "keyword_match": round(tailored_row.keyword_match - baseline_row.keyword_match, 1),
        }

    return {
        "job_id": job.id,
        "title": job.title,
        "company": job.company,
        "keywords_from_posting": keywords,
        "keyword_count": len(keywords),
        "baseline": ats_view(baseline_row),
        "tailored": ats_view(tailored_row),
        "lift": lift,
        "resume_id": tailored.id if tailored else None,
        "resume_is_final": bool(tailored.is_final) if tailored else None,
        "alignment": plan,
        "how_it_works": (
            "Keywords come from this posting, not from an industry list. A term is only "
            "added to the résumé when your own evidence already supports the capability it "
            "names — the claim never changes, only the wording. Terms nothing supports are "
            "reported as gaps and left off."
        ),
    }
