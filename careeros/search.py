"""Natural-language job search.

Compiles a sentence into structured filters. The deterministic parser handles
the phrasings the product promises ("H1B-friendly cybersecurity jobs in Texas",
"jobs with deadline today", "match above 85%"), and the LLM refines anything it
does not recognise. Filters are then applied in SQL + Python.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.ai.provider import LLMProvider, get_provider
from careeros.ai.schemas import SearchFilterOut
from careeros.config import countries, taxonomy
from careeros.db.models import EligibilityAssessment, Job, JobClassification, PriorityScore
from careeros.engines.textutil import normalize
from careeros.enums import AuthVerdict

_US_REGIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

_EMPLOYMENT_WORDS = {
    "c2c": ["c2c", "corp to corp", "corp-to-corp"],
    "w2": ["w2", "w-2"],
    "c2h": ["c2h", "contract to hire", "contract-to-hire"],
    "contract": ["contract"],
    "full_time": ["full time", "full-time", "fulltime", "permanent"],
    "part_time": ["part time", "part-time"],
    "internship": ["internship", "intern"],
    "freelance": ["freelance"],
}

#: Command words that carry no filtering meaning of their own.
_QUERY_NOISE = (
    r"\b(find|show|get|list|search|me|please|jobs?|roles?|positions?|openings?|opportunities|"
    r"in|on|at|with|that|which|where|what|my|is|are|above|over|under|below|all|any|categories|"
    r"category|across|the|a|an|and|or|for|to|of|from|resume|match(?:es|ing)?|score|profile|"
    r"deadline|today|sponsorship|unknown|posted|last|days?|week)\b"
)

_ARRANGEMENT_WORDS = {
    "remote": ["remote", "work from home", "wfh"],
    "hybrid": ["hybrid"],
    "onsite": ["onsite", "on-site", "in office"],
}


@dataclass
class SearchFilters:
    text: str | None = None
    countries: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    work_arrangements: list[str] = field(default_factory=list)
    sponsorship: str | None = None          # required|available|unknown|not_needed
    min_match_score: float | None = None
    min_ats_score: float | None = None
    posted_within_days: int | None = None
    deadline_within_days: int | None = None
    deadline_today: bool = False
    seniority: list[str] = field(default_factory=list)
    include_ineligible: bool = True
    method: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        bits = []
        if self.domains:
            bits.append("domain " + "/".join(self.domains))
        if self.countries:
            bits.append("country " + "/".join(self.countries))
        if self.regions:
            bits.append("region " + "/".join(self.regions))
        if self.work_arrangements:
            bits.append("/".join(self.work_arrangements))
        if self.employment_types:
            bits.append("/".join(self.employment_types))
        if self.sponsorship:
            bits.append(f"sponsorship {self.sponsorship}")
        if self.deadline_today:
            bits.append("deadline today")
        elif self.deadline_within_days is not None:
            bits.append(f"deadline within {self.deadline_within_days}d")
        if self.posted_within_days is not None:
            bits.append(f"posted within {self.posted_within_days}d")
        if self.min_match_score is not None:
            bits.append(f"match >= {self.min_match_score:g}")
        if self.seniority:
            bits.append("seniority " + "/".join(self.seniority))
        if self.text:
            bits.append(f'text "{self.text}"')
        return ", ".join(bits) or "no filters"


class QueryParser:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_provider()
        self.taxonomy = taxonomy()
        self.countries = countries()

    def parse(self, query: str, use_ai: bool = True) -> SearchFilters:
        filters = self._rules(query)
        if not use_ai or not getattr(self.provider, "available", False):
            return filters
        out = self.provider.structured(
            system=(
                "Compile a job-search sentence into filters. Use ISO country codes. "
                "`domains` must be snake_case career domain ids. Leave a field empty "
                "when the sentence does not constrain it - never guess."
            ),
            prompt=query,
            schema=SearchFilterOut,
        )
        return self._merge(filters, out) if out else filters

    # -- deterministic ------------------------------------------------------
    def _rules(self, query: str) -> SearchFilters:
        norm = normalize(query)
        f = SearchFilters()

        for pack in self.countries.all():
            names = [n for n in (pack.name.lower(), *pack.aliases) if len(n) >= 3]
            hit = any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", norm) for n in names)
            # The bare ISO code is only a country signal when typed in caps,
            # otherwise "in" and "us" swallow ordinary English words.
            hit = hit or re.search(rf"(?<![A-Za-z]){pack.code}(?![A-Za-z])", query or "") is not None
            if hit:
                f.countries.append(pack.code)

        for spec in self.taxonomy.domains:
            if spec["id"] == "other":
                continue
            probes = [spec["id"].replace("_", " "), spec["label"].lower(), *spec.get("title_patterns", [])[:6]]
            if any(p and p in norm for p in probes):
                f.domains.append(spec["id"])

        for name, code in _US_REGIONS.items():
            if re.search(rf"(?<![a-z]){name}(?![a-z])", norm):
                f.regions.append(code)
                if "US" not in f.countries:
                    f.countries.append("US")

        for key, words in _EMPLOYMENT_WORDS.items():
            if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", norm) for w in words):
                f.employment_types.append(key)
        for key, words in _ARRANGEMENT_WORDS.items():
            if any(w in norm for w in words):
                f.work_arrangements.append(key)

        for level in self.taxonomy.seniority_levels:
            if re.search(rf"(?<![a-z]){re.escape(level['id'])}(?![a-z])", norm):
                f.seniority.append(level["id"])

        # Sponsorship intent
        if any(p in norm for p in ("sponsor h1b", "sponsors h1b", "h1b friendly", "h1b-friendly",
                                   "that sponsor", "sponsorship available", "will sponsor")):
            f.sponsorship = "available"
        elif "sponsorship is unknown" in norm or "sponsorship unknown" in norm:
            f.sponsorship = "unknown"
        elif "no sponsorship" in norm or "without sponsorship" in norm:
            f.sponsorship = "not_needed"

        # Dates
        if "deadline today" in norm or "closing today" in norm or "final application date" in norm:
            f.deadline_today = True
        m = re.search(r"deadline (?:within|in) (\d+) days?", norm)
        if m:
            f.deadline_within_days = int(m.group(1))
        if "posted today" in norm or "jobs posted today" in norm:
            f.posted_within_days = 0
        m = re.search(r"posted (?:within|in) (?:the )?(?:last )?(\d+) days?", norm)
        if m:
            f.posted_within_days = int(m.group(1))
        if "this week" in norm:
            f.posted_within_days = f.posted_within_days if f.posted_within_days is not None else 7

        # Scores
        m = re.search(r"match(?:es)?\s*(?:score\s*)?(?:is\s*)?(?:above|over|greater than|>=?|at least)\s*(\d{1,3})", norm)
        if m:
            f.min_match_score = float(m.group(1))
        m = re.search(r"ats\s*(?:score\s*)?(?:above|over|>=?|at least)\s*(\d{1,3})", norm)
        if m:
            f.min_ats_score = float(m.group(1))

        # Leftover free text
        # Free text is an AND filter, so it is only safe as a fallback: once a
        # domain/location/type filter has matched, the leftover words are
        # usually fragments that would wrongly exclude good jobs. A seniority,
        # deadline or score filter alone does not narrow *what* the job is, so
        # text still applies in that case.
        narrowed = any(
            [f.domains, f.countries, f.regions, f.employment_types, f.work_arrangements, f.sponsorship]
        )
        if not narrowed:
            stripped = re.sub(_QUERY_NOISE, " ", norm)
            stripped = re.sub(r"[^a-z0-9 +#./-]", " ", stripped)
            stripped = re.sub(r"\s+", " ", stripped).strip()
            if stripped and any(ch.isalnum() for ch in stripped):
                f.text = stripped
        return f

    @staticmethod
    def _merge(base: SearchFilters, out: SearchFilterOut) -> SearchFilters:
        merged = SearchFilters(**base.to_dict())
        merged.method = "hybrid"
        for attr in ("countries", "domains", "regions", "employment_types", "work_arrangements", "seniority"):
            extra = [v for v in (getattr(out, attr, []) or []) if v]
            current = getattr(merged, attr)
            setattr(merged, attr, list(dict.fromkeys(current + extra)))
        merged.sponsorship = merged.sponsorship or out.sponsorship
        merged.min_match_score = merged.min_match_score if merged.min_match_score is not None else out.min_match_score
        merged.posted_within_days = (
            merged.posted_within_days if merged.posted_within_days is not None else out.posted_within_days
        )
        if out.deadline_within_days is not None and merged.deadline_within_days is None:
            merged.deadline_within_days = out.deadline_within_days
            merged.deadline_today = merged.deadline_today or out.deadline_within_days == 0
        merged.text = merged.text or out.text
        return merged


def run_search(
    session: Session,
    filters: SearchFilters,
    user_id: int | None = None,
    limit: int = 50,
    today: date | None = None,
) -> list[Job]:
    today = today or date.today()
    stmt = select(Job).where(Job.archived.is_(False))

    if filters.countries:
        stmt = stmt.where(Job.country_code.in_([c.upper() for c in filters.countries]))
    if filters.regions:
        stmt = stmt.where(Job.region.in_(filters.regions))
    if filters.employment_types:
        stmt = stmt.where(Job.employment_type.in_(filters.employment_types))
    if filters.work_arrangements:
        stmt = stmt.where(Job.work_arrangement.in_(filters.work_arrangements))
    if filters.posted_within_days is not None:
        stmt = stmt.where(Job.posted_on >= today - timedelta(days=filters.posted_within_days))
    if filters.deadline_today:
        stmt = stmt.where(Job.deadline_on == today)
    elif filters.deadline_within_days is not None:
        stmt = stmt.where(
            Job.deadline_on.is_not(None),
            Job.deadline_on <= today + timedelta(days=filters.deadline_within_days),
            Job.deadline_on >= today,
        )
    if filters.domains or filters.seniority:
        stmt = stmt.join(JobClassification, JobClassification.job_id == Job.id)
        if filters.domains:
            stmt = stmt.where(JobClassification.domain_id.in_(filters.domains))
        if filters.seniority:
            stmt = stmt.where(JobClassification.seniority.in_(filters.seniority))
    if filters.min_match_score is not None or filters.min_ats_score is not None:
        stmt = stmt.join(PriorityScore, PriorityScore.job_id == Job.id)
        if filters.min_match_score is not None:
            stmt = stmt.where(PriorityScore.match_score >= filters.min_match_score)

    jobs = list(session.scalars(stmt).unique().all())

    if filters.text:
        needle = normalize(filters.text)
        words = [w for w in needle.split() if len(w) > 2]
        jobs = [
            j for j in jobs
            if all(w in normalize(f"{j.title} {j.company} {j.description}") for w in words)
        ] or [
            j for j in jobs
            if any(w in normalize(f"{j.title} {j.company} {j.description}") for w in words)
        ]

    if filters.sponsorship and user_id is not None:
        jobs = _filter_sponsorship(session, jobs, filters.sponsorship, user_id)

    if not filters.include_ineligible and user_id is not None:
        eligible = _verdicts(session, jobs, user_id)
        jobs = [j for j in jobs if eligible.get(j.id) != AuthVerdict.NOT_COMPATIBLE.value]

    jobs.sort(key=lambda j: (j.priority.overall if j.priority else 0.0), reverse=True)
    return jobs[:limit]


def _verdicts(session: Session, jobs: list[Job], user_id: int) -> dict[int, str]:
    if not jobs:
        return {}
    rows = session.scalars(
        select(EligibilityAssessment).where(
            EligibilityAssessment.user_id == user_id,
            EligibilityAssessment.job_id.in_([j.id for j in jobs]),
        )
    ).all()
    return {r.job_id: r.verdict for r in rows}


def _filter_sponsorship(session: Session, jobs: list[Job], mode: str, user_id: int) -> list[Job]:
    """`available` / `unknown` map onto the recorded signals, not onto guesses."""
    rows = {
        r.job_id: r
        for r in session.scalars(
            select(EligibilityAssessment).where(
                EligibilityAssessment.user_id == user_id,
                EligibilityAssessment.job_id.in_([j.id for j in jobs] or [0]),
            )
        ).all()
    }
    out = []
    for job in jobs:
        row = rows.get(job.id)
        signals = {s.get("signal") for s in (row.signals if row else [])}
        if mode == "available" and "sponsorship_available" in signals:
            out.append(job)
        elif mode == "unknown" and row and row.verdict == AuthVerdict.UNKNOWN.value:
            out.append(job)
        elif mode == "not_needed" and row and row.verdict == AuthVerdict.COMPATIBLE.value:
            out.append(job)
    return out
