"""Cross-domain skill matching against the Career Evidence Database.

The matcher answers two different questions and never confuses them:

1. *Is this person plausibly a fit?*  -- transferable skills count here.
2. *What may a resume claim?*        -- only DIRECT and PARTIAL-narrower
   matches are claimable; RELATED matches are surfaced to the user as
   "adjacent experience" and are explicitly `claimable=False`, so the resume
   engine can never turn "I know Active Directory" into "I have done SAP GRC".

Every match carries the evidence ids that back it, which is what makes a
generated resume auditable line by line.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from careeros.config import Taxonomy, taxonomy as default_taxonomy
from careeros.engines.classifier import ClassificationResult, SkillReq
from careeros.engines.skills import SkillScanner, default_scanner
from careeros.engines.textutil import content_tokens, normalize
from careeros.enums import MatchKind

REQUIRED_WEIGHT = 1.0
PREFERRED_WEIGHT = 0.4

SKILL_COMPONENT = 0.65
EXPERIENCE_COMPONENT = 0.20
TITLE_COMPONENT = 0.15


@dataclass
class EvidenceRef:
    """Minimal view of an evidence row -- keeps the matcher ORM-independent."""

    id: int
    text: str
    skills: Sequence[str] = ()
    technologies: Sequence[str] = ()
    role_title: str | None = None
    employer: str | None = None
    strength: float = 0.5
    verified: bool = False
    years: float = 0.0


@dataclass
class ProfileIndex:
    """Which skills the user can evidence, and with what."""

    skills: dict[str, list[int]] = field(default_factory=dict)   # skill_id -> evidence ids
    strength: dict[str, float] = field(default_factory=dict)     # skill_id -> 0..1
    titles: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    total_experience_years: float = 0.0
    education_rank: int = 0

    def has(self, skill_id: str) -> bool:
        return skill_id in self.skills


@dataclass
class SkillMatch:
    requirement: str
    kind: MatchKind
    score: float
    level: str = "required"
    requirement_skill_id: str | None = None
    via_skill_id: str | None = None
    evidence_ids: list[int] = field(default_factory=list)
    claimable: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class MatchResult:
    match_score: float = 0.0
    skill_score: float = 0.0
    experience_match: float = 0.0
    title_match: float = 0.0
    matches: list[SkillMatch] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    transferable: list[str] = field(default_factory=list)

    @property
    def direct_count(self) -> int:
        return sum(1 for m in self.matches if m.kind is MatchKind.DIRECT)

    @property
    def related_count(self) -> int:
        return sum(1 for m in self.matches if m.kind is MatchKind.RELATED)

    @property
    def partial_count(self) -> int:
        return sum(1 for m in self.matches if m.kind is MatchKind.PARTIAL)

    @property
    def missing_count(self) -> int:
        return sum(1 for m in self.matches if m.kind is MatchKind.MISSING)

    def claimable_evidence_ids(self) -> list[int]:
        out: list[int] = []
        for m in self.matches:
            if m.claimable:
                out.extend(i for i in m.evidence_ids if i not in out)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_score": self.match_score,
            "skill_score": self.skill_score,
            "experience_match": self.experience_match,
            "title_match": self.title_match,
            "direct_count": self.direct_count,
            "related_count": self.related_count,
            "partial_count": self.partial_count,
            "missing_count": self.missing_count,
            "matches": [m.to_dict() for m in self.matches],
            "gaps": self.gaps,
            "transferable": self.transferable,
        }


class SkillMatcher:
    def __init__(self, tax: Taxonomy | None = None, scanner: SkillScanner | None = None) -> None:
        self.taxonomy = tax or default_taxonomy()
        self.scanner = scanner or default_scanner()

    # -- profile ------------------------------------------------------------
    def build_profile(
        self,
        evidence: Iterable[EvidenceRef],
        certifications: Sequence[str] = (),
        total_experience_years: float = 0.0,
        education_rank: int = 0,
    ) -> ProfileIndex:
        index = ProfileIndex(
            certifications=[normalize(c) for c in certifications],
            total_experience_years=total_experience_years,
            education_rank=education_rank,
        )
        for item in evidence:
            blob = " ".join(
                [item.text or "", " ".join(item.technologies or ()), item.role_title or ""]
            )
            found = set(self.scanner.scan(blob))
            # Explicit skill tags on the evidence row are authoritative.
            found.update(s for s in (item.skills or ()) if s)
            for skill_id in found:
                index.skills.setdefault(skill_id, []).append(item.id)
                # Strength grows with corroboration but saturates: five bullets
                # about Splunk is strong, fifty is not ten times stronger.
                prior = index.strength.get(skill_id, 0.0)
                bump = item.strength * (1.2 if item.verified else 0.9)
                index.strength[skill_id] = min(1.0, prior + bump * (1.0 - prior))
            if item.role_title:
                index.titles.append(normalize(item.role_title))
        return index

    # -- one requirement ----------------------------------------------------
    def match_requirement(self, req: SkillReq, profile: ProfileIndex) -> SkillMatch:
        weights = self.taxonomy.weights
        skill_id = req.skill_id or self.scanner.resolve(req.name)

        if not skill_id:
            # Unresolvable requirement: fall back to literal token overlap so a
            # niche, non-technical requirement is not silently dropped.
            needle = set(content_tokens(req.name))
            if needle and any(needle <= set(content_tokens(t)) for t in profile.titles):
                return SkillMatch(
                    requirement=req.name,
                    kind=MatchKind.PARTIAL,
                    score=weights.get("broader", 0.55),
                    level=req.level,
                    claimable=False,
                    note="Matched against a previous job title, not a catalogued skill.",
                )
            return SkillMatch(
                requirement=req.name,
                kind=MatchKind.UNKNOWN,
                score=0.0,
                level=req.level,
                note="Requirement could not be mapped to a known skill - review manually.",
            )

        if profile.has(skill_id):
            return SkillMatch(
                requirement=req.name,
                kind=MatchKind.DIRECT,
                score=weights.get("direct", 1.0),
                level=req.level,
                requirement_skill_id=skill_id,
                via_skill_id=skill_id,
                evidence_ids=profile.skills[skill_id],
                claimable=True,
            )

        neighbours = self.taxonomy.neighbours(skill_id)

        # The user holds a *more specific* form of the requirement. Real,
        # claimable experience -- "Azure AD" does evidence "IAM".
        candidate = self._best_candidate(neighbours["narrower"], profile)
        if candidate:
            label = self.taxonomy.skills[candidate].label
            return SkillMatch(
                requirement=req.name,
                kind=MatchKind.PARTIAL,
                score=weights.get("narrower", 0.8),
                level=req.level,
                requirement_skill_id=skill_id,
                via_skill_id=candidate,
                evidence_ids=profile.skills[candidate],
                claimable=True,
                note=f"Evidenced by the more specific skill '{label}'.",
            )

        # The user holds the umbrella but not this specific tool. Partial, and
        # NOT claimable as the specific tool.
        candidate = self._best_candidate(neighbours["broader"], profile)
        if candidate:
            label = self.taxonomy.skills[candidate].label
            return SkillMatch(
                requirement=req.name,
                kind=MatchKind.PARTIAL,
                score=weights.get("broader", 0.55),
                level=req.level,
                requirement_skill_id=skill_id,
                via_skill_id=candidate,
                evidence_ids=profile.skills[candidate],
                claimable=False,
                note=f"General '{label}' background; no evidence of this specific tool.",
            )

        # Adjacent skill: relevant, explicitly not claimable.
        candidate = self._best_candidate(neighbours["related"], profile)
        if candidate:
            label = self.taxonomy.skills[candidate].label
            return SkillMatch(
                requirement=req.name,
                kind=MatchKind.RELATED,
                score=weights.get("related", 0.45),
                level=req.level,
                requirement_skill_id=skill_id,
                via_skill_id=candidate,
                evidence_ids=profile.skills[candidate],
                claimable=False,
                note=f"Transferable from '{label}' - do not present as direct experience.",
            )

        return SkillMatch(
            requirement=req.name,
            kind=MatchKind.MISSING,
            score=0.0,
            level=req.level,
            requirement_skill_id=skill_id,
            claimable=False,
        )

    @staticmethod
    def _best_candidate(candidates: Sequence[str], profile: ProfileIndex) -> str | None:
        """The neighbour the profile evidences most strongly, or None.

        Several skills can satisfy one requirement -- both "Active Directory"
        and "Access Review" sit under "IAM". Pick the one the user can actually
        back up, rather than whichever happens to sort first.
        """
        held = [c for c in candidates if profile.has(c)]
        if not held:
            return None
        return min(
            held,
            key=lambda c: (-profile.strength.get(c, 0.0), -len(profile.skills.get(c, [])), c),
        )

    # -- experience / title -------------------------------------------------
    @staticmethod
    def _experience_match(required_years: float | None, actual_years: float) -> float:
        if not required_years:
            return 1.0 if actual_years else 0.6
        if actual_years >= required_years:
            return 1.0
        if required_years <= 0:
            return 1.0
        # Partial credit, floored: 60% of the ask is meaningfully close.
        return max(0.0, min(1.0, actual_years / required_years))

    @staticmethod
    def _title_match(job_title: str, candidate_titles: Sequence[str]) -> float:
        job_tokens = set(content_tokens(job_title))
        if not job_tokens:
            return 0.0
        best = 0.0
        for title in candidate_titles:
            other = set(content_tokens(title))
            if not other:
                continue
            overlap = len(job_tokens & other) / len(job_tokens)
            best = max(best, overlap)
        return round(best, 3)

    # -- full match ---------------------------------------------------------
    def match(
        self,
        classification: ClassificationResult,
        profile: ProfileIndex,
        job_title: str = "",
        preferred_titles: Sequence[str] = (),
    ) -> MatchResult:
        result = MatchResult()
        weighted_sum = 0.0
        weight_total = 0.0

        for req in classification.required_skills:
            m = self.match_requirement(req, profile)
            m.level = "required"
            result.matches.append(m)
            weighted_sum += m.score * REQUIRED_WEIGHT
            weight_total += REQUIRED_WEIGHT
            if m.kind is MatchKind.MISSING:
                result.gaps.append(req.name)
            elif m.kind is MatchKind.RELATED:
                result.transferable.append(req.name)

        for req in classification.preferred_skills:
            m = self.match_requirement(req, profile)
            m.level = req.level or "preferred"
            result.matches.append(m)
            weighted_sum += m.score * PREFERRED_WEIGHT
            weight_total += PREFERRED_WEIGHT
            if m.kind is MatchKind.RELATED:
                result.transferable.append(req.name)

        result.skill_score = round(100 * weighted_sum / weight_total, 1) if weight_total else 0.0
        result.experience_match = round(
            100 * self._experience_match(classification.min_experience_years, profile.total_experience_years), 1
        )
        result.title_match = round(
            100 * self._title_match(job_title, list(profile.titles) + list(preferred_titles)), 1
        )
        result.match_score = round(
            SKILL_COMPONENT * result.skill_score
            + EXPERIENCE_COMPONENT * result.experience_match
            + TITLE_COMPONENT * result.title_match,
            1,
        )
        return result
