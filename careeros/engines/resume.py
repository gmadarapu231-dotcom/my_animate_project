"""Resume assembly and tailoring, built on the Career Evidence Database.

A resume here is never a blob of prose that gets edited. It is *assembled* from
atomic evidence rows, and every block keeps the ids it came from. That single
choice is what makes the tailoring rules enforceable:

The AI MAY reorder sections, reorder and rewrite bullets, improve grammar and
clarity, fold redundant bullets together, adopt the posting's vocabulary and
re-pitch the summary and skills sections.

The AI MUST NOT invent experience, employers, projects, technologies,
certifications, degrees, job titles, responsibilities, achievements, security
clearances or visa status.

The second list is enforced by `factuality.py`, which re-derives every entity
in the final text and refuses to pass a resume whose claims do not trace back
to cited evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from careeros.ai.provider import LLMProvider, get_provider
from careeros.ai.schemas import TailoringOut
from careeros.config import Taxonomy, taxonomy as default_taxonomy
from careeros.engines.classifier import ClassificationResult
from careeros.engines.matching import MatchResult
from careeros.engines.skills import SkillScanner, default_scanner
from careeros.engines.textutil import normalize
from careeros.enums import ResumeKind

MAX_BULLETS_PER_ROLE = 6
MAX_SKILL_LINES = 6


@dataclass
class EvidenceRecord:
    """Everything the resume layer needs from one evidence row."""

    id: int
    kind: str
    text: str
    employer: str | None = None
    role_title: str | None = None
    project: str | None = None
    technologies: Sequence[str] = ()
    skills: Sequence[str] = ()
    start_date: date | None = None
    end_date: date | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    strength: float = 0.5


@dataclass
class ResumeBlock:
    text: str
    evidence_ids: list[int] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    rewritten: bool = False
    original_text: str | None = None
    #: Figures this block legitimately states that come from the user's
    #: profile rather than from an evidence bullet (total years of experience,
    #: a count of employers). Declared here so the factuality checker can tell
    #: them apart from a figure the model invented.
    derived_numbers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResumeEntry:
    heading: str | None = None
    subheading: str | None = None
    meta: str | None = None
    blocks: list[ResumeBlock] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "subheading": self.subheading,
            "meta": self.meta,
            "blocks": [b.to_dict() for b in self.blocks],
        }


@dataclass
class ResumeSection:
    name: str
    kind: str
    entries: list[ResumeEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "entries": [e.to_dict() for e in self.entries]}


@dataclass
class Resume:
    name: str
    kind: ResumeKind
    sections: list[ResumeSection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    track_id: int | None = None
    job_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "sections": [s.to_dict() for s in self.sections],
            "notes": self.notes,
            "track_id": self.track_id,
            "job_id": self.job_id,
        }

    def all_blocks(self) -> list[ResumeBlock]:
        return [b for s in self.sections for e in s.entries for b in e.blocks]

    def cited_evidence_ids(self) -> set[int]:
        return {i for b in self.all_blocks() for i in b.evidence_ids}


@dataclass
class ContactInfo:
    full_name: str
    email: str
    phone: str | None = None
    location: str | None = None
    links: dict[str, str] = field(default_factory=dict)


TAILOR_SYSTEM = """You tailor a resume by REWORDING existing verified evidence. \
You are given evidence items, each with an id, and a target job description.

ABSOLUTE RULES - violating any of these makes the output unusable:
- Every bullet you return MUST cite the `evidence_id` it was derived from.
- You may rephrase, tighten, reorder, merge and adopt the job description's \
vocabulary WHERE IT DESCRIBES THE SAME FACT.
- You MUST NOT introduce any employer, project, technology, tool, metric, \
certification, degree, job title, responsibility, achievement, security \
clearance or visa status that is not present in the evidence item you cite.
- You MUST NOT change a number, a date, a scale or an outcome.
- If a job requirement is not supported by the evidence, say nothing about it. \
Do not hint, do not imply adjacency. Leave the gap.
- The professional summary must be assembled only from facts in the evidence.

Return concise, achievement-oriented bullets in the candidate's own register."""


class ResumeBuilder:
    def __init__(
        self,
        tax: Taxonomy | None = None,
        scanner: SkillScanner | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.taxonomy = tax or default_taxonomy()
        self.scanner = scanner or default_scanner()
        self.provider = provider or get_provider()

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _date_range(start: date | None, end: date | None) -> str:
        if not start and not end:
            return ""
        fmt = lambda d: d.strftime("%b %Y") if d else "Present"   # noqa: E731
        return f"{fmt(start)} - {fmt(end)}"

    def _employment_groups(self, evidence: Iterable[EvidenceRecord]) -> list[tuple[str, str, date | None, date | None, list[EvidenceRecord]]]:
        """Group evidence by employer+title, newest first."""
        groups: dict[tuple[str, str], list[EvidenceRecord]] = {}
        for item in evidence:
            key = (item.employer or "Independent / Project work", item.role_title or "")
            groups.setdefault(key, []).append(item)
        out = []
        for (employer, title), items in groups.items():
            starts = [i.start_date for i in items if i.start_date]
            ends = [i.end_date for i in items if i.end_date]
            start = min(starts) if starts else None
            end = None if any(i.end_date is None and i.start_date for i in items) else (max(ends) if ends else None)
            out.append((employer, title, start, end, items))
        out.sort(key=lambda g: (g[2] or date.min), reverse=True)
        return out

    def _skill_lines(self, evidence: Iterable[EvidenceRecord], emphasise: Sequence[str] = ()) -> list[ResumeBlock]:
        """Skills grouped by the domains they belong to, JD-relevant ones first.

        Only skills the evidence actually supports are listed -- the skills
        section is generated, never hand-maintained, so it cannot drift.
        """
        by_skill: dict[str, list[int]] = {}
        for item in evidence:
            blob = " ".join([item.text or "", " ".join(item.technologies or ()), item.role_title or ""])
            found = set(self.scanner.scan(blob)) | {s for s in (item.skills or ()) if s}
            for skill_id in found:
                by_skill.setdefault(skill_id, []).append(item.id)

        emphasis = list(emphasise)
        grouped: dict[str, list[str]] = {}
        for skill_id in by_skill:
            node = self.taxonomy.skills.get(skill_id)
            domain = (node.domains[0] if node and node.domains else "general")
            grouped.setdefault(domain, []).append(skill_id)

        def group_rank(item: tuple[str, list[str]]) -> tuple[int, str]:
            hits = sum(1 for s in item[1] if s in emphasis)
            return (-hits, item[0])

        blocks: list[ResumeBlock] = []
        for domain, skill_ids in sorted(grouped.items(), key=group_rank)[:MAX_SKILL_LINES]:
            skill_ids.sort(key=lambda s: (s not in emphasis, s))
            labels = [self.taxonomy.skills[s].label for s in skill_ids if s in self.taxonomy.skills]
            if not labels:
                continue
            domain_label = self.taxonomy.domain_by_id.get(domain, {}).get("label", domain.replace("_", " ").title())
            evidence_ids = sorted({i for s in skill_ids for i in by_skill.get(s, [])})
            blocks.append(
                ResumeBlock(
                    text=f"{domain_label}: " + ", ".join(labels),
                    evidence_ids=evidence_ids,
                    skills=skill_ids,
                )
            )
        return blocks

    def _summary_block(
        self,
        contact: ContactInfo,
        evidence: Sequence[EvidenceRecord],
        total_years: float,
        headline: str | None,
        emphasise: Sequence[str],
    ) -> ResumeBlock:
        """A summary built only from countable facts, never adjectives about fit."""
        titles = [e.role_title for e in evidence if e.role_title]
        current_title = headline or (titles[0] if titles else "Professional")
        top_skills = [
            self.taxonomy.skills[s].label
            for s in emphasise
            if s in self.taxonomy.skills
        ][:4]
        employers = list(dict.fromkeys(e.employer for e in evidence if e.employer))

        parts = [f"{current_title} with {total_years:g}+ years of experience"] if total_years else [current_title]
        if top_skills:
            parts.append("focused on " + ", ".join(top_skills))
        if employers:
            parts.append(f"across {len(employers)} organisation{'s' if len(employers) > 1 else ''}")
        text = "; ".join(parts) + "."
        return ResumeBlock(
            text=text,
            evidence_ids=[e.id for e in evidence],
            skills=list(emphasise[:6]),
            derived_numbers=[str(int(total_years)) if float(total_years).is_integer() else str(total_years),
                             str(len(employers))],
        )

    # -- assembly -----------------------------------------------------------
    def build(
        self,
        contact: ContactInfo,
        evidence: Sequence[EvidenceRecord],
        certifications: Sequence[dict[str, Any]] = (),
        education: Sequence[dict[str, Any]] = (),
        total_years: float = 0.0,
        kind: ResumeKind = ResumeKind.MASTER,
        name: str = "Master Resume",
        emphasise: Sequence[str] = (),
        headline: str | None = None,
        include_evidence_ids: set[int] | None = None,
    ) -> Resume:
        """Assemble a resume from evidence. `include_evidence_ids` narrows the
        set for a track- or job-specific variant."""
        pool = [e for e in evidence if include_evidence_ids is None or e.id in include_evidence_ids]
        resume = Resume(name=name, kind=kind)

        summary = ResumeSection(name="Professional Summary", kind="summary")
        summary.entries.append(
            ResumeEntry(blocks=[self._summary_block(contact, pool, total_years, headline, emphasise)])
        )
        resume.sections.append(summary)

        skills_section = ResumeSection(name="Skills", kind="skills")
        skill_blocks = self._skill_lines(pool, emphasise)
        if skill_blocks:
            skills_section.entries.append(ResumeEntry(blocks=skill_blocks))
            resume.sections.append(skills_section)

        experience = ResumeSection(name="Professional Experience", kind="experience")
        for employer, title, start, end, items in self._employment_groups(pool):
            bullets = [i for i in items if i.kind in {"responsibility", "achievement", "project"}]
            bullets.sort(key=lambda i: (-i.strength, i.id))
            entry = ResumeEntry(
                heading=employer,
                subheading=title or None,
                meta=self._date_range(start, end),
                blocks=[
                    ResumeBlock(
                        text=i.text,
                        evidence_ids=[i.id],
                        skills=sorted(set(self.scanner.scan(i.text)) | {s for s in (i.skills or ()) if s}),
                    )
                    for i in bullets[:MAX_BULLETS_PER_ROLE]
                ],
            )
            if entry.blocks:
                experience.entries.append(entry)
        if experience.entries:
            resume.sections.append(experience)

        if certifications:
            section = ResumeSection(name="Certifications", kind="certifications")
            section.entries.append(
                ResumeEntry(
                    blocks=[
                        ResumeBlock(
                            text=", ".join(
                                c["name"] + (f" ({c['issuer']})" if c.get("issuer") else "")
                                for c in certifications
                            ),
                            evidence_ids=[],
                        )
                    ]
                )
            )
            resume.sections.append(section)

        if education:
            section = ResumeSection(name="Education", kind="education")
            section.entries.append(
                ResumeEntry(
                    blocks=[
                        ResumeBlock(
                            text=f"{e['degree']}"
                            + (f", {e['field']}" if e.get("field") else "")
                            + f" - {e['institution']}"
                            + (f" ({e['completed_on']})" if e.get("completed_on") else ""),
                            evidence_ids=[],
                        )
                        for e in education
                    ]
                )
            )
            resume.sections.append(section)

        return resume

    # -- tailoring ----------------------------------------------------------
    def _relevance(self, item: EvidenceRecord, wanted: set[str]) -> float:
        found = set(self.scanner.scan(item.text)) | {s for s in (item.skills or ()) if s}
        if not wanted:
            return item.strength
        overlap = len(found & wanted)
        return overlap * 2.0 + item.strength + (0.5 if item.verified else 0.0)

    def tailor(
        self,
        contact: ContactInfo,
        evidence: Sequence[EvidenceRecord],
        classification: ClassificationResult,
        match: MatchResult,
        job_title: str,
        company: str,
        certifications: Sequence[dict[str, Any]] = (),
        education: Sequence[dict[str, Any]] = (),
        total_years: float = 0.0,
        track_id: int | None = None,
        job_id: int | None = None,
        use_ai: bool = True,
    ) -> Resume:
        """Produce a job-specific resume.

        Selection is driven by *claimable* matches only. A RELATED (merely
        transferable) match never promotes an evidence item into the tailored
        resume as if it proved the requirement -- it is reported to the user as
        a gap instead.
        """
        claimable = {
            m.via_skill_id for m in match.matches if m.claimable and m.via_skill_id
        }
        wanted = claimable | {
            m.requirement_skill_id for m in match.matches if m.claimable and m.requirement_skill_id
        }
        wanted.discard(None)  # type: ignore[arg-type]

        ranked = sorted(evidence, key=lambda e: -self._relevance(e, wanted))  # type: ignore[arg-type]
        keep = {e.id for e in ranked if self._relevance(e, wanted) > 0.0}  # type: ignore[arg-type]

        emphasise = [s for s in classification.all_skill_ids if s in wanted]
        resume = self.build(
            contact=contact,
            evidence=ranked,
            certifications=certifications,
            education=education,
            total_years=total_years,
            kind=ResumeKind.TAILORED,
            name=f"{job_title} - {company}",
            emphasise=emphasise,
            headline=self._honest_headline(job_title, evidence),
            include_evidence_ids=keep or None,
        )
        resume.track_id = track_id
        resume.job_id = job_id

        resume.notes.append(
            f"Assembled from {len(resume.cited_evidence_ids())} evidence items; "
            f"{len(emphasise)} job skills evidenced directly."
        )
        if match.gaps:
            resume.notes.append(
                "Gaps left unclaimed (required by the posting, unsupported by evidence): "
                + ", ".join(match.gaps)
            )
        if match.transferable:
            resume.notes.append(
                "Adjacent experience NOT presented as direct: " + ", ".join(match.transferable)
            )

        if use_ai and getattr(self.provider, "available", False):
            self._ai_rewrite(resume, classification, evidence, job_title, company)
        return resume

    @staticmethod
    def _honest_headline(job_title: str, evidence: Sequence[EvidenceRecord]) -> str | None:
        """Use the posting's title only when the candidate has actually held it.

        Otherwise keep the real most recent title: a headline is a claim.
        """
        held = {normalize(e.role_title) for e in evidence if e.role_title}
        if normalize(job_title) in held:
            return job_title
        for title in held:
            if title and title in normalize(job_title):
                return next(e.role_title for e in evidence if normalize(e.role_title) == title)
        titles = [e.role_title for e in evidence if e.role_title]
        return titles[0] if titles else None

    def _ai_rewrite(
        self,
        resume: Resume,
        classification: ClassificationResult,
        evidence: Sequence[EvidenceRecord],
        job_title: str,
        company: str,
    ) -> None:
        cited = resume.cited_evidence_ids()
        by_id = {e.id: e for e in evidence if e.id in cited}
        if not by_id:
            return

        evidence_block = "\n".join(
            f"[{e.id}] ({e.kind}) employer={e.employer or 'n/a'} title={e.role_title or 'n/a'} "
            f"tech={', '.join(e.technologies) or 'n/a'} metrics={e.metrics or 'n/a'}\n    {e.text}"
            for e in by_id.values()
        )
        prompt = (
            f"TARGET ROLE: {job_title} at {company}\n"
            f"TARGET DOMAIN: {classification.domain_label} / {classification.function}\n"
            f"KEY POSTING TERMS: {', '.join(classification.ats_keywords[:20])}\n\n"
            f"VERIFIED EVIDENCE (the only facts you may use):\n{evidence_block}\n\n"
            "Rewrite each evidence item as one resume bullet, citing its evidence_id. "
            "Also produce a professional summary assembled only from these facts."
        )
        out = self.provider.structured(system=TAILOR_SYSTEM, prompt=prompt, schema=TailoringOut)
        if not out:
            return

        rewrites = {b.evidence_id: b.text for b in out.bullets if b.text.strip()}
        for section in resume.sections:
            if section.kind == "summary" and out.summary.strip():
                block = section.entries[0].blocks[0]
                block.original_text = block.text
                block.text = out.summary.strip()
                block.rewritten = True
            if section.kind != "experience":
                continue
            for entry in section.entries:
                for block in entry.blocks:
                    # Only rewrite a block that cites exactly one evidence item,
                    # so a rewrite can always be checked against its source.
                    if len(block.evidence_ids) == 1 and block.evidence_ids[0] in rewrites:
                        block.original_text = block.text
                        block.text = rewrites[block.evidence_ids[0]]
                        block.rewritten = True
        resume.notes.extend(out.notes)


def render_text(resume: Resume, contact: ContactInfo) -> str:
    """Plain-text render -- the format ATS parsers handle most reliably."""
    lines: list[str] = [contact.full_name, contact.email]
    if contact.phone:
        lines.append(contact.phone)
    if contact.location:
        lines.append(contact.location)
    for label, url in (contact.links or {}).items():
        lines.append(f"{label}: {url}")
    lines.append("")

    for section in resume.sections:
        lines.append(section.name.upper())
        for entry in section.entries:
            header_bits = [b for b in (entry.heading, entry.subheading) if b]
            if header_bits:
                header = " - ".join(header_bits)
                if entry.meta:
                    header = f"{header} ({entry.meta})"
                lines.append(header)
            for block in entry.blocks:
                prefix = "- " if section.kind in {"experience", "skills"} else ""
                lines.append(f"{prefix}{block.text}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
