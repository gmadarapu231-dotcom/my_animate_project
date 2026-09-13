"""Factuality check: every resume claim must trace to cited evidence.

This is the gate between "the AI rewrote a bullet" and "the resume goes out".
It re-derives the *entities* in each final bullet -- skills and technologies,
employers, job titles, certifications, degrees, numbers and any clearance or
visa language -- and asks whether the cited evidence supports each one.

The check is deliberately deterministic and runs even when the LLM rewrote the
text, because the whole point is to catch the model inventing something. It
never needs a network call, and an `unsupported` verdict blocks finalisation.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from careeros.engines.resume import EvidenceRecord, Resume, ResumeBlock
from careeros.engines.skills import SkillScanner, default_scanner
from careeros.engines.textutil import normalize
from careeros.enums import ClaimVerdict

#: Claims the system will never generate and must never let through.
_FORBIDDEN_PATTERNS = {
    "security clearance": re.compile(r"\b(security clearance|top secret|ts/sci|public trust|secret clearance)\b"),
    "visa / work authorization": re.compile(r"\b(h1b|h-1b|green card|us citizen|work authoriz|opt|ead|visa status)\b"),
}

_NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)?%?\b")

_DEGREE = re.compile(
    r"\b(ph\.?d|doctorate|master'?s?|mba|m\.?s\.?|m\.?tech|bachelor'?s?|b\.?s\.?|b\.?tech|b\.?e\.?|associate'?s? degree)\b"
)


@dataclass
class Claim:
    text: str
    verdict: ClaimVerdict
    evidence_ids: list[int] = field(default_factory=list)
    section: str | None = None
    issues: list[str] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


@dataclass
class FactualityReport:
    passed: bool
    claims: list[Claim] = field(default_factory=list)
    summary: str = ""

    @property
    def unsupported(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict is ClaimVerdict.UNSUPPORTED]

    @property
    def unsupported_count(self) -> int:
        return len(self.unsupported)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "unsupported_count": self.unsupported_count,
            "summary": self.summary,
            "claims": [c.to_dict() for c in self.claims],
        }


class FactualityChecker:
    def __init__(self, scanner: SkillScanner | None = None) -> None:
        self.scanner = scanner or default_scanner()

    # -- entity extraction --------------------------------------------------
    def _skills(self, text: str) -> set[str]:
        return set(self.scanner.scan(text))

    @staticmethod
    def _numbers(text: str) -> set[str]:
        return {m.group(0).replace(",", "").rstrip("%") for m in _NUMBER.finditer(text or "")}

    @staticmethod
    def _degrees(text: str) -> set[str]:
        return {m.group(0) for m in _DEGREE.finditer(normalize(text))}

    def _evidence_blob(self, ids: Sequence[int], by_id: dict[int, EvidenceRecord]) -> str:
        parts: list[str] = []
        for eid in ids:
            item = by_id.get(eid)
            if not item:
                continue
            parts.extend(
                [
                    item.text or "",
                    " ".join(item.technologies or ()),
                    " ".join(item.skills or ()),
                    item.employer or "",
                    item.role_title or "",
                    item.project or "",
                    " ".join(f"{k} {v}" for k, v in (item.metrics or {}).items()),
                ]
            )
        return normalize(" ".join(parts))

    # -- one block ----------------------------------------------------------
    def check_block(
        self,
        block: ResumeBlock,
        by_id: dict[int, EvidenceRecord],
        section: str,
        known_certifications: set[str],
        known_degrees: set[str],
        known_institutions: set[str],
    ) -> Claim:
        issues: list[str] = []
        text = block.text or ""
        norm = normalize(text)

        # 1. Nothing the system is forbidden from asserting, ever.
        for label, pattern in _FORBIDDEN_PATTERNS.items():
            if pattern.search(norm):
                issues.append(f"Mentions {label}; the system never asserts this on a resume.")

        # 2. A claim with no citation is only acceptable for generated
        #    structural lines (certifications/education), which are checked
        #    against the profile instead.
        if not block.evidence_ids:
            if section in {"certifications", "education"}:
                return self._check_profile_line(
                    text, section, known_certifications, known_degrees, known_institutions, issues
                )
            issues.append("No evidence cited.")
            return Claim(text=text, verdict=ClaimVerdict.UNSUPPORTED, section=section, issues=issues)

        missing_ids = [i for i in block.evidence_ids if i not in by_id]
        if missing_ids:
            issues.append(f"Cites evidence not in the database: {missing_ids}.")

        blob = self._evidence_blob(block.evidence_ids, by_id)
        cited = [by_id[i] for i in block.evidence_ids if i in by_id]

        # A skills line reads "<Category>: skill, skill". The category is a
        # generated heading, so only the list after the colon is a claim.
        claim_text = text
        if section == "skills" and ":" in text:
            claim_text = text.split(":", 1)[1]

        # 3. Every skill/technology asserted must appear in the cited evidence.
        evidence_skills: set[str] = set()
        for item in cited:
            evidence_skills |= self._skills(
                " ".join([item.text or "", " ".join(item.technologies or ()), item.role_title or ""])
            )
            evidence_skills |= {s for s in (item.skills or ()) if s}
        invented_skills = self._skills(claim_text) - evidence_skills
        if invented_skills:
            issues.append(
                "Technologies/skills not in the cited evidence: " + ", ".join(sorted(invented_skills)) + "."
            )

        # 4. Numbers may be dropped but never invented or altered.
        evidence_numbers = self._numbers(blob) | {
            str(v) for item in cited for v in (item.metrics or {}).values() if isinstance(v, (int, float))
        }
        evidence_numbers |= {n.replace(",", "").rstrip("%") for n in (block.derived_numbers or [])}
        invented_numbers = {n for n in self._numbers(text) if n not in evidence_numbers}
        # A standalone year already present in a date range is not a claim.
        invented_numbers = {n for n in invented_numbers if not (len(n) == 4 and n.startswith(("19", "20")))}
        if invented_numbers:
            issues.append("Figures not supported by the evidence: " + ", ".join(sorted(invented_numbers)) + ".")

        # 5. Degrees are claims about credentials.
        invented_degrees = self._degrees(text) - self._degrees(blob) - known_degrees
        if invented_degrees:
            issues.append("Degree claims not in evidence: " + ", ".join(sorted(invented_degrees)) + ".")

        if issues:
            return Claim(
                text=text,
                verdict=ClaimVerdict.UNSUPPORTED,
                evidence_ids=list(block.evidence_ids),
                section=section,
                issues=issues,
            )

        verified = all(by_id[i].verified for i in block.evidence_ids if i in by_id)
        if block.rewritten and block.original_text and normalize(block.original_text) != norm:
            verdict = ClaimVerdict.REPHRASED if verified else ClaimVerdict.WEAKLY_SUPPORTED
            note = "Reworded from evidence; facts unchanged."
        else:
            verdict = ClaimVerdict.SUPPORTED if verified else ClaimVerdict.WEAKLY_SUPPORTED
            note = None if verified else "Traces to evidence the user has not yet verified."

        return Claim(
            text=text,
            verdict=verdict,
            evidence_ids=list(block.evidence_ids),
            section=section,
            note=note,
        )

    @staticmethod
    def _check_profile_line(
        text: str,
        section: str,
        known_certifications: set[str],
        known_degrees: set[str],
        known_institutions: set[str],
        issues: list[str],
    ) -> Claim:
        """Verify a builder-generated credential line against the profile.

        Certifications are a comma-separated list, so each item is checked
        individually. An education line is a single record ("<degree>, <field>
        - <institution> (<year>)") and is checked as a whole: the degree and
        the institution must both be ones the profile records.
        """
        norm = normalize(text)
        if section == "certifications":
            for part in (p.strip() for p in re.split(r"[,;]", norm) if p.strip()):
                head = re.sub(r"\s*\(.*?\)\s*", " ", part).strip()
                if not any(head.startswith(k) or k in head for k in known_certifications):
                    issues.append(f"Certification '{part}' is not recorded in the user's profile.")
        else:
            if not any(d and d in norm for d in known_degrees):
                issues.append("Degree on this line is not recorded in the user's profile.")
            if known_institutions and not any(i and i in norm for i in known_institutions):
                issues.append("Institution on this line is not recorded in the user's profile.")
        verdict = ClaimVerdict.UNSUPPORTED if issues else ClaimVerdict.SUPPORTED
        return Claim(text=text, verdict=verdict, section=section, issues=issues)

    # -- whole resume -------------------------------------------------------
    def check(
        self,
        resume: Resume,
        evidence: Iterable[EvidenceRecord],
        certifications: Sequence[dict[str, Any]] = (),
        education: Sequence[dict[str, Any]] = (),
    ) -> FactualityReport:
        by_id = {e.id: e for e in evidence}
        known_certs = {normalize(c["name"]) for c in certifications}
        known_degrees = {normalize(e["degree"]) for e in education}
        known_institutions = {normalize(e["institution"]) for e in education if e.get("institution")}
        # Degree keywords from the profile, so "BS Information Systems" on the
        # resume is checked against "Bachelor of Science" in the profile.
        for deg in list(known_degrees):
            known_degrees |= self._degrees(deg)

        claims: list[Claim] = []
        for section in resume.sections:
            for entry in section.entries:
                for block in entry.blocks:
                    claims.append(
                        self.check_block(
                            block, by_id, section.kind, known_certs, known_degrees, known_institutions
                        )
                    )

        unsupported = [c for c in claims if c.verdict is ClaimVerdict.UNSUPPORTED]
        weak = [c for c in claims if c.verdict is ClaimVerdict.WEAKLY_SUPPORTED]
        passed = not unsupported
        summary = (
            f"{len(claims)} claims checked: {len(claims) - len(unsupported) - len(weak)} supported, "
            f"{len(weak)} weakly supported (unverified evidence), {len(unsupported)} unsupported."
        )
        if unsupported:
            summary += " Resume cannot be finalised until unsupported claims are removed."
        return FactualityReport(passed=passed, claims=claims, summary=summary)
