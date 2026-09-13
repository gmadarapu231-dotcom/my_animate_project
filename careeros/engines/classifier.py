"""Domain-agnostic job classification.

The classifier answers, for *any* posting in any field:

    domain / function / specialisation / seniority / industry
    required skills / preferred skills / certifications / soft skills
    experience / education / ATS keyword set

It is deliberately two-layered:

* a deterministic scorer over the seeded taxonomy, which always runs; and
* an optional LLM pass that can *invent a domain the taxonomy has never seen*.

When neither layer is confident the job is filed under `other` with whatever
label the evidence supports -- never force-fitted into a seeded bucket. New
domain ids returned by the model are persisted (see `pipeline.py`), so the
taxonomy grows without a code change.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from careeros.ai.provider import LLMProvider, get_provider
from careeros.ai.schemas import JobClassificationOut
from careeros.config import Taxonomy, taxonomy as default_taxonomy
from careeros.engines.skills import SkillScanner, default_scanner
from careeros.engines.textutil import (
    bulletize,
    candidate_phrases,
    extract_education_requirement,
    extract_years_experience,
    normalize,
    split_requirement_sections,
)

TITLE_WEIGHT = 6.0          # a title match is the strongest single signal
KEYWORD_TITLE_BONUS = 2.0
MAX_ATS_KEYWORDS = 40

_CERT_PATTERNS = re.compile(
    r"\b("
    r"ccna|ccnp|ccie|jncia|jncip|comptia [a-z+]+|security\+|network\+|a\+|"
    r"cissp|cisa|cism|crisc|ceh|oscp|gsec|gcih|gcia|sc-\d{3}|az-\d{3}|ms-\d{3}|"
    r"aws certified [a-z ]+|azure [a-z ]*(?:administrator|architect|engineer)|"
    r"gcp professional [a-z ]+|terraform associate|ckad|cka|"
    r"pmp|capm|csm|safe [a-z]*|itil|six sigma (?:green|black) belt|"
    r"cfa|cpa|cma|frm|series \d{1,2}|"
    r"rhce|rhcsa|mcsa|mcse|sap certified [a-z ]+|"
    r"cphq|rhia|rhit|ccs|cpc|chda"
    r")\b"
)

_SOFT_SKILLS = {
    "communication": ["communication", "communicate", "verbal and written"],
    "collaboration": ["collaboration", "cross-functional", "team player", "partner with"],
    "leadership": ["leadership", "mentor", "lead a team", "coaching"],
    "problem_solving": ["problem solving", "problem-solving", "analytical", "troubleshooting"],
    "stakeholder_management": ["stakeholder", "client facing", "customer facing", "executive"],
    "time_management": ["time management", "prioritize", "deadline driven", "multitask"],
    "adaptability": ["adaptability", "fast-paced", "ambiguity", "self-starter"],
    "documentation": ["documentation", "document", "runbook", "write clear"],
}

_INDUSTRY_HINTS = {
    "financial services": ["bank", "banking", "fintech", "capital markets", "insurance", "trading"],
    "healthcare": ["hospital", "healthcare", "clinical", "patient", "payer", "provider network", "pharma"],
    "government / public sector": ["federal", "government", "public sector", "state agency", "dod", "civilian agency"],
    "retail / e-commerce": ["retail", "e-commerce", "ecommerce", "merchandising", "storefront"],
    "manufacturing": ["manufacturing", "plant floor", "industrial", "supply chain operations"],
    "telecom": ["telecom", "carrier", "service provider", "5g", "isp"],
    "energy / utilities": ["energy", "utility", "utilities", "oil and gas", "power grid"],
    "education": ["university", "higher education", "k-12", "edtech", "campus"],
    "consulting": ["consulting", "client engagements", "professional services", "big 4"],
    "technology / saas": ["saas", "software company", "platform company", "product company"],
}


@dataclass
class SkillReq:
    name: str
    level: str = "required"
    skill_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClassificationResult:
    domain_id: str
    domain_label: str
    function: str | None = None
    specialization: str | None = None
    industry: str | None = None
    seniority: str = "mid"
    min_experience_years: float | None = None
    education_requirement: str | None = None
    required_skills: list[SkillReq] = field(default_factory=list)
    preferred_skills: list[SkillReq] = field(default_factory=list)
    required_certifications: list[str] = field(default_factory=list)
    soft_skills: list[str] = field(default_factory=list)
    ats_keywords: list[str] = field(default_factory=list)
    work_arrangement: str | None = None
    confidence: float = 0.0
    method: str = "heuristic"
    rationale: str | None = None
    domain_scores: dict[str, float] = field(default_factory=dict)
    is_new_domain: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["required_skills"] = [s.to_dict() for s in self.required_skills]
        d["preferred_skills"] = [s.to_dict() for s in self.preferred_skills]
        return d

    @property
    def all_skill_ids(self) -> list[str]:
        seen: list[str] = []
        for s in list(self.required_skills) + list(self.preferred_skills):
            if s.skill_id and s.skill_id not in seen:
                seen.append(s.skill_id)
        return seen


SYSTEM_PROMPT = """You classify job postings from ANY profession, industry and country.

You are not limited to technology. A veterinary practice manager, a shipping \
logistics coordinator, a municipal bond analyst and an SAP GRC consultant are \
all equally valid inputs.

Rules:
1. If none of the seeded domains fits, INVENT a new snake_case `domain_id` and \
a human-readable `domain_label`. Never force a job into a domain that does not \
describe it.
2. Extract skills using the job description's own wording.
3. Mark a skill `required` only when the posting presents it as a requirement; \
otherwise `preferred`.
4. `ats_keywords` are the terms an applicant tracking system would filter on \
for THIS posting and THIS industry - derive them from the text, do not reuse a \
generic technology list.
5. Do not infer work authorization, visa status or salary here.
6. `confidence` is your confidence in `domain_id`, from 0 to 1."""


class JobClassifier:
    def __init__(
        self,
        tax: Taxonomy | None = None,
        scanner: SkillScanner | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.taxonomy = tax or default_taxonomy()
        self.scanner = scanner or default_scanner()
        self.provider = provider or get_provider()

    # -- deterministic layer ------------------------------------------------
    def _score_domains(self, title_norm: str, body_norm: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        for spec in self.taxonomy.domains:
            if spec["id"] == "other":
                continue
            score = 0.0
            for pattern in spec.get("title_patterns", []):
                p = normalize(pattern)
                if p and p in title_norm:
                    # Longer, more specific title patterns count for more.
                    score += TITLE_WEIGHT * (1 + len(p.split()) / 10)
                elif p and p in body_norm:
                    score += 1.0
            for keyword, weight in (spec.get("keywords") or {}).items():
                k = normalize(str(keyword))
                if not k:
                    continue
                if k in title_norm:
                    score += float(weight) * KEYWORD_TITLE_BONUS
                elif k in body_norm:
                    score += float(weight) * 0.5
            if score:
                scores[spec["id"]] = round(score, 2)
        return scores

    def _seniority(self, title_norm: str, body_norm: str) -> str:
        best: tuple[int, str] | None = None
        for level in self.taxonomy.seniority_levels:
            for pattern in level.get("patterns", []):
                p = normalize(pattern)
                if not p:
                    continue
                if p in title_norm:
                    cand = (100 + len(p), level["id"])
                elif p in body_norm:
                    cand = (len(p), level["id"])
                else:
                    continue
                if best is None or cand > best:
                    best = cand
        return best[1] if best else self.taxonomy.default_seniority

    def _work_arrangement(self, text_norm: str) -> str | None:
        for spec in self.taxonomy.work_arrangements:
            for pattern in spec.get("patterns", []):
                if normalize(pattern) in text_norm:
                    return spec["id"]
        return None

    def _function(self, domain_id: str, text_norm: str) -> str | None:
        spec = self.taxonomy.domain_by_id.get(domain_id)
        if not spec:
            return None
        best: tuple[int, str] | None = None
        for fn in spec.get("functions", []):
            probe = fn.replace("_", " ")
            hits = text_norm.count(probe)
            if hits:
                cand = (hits * len(probe), fn)
                if best is None or cand > best:
                    best = cand
        return best[1] if best else (spec.get("functions") or [None])[0]

    def _industry(self, text_norm: str) -> str | None:
        best: tuple[int, str] | None = None
        for industry, needles in _INDUSTRY_HINTS.items():
            hits = sum(text_norm.count(n) for n in needles)
            if hits:
                cand = (hits, industry)
                if best is None or cand > best:
                    best = cand
        return best[1] if best else None

    def _requirements(self, description: str) -> tuple[list[SkillReq], list[SkillReq]]:
        sections = split_requirement_sections(description)
        required: dict[str, SkillReq] = {}
        preferred: dict[str, SkillReq] = {}

        def harvest(block: str, bucket: dict[str, SkillReq], level: str) -> None:
            for line in bulletize(block):
                for skill_id, hit in self.scanner.scan(line).items():
                    surface = max(hit.surfaces, key=len)
                    bucket.setdefault(skill_id, SkillReq(name=surface, level=level, skill_id=skill_id))

        harvest(sections.get("required", ""), required, "required")
        harvest(sections.get("preferred", ""), preferred, "preferred")
        # Required wins: a skill named in both sections is a requirement.
        for skill_id in list(preferred):
            if skill_id in required:
                del preferred[skill_id]

        # Anything only seen in unlabelled prose is a weaker signal: keep it,
        # but as `preferred` so it never inflates the required-match score.
        leftover = f"{sections.get('other','')}\n{sections.get('responsibilities','')}"
        for skill_id, hit in self.scanner.scan(leftover).items():
            if skill_id in required or skill_id in preferred:
                continue
            surface = max(hit.surfaces, key=len)
            preferred[skill_id] = SkillReq(name=surface, level="unspecified", skill_id=skill_id)

        return list(required.values()), list(preferred.values())

    def _certifications(self, text_norm: str) -> list[str]:
        found = {m.group(1).strip() for m in _CERT_PATTERNS.finditer(text_norm)}
        return sorted(found)

    def _soft_skills(self, text_norm: str) -> list[str]:
        return sorted(
            name for name, needles in _SOFT_SKILLS.items() if any(n in text_norm for n in needles)
        )

    def _ats_keywords(self, title: str, description: str, skills: list[SkillReq]) -> list[str]:
        """Derive the keyword set an ATS would filter on -- from THIS posting.

        Weighting: title terms and required-section terms outrank prose, and
        recognised skills are always included. Nothing about the weighting is
        domain-specific, which is why it works for an EHR analyst posting as
        well as a Kubernetes one.
        """
        sections = split_requirement_sections(description)
        weights = {"required": 3.0, "preferred": 1.6, "responsibilities": 1.2, "other": 0.8}
        scored: dict[str, float] = {}

        for phrase in candidate_phrases(title, max_n=3):
            scored[phrase] = scored.get(phrase, 0.0) + 4.0
        for section, weight in weights.items():
            body = sections.get(section, "")
            if not body:
                continue
            for phrase in candidate_phrases(body, max_n=3):
                scored[phrase] = scored.get(phrase, 0.0) + weight

        # Recognised skills are keywords by definition.
        for req in skills:
            name = normalize(req.name)
            if name:
                scored[name] = scored.get(name, 0.0) + (5.0 if req.level == "required" else 3.0)

        # Prefer multi-word phrases: "access control" beats "access".
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1] * (1 + 0.35 * kv[0].count(" ")), kv[0]))
        out: list[str] = []
        for phrase, _score in ranked:
            if any(phrase != other and phrase in other for other in out):
                continue   # already covered by a longer phrase we kept
            out.append(phrase)
            if len(out) >= MAX_ATS_KEYWORDS:
                break
        return out

    def heuristic_classify(self, title: str, description: str) -> ClassificationResult:
        title_norm = normalize(title)
        body_norm = normalize(description)
        full_norm = f"{title_norm} {body_norm}"

        scores = self._score_domains(title_norm, body_norm)
        total = sum(scores.values()) or 1.0
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top_id, top_score = ranked[0] if ranked else ("other", 0.0)
        confidence = top_score / total

        if not ranked or confidence < self.taxonomy.confidence_floor:
            domain_id, domain_label = "other", "Other / Uncategorised"
            confidence = min(confidence, self.taxonomy.confidence_floor)
        else:
            domain_id = top_id
            domain_label = self.taxonomy.domain_by_id.get(top_id, {}).get("label", top_id)

        required, preferred = self._requirements(description)
        result = ClassificationResult(
            domain_id=domain_id,
            domain_label=domain_label,
            function=self._function(domain_id, full_norm),
            specialization=None,
            industry=self._industry(full_norm),
            seniority=self._seniority(title_norm, body_norm),
            min_experience_years=extract_years_experience(description),
            education_requirement=extract_education_requirement(description),
            required_skills=required,
            preferred_skills=preferred,
            required_certifications=self._certifications(full_norm),
            soft_skills=self._soft_skills(full_norm),
            work_arrangement=self._work_arrangement(full_norm),
            confidence=round(confidence, 3),
            method="heuristic",
            domain_scores={k: round(v / total, 3) for k, v in ranked[:6]},
        )
        result.specialization = self._specialization(result, title_norm)
        result.ats_keywords = self._ats_keywords(title, description, required + preferred)
        result.rationale = self._rationale(result, ranked)
        return result

    def _specialization(self, result: ClassificationResult, title_norm: str) -> str | None:
        """The most specific thing this job is about."""
        labels = [
            self.taxonomy.skills[s.skill_id].label
            for s in result.required_skills
            if s.skill_id and s.skill_id in self.taxonomy.skills
        ]
        for label in labels:
            if normalize(label) in title_norm:
                return label
        if labels:
            return " / ".join(labels[:2])
        return result.function.replace("_", " ").title() if result.function else None

    @staticmethod
    def _rationale(result: ClassificationResult, ranked: list[tuple[str, float]]) -> str:
        if not ranked:
            return "No taxonomy signal matched; filed as Other pending AI review."
        top = ", ".join(f"{d} {s:.0f}" for d, s in ranked[:3])
        return (
            f"Domain signal: {top}. Seniority '{result.seniority}' from title/description; "
            f"{len(result.required_skills)} required and {len(result.preferred_skills)} "
            f"preferred skills recognised."
        )

    # -- AI layer ----------------------------------------------------------
    def _cacheable_context(self) -> str:
        lines = ["Seeded career domains (not exhaustive - invent new ones when needed):"]
        for spec in self.taxonomy.domains:
            if spec["id"] == "other":
                continue
            fns = ", ".join(spec.get("functions", [])[:8])
            lines.append(f"- {spec['id']} ({spec['label']}): {fns}")
        return "\n".join(lines)

    def _merge(self, base: ClassificationResult, ai: JobClassificationOut) -> ClassificationResult:
        merged = ClassificationResult(**{**base.to_dict(), "required_skills": [], "preferred_skills": []})
        merged.required_skills = list(base.required_skills)
        merged.preferred_skills = list(base.preferred_skills)
        merged.method = "hybrid"

        # The model owns the taxonomy decision when it is more confident, and
        # is the only layer that can name a domain the taxonomy lacks.
        if ai.domain_id and (ai.confidence >= base.confidence or base.domain_id == "other"):
            merged.domain_id = ai.domain_id.strip().lower().replace(" ", "_")
            merged.domain_label = ai.domain_label or merged.domain_id.replace("_", " ").title()
            merged.confidence = float(ai.confidence)
            merged.is_new_domain = merged.domain_id not in self.taxonomy.domain_by_id

        merged.function = ai.function or merged.function
        merged.specialization = ai.specialization or merged.specialization
        merged.industry = ai.industry or merged.industry
        merged.seniority = ai.seniority or merged.seniority
        merged.min_experience_years = (
            ai.min_experience_years if ai.min_experience_years is not None else merged.min_experience_years
        )
        merged.education_requirement = ai.education_requirement or merged.education_requirement
        merged.rationale = ai.rationale or merged.rationale

        known_req = {s.skill_id or normalize(s.name) for s in merged.required_skills}
        for item in ai.required_skills:
            skill_id = item.skill_id or self.scanner.resolve(item.name)
            key = skill_id or normalize(item.name)
            if key in known_req:
                continue
            merged.required_skills.append(SkillReq(name=item.name, level="required", skill_id=skill_id))
            known_req.add(key)

        known_pref = {s.skill_id or normalize(s.name) for s in merged.preferred_skills} | known_req
        for item in ai.preferred_skills:
            skill_id = item.skill_id or self.scanner.resolve(item.name)
            key = skill_id or normalize(item.name)
            if key in known_pref:
                continue
            merged.preferred_skills.append(SkillReq(name=item.name, level="preferred", skill_id=skill_id))
            known_pref.add(key)

        for cert in ai.required_certifications:
            if cert.lower() not in {c.lower() for c in merged.required_certifications}:
                merged.required_certifications.append(cert)
        for soft in ai.soft_skills:
            if soft.lower() not in {s.lower() for s in merged.soft_skills}:
                merged.soft_skills.append(soft)

        if ai.ats_keywords:
            seen = list(dict.fromkeys(normalize(k) for k in ai.ats_keywords if k.strip()))
            merged.ats_keywords = list(dict.fromkeys(seen + merged.ats_keywords))[:MAX_ATS_KEYWORDS]
        return merged

    def classify(self, title: str, description: str, use_ai: bool = True) -> ClassificationResult:
        base = self.heuristic_classify(title, description)
        if not use_ai or not getattr(self.provider, "available", False):
            return base

        prompt = (
            f"JOB TITLE: {title}\n\n"
            f"JOB DESCRIPTION:\n{description}\n\n"
            f"Deterministic pre-read (may be wrong, override it freely): "
            f"domain={base.domain_id} confidence={base.confidence:.2f} "
            f"seniority={base.seniority}"
        )
        ai = self.provider.structured(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            schema=JobClassificationOut,
            cacheable_context=self._cacheable_context(),
        )
        return self._merge(base, ai) if ai else base
