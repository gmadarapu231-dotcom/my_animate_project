"""SQLAlchemy 2.0 ORM -- the Career OS schema.

Layout follows the product hierarchy:

    Country -> Work authorization -> Career track -> Job -> Eligibility
            -> Priority -> Resume -> Application -> Email -> Interview -> Outcome

Two design rules run through the whole schema:

1. **Nothing about a job category is hard-coded.** `CareerDomain` is a table,
   seeded from YAML and extended at runtime by the classifier. Domains,
   functions and specialisations are free strings everywhere else.
2. **Every generated resume line is traceable.** `ResumeSection.blocks` stores
   the evidence ids each bullet came from, and `FactualityReport` records the
   verdict per claim. A resume with an unsupported claim cannot be marked
   final.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from careeros.enums import (
    ApplicationStatus,
    AuthVerdict,
    AutomationMode,
    DeadlineBucket,
    DraftState,
    EmailCategory,
    EvidenceKind,
    ReasonConfidence,
    ResumeKind,
    VerificationState,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ===========================================================================
# 1. Taxonomy (extensible at runtime -- no code change for a new category)
# ===========================================================================
class CareerDomain(Base, TimestampMixin):
    """A career domain such as `cybersecurity`, `sap` or `veterinary_nursing`.

    Seeded from `config/taxonomy/domains.yaml`. When the classifier meets a job
    it cannot place, it inserts a new row with `origin='inferred'` rather than
    forcing the job into an existing bucket.
    """

    __tablename__ = "career_domain"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    origin: Mapped[str] = mapped_column(String(16), default="seed")  # seed | inferred | user
    functions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    title_patterns: Mapped[list[Any]] = mapped_column(JSON, default=list)
    keywords: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    job_count: Mapped[int] = mapped_column(Integer, default=0)


class LearnedSkill(Base, TimestampMixin):
    """Skill nodes discovered at runtime that are not yet in `skills.yaml`.

    Keeps the shipped graph curated while letting the system grow. Promotion
    into the YAML file is a human review step (see docs/03-ai-architecture.md).
    """

    __tablename__ = "learned_skill"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    label: Mapped[str] = mapped_column(String(160))
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    related: Mapped[list[Any]] = mapped_column(JSON, default=list)
    domains: Mapped[list[Any]] = mapped_column(JSON, default=list)
    observations: Mapped[int] = mapped_column(Integer, default=1)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)


# ===========================================================================
# 2. User, work authorization, career tracks
# ===========================================================================
class User(Base, TimestampMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    links: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # linkedin, github, portfolio

    # Location & mobility
    home_country: Mapped[str] = mapped_column(String(8), default="US")
    home_city: Mapped[Optional[str]] = mapped_column(String(128))
    home_region: Mapped[Optional[str]] = mapped_column(String(128))
    preferred_locations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    remote_preference: Mapped[str] = mapped_column(String(16), default="any")  # remote|hybrid|onsite|any
    open_to_relocation: Mapped[bool] = mapped_column(Boolean, default=False)

    # Professional snapshot (denormalised for display; the truth is in evidence)
    current_title: Mapped[Optional[str]] = mapped_column(String(255))
    total_experience_years: Mapped[float] = mapped_column(Float, default=0.0)
    industries: Mapped[list[Any]] = mapped_column(JSON, default=list)

    automation_mode: Mapped[str] = mapped_column(String(16), default=AutomationMode.ASSISTED.value)

    work_auth: Mapped[list["WorkAuthorization"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    tracks: Mapped[list["CareerTrack"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    employers: Mapped[list["Employer"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class WorkAuthorization(Base, TimestampMixin):
    """The user's status *per country*. A user may be authorised in several."""

    __tablename__ = "work_authorization"
    __table_args__ = (UniqueConstraint("user_id", "country_code", name="uq_workauth_user_country"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    country_code: Mapped[str] = mapped_column(String(8))
    status_id: Mapped[str] = mapped_column(String(64))        # matches a status id in the country pack
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    needs_sponsorship: Mapped[bool] = mapped_column(Boolean, default=False)
    employment_preferences: Mapped[list[Any]] = mapped_column(JSON, default=list)  # w2, c2c, full_time...
    notes: Mapped[Optional[str]] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="work_auth")


class CareerTrack(Base, TimestampMixin):
    """One of the user's parallel career bets (Cybersecurity, SAP Security...)."""

    __tablename__ = "career_track"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    domain_id: Mapped[str] = mapped_column(ForeignKey("career_domain.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)   # lower = user's stronger preference

    preferred_titles: Mapped[list[Any]] = mapped_column(JSON, default=list)
    preferred_industries: Mapped[list[Any]] = mapped_column(JSON, default=list)
    core_skills: Mapped[list[Any]] = mapped_column(JSON, default=list)  # skill-graph ids
    min_match_score: Mapped[float] = mapped_column(Float, default=60.0)
    salary_floor: Mapped[Optional[float]] = mapped_column(Float)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(8))
    countries: Mapped[list[Any]] = mapped_column(JSON, default=list)

    user: Mapped[User] = relationship(back_populates="tracks")
    resumes: Mapped[list["ResumeDocument"]] = relationship(back_populates="track")


# ===========================================================================
# 3. Career Evidence Database
# ===========================================================================
class Employer(Base, TimestampMixin):
    __tablename__ = "employer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    location: Mapped[Optional[str]] = mapped_column(String(255))
    country_code: Mapped[Optional[str]] = mapped_column(String(8))
    employment_type: Mapped[Optional[str]] = mapped_column(String(32))
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date)   # NULL = current
    titles: Mapped[list[Any]] = mapped_column(JSON, default=list)

    user: Mapped[User] = relationship(back_populates="employers")
    evidence: Mapped[list["EvidenceItem"]] = relationship(back_populates="employer")


class EvidenceItem(Base, TimestampMixin):
    """An atomic, verified career fact.

    This is the substrate the whole resume layer stands on:
    project -> technology -> responsibility -> achievement -> employer -> dates.
    Resume bullets are *assembled from* evidence and cite it by id, so a
    tailored resume can never quietly acquire experience the user lacks.
    """

    __tablename__ = "evidence_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    employer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employer.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), default=EvidenceKind.RESPONSIBILITY.value, index=True)

    text: Mapped[str] = mapped_column(Text)                       # the user's own words
    project: Mapped[Optional[str]] = mapped_column(String(255))
    role_title: Mapped[Optional[str]] = mapped_column(String(255))
    technologies: Mapped[list[Any]] = mapped_column(JSON, default=list)
    skills: Mapped[list[Any]] = mapped_column(JSON, default=list)   # skill-graph ids
    domains: Mapped[list[Any]] = mapped_column(JSON, default=list)  # career domains this supports

    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"users": 4000, "reduction_pct": 35}

    verification: Mapped[str] = mapped_column(String(16), default=VerificationState.UNVERIFIED.value)
    source: Mapped[Optional[str]] = mapped_column(String(255))   # "master resume v3", "user entry"...
    strength: Mapped[float] = mapped_column(Float, default=0.5)  # user-rated depth, 0-1
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)

    employer: Mapped[Optional[Employer]] = relationship(back_populates="evidence")


class Certification(Base, TimestampMixin):
    __tablename__ = "certification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    issuer: Mapped[Optional[str]] = mapped_column(String(255))
    issued_on: Mapped[Optional[date]] = mapped_column(Date)
    expires_on: Mapped[Optional[date]] = mapped_column(Date)
    credential_id: Mapped[Optional[str]] = mapped_column(String(128))
    skills: Mapped[list[Any]] = mapped_column(JSON, default=list)
    verification: Mapped[str] = mapped_column(String(16), default=VerificationState.UNVERIFIED.value)


class Education(Base, TimestampMixin):
    __tablename__ = "education"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    degree: Mapped[str] = mapped_column(String(255))
    field: Mapped[Optional[str]] = mapped_column(String(255))
    institution: Mapped[str] = mapped_column(String(255))
    country_code: Mapped[Optional[str]] = mapped_column(String(8))
    started_on: Mapped[Optional[date]] = mapped_column(Date)
    completed_on: Mapped[Optional[date]] = mapped_column(Date)
    verification: Mapped[str] = mapped_column(String(16), default=VerificationState.UNVERIFIED.value)


# ===========================================================================
# 4. Jobs
# ===========================================================================
class Job(Base, TimestampMixin):
    """A posting as fetched, plus normalised fields shared by every country."""

    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_source_external"),
        Index("ix_job_fingerprint", "fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))     # dedupe key across sources
    url: Mapped[Optional[str]] = mapped_column(Text)

    title: Mapped[str] = mapped_column(String(255))
    company: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)

    country_code: Mapped[str] = mapped_column(String(8), index=True)
    city: Mapped[Optional[str]] = mapped_column(String(128))
    region: Mapped[Optional[str]] = mapped_column(String(128))
    work_arrangement: Mapped[Optional[str]] = mapped_column(String(16))  # remote|hybrid|onsite

    employment_type: Mapped[Optional[str]] = mapped_column(String(32))
    salary_raw: Mapped[Optional[str]] = mapped_column(String(255))       # preserved verbatim
    salary_min: Mapped[Optional[float]] = mapped_column(Float)           # normalised, annual, source currency
    salary_max: Mapped[Optional[float]] = mapped_column(Float)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(8))
    salary_period: Mapped[Optional[str]] = mapped_column(String(16))     # original period
    salary_components: Mapped[list[Any]] = mapped_column(JSON, default=list)  # ctc, base, variable...

    posted_on: Mapped[Optional[date]] = mapped_column(Date)
    deadline_on: Mapped[Optional[date]] = mapped_column(Date, index=True)
    applicant_count: Mapped[Optional[int]] = mapped_column(Integer)

    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    classification: Mapped[Optional["JobClassification"]] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    priority: Mapped[Optional["PriorityScore"]] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    applications: Mapped[list["Application"]] = relationship(back_populates="job")


class JobClassification(Base, TimestampMixin):
    """What the AI understood the job to be. Domain/function are free strings."""

    __tablename__ = "job_classification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), unique=True)

    domain_id: Mapped[str] = mapped_column(String(64), index=True)
    domain_label: Mapped[str] = mapped_column(String(128))
    function: Mapped[Optional[str]] = mapped_column(String(128))
    specialization: Mapped[Optional[str]] = mapped_column(String(160))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    seniority: Mapped[str] = mapped_column(String(32), default="mid")

    required_skills: Mapped[list[Any]] = mapped_column(JSON, default=list)
    preferred_skills: Mapped[list[Any]] = mapped_column(JSON, default=list)
    required_certifications: Mapped[list[Any]] = mapped_column(JSON, default=list)
    soft_skills: Mapped[list[Any]] = mapped_column(JSON, default=list)
    min_experience_years: Mapped[Optional[float]] = mapped_column(Float)
    education_requirement: Mapped[Optional[str]] = mapped_column(String(255))

    ats_keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(32), default="heuristic")  # heuristic | llm | hybrid
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    domain_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="classification")


class EligibilityAssessment(Base, TimestampMixin):
    """Work-authorization / employment-type verdict for one user + one job.

    `evidence` always records the JD phrase that produced the verdict so the UI
    can show the source. `disclaimer` is rendered next to every verdict.
    """

    __tablename__ = "eligibility_assessment"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_eligibility_user_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))

    verdict: Mapped[str] = mapped_column(String(32), default=AuthVerdict.UNKNOWN.value)
    score: Mapped[float] = mapped_column(Float, default=50.0)
    user_status_id: Mapped[str] = mapped_column(String(64))
    employment_type_match: Mapped[bool] = mapped_column(Boolean, default=True)
    signals: Mapped[list[Any]] = mapped_column(JSON, default=list)   # [{signal, pattern, excerpt}]
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    disclaimer: Mapped[str] = mapped_column(
        Text, default="AI assessment - verify with employer/recruiter."
    )


class MatchAssessment(Base, TimestampMixin):
    """Skill/experience fit between a career track's evidence and a job."""

    __tablename__ = "match_assessment"
    __table_args__ = (UniqueConstraint("job_id", "track_id", name="uq_match_job_track"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("career_track.id", ondelete="CASCADE"))

    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    direct_count: Mapped[int] = mapped_column(Integer, default=0)
    related_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    skill_matches: Mapped[list[Any]] = mapped_column(JSON, default=list)
    experience_match: Mapped[float] = mapped_column(Float, default=0.0)
    title_match: Mapped[float] = mapped_column(Float, default=0.0)
    gaps: Mapped[list[Any]] = mapped_column(JSON, default=list)


class AtsAssessment(Base, TimestampMixin):
    """Universal ATS scoring. Keywords are derived from the JD, per industry."""

    __tablename__ = "ats_assessment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resume_document.id", ondelete="CASCADE"))

    overall: Mapped[float] = mapped_column(Float, default=0.0)
    keyword_match: Mapped[float] = mapped_column(Float, default=0.0)
    required_skills_match: Mapped[float] = mapped_column(Float, default=0.0)
    preferred_skills_match: Mapped[float] = mapped_column(Float, default=0.0)
    experience_match: Mapped[float] = mapped_column(Float, default=0.0)
    title_match: Mapped[float] = mapped_column(Float, default=0.0)
    education_match: Mapped[float] = mapped_column(Float, default=0.0)
    certification_match: Mapped[float] = mapped_column(Float, default=0.0)
    formatting_score: Mapped[float] = mapped_column(Float, default=100.0)

    matched_keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    missing_keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    suggestions: Mapped[list[Any]] = mapped_column(JSON, default=list)


class PriorityScore(Base, TimestampMixin):
    """Ranking inputs and the composite. A deadline today always floats to #1."""

    __tablename__ = "priority_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), unique=True)

    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    eligibility_score: Mapped[float] = mapped_column(Float, default=0.0)
    urgency_score: Mapped[float] = mapped_column(Float, default=0.0)
    competition_score: Mapped[float] = mapped_column(Float, default=0.0)
    overall: Mapped[float] = mapped_column(Float, default=0.0)

    deadline_bucket: Mapped[str] = mapped_column(String(16), default=DeadlineBucket.NONE.value)
    days_to_deadline: Mapped[Optional[int]] = mapped_column(Integer)
    best_track_id: Mapped[Optional[int]] = mapped_column(ForeignKey("career_track.id", ondelete="SET NULL"))
    explanation: Mapped[list[Any]] = mapped_column(JSON, default=list)

    job: Mapped[Job] = relationship(back_populates="priority")


# ===========================================================================
# 5. Resumes
# ===========================================================================
class ResumeDocument(Base, TimestampMixin):
    """Master / track / tailored resume. Content is structured, not a blob."""

    __tablename__ = "resume_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default=ResumeKind.MASTER.value)
    track_id: Mapped[Optional[int]] = mapped_column(ForeignKey("career_track.id", ondelete="SET NULL"))
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job.id", ondelete="SET NULL"))

    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    storage_path: Mapped[Optional[str]] = mapped_column(String(512))   # /resumes/<track>/<file>
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list)    # see engines/resume.py
    rendered_text: Mapped[Optional[str]] = mapped_column(Text)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    tailoring_notes: Mapped[list[Any]] = mapped_column(JSON, default=list)

    track: Mapped[Optional[CareerTrack]] = relationship(back_populates="resumes")
    factuality: Mapped[list["FactualityReport"]] = relationship(
        back_populates="resume", cascade="all, delete-orphan"
    )


class FactualityReport(Base, TimestampMixin):
    """Per-claim traceability check. A resume cannot be finalised while any
    claim is `unsupported`."""

    __tablename__ = "factuality_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resume_document.id", ondelete="CASCADE"))
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    claims: Mapped[list[Any]] = mapped_column(JSON, default=list)   # [{text, verdict, evidence_ids, note}]
    unsupported_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Optional[str]] = mapped_column(Text)

    resume: Mapped[ResumeDocument] = relationship(back_populates="factuality")


class CoverLetter(Base, TimestampMixin):
    __tablename__ = "cover_letter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    body: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)


# ===========================================================================
# 6. Applications, email, outcomes
# ===========================================================================
class Application(Base, TimestampMixin):
    __tablename__ = "application"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_application_user_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"))
    track_id: Mapped[Optional[int]] = mapped_column(ForeignKey("career_track.id", ondelete="SET NULL"))
    resume_id: Mapped[Optional[int]] = mapped_column(ForeignKey("resume_document.id", ondelete="SET NULL"))

    status: Mapped[str] = mapped_column(String(32), default=ApplicationStatus.DISCOVERED.value, index=True)
    applied_on: Mapped[Optional[date]] = mapped_column(Date)
    last_activity_on: Mapped[Optional[date]] = mapped_column(Date)
    submission_mode: Mapped[str] = mapped_column(String(16), default=AutomationMode.ASSISTED.value)
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # screening question answers
    notes: Mapped[Optional[str]] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="applications")
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ApplicationEvent(Base):
    __tablename__ = "application_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    from_status: Mapped[Optional[str]] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), default="user")  # user | email | pipeline
    detail: Mapped[Optional[str]] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="events")


class EmailMessage(Base, TimestampMixin):
    """A job-related message pulled through the Gmail API.

    Only metadata and extracted fields are stored by default; see
    docs/09-security-architecture.md for the retention settings.
    """

    __tablename__ = "email_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    gmail_id: Mapped[str] = mapped_column(String(128), unique=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(128))
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    sender: Mapped[Optional[str]] = mapped_column(String(255))
    subject: Mapped[Optional[str]] = mapped_column(String(512))
    snippet: Mapped[Optional[str]] = mapped_column(Text)

    category: Mapped[str] = mapped_column(String(32), default=EmailCategory.OTHER.value, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    application_id: Mapped[Optional[int]] = mapped_column(ForeignKey("application.id", ondelete="SET NULL"))
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False)
    action_due_on: Mapped[Optional[date]] = mapped_column(Date)


class EmailDraft(Base, TimestampMixin):
    """A generated reply. Default flow is DRAFT -> USER APPROVAL -> SEND."""

    __tablename__ = "email_draft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    email_id: Mapped[Optional[int]] = mapped_column(ForeignKey("email_message.id", ondelete="SET NULL"))
    application_id: Mapped[Optional[int]] = mapped_column(ForeignKey("application.id", ondelete="SET NULL"))

    intent: Mapped[str] = mapped_column(String(64))          # interview_availability, thank_you...
    subject: Mapped[Optional[str]] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default=DraftState.AWAITING_APPROVAL.value)
    high_impact_topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class RejectionAnalysis(Base, TimestampMixin):
    """Explicit reason and hypotheses are kept strictly apart."""

    __tablename__ = "rejection_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id", ondelete="CASCADE"))
    stage: Mapped[Optional[str]] = mapped_column(String(32))
    explicit_reason: Mapped[Optional[str]] = mapped_column(Text)
    explicit_quote: Mapped[Optional[str]] = mapped_column(Text)
    possible_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{reason, basis, confidence}]
    confidence: Mapped[str] = mapped_column(String(16), default=ReasonConfidence.INFERRED.value)


class Recommendation(Base, TimestampMixin):
    """"What should I apply for today?" - one row per generated answer."""

    __tablename__ = "recommendation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    for_date: Mapped[date] = mapped_column(Date, index=True)
    top_track_id: Mapped[Optional[int]] = mapped_column(ForeignKey("career_track.id", ondelete="SET NULL"))
    track_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    top_jobs: Mapped[list[Any]] = mapped_column(JSON, default=list)   # [{job_id, why, priority}]
    narrative: Mapped[Optional[str]] = mapped_column(Text)


class AgentRun(Base):
    """One agentic conversation: prompt in, answer out, every tool call recorded.

    The agent is autonomous over *strategy*, never over truthfulness, so the
    record of what it actually did has to be inspectable after the fact.
    """

    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    prompt: Mapped[str] = mapped_column(Text)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(32))
    turns: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text)

    tool_calls: Mapped[list["AgentToolCall"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class AgentToolCall(Base):
    """A single tool invocation inside an agent run."""

    __tablename__ = "agent_tool_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id", ondelete="CASCADE"))
    turn: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_summary: Mapped[Optional[str]] = mapped_column(Text)
    mutating: Mapped[bool] = mapped_column(Boolean, default=False)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class PipelineRun(Base):
    """Audit trail for the daily automation."""

    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
