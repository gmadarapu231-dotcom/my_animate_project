"""Universal ATS scoring.

"Universal" means the engine never carries an industry's keyword list. The
keyword set comes from the posting itself (`JobClassifier._ats_keywords`), so a
SAP GRC posting is scored on SAP GRC terms, a healthcare posting on EHR/HIPAA
terms and a marketing posting on SEO/SEM terms -- with the same code.

The score is a weighted blend of eight components, each reported separately so
the UI can say *why* a resume scores what it does.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from careeros.engines.classifier import ClassificationResult
from careeros.engines.skills import SkillScanner, default_scanner
from careeros.engines.textutil import EDU_RANK, content_tokens, normalize

COMPONENT_WEIGHTS = {
    "keyword_match": 0.25,
    "required_skills_match": 0.25,
    "preferred_skills_match": 0.10,
    "experience_match": 0.15,
    "title_match": 0.10,
    "education_match": 0.08,
    "certification_match": 0.07,
}

#: Applied as a multiplier at the end -- an unparseable resume scores nothing
#: on any component, no matter how well it matches.
FORMATTING_FLOOR = 0.6


@dataclass
class AtsResult:
    overall: float = 0.0
    keyword_match: float = 0.0
    required_skills_match: float = 0.0
    preferred_skills_match: float = 0.0
    experience_match: float = 0.0
    title_match: float = 0.0
    education_match: float = 0.0
    certification_match: float = 0.0
    formatting_score: float = 100.0
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResumeFacts:
    """What the ATS engine needs to know about the candidate side."""

    text: str
    titles: Sequence[str] = ()
    certifications: Sequence[str] = ()
    total_experience_years: float = 0.0
    education_level: str | None = None


_FORMATTING_CHECKS = (
    ("contact block", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), 10),
    ("experience section", re.compile(r"(?im)^\s*(professional\s+)?experience\b"), 12),
    ("skills section", re.compile(r"(?im)^\s*(technical\s+)?skills\b"), 10),
    ("education section", re.compile(r"(?im)^\s*education\b"), 8),
    ("bullet points", re.compile(r"(?m)^\s*[-*•]"), 10),
)


class AtsEngine:
    def __init__(self, scanner: SkillScanner | None = None) -> None:
        self.scanner = scanner or default_scanner()

    # -- components ---------------------------------------------------------
    def _keyword_match(self, keywords: Sequence[str], resume_norm: str) -> tuple[float, list[str], list[str]]:
        if not keywords:
            return 100.0, [], []
        matched, missing = [], []
        # Earlier keywords are the higher-value ones; weight the score by rank
        # so missing the top term costs more than missing the 38th.
        total_weight = 0.0
        hit_weight = 0.0
        for rank, keyword in enumerate(keywords):
            weight = 1.0 / (1.0 + rank * 0.05)
            total_weight += weight
            if keyword and keyword in resume_norm:
                matched.append(keyword)
                hit_weight += weight
            else:
                missing.append(keyword)
        return round(100 * hit_weight / total_weight, 1), matched, missing

    def _skills_match(self, skills, resume_skill_ids: set[str], resume_norm: str) -> float:
        if not skills:
            return 100.0
        hits = 0
        for req in skills:
            if req.skill_id and req.skill_id in resume_skill_ids:
                hits += 1
            elif normalize(req.name) in resume_norm:
                hits += 1
        return round(100 * hits / len(skills), 1)

    @staticmethod
    def _experience_match(required: float | None, actual: float) -> float:
        if not required:
            return 100.0
        if actual >= required:
            return 100.0
        return round(max(0.0, 100 * actual / required), 1)

    @staticmethod
    def _title_match(job_title: str, resume_titles: Sequence[str], resume_norm: str) -> float:
        job_tokens = set(content_tokens(job_title))
        if not job_tokens:
            return 100.0
        best = 0.0
        for title in resume_titles:
            other = set(content_tokens(title))
            if other:
                best = max(best, len(job_tokens & other) / len(job_tokens))
        if normalize(job_title) in resume_norm:
            best = 1.0
        return round(100 * best, 1)

    @staticmethod
    def _education_match(required: str | None, actual: str | None) -> float:
        if not required:
            return 100.0
        need = EDU_RANK.get(required, 0)
        have = EDU_RANK.get(actual or "", 0)
        if not have:
            return 0.0
        if have >= need:
            return 100.0
        return round(100 * have / need, 1)

    @staticmethod
    def _certification_match(required: Sequence[str], held: Sequence[str], resume_norm: str) -> float:
        if not required:
            return 100.0
        held_norm = {normalize(c) for c in held}
        hits = 0
        for cert in required:
            c = normalize(cert)
            if any(c in h or h in c for h in held_norm) or c in resume_norm:
                hits += 1
        return round(100 * hits / len(required), 1)

    def _formatting(self, resume_text: str) -> tuple[float, list[str]]:
        score = 50.0
        notes: list[str] = []
        for label, pattern, points in _FORMATTING_CHECKS:
            if pattern.search(resume_text or ""):
                score += points
            else:
                notes.append(f"ATS parsing: add a clearly labelled {label}.")
        words = len((resume_text or "").split())
        if words < 150:
            notes.append("Resume looks short for ATS keyword coverage (under ~150 words).")
            score -= 10
        if words > 1400:
            notes.append("Resume is long; most ATS screens weight the first two pages.")
            score -= 5
        if "\t" in (resume_text or "") or "|" in (resume_text or ""):
            notes.append("Avoid tables/columns - many parsers flatten them incorrectly.")
            score -= 5
        return max(0.0, min(100.0, score)), notes

    # -- public -------------------------------------------------------------
    def score(
        self,
        classification: ClassificationResult,
        resume: ResumeFacts,
        job_title: str = "",
    ) -> AtsResult:
        resume_norm = normalize(resume.text)
        resume_skill_ids = set(self.scanner.scan(resume.text))

        keyword_score, matched, missing = self._keyword_match(classification.ats_keywords, resume_norm)
        formatting, format_notes = self._formatting(resume.text)

        result = AtsResult(
            keyword_match=keyword_score,
            required_skills_match=self._skills_match(
                classification.required_skills, resume_skill_ids, resume_norm
            ),
            preferred_skills_match=self._skills_match(
                classification.preferred_skills, resume_skill_ids, resume_norm
            ),
            experience_match=self._experience_match(
                classification.min_experience_years, resume.total_experience_years
            ),
            title_match=self._title_match(job_title, resume.titles, resume_norm),
            education_match=self._education_match(
                classification.education_requirement, resume.education_level
            ),
            certification_match=self._certification_match(
                classification.required_certifications, resume.certifications, resume_norm
            ),
            formatting_score=formatting,
            matched_keywords=matched,
            missing_keywords=missing,
        )

        blended = sum(getattr(result, name) * weight for name, weight in COMPONENT_WEIGHTS.items())
        penalty = FORMATTING_FLOOR + (1 - FORMATTING_FLOOR) * (formatting / 100)
        result.overall = round(blended * penalty, 1)
        result.suggestions = self._suggestions(result, classification, format_notes)
        return result

    @staticmethod
    def _suggestions(
        result: AtsResult, classification: ClassificationResult, format_notes: list[str]
    ) -> list[str]:
        out: list[str] = []
        top_missing = [k for k in result.missing_keywords[:8]]
        if top_missing:
            out.append(
                "Highest-value terms absent from the resume: " + ", ".join(top_missing) + "."
            )
        if result.required_skills_match < 80:
            missing_required = [
                s.name for s in classification.required_skills if s.name.lower() not in
                " ".join(result.matched_keywords)
            ][:6]
            if missing_required:
                out.append(
                    "Required skills not clearly represented: "
                    + ", ".join(missing_required)
                    + ". Only add these if the Career Evidence Database supports them."
                )
        if result.experience_match < 100 and classification.min_experience_years:
            out.append(
                f"Posting asks for {classification.min_experience_years:g}+ years; make total "
                f"relevant experience explicit near the top of the resume."
            )
        if result.certification_match < 100 and classification.required_certifications:
            out.append(
                "Certifications named in the posting are not on the resume: "
                + ", ".join(classification.required_certifications)
                + ". Do not add a certification you do not hold."
            )
        if result.title_match < 50:
            out.append(
                "Consider a target-title line (e.g. a summary headline) matching the posting's "
                "title wording, where your history honestly supports it."
            )
        out.extend(format_notes)
        return out
