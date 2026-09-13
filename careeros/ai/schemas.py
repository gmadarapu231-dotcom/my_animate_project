"""Pydantic shapes the LLM is asked to return.

These double as the contract for structured outputs and as the interchange
format between the deterministic engines and the AI layer -- the heuristic
path fills the same objects, so callers never branch on which one ran.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkillRequirement(BaseModel):
    name: str = Field(description="The skill exactly as the job description words it")
    level: str = Field(default="required", description="required | preferred | unspecified")
    skill_id: str | None = Field(default=None, description="Canonical skill-graph id if known")


class JobClassificationOut(BaseModel):
    """Domain-agnostic reading of a job description.

    `domain_id` is free text: the model is told to invent a new snake_case id
    when no seeded domain fits, rather than forcing the job into one.
    """

    domain_id: str
    domain_label: str
    function: str | None = None
    specialization: str | None = None
    industry: str | None = None
    seniority: str = "mid"
    min_experience_years: float | None = None
    education_requirement: str | None = None
    required_skills: list[SkillRequirement] = Field(default_factory=list)
    preferred_skills: list[SkillRequirement] = Field(default_factory=list)
    required_certifications: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    ats_keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    rationale: str | None = None


class BulletRewrite(BaseModel):
    evidence_id: int = Field(description="Id of the evidence item this bullet is assembled from")
    text: str = Field(description="Rewritten bullet. Facts must be unchanged.")
    used_jd_terms: list[str] = Field(default_factory=list)


class TailoringOut(BaseModel):
    summary: str = Field(default="", description="Professional summary, assembled from evidence only")
    bullets: list[BulletRewrite] = Field(default_factory=list)
    skills_order: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ClaimCheck(BaseModel):
    text: str
    verdict: str = Field(description="supported | weakly_supported | unsupported | rephrased")
    evidence_ids: list[int] = Field(default_factory=list)
    note: str | None = None


class FactualityOut(BaseModel):
    passed: bool
    claims: list[ClaimCheck] = Field(default_factory=list)
    summary: str | None = None


class EmailExtractionOut(BaseModel):
    category: str = Field(description="recruiter|interview|assessment|application_confirmation|rejection|offer|sponsorship|follow_up|other")
    company: str | None = None
    position: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    job_url: str | None = None
    interview_datetime: str | None = None
    assessment_deadline: str | None = None
    required_action: str | None = None
    application_status_hint: str | None = None
    rejection_reason_explicit: str | None = None
    rejection_reason_quote: str | None = None
    confidence: float = 0.5


class SearchFilterOut(BaseModel):
    """Natural-language search compiled into structured filters."""

    text: str | None = None
    countries: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    work_arrangements: list[str] = Field(default_factory=list)
    sponsorship: str | None = Field(default=None, description="required|available|unknown|not_needed")
    min_match_score: float | None = None
    posted_within_days: int | None = None
    deadline_within_days: int | None = None
    seniority: list[str] = Field(default_factory=list)
