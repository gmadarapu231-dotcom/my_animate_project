"""The agent's tool surface: the CareerOS engines, exposed as callable tools.

Each tool is a thin, typed wrapper over an engine or a service that already
exists. Nothing here contains career logic of its own -- if the agent and the
scheduled pipeline disagree about a match score, that is a bug, because both
call the same code.

Two rules shape this file:

* **Outputs are compact.** Every result is re-entering the context window on
  the next turn, so tools return summaries with ids to drill into, not object
  dumps. `get_job` returns a 1200-character description excerpt, not the whole
  posting.
* **Mutating tools carry their own gate.** `tailor_resume` runs the factuality
  checker before returning; `draft_email_reply` runs high-impact detection.
  The check is part of the action, so the agent cannot perform the action
  without it.

What is deliberately absent is documented in `careeros.agent.guards`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.agent.guards import (
    assert_tool_surface_is_safe,
    enforce_draft_gate,
    enforce_resume_gate,
)
from careeros.analytics import Analytics
from careeros.db.models import (
    Application,
    ApplicationEvent,
    AtsAssessment,
    CoverLetter,
    EligibilityAssessment,
    EmailDraft,
    EmailMessage,
    Job,
    User,
)
from careeros.enums import ApplicationStatus, AutomationMode, DeadlineBucket, EmailCategory
from careeros.gmail.drafts import DraftGenerator
from careeros.pipeline import Pipeline
from careeros.search import QueryParser, run_search
from careeros.services import (
    build_profile_index,
    load_certifications,
    load_education,
    load_evidence_records,
)

DESCRIPTION_EXCERPT = 1200


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    mutating: bool = False

    def definition(self) -> dict[str, Any]:
        """The wire format for the Messages API `tools` array."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class AgentContext:
    """Everything the tools need, resolved once per run."""

    session: Session
    user: User
    pipeline: Pipeline
    use_ai_inside_tools: bool = True
    #: Names of mutating tools that actually ran, for the run summary.
    mutations: list[str] = field(default_factory=list)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
def _job_summary(job: Job) -> dict[str, Any]:
    priority, classification = job.priority, job.classification
    bucket = DeadlineBucket(priority.deadline_bucket) if priority else DeadlineBucket.NONE
    return {
        "job_id": job.id,
        "title": job.title,
        "company": job.company,
        "country": job.country_code,
        "location": ", ".join(p for p in (job.city, job.region) if p),
        "domain": classification.domain_id if classification else None,
        "seniority": classification.seniority if classification else None,
        "employment_type": job.employment_type,
        "salary": job.salary_raw,
        "deadline": job.deadline_on.isoformat() if job.deadline_on else None,
        "deadline_bucket": bucket.value,
        "applicants": job.applicant_count,
        "priority": priority.overall if priority else None,
        "match": priority.match_score if priority else None,
        "eligibility": priority.eligibility_score if priority else None,
    }


def make_list_jobs(ctx: AgentContext) -> ToolSpec:
    def handler(
        country: str | None = None,
        domain: str | None = None,
        deadline_bucket: str | None = None,
        include_ineligible: bool = True,
        limit: int = 15,
    ) -> dict[str, Any]:
        jobs = ctx.pipeline.ranked_jobs()
        if country:
            jobs = [j for j in jobs if j.country_code == country.upper()]
        if domain:
            jobs = [j for j in jobs if j.classification and j.classification.domain_id == domain]
        if deadline_bucket:
            jobs = [j for j in jobs if j.priority and j.priority.deadline_bucket == deadline_bucket]
        if not include_ineligible:
            jobs = [j for j in jobs if j.priority and j.priority.eligibility_score > 0]
        return {
            "count": len(jobs),
            "note": "Ordered by deadline tier first, then priority score.",
            "jobs": [_job_summary(j) for j in jobs[: min(limit, 40)]],
        }

    return ToolSpec(
        name="list_jobs",
        description=(
            "List discovered jobs in ranked order (deadline tier first, then priority "
            "score). Use this to orient before drilling into specific jobs. Filter by "
            "country code, career domain id, or deadline bucket "
            "(today | within_48h | within_7d | future | none)."
        ),
        input_schema=_obj(
            {
                "country": {"type": "string", "description": "ISO country code, e.g. US or IN"},
                "domain": {"type": "string", "description": "Career domain id, e.g. cybersecurity"},
                "deadline_bucket": {
                    "type": "string",
                    "enum": ["today", "within_48h", "within_7d", "future", "none"],
                },
                "include_ineligible": {
                    "type": "boolean",
                    "description": "Include jobs ruled out by work authorization (default true)",
                },
                "limit": {"type": "integer", "description": "Max jobs to return (default 15, max 40)"},
            }
        ),
        handler=handler,
    )


def make_get_job(ctx: AgentContext) -> ToolSpec:
    def handler(job_id: int) -> dict[str, Any]:
        job = ctx.session.get(Job, job_id)
        if job is None:
            return {"error": f"No job with id {job_id}"}
        classification, priority = job.classification, job.priority
        eligibility = ctx.session.scalars(
            select(EligibilityAssessment).where(
                EligibilityAssessment.user_id == ctx.user.id,
                EligibilityAssessment.job_id == job.id,
            )
        ).first()
        payload = _job_summary(job)
        payload["url"] = job.url
        payload["posted"] = job.posted_on.isoformat() if job.posted_on else None
        payload["description_excerpt"] = (job.description or "")[:DESCRIPTION_EXCERPT]
        if classification:
            payload["classification"] = {
                "domain": classification.domain_id,
                "domain_label": classification.domain_label,
                "function": classification.function,
                "specialization": classification.specialization,
                "industry": classification.industry,
                "min_experience_years": classification.min_experience_years,
                "education_requirement": classification.education_requirement,
                "required_skills": [s.get("name") for s in classification.required_skills or []],
                "preferred_skills": [s.get("name") for s in classification.preferred_skills or []],
                "required_certifications": classification.required_certifications,
                "top_ats_keywords": (classification.ats_keywords or [])[:15],
                "confidence": classification.confidence,
                "method": classification.method,
            }
        if eligibility:
            payload["work_authorization"] = {
                "verdict": eligibility.verdict,
                "reasons": eligibility.reasons,
                "source_quotes": [e.get("quote") for e in eligibility.evidence or []],
                "disclaimer": eligibility.disclaimer,
                "note": (
                    "This verdict comes from the country pack's rules applied to the "
                    "posting's own words. You cannot change it. Always repeat the "
                    "disclaimer when you report it."
                ),
            }
        if priority:
            payload["why_this_rank"] = priority.explanation
        return payload

    return ToolSpec(
        name="get_job",
        description=(
            "Full detail for one job: classification, required and preferred skills, "
            "ATS keywords, the work-authorization verdict with the exact posting "
            "phrases behind it, and why it ranks where it does. Includes a description "
            "excerpt so you can read the posting's own wording."
        ),
        input_schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=handler,
    )


def make_search_jobs(ctx: AgentContext) -> ToolSpec:
    def handler(query: str, limit: int = 15) -> dict[str, Any]:
        filters = QueryParser().parse(query, use_ai=False)
        jobs = run_search(ctx.session, filters, user_id=ctx.user.id, limit=min(limit, 40))
        return {
            "query": query,
            "interpreted_as": filters.describe(),
            "count": len(jobs),
            "jobs": [_job_summary(j) for j in jobs],
        }

    return ToolSpec(
        name="search_jobs",
        description=(
            "Search jobs with a natural-language query, e.g. 'H1B-friendly cybersecurity "
            "jobs in Texas', 'remote data engineering in India', 'deadline today', "
            "'match above 85%'. Returns how the query was interpreted alongside the "
            "results, so you can tell whether the filter matched your intent."
        ),
        input_schema=_obj(
            {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]
        ),
        handler=handler,
    )


def make_get_profile(ctx: AgentContext) -> ToolSpec:
    def handler() -> dict[str, Any]:
        from careeros.db.models import CareerTrack, WorkAuthorization

        tracks = ctx.session.scalars(
            select(CareerTrack).where(CareerTrack.user_id == ctx.user.id)
            .order_by(CareerTrack.priority)
        ).all()
        auth = ctx.session.scalars(
            select(WorkAuthorization).where(WorkAuthorization.user_id == ctx.user.id)
        ).all()
        return {
            "name": ctx.user.full_name,
            "current_title": ctx.user.current_title,
            "total_experience_years": ctx.user.total_experience_years,
            "home_country": ctx.user.home_country,
            "location": ", ".join(p for p in (ctx.user.home_city, ctx.user.home_region) if p),
            "preferred_locations": ctx.user.preferred_locations,
            "remote_preference": ctx.user.remote_preference,
            "open_to_relocation": ctx.user.open_to_relocation,
            "industries": ctx.user.industries,
            "automation_mode": ctx.user.automation_mode,
            "certifications": [c["name"] for c in load_certifications(ctx.session, ctx.user.id)],
            "education": [
                f"{e['degree']}{', ' + e['field'] if e.get('field') else ''} - {e['institution']}"
                for e in load_education(ctx.session, ctx.user.id)
            ],
            "work_authorization": [
                {
                    "country": a.country_code,
                    "status": a.status_id,
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
                    "preference_rank": t.priority,
                    "min_match_score": t.min_match_score,
                    "countries": t.countries,
                }
                for t in tracks
            ],
        }

    return ToolSpec(
        name="get_profile",
        description=(
            "The user's Master Profile: experience, location and mobility, work "
            "authorization per country, credentials, and their parallel career tracks. "
            "Read this before giving any strategic advice."
        ),
        input_schema=_obj({}),
        handler=handler,
    )


def make_search_evidence(ctx: AgentContext) -> ToolSpec:
    def handler(query: str | None = None, skill: str | None = None, limit: int = 15) -> dict[str, Any]:
        records = load_evidence_records(ctx.session, ctx.user.id)
        if skill:
            scanner = ctx.pipeline.matcher.scanner
            records = [
                r for r in records
                if skill in set(scanner.scan(r.text)) | {s for s in (r.skills or ()) if s}
            ]
        if query:
            needle = query.lower()
            words = [w for w in needle.split() if len(w) > 2]
            records = [
                r for r in records
                if any(w in f"{r.text} {r.role_title or ''} {' '.join(r.technologies or ())}".lower()
                       for w in words)
            ]
        return {
            "count": len(records),
            "note": (
                "This is the Career Evidence Database - the ONLY permitted source for "
                "any resume or cover-letter claim. You cannot add to it."
            ),
            "evidence": [
                {
                    "evidence_id": r.id,
                    "kind": r.kind,
                    "text": r.text,
                    "employer": r.employer,
                    "role_title": r.role_title,
                    "technologies": list(r.technologies),
                    "metrics": r.metrics,
                    "verified": r.verified,
                    "strength": r.strength,
                }
                for r in records[: min(limit, 40)]
            ],
        }

    return ToolSpec(
        name="search_evidence",
        description=(
            "Search the Career Evidence Database - the atomic verified facts every "
            "resume claim must trace back to. Filter by free text or by a skill-graph "
            "id. Use this to check what the user can actually evidence before you "
            "claim anything on their behalf."
        ),
        input_schema=_obj(
            {
                "query": {"type": "string", "description": "Free-text match on evidence text"},
                "skill": {"type": "string", "description": "Skill-graph id, e.g. iam or sap_grc"},
                "limit": {"type": "integer"},
            }
        ),
        handler=handler,
    )


def make_get_match_detail(ctx: AgentContext) -> ToolSpec:
    def handler(job_id: int) -> dict[str, Any]:
        job = ctx.session.get(Job, job_id)
        if job is None or job.classification is None:
            return {"error": f"Job {job_id} not found or not yet classified"}
        classification = ctx.pipeline.classification_from_row(job.classification)
        profile = build_profile_index(ctx.session, ctx.user, ctx.pipeline.matcher)
        result = ctx.pipeline.matcher.match(classification, profile, job_title=job.title)
        return {
            "job_id": job.id,
            "match_score": result.match_score,
            "skill_score": result.skill_score,
            "experience_match": result.experience_match,
            "title_match": result.title_match,
            "counts": {
                "direct": result.direct_count,
                "partial": result.partial_count,
                "related": result.related_count,
                "missing": result.missing_count,
            },
            "requirements": [
                {
                    "requirement": m.requirement,
                    "level": m.level,
                    "kind": m.kind.value,
                    "claimable": m.claimable,
                    "evidenced_by_skill": m.via_skill_id,
                    "evidence_ids": m.evidence_ids,
                    "note": m.note,
                }
                for m in result.matches
            ],
            "gaps_required_but_unevidenced": result.gaps,
            "transferable_not_claimable": result.transferable,
            "note": (
                "`claimable: false` means the user has adjacent experience but NOT this "
                "requirement. You may tell the user it is transferable. You may never "
                "present it to an employer as direct experience."
            ),
        }

    return ToolSpec(
        name="get_match_detail",
        description=(
            "Per-requirement breakdown of how the user matches one job: which "
            "requirements are directly evidenced, which are partially or only "
            "transferably covered, which are outright gaps, and the evidence ids "
            "behind each. The `claimable` flag tells you what a resume may assert."
        ),
        input_schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=handler,
    )


def make_get_ats_report(ctx: AgentContext) -> ToolSpec:
    def handler(job_id: int) -> dict[str, Any]:
        row = ctx.session.scalars(
            select(AtsAssessment).where(AtsAssessment.job_id == job_id)
            .order_by(AtsAssessment.id.desc())
        ).first()
        if row is None:
            return {"error": f"No ATS assessment for job {job_id}. Run analyze_job first."}
        return {
            "job_id": job_id,
            "overall": row.overall,
            "components": {
                "keyword_match": row.keyword_match,
                "required_skills_match": row.required_skills_match,
                "preferred_skills_match": row.preferred_skills_match,
                "experience_match": row.experience_match,
                "title_match": row.title_match,
                "education_match": row.education_match,
                "certification_match": row.certification_match,
                "formatting_score": row.formatting_score,
            },
            "missing_keywords": (row.missing_keywords or [])[:20],
            "suggestions": row.suggestions,
            "note": (
                "Suggestions may only be acted on where the Career Evidence Database "
                "supports the keyword. Never advise adding a keyword the user cannot evidence."
            ),
        }

    return ToolSpec(
        name="get_ats_report",
        description=(
            "ATS scoring for a job against the user's current resume: eight component "
            "scores, the highest-value keywords absent from the resume, and concrete "
            "suggestions. Keywords are derived from that specific posting, so this "
            "works the same for any industry."
        ),
        input_schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=handler,
    )


def make_list_applications(ctx: AgentContext) -> ToolSpec:
    def handler(status: str | None = None, limit: int = 30) -> dict[str, Any]:
        rows = ctx.session.execute(
            select(Application, Job).join(Job, Job.id == Application.job_id)
            .where(Application.user_id == ctx.user.id)
        ).all()
        if status:
            rows = [(a, j) for a, j in rows if a.status == status]
        return {
            "count": len(rows),
            "valid_statuses": [s.value for s in ApplicationStatus],
            "applications": [
                {
                    "application_id": a.id,
                    "job_id": j.id,
                    "title": j.title,
                    "company": j.company,
                    "country": j.country_code,
                    "status": a.status,
                    "applied_on": a.applied_on.isoformat() if a.applied_on else None,
                    "last_activity": a.last_activity_on.isoformat() if a.last_activity_on else None,
                    "match": j.priority.match_score if j.priority else None,
                }
                for a, j in rows[: min(limit, 60)]
            ],
        }

    return ToolSpec(
        name="list_applications",
        description="The application pipeline across all lifecycle states, optionally filtered by status.",
        input_schema=_obj(
            {"status": {"type": "string"}, "limit": {"type": "integer"}}
        ),
        handler=handler,
    )


def make_get_application(ctx: AgentContext) -> ToolSpec:
    def handler(application_id: int) -> dict[str, Any]:
        from careeros.db.models import RejectionAnalysis

        row = ctx.session.get(Application, application_id)
        if row is None or row.user_id != ctx.user.id:
            return {"error": f"No application with id {application_id}"}
        events = ctx.session.scalars(
            select(ApplicationEvent).where(ApplicationEvent.application_id == row.id)
            .order_by(ApplicationEvent.at)
        ).all()
        rejection = ctx.session.scalars(
            select(RejectionAnalysis).where(RejectionAnalysis.application_id == row.id)
        ).first()
        payload: dict[str, Any] = {
            "application_id": row.id,
            "job_id": row.job_id,
            "status": row.status,
            "notes": row.notes,
            "history": [
                {"at": e.at.isoformat(), "from": e.from_status, "to": e.to_status,
                 "source": e.source, "detail": e.detail}
                for e in events
            ],
        }
        if rejection:
            payload["rejection"] = {
                "stage": rejection.stage,
                "reason_stated_by_employer": rejection.explicit_reason,
                "exact_quote": rejection.explicit_quote,
                "careeros_hypotheses": rejection.possible_reasons,
                "note": (
                    "`reason_stated_by_employer` is fact. `careeros_hypotheses` are "
                    "guesses produced from our own scores - never report a hypothesis "
                    "as something the employer said."
                ),
            }
        return payload

    return ToolSpec(
        name="get_application",
        description=(
            "One application's full history: every status transition with its source "
            "(user, email or pipeline), and any rejection analysis. Rejection analysis "
            "keeps the employer's stated reason strictly apart from our hypotheses."
        ),
        input_schema=_obj({"application_id": {"type": "integer"}}, ["application_id"]),
        handler=handler,
    )


def make_get_analytics(ctx: AgentContext) -> ToolSpec:
    def handler() -> dict[str, Any]:
        data = Analytics(ctx.session).dashboard(ctx.user.id)
        return {
            "overall": data["overall"],
            "by_track": data["by_track"],
            "by_domain": data["by_domain"],
            "by_country": data["by_country"],
            "by_visa_verdict": data["by_visa_verdict"],
            "by_match_band": data["by_match_band"],
            "rejections": data["rejections"],
            "note": (
                "Any group with `confident: false` has too few applications for its "
                "rates to mean anything. Say so rather than drawing a conclusion."
            ),
        }

    return ToolSpec(
        name="get_analytics",
        description=(
            "Outcome analytics: the application funnel plus breakdowns by career track, "
            "domain, country, visa verdict and match band, and rejection statistics. "
            "Groups below the sample threshold are flagged as low-confidence."
        ),
        input_schema=_obj({}),
        handler=handler,
    )


def make_get_learning_signals(ctx: AgentContext) -> ToolSpec:
    def handler() -> dict[str, Any]:
        signals = Analytics(ctx.session).learning_signals(ctx.user.id)
        return {
            "count": len(signals),
            "signals": [s.to_dict() for s in signals],
            "note": (
                "Derived from the user's own outcome history. Recommend skills, "
                "certifications, resume changes or different targets. Never recommend "
                "claiming a qualification they do not hold."
            ),
        }

    return ToolSpec(
        name="get_learning_signals",
        description=(
            "The career learning loop's current findings: recurring skill gaps across "
            "targeted jobs, certifications the postings keep asking for, which track is "
            "converting, and whether the resume rather than the fit is the problem."
        ),
        input_schema=_obj({}),
        handler=handler,
    )


def make_list_email(ctx: AgentContext) -> ToolSpec:
    def handler(category: str | None = None, requires_action: bool | None = None, limit: int = 20) -> dict[str, Any]:
        stmt = select(EmailMessage).where(EmailMessage.user_id == ctx.user.id)
        if category:
            stmt = stmt.where(EmailMessage.category == category)
        if requires_action is not None:
            stmt = stmt.where(EmailMessage.requires_action.is_(requires_action))
        rows = ctx.session.scalars(stmt.order_by(EmailMessage.received_at.desc())).all()
        return {
            "count": len(rows),
            "categories": [c.value for c in EmailCategory],
            "messages": [
                {
                    "email_id": m.id,
                    "category": m.category,
                    "sender": m.sender,
                    "subject": m.subject,
                    "received_at": m.received_at.isoformat() if m.received_at else None,
                    "application_id": m.application_id,
                    "requires_action": m.requires_action,
                    "action_due_on": m.action_due_on.isoformat() if m.action_due_on else None,
                    "extracted": {
                        k: v for k, v in (m.extracted or {}).items()
                        if k in {"company", "position", "interview_datetime",
                                 "assessment_deadline", "required_action",
                                 "rejection_reason_explicit"} and v
                    },
                }
                for m in rows[: min(limit, 50)]
            ],
        }

    return ToolSpec(
        name="list_email_messages",
        description=(
            "Classified job-related email with the fields extracted from each message "
            "(company, position, interview time, assessment deadline, required action). "
            "Filter by category or by whether the message needs action."
        ),
        input_schema=_obj(
            {
                "category": {"type": "string"},
                "requires_action": {"type": "boolean"},
                "limit": {"type": "integer"},
            }
        ),
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Mutating tools -- each carries its own gate
# ---------------------------------------------------------------------------
def make_analyze_job(ctx: AgentContext) -> ToolSpec:
    def handler(job_id: int) -> dict[str, Any]:
        job = ctx.session.get(Job, job_id)
        if job is None:
            return {"error": f"No job with id {job_id}"}
        ctx.pipeline.classify_jobs([job])
        ctx.pipeline.assess_for_user(ctx.user, jobs=[job])
        ctx.session.flush()
        ctx.mutations.append("analyze_job")
        return make_get_job(ctx).handler(job_id=job_id)

    return ToolSpec(
        name="analyze_job",
        description=(
            "Re-run the full analysis for one job: classification, work-authorization "
            "eligibility, skill match, ATS score and priority. Use when a job looks "
            "unclassified or stale. Returns the refreshed detail."
        ),
        input_schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=handler,
        mutating=True,
    )


def make_tailor_resume(ctx: AgentContext) -> ToolSpec:
    def handler(job_id: int) -> dict[str, Any]:
        job = ctx.session.get(Job, job_id)
        if job is None:
            return {"error": f"No job with id {job_id}"}
        try:
            doc, report = ctx.pipeline.tailor_for_job(
                ctx.user, job, use_ai=ctx.use_ai_inside_tools
            )
        except ValueError as exc:
            return {"error": str(exc)}
        ctx.session.flush()
        ctx.mutations.append("tailor_resume")
        # The gate: is_final is set from the factuality verdict, here, always.
        return enforce_resume_gate(doc, report)

    return ToolSpec(
        name="tailor_resume",
        description=(
            "Build a job-specific resume assembled from the Career Evidence Database, "
            "then run the factuality check. The check always runs and its verdict "
            "always comes back: a resume with any unsupported claim returns "
            "`is_final: false` and cannot be sent. Gaps the user cannot evidence are "
            "reported, never filled in."
        ),
        input_schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=handler,
        mutating=True,
    )


def make_draft_cover_letter(ctx: AgentContext) -> ToolSpec:
    def handler(job_id: int) -> dict[str, Any]:
        job = ctx.session.get(Job, job_id)
        if job is None or job.classification is None:
            return {"error": f"Job {job_id} not found or not yet classified"}
        classification = ctx.pipeline.classification_from_row(job.classification)
        evidence = load_evidence_records(ctx.session, ctx.user.id)
        profile = build_profile_index(ctx.session, ctx.user, ctx.pipeline.matcher)
        match = ctx.pipeline.matcher.match(classification, profile, job_title=job.title)

        used_ids = sorted({i for m in match.matches if m.claimable for i in m.evidence_ids})
        by_id = {e.id: e for e in evidence}
        highlights = [by_id[i].text for i in used_ids[:3] if i in by_id]

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
            f"Thank you for your consideration.\n\n{ctx.user.full_name}"
        )
        row = CoverLetter(user_id=ctx.user.id, job_id=job.id, body=body, evidence_ids=used_ids)
        ctx.session.add(row)
        ctx.session.flush()
        ctx.mutations.append("draft_cover_letter")
        return {
            "cover_letter_id": row.id,
            "body": body,
            "evidence_ids_used": used_ids,
            "gaps_acknowledged_not_hidden": match.gaps,
            "note": (
                "Assembled only from claimable evidence. Naming the role applied for is "
                "fine; claiming experience the evidence does not support is not."
            ),
        }

    return ToolSpec(
        name="draft_cover_letter",
        description=(
            "Draft a cover letter for a job using only evidence the user can actually "
            "claim for it. Requirements they cannot evidence are acknowledged as gaps "
            "rather than papered over."
        ),
        input_schema=_obj({"job_id": {"type": "integer"}}, ["job_id"]),
        handler=handler,
        mutating=True,
    )


def make_draft_email_reply(ctx: AgentContext) -> ToolSpec:
    def handler(email_id: int) -> dict[str, Any]:
        message = ctx.session.get(EmailMessage, email_id)
        if message is None or message.user_id != ctx.user.id:
            return {"error": f"No email with id {email_id}"}
        extracted = message.extracted or {}
        try:
            category = EmailCategory(message.category)
        except ValueError:
            category = EmailCategory.OTHER

        draft = DraftGenerator(provider=ctx.pipeline.provider).generate(
            category=category,
            subject=message.subject or "",
            body=message.snippet or "",
            candidate_first_name=(ctx.user.full_name or "").split()[0] or "there",
            recruiter_name=extracted.get("recruiter_name"),
            company=extracted.get("company"),
            position=extracted.get("position"),
            interview_datetime=extracted.get("interview_datetime"),
            assessment_deadline=extracted.get("assessment_deadline"),
            automation_mode=AutomationMode(ctx.user.automation_mode),
            use_ai=ctx.use_ai_inside_tools,
        )
        row = EmailDraft(
            user_id=ctx.user.id,
            email_id=message.id,
            application_id=message.application_id,
            intent=draft.intent,
            subject=draft.subject,
            body=draft.body,
            state=draft.state.value,
            high_impact_topics=draft.high_impact_topics,
        )
        ctx.session.add(row)
        ctx.session.flush()
        ctx.mutations.append("draft_email_reply")
        # The gate: high-impact detection always runs and always reports.
        payload = enforce_draft_gate(draft)
        payload["draft_id"] = row.id
        return payload

    return ToolSpec(
        name="draft_email_reply",
        description=(
            "Draft a reply to a classified job email. High-impact topics (immigration, "
            "sponsorship commitments, salary negotiation, contracts, offers, "
            "resignation, legal) are detected on both the incoming message and the "
            "generated reply; when any fires the draft is held and cannot be approved "
            "or sent. Nothing here sends mail - there is no send tool."
        ),
        input_schema=_obj({"email_id": {"type": "integer"}}, ["email_id"]),
        handler=handler,
        mutating=True,
    )


def make_update_application_status(ctx: AgentContext) -> ToolSpec:
    def handler(application_id: int, status: str, detail: str | None = None) -> dict[str, Any]:
        row = ctx.session.get(Application, application_id)
        if row is None or row.user_id != ctx.user.id:
            return {"error": f"No application with id {application_id}"}
        try:
            target = ApplicationStatus(status)
        except ValueError:
            return {
                "error": f"Unknown status '{status}'",
                "valid_statuses": [s.value for s in ApplicationStatus],
            }
        ctx.session.add(
            ApplicationEvent(
                application_id=row.id,
                from_status=row.status,
                to_status=target.value,
                source="agent",
                detail=detail,
            )
        )
        previous, row.status = row.status, target.value
        row.last_activity_on = date.today()
        if target is ApplicationStatus.APPLIED and row.applied_on is None:
            row.applied_on = date.today()
        ctx.session.flush()
        ctx.mutations.append("update_application_status")
        return {
            "application_id": row.id,
            "from": previous,
            "to": row.status,
            "recorded_source": "agent",
        }

    return ToolSpec(
        name="update_application_status",
        description=(
            "Move an application to a new lifecycle status, recording the transition "
            "with source 'agent' so it is distinguishable from the user's own updates "
            "and from email-driven ones. Only do this when the user asked, or when an "
            "email clearly established the new state."
        ),
        input_schema=_obj(
            {
                "application_id": {"type": "integer"},
                "status": {"type": "string", "description": "A valid ApplicationStatus value"},
                "detail": {"type": "string", "description": "Why the status changed"},
            },
            ["application_id", "status"],
        ),
        handler=handler,
        mutating=True,
    )


def make_add_application_note(ctx: AgentContext) -> ToolSpec:
    def handler(application_id: int, note: str) -> dict[str, Any]:
        row = ctx.session.get(Application, application_id)
        if row is None or row.user_id != ctx.user.id:
            return {"error": f"No application with id {application_id}"}
        existing = (row.notes or "").strip()
        row.notes = f"{existing}\n{note}".strip() if existing else note
        ctx.session.flush()
        ctx.mutations.append("add_application_note")
        return {"application_id": row.id, "notes": row.notes}

    return ToolSpec(
        name="add_application_note",
        description="Append a note to an application - context worth keeping for later.",
        input_schema=_obj(
            {"application_id": {"type": "integer"}, "note": {"type": "string"}},
            ["application_id", "note"],
        ),
        handler=handler,
        mutating=True,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_FACTORIES: tuple[Callable[[AgentContext], ToolSpec], ...] = (
    # read
    make_list_jobs,
    make_get_job,
    make_search_jobs,
    make_get_profile,
    make_search_evidence,
    make_get_match_detail,
    make_get_ats_report,
    make_list_applications,
    make_get_application,
    make_get_analytics,
    make_get_learning_signals,
    make_list_email,
    # mutating, each gated
    make_analyze_job,
    make_tailor_resume,
    make_draft_cover_letter,
    make_draft_email_reply,
    make_update_application_status,
    make_add_application_note,
)


def build_tools(ctx: AgentContext) -> dict[str, ToolSpec]:
    """Build the tool surface and re-assert that it is safe.

    The safety check runs every time rather than once at import, so a tool
    added later cannot slip a forbidden capability past review.
    """
    tools = {}
    for factory in _FACTORIES:
        spec = factory(ctx)
        tools[spec.name] = spec
    assert_tool_surface_is_safe(tools)
    return tools


def serialize_result(value: Any, max_chars: int = 12000) -> str:
    """Tool results go back into the context window, so cap their size."""
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f'… [truncated at {max_chars} chars; narrow your query]'
