"""The daily automation loop.

    fetch -> dedupe -> classify -> eligibility -> match -> ATS -> priority
          -> rank -> tailor high-priority resumes -> recommend

Each stage is idempotent and independently callable, so a failure in one stage
never loses the work of the others and the whole run can be re-driven safely.
Stage errors are collected on the `PipelineRun` row rather than raised, because
a single malformed posting must not stop the morning run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.ai.provider import LLMProvider, get_provider
from careeros.config import countries, taxonomy
from careeros.db.models import (
    AtsAssessment,
    Application,
    CareerDomain,
    CareerTrack,
    EligibilityAssessment,
    Job,
    JobClassification,
    MatchAssessment,
    PipelineRun,
    PriorityScore,
    Recommendation,
    ResumeDocument,
    User,
)
from careeros.engines.ats import AtsEngine, ResumeFacts
from careeros.engines.classifier import ClassificationResult, JobClassifier, SkillReq
from careeros.engines.factuality import FactualityChecker
from careeros.engines.matching import MatchResult, SkillMatcher
from careeros.engines.priority import PriorityEngine, PriorityResult
from careeros.engines.resume import ResumeBuilder, render_text
from careeros.engines.salary import SalaryNormalizer
from careeros.engines.skills import default_scanner
from careeros.engines.workauth import WorkAuthorizationEngine
from careeros.enums import ApplicationStatus, AuthVerdict, DeadlineBucket, ResumeKind
from careeros.services import (
    build_profile_index,
    education_level,
    load_certifications,
    load_contact,
    load_education,
    load_evidence_records,
    load_user,
    work_auth_for,
)
from careeros.sources.base import JobSource, RawJob

logger = logging.getLogger(__name__)

#: Only the top slice of the ranked list gets a tailored resume each morning --
#: tailoring is the expensive stage and the user can only act on so many.
DEFAULT_TAILOR_LIMIT = 5


@dataclass
class StageStats:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    classified: int = 0
    assessed: int = 0
    matched: int = 0
    scored: int = 0
    tailored: int = 0
    new_domains: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "classified": self.classified,
            "assessed": self.assessed,
            "matched": self.matched,
            "scored": self.scored,
            "tailored": self.tailored,
            "new_domains": self.new_domains,
            "errors": self.errors,
        }


class Pipeline:
    def __init__(self, session: Session, provider: LLMProvider | None = None) -> None:
        self.session = session
        self.provider = provider or get_provider()
        scanner = default_scanner()
        self.taxonomy = taxonomy()
        self.classifier = JobClassifier(scanner=scanner, provider=self.provider)
        self.matcher = SkillMatcher(scanner=scanner)
        self.ats = AtsEngine(scanner=scanner)
        self.priority = PriorityEngine()
        self.resumes = ResumeBuilder(scanner=scanner, provider=self.provider)
        self.factuality = FactualityChecker(scanner=scanner)

    # ------------------------------------------------------------------
    # 1. Ingest + dedupe
    # ------------------------------------------------------------------
    def ingest(self, sources: Sequence[JobSource], limit: int | None = None, stats: StageStats | None = None) -> StageStats:
        stats = stats or StageStats()
        registry = countries()
        for source in sources:
            try:
                for raw in source.fetch(limit=limit):
                    stats.fetched += 1
                    if self._upsert_job(raw, registry):
                        stats.inserted += 1
                    else:
                        stats.duplicates += 1
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                stats.errors.append(f"ingest[{source.id}]: {type(exc).__name__}: {exc}")
                logger.exception("ingest failed for source %s", source.id)
        self.session.flush()
        return stats

    def _upsert_job(self, raw: RawJob, registry) -> bool:
        fingerprint = raw.fingerprint()
        existing = self.session.scalars(
            select(Job).where(Job.fingerprint == fingerprint)
        ).first()
        if existing:
            # Keep the richest copy: a later source may carry a deadline or an
            # applicant count the first one lacked.
            existing.deadline_on = existing.deadline_on or raw.deadline_on
            existing.applicant_count = existing.applicant_count or raw.applicant_count
            existing.salary_raw = existing.salary_raw or raw.salary_raw
            existing.url = existing.url or raw.url
            return False

        pack = registry.get(raw.country) or registry.detect(
            " ".join(filter(None, [raw.location, raw.city, raw.region, raw.description[:400]]))
        )
        salary = SalaryNormalizer(pack).parse(raw.salary_raw)

        job = Job(
            source=raw.source,
            external_id=raw.external_id,
            fingerprint=fingerprint,
            url=raw.url,
            title=raw.title,
            company=raw.company,
            description=raw.description,
            country_code=pack.code,
            city=raw.city or (raw.location.split(",")[0].strip() if raw.location else None),
            region=raw.region,
            employment_type=raw.employment_type,
            salary_raw=raw.salary_raw,
            salary_min=salary.annual_min,
            salary_max=salary.annual_max,
            salary_currency=salary.currency,
            salary_period=salary.period,
            salary_components=salary.components,
            posted_on=raw.posted_on,
            deadline_on=raw.deadline_on,
            applicant_count=raw.applicant_count,
            raw=raw.raw,
        )
        self.session.add(job)
        return True

    # ------------------------------------------------------------------
    # 2. Classify
    # ------------------------------------------------------------------
    def classify_jobs(self, jobs: Iterable[Job] | None = None, stats: StageStats | None = None) -> StageStats:
        stats = stats or StageStats()
        targets = list(jobs) if jobs is not None else self.session.scalars(
            select(Job).where(~Job.id.in_(select(JobClassification.job_id)))
        ).all()
        for job in targets:
            try:
                result = self.classifier.classify(job.title, job.description)
                self._persist_classification(job, result, stats)
                stats.classified += 1
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"classify[job {job.id}]: {type(exc).__name__}: {exc}")
                logger.exception("classification failed for job %s", job.id)
        self.session.flush()
        return stats

    def _persist_classification(self, job: Job, result: ClassificationResult, stats: StageStats) -> JobClassification:
        # A domain the taxonomy has never seen is recorded, not discarded.
        domain = self.session.get(CareerDomain, result.domain_id)
        if domain is None:
            domain = CareerDomain(
                id=result.domain_id,
                label=result.domain_label,
                origin="inferred",
                functions=[result.function] if result.function else [],
            )
            self.session.add(domain)
            stats.new_domains.append(result.domain_id)
        domain.job_count = (domain.job_count or 0) + 1

        if job.work_arrangement is None and result.work_arrangement:
            job.work_arrangement = result.work_arrangement

        row = job.classification or JobClassification(job_id=job.id)
        row.domain_id = result.domain_id
        row.domain_label = result.domain_label
        row.function = result.function
        row.specialization = result.specialization
        row.industry = result.industry
        row.seniority = result.seniority
        row.required_skills = [s.to_dict() for s in result.required_skills]
        row.preferred_skills = [s.to_dict() for s in result.preferred_skills]
        row.required_certifications = result.required_certifications
        row.soft_skills = result.soft_skills
        row.min_experience_years = result.min_experience_years
        row.education_requirement = result.education_requirement
        row.ats_keywords = result.ats_keywords
        row.confidence = result.confidence
        row.method = result.method
        row.rationale = result.rationale
        row.domain_scores = result.domain_scores
        if row.id is None:
            row.job = job
            self.session.add(row)
        return row

    @staticmethod
    def classification_from_row(row: JobClassification) -> ClassificationResult:
        """Rehydrate the engine dataclass from a persisted classification."""
        return ClassificationResult(
            domain_id=row.domain_id,
            domain_label=row.domain_label,
            function=row.function,
            specialization=row.specialization,
            industry=row.industry,
            seniority=row.seniority,
            min_experience_years=row.min_experience_years,
            education_requirement=row.education_requirement,
            required_skills=[SkillReq(**s) for s in (row.required_skills or [])],
            preferred_skills=[SkillReq(**s) for s in (row.preferred_skills or [])],
            required_certifications=list(row.required_certifications or []),
            soft_skills=list(row.soft_skills or []),
            ats_keywords=list(row.ats_keywords or []),
            confidence=row.confidence,
            method=row.method,
            rationale=row.rationale,
            domain_scores=dict(row.domain_scores or {}),
        )

    # ------------------------------------------------------------------
    # 3. Eligibility, match, ATS, priority
    # ------------------------------------------------------------------
    def assess_for_user(
        self,
        user: User,
        jobs: Iterable[Job] | None = None,
        today: date | None = None,
        stats: StageStats | None = None,
    ) -> StageStats:
        stats = stats or StageStats()
        today = today or date.today()
        registry = countries()

        targets = list(jobs) if jobs is not None else self.session.scalars(
            select(Job).where(Job.archived.is_(False))
        ).all()
        tracks = self.session.scalars(
            select(CareerTrack).where(CareerTrack.user_id == user.id, CareerTrack.active.is_(True))
        ).all()

        profile = build_profile_index(self.session, user, self.matcher)
        education = load_education(self.session, user.id)
        certs = [c["name"] for c in load_certifications(self.session, user.id)]

        for job in targets:
            if job.classification is None:
                continue
            try:
                classification = self.classification_from_row(job.classification)
                eligibility = self._assess_eligibility(user, job, registry, stats)
                best_track, best_match = self._assess_match(job, classification, profile, tracks, stats)
                self._score_ats(job, classification, user, profile, certs, education, stats)
                self._score_priority(
                    job=job,
                    match=best_match,
                    eligibility_score=eligibility.score if eligibility else 50.0,
                    verdict=AuthVerdict(eligibility.verdict) if eligibility else AuthVerdict.UNKNOWN,
                    best_track_id=best_track.id if best_track else None,
                    today=today,
                    stats=stats,
                )
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"assess[job {job.id}]: {type(exc).__name__}: {exc}")
                logger.exception("assessment failed for job %s", job.id)
        self.session.flush()
        return stats

    def _assess_eligibility(self, user: User, job: Job, registry, stats: StageStats) -> EligibilityAssessment | None:
        pack = registry.get(job.country_code)
        if pack is None:
            return None
        auth = work_auth_for(self.session, user.id, job.country_code)
        status_id = auth.status_id if auth else "unknown"
        prefs = list(auth.employment_preferences or []) if auth else []

        result = WorkAuthorizationEngine(pack).assess(
            description=job.description,
            user_status_id=status_id,
            employment_preferences=prefs,
            declared_employment_type=job.employment_type,
        )
        row = self.session.scalars(
            select(EligibilityAssessment).where(
                EligibilityAssessment.user_id == user.id, EligibilityAssessment.job_id == job.id
            )
        ).first() or EligibilityAssessment(user_id=user.id, job_id=job.id)

        row.verdict = result.verdict.value
        row.score = result.score
        row.user_status_id = result.user_status_id
        row.employment_type_match = result.employment_type_match
        row.signals = [s.to_dict() for s in result.signals]
        row.evidence = result.evidence
        row.reasons = result.reasons
        row.disclaimer = result.disclaimer
        if row.id is None:
            self.session.add(row)
        if job.employment_type is None and result.detected_employment_type:
            job.employment_type = result.detected_employment_type
        stats.assessed += 1
        return row

    def _assess_match(
        self,
        job: Job,
        classification: ClassificationResult,
        profile,
        tracks: Sequence[CareerTrack],
        stats: StageStats,
    ) -> tuple[CareerTrack | None, MatchResult]:
        """Score the job against every active track and keep the best.

        This is how a multi-track user gets one ranked list: the same posting is
        evaluated as a Cybersecurity opportunity *and* as an SAP one, and the
        track that fits best is the one recorded.
        """
        best_track: CareerTrack | None = None
        best_result = MatchResult()
        for track in tracks or [None]:  # type: ignore[list-item]
            result = self.matcher.match(
                classification,
                profile,
                job_title=job.title,
                preferred_titles=list(track.preferred_titles or []) if track else (),
            )
            # A track whose domain matches the job gets a small, explicit edge:
            # the same skills are worth more inside their own field.
            if track and track.domain_id == classification.domain_id:
                result.match_score = round(min(100.0, result.match_score * 1.05), 1)
            if track is None or result.match_score > best_result.match_score:
                best_track, best_result = track, result

            if track is not None:
                row = self.session.scalars(
                    select(MatchAssessment).where(
                        MatchAssessment.job_id == job.id, MatchAssessment.track_id == track.id
                    )
                ).first() or MatchAssessment(job_id=job.id, track_id=track.id)
                row.match_score = result.match_score
                row.direct_count = result.direct_count
                row.related_count = result.related_count
                row.partial_count = result.partial_count
                row.missing_count = result.missing_count
                row.skill_matches = [m.to_dict() for m in result.matches]
                row.experience_match = result.experience_match
                row.title_match = result.title_match
                row.gaps = result.gaps
                if row.id is None:
                    self.session.add(row)
                stats.matched += 1
        return best_track, best_result

    def _score_ats(
        self,
        job: Job,
        classification: ClassificationResult,
        user: User,
        profile,
        certifications: Sequence[str],
        education: Sequence[dict[str, Any]],
        stats: StageStats,
    ) -> AtsAssessment:
        """Baseline ATS score for the *current* master resume.

        Scored before tailoring so the dashboard can show the lift tailoring
        buys. A job-specific score is written again after a resume is tailored.
        """
        master = self.session.scalars(
            select(ResumeDocument).where(
                ResumeDocument.user_id == user.id, ResumeDocument.kind == ResumeKind.MASTER.value
            ).order_by(ResumeDocument.version.desc())
        ).first()
        text = master.rendered_text if master and master.rendered_text else " ".join(
            r.text for r in load_evidence_records(self.session, user.id)
        )
        facts = ResumeFacts(
            text=text,
            titles=list(profile.titles),
            certifications=certifications,
            total_experience_years=float(user.total_experience_years or 0.0),
            education_level=education_level(education),
        )
        result = self.ats.score(classification, facts, job_title=job.title)
        row = self.session.scalars(
            select(AtsAssessment).where(
                AtsAssessment.job_id == job.id,
                AtsAssessment.resume_id == (master.id if master else None),
            )
        ).first() or AtsAssessment(job_id=job.id, resume_id=master.id if master else None)

        for field_name in (
            "overall", "keyword_match", "required_skills_match", "preferred_skills_match",
            "experience_match", "title_match", "education_match", "certification_match",
            "formatting_score",
        ):
            setattr(row, field_name, getattr(result, field_name))
        row.matched_keywords = result.matched_keywords
        row.missing_keywords = result.missing_keywords
        row.suggestions = result.suggestions
        if row.id is None:
            self.session.add(row)
        return row

    def _score_priority(
        self,
        job: Job,
        match: MatchResult,
        eligibility_score: float,
        verdict: AuthVerdict,
        best_track_id: int | None,
        today: date,
        stats: StageStats,
    ) -> PriorityResult:
        result = self.priority.score(
            match_score=match.match_score,
            eligibility_score=eligibility_score,
            verdict=verdict,
            deadline_on=job.deadline_on,
            posted_on=job.posted_on,
            applicant_count=job.applicant_count,
            today=today,
        )
        row = job.priority or PriorityScore(job_id=job.id)
        row.match_score = result.match_score
        row.eligibility_score = result.eligibility_score
        row.urgency_score = result.urgency_score
        row.competition_score = result.competition_score
        row.overall = result.overall
        row.deadline_bucket = result.deadline_bucket.value
        row.days_to_deadline = result.days_to_deadline
        row.best_track_id = best_track_id
        row.explanation = result.explanation
        if row.id is None:
            row.job = job
            self.session.add(row)
        if result.deadline_bucket is DeadlineBucket.EXPIRED:
            job.archived = True
        stats.scored += 1
        return result

    # ------------------------------------------------------------------
    # 4. Ranking + tailoring
    # ------------------------------------------------------------------
    def ranked_jobs(self, limit: int | None = None, include_archived: bool = False) -> list[Job]:
        stmt = select(Job).join(PriorityScore, PriorityScore.job_id == Job.id)
        if not include_archived:
            stmt = stmt.where(Job.archived.is_(False))
        jobs = list(self.session.scalars(stmt).all())
        jobs.sort(key=lambda j: self._sort_key(j))
        return jobs[:limit] if limit else jobs

    @staticmethod
    def _sort_key(job: Job) -> tuple[int, float, int]:
        p = job.priority
        if p is None:
            return (9, 0.0, 9999)
        result = PriorityResult(
            match_score=p.match_score,
            eligibility_score=p.eligibility_score,
            overall=p.overall,
            deadline_bucket=DeadlineBucket(p.deadline_bucket),
            days_to_deadline=p.days_to_deadline,
        )
        return result.sort_key()

    def tailor_for_job(self, user: User, job: Job, use_ai: bool = True) -> tuple[ResumeDocument, Any]:
        """Build a job-specific resume and run the factuality gate on it."""
        if job.classification is None:
            raise ValueError(f"Job {job.id} has not been classified yet")

        classification = self.classification_from_row(job.classification)
        evidence = load_evidence_records(self.session, user.id)
        certs = load_certifications(self.session, user.id)
        education = load_education(self.session, user.id)
        profile = build_profile_index(self.session, user, self.matcher)
        match = self.matcher.match(classification, profile, job_title=job.title)

        track_id = job.priority.best_track_id if job.priority else None
        resume = self.resumes.tailor(
            contact=load_contact(self.session, user),
            evidence=evidence,
            classification=classification,
            match=match,
            job_title=job.title,
            company=job.company,
            certifications=certs,
            education=education,
            total_years=float(user.total_experience_years or 0.0),
            track_id=track_id,
            job_id=job.id,
            use_ai=use_ai,
        )
        report = self.factuality.check(resume, evidence, certs, education)
        rendered = render_text(resume, load_contact(self.session, user))

        row = self.session.scalars(
            select(ResumeDocument).where(
                ResumeDocument.user_id == user.id,
                ResumeDocument.job_id == job.id,
                ResumeDocument.kind == ResumeKind.TAILORED.value,
            ).order_by(ResumeDocument.version.desc())
        ).first()
        version = (row.version + 1) if row else 1

        doc = ResumeDocument(
            user_id=user.id,
            kind=ResumeKind.TAILORED.value,
            track_id=track_id,
            job_id=job.id,
            name=resume.name,
            version=version,
            storage_path=self._storage_path(job, track_id, version),
            sections=[s.to_dict() for s in resume.sections],
            rendered_text=rendered,
            # The gate: a resume with an unsupported claim is never final.
            is_final=report.passed,
            tailoring_notes=resume.notes + ([] if report.passed else [report.summary]),
        )
        self.session.add(doc)
        self.session.flush()

        from careeros.db.models import FactualityReport as FactualityRow

        self.session.add(
            FactualityRow(
                resume_id=doc.id,
                passed=report.passed,
                claims=[c.to_dict() for c in report.claims],
                unsupported_count=report.unsupported_count,
                summary=report.summary,
            )
        )
        return doc, report

    def _storage_path(self, job: Job, track_id: int | None, version: int) -> str:
        track = self.session.get(CareerTrack, track_id) if track_id else None
        slug = (track.name if track else "general").lower().replace(" ", "-")
        company = "".join(c for c in job.company.lower().replace(" ", "-") if c.isalnum() or c == "-")
        return f"/resumes/tailored/{slug}/{company}/{job.id}-v{version}.txt"

    # ------------------------------------------------------------------
    # 5. Recommendations
    # ------------------------------------------------------------------
    def recommend(self, user: User, for_date: date | None = None, top_n: int = 3) -> Recommendation:
        """Answer "what should I apply for today?" with a reason per claim."""
        for_date = for_date or date.today()
        jobs = self.ranked_jobs(limit=50)
        actionable = [j for j in jobs if j.priority and j.priority.eligibility_score > 0]

        by_track: dict[int, list[Job]] = {}
        for job in actionable:
            if job.priority and job.priority.best_track_id:
                by_track.setdefault(job.priority.best_track_id, []).append(job)

        top_track_id = None
        track_reasons: list[str] = []
        if by_track:
            def track_value(item: tuple[int, list[Job]]) -> float:
                _tid, items = item
                avg = sum(j.priority.match_score for j in items) / len(items)
                urgent = sum(
                    1 for j in items
                    if j.priority.deadline_bucket in (DeadlineBucket.TODAY.value, DeadlineBucket.WITHIN_48H.value)
                )
                return avg + 8 * urgent + 2 * len(items)

            top_track_id, items = max(by_track.items(), key=track_value)
            track = self.session.get(CareerTrack, top_track_id)
            avg = sum(j.priority.match_score for j in items) / len(items)
            deadlines_today = [
                j for j in items if j.priority.deadline_bucket == DeadlineBucket.TODAY.value
            ]
            track_reasons = [
                f"{avg:.0f}% average job match across {len(items)} open job(s)",
                f"{len(deadlines_today)} deadline(s) today",
                f"Strongest evidence alignment: {track.name if track else 'unassigned'}",
            ]

        top_jobs = []
        for job in actionable[:top_n]:
            p = job.priority
            top_jobs.append(
                {
                    "job_id": job.id,
                    "title": job.title,
                    "company": job.company,
                    "priority": p.overall if p else 0.0,
                    "flag": DeadlineBucket(p.deadline_bucket).label if p else "",
                    "why": "; ".join((p.explanation or [])[:3]) if p else "",
                }
            )

        narrative = self._narrative(top_track_id, track_reasons, top_jobs)
        row = self.session.scalars(
            select(Recommendation).where(
                Recommendation.user_id == user.id, Recommendation.for_date == for_date
            )
        ).first() or Recommendation(user_id=user.id, for_date=for_date)
        row.top_track_id = top_track_id
        row.track_reasons = track_reasons
        row.top_jobs = top_jobs
        row.narrative = narrative
        if row.id is None:
            self.session.add(row)
        return row

    def _narrative(self, track_id: int | None, reasons: list[str], top_jobs: list[dict]) -> str:
        track = self.session.get(CareerTrack, track_id) if track_id else None
        lines = []
        if track:
            lines.append(f"TOP CAREER TRACK TODAY: {track.name}")
            lines.extend(f"- {r}" for r in reasons)
            lines.append("")
        for i, job in enumerate(top_jobs, start=1):
            lines.append(f"TOP JOB #{i}: {job['title']} @ {job['company']} ({job['flag']}, priority {job['priority']:.0f})")
        if not top_jobs:
            lines.append("No actionable jobs today. Run an ingest, or widen your career tracks.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------
    def run_daily(
        self,
        sources: Sequence[JobSource],
        user: User | None = None,
        today: date | None = None,
        tailor_limit: int = DEFAULT_TAILOR_LIMIT,
        use_ai: bool = True,
    ) -> PipelineRun:
        run = PipelineRun(started_at=datetime.now(timezone.utc))
        self.session.add(run)
        stats = StageStats()

        user = user or load_user(self.session)
        if user is None:
            stats.errors.append("No user profile found - run `careeros init-profile` first.")
            run.finished_at = datetime.now(timezone.utc)
            run.ok = False
            run.stats = stats.to_dict()
            run.errors = stats.errors
            return run

        self.ingest(sources, stats=stats)
        self.classify_jobs(stats=stats)
        self.assess_for_user(user, today=today, stats=stats)

        for job in self.ranked_jobs(limit=tailor_limit):
            if job.priority and job.priority.eligibility_score <= 0:
                continue
            try:
                self.tailor_for_job(user, job, use_ai=use_ai)
                stats.tailored += 1
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"tailor[job {job.id}]: {type(exc).__name__}: {exc}")

        self._ensure_applications(user)
        self.recommend(user, for_date=today)

        run.finished_at = datetime.now(timezone.utc)
        run.ok = not stats.errors
        run.stats = stats.to_dict()
        run.errors = stats.errors
        return run

    def _ensure_applications(self, user: User) -> None:
        """Every ranked job gets a tracking row so the lifecycle starts at
        `discovered` rather than at `applied`."""
        existing = {
            a.job_id
            for a in self.session.scalars(select(Application).where(Application.user_id == user.id)).all()
        }
        for job in self.ranked_jobs():
            if job.id in existing:
                continue
            self.session.add(
                Application(
                    user_id=user.id,
                    job_id=job.id,
                    track_id=job.priority.best_track_id if job.priority else None,
                    status=ApplicationStatus.DISCOVERED.value,
                )
            )
