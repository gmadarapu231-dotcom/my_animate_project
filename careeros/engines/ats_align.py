"""Making a tailored résumé read in the posting's own vocabulary.

ATS screening is substring matching. Two résumés describing identical
experience score differently because one wrote "microsegmentation" and the
posting wrote "micro-segmentation". Closing that gap is legitimate and it is
what this module does.

It is also where keyword stuffing lives, so the rule is narrow and absolute:

    **A keyword is only ever added when the user's own evidence already
    supports the capability it names. The claim never changes -- only the
    words used for it.**

Which gives four verdicts per posting keyword:

    PRESENT       already in the résumé verbatim. Nothing to do.
    ALIGNED       evidence supports it, worded differently. The posting's
                  wording is added to the skills index, citing the evidence
                  that justifies it.
    UNSUPPORTED   no evidence supports it. Never inserted. Reported as a gap,
                  because the honest answer to "you are missing this keyword"
                  is "get that experience", not "type the word".
    FRAGMENT      an artifact of our own n-gram extraction ("asa and cisco",
                  "deep hands-on bgp"). No real ATS matches on these either,
                  so they are excluded from scoring and never inserted --
                  putting them on a résumé would read as gibberish to the
                  human who opens it after the screen.
    NOT_A_CLAIM   not a capability at all -- a degree requirement, visa
                  language, boilerplate. Excluded from scoring and from the
                  résumé.

Alignment runs *before* the factuality gate, deliberately. Anything it adds is
re-derived and checked against evidence like any other claim, so a mistake here
fails the résumé rather than shipping.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Sequence

from careeros.engines.resume import EvidenceRecord, Resume, ResumeBlock, ResumeEntry, ResumeSection
from careeros.engines.skills import SkillScanner, default_scanner
from careeros.engines.textutil import content_tokens, normalize


class KeywordVerdict(StrEnum):
    PRESENT = "present"
    ALIGNED = "aligned"
    UNSUPPORTED = "unsupported"
    FRAGMENT = "fragment"
    NOT_A_CLAIM = "not_a_claim"

    @property
    def counts_for_ats(self) -> bool:
        """Only terms a real ATS would match on are scored against a résumé."""
        return self not in (KeywordVerdict.NOT_A_CLAIM, KeywordVerdict.FRAGMENT)


#: Never inserted, whatever the posting says. Education and work authorization
#: are driven by the user's real records, not by matching a phrase; clearance
#: language is on the factuality checker's forbidden list.
_NEVER = re.compile(
    r"(?i)\b("
    r"bachelor|master|mba|phd|doctorate|b\.?s\.?|m\.?s\.?|b\.?tech|m\.?tech|degree|diploma"
    r"|clearance|top secret|ts/sci|public trust"
    r"|h-?1b|green card|citizen|visa|sponsor|sponsorship|work authoriz|opt\b|ead\b"
    r"|equal opportunity|eeo|veteran|disabilit"
    r")\b"
)

#: A keyword made only of these is a requirement's grammar, not a capability.
_BOILERPLATE = frozenset(
    """
    ability able about across all and any are area as at background be been
    both bonus but by can candidate candidates commitment communication
    culture deep degree demonstrated desirable detail domain either
    environment equivalent excellent experience experienced fast field focus
    for from good great growth hands has have highly ideal in including
    industry is it its least level like minimum must new nice of on one or
    other our paced plus preferred proven related relevant required
    requirement requirements role roles similar skills solid space strong
    team teams technical the their this those to top track understanding up
    familiarity fluency knowledge exposure expertise proficiency
    player contributor professional individual self starter mindset attitude
    thinker communicator collaborator
    us using want we well what what's when where whether which who will with
    within work working would year years you your
    competitive compensation benefits salary opportunity opportunities
    exceptional outstanding talented motivated passionate dynamic
    company companies organisation organization business office onsite
    remote hybrid client clients customer customers stakeholder stakeholders
    please apply application applicants
    """.split()
)


#: Tokens that only ever join two terms. A keyword containing one mid-phrase is
#: an n-gram that ran across a boundary, not a term anybody screens for.
_CONNECTIVES = frozenset(
    "and or of the a an with without for in on to at by from as vs versus plus "
    "including include includes across using use used".split()
)

#: Modifiers that make a keyword a sentence fragment when they lead or trail it.
_EDGE_FILLERS = frozenset(
    "own deep strong solid proven excellent good great highly very significant "
    "extensive hands hands-on demonstrable demonstrated".split()
)

#: More than this and it is a clause, not a term.
_MAX_TERM_TOKENS = 4


def is_presentable(keyword: str) -> bool:
    """Would a human reading this on a résumé see a real term?

    That question has to be asked, because the résumé passes the ATS and then
    lands in front of a person. "cisco asa" passes both. "asa and cisco" passes
    the first and embarrasses the candidate at the second.
    """
    tokens = [t for t in content_tokens(keyword) if t]
    raw = normalize(keyword).split()
    if not tokens or len(raw) > _MAX_TERM_TOKENS:
        return False
    if any(token in _CONNECTIVES for token in raw):
        return False
    if any(len(token) == 1 and token.isalpha() for token in raw):
        # A lone letter is a possessive or initial the extractor split off:
        # "bank s zero trust" came from "the bank's zero trust programme".
        return False
    if raw[0] in _EDGE_FILLERS or raw[-1] in _EDGE_FILLERS:
        return False
    return True


@dataclass
class KeywordDecision:
    keyword: str
    verdict: KeywordVerdict
    rank: int = 0
    skill_id: str | None = None
    evidence_ids: list[int] = field(default_factory=list)
    surface_in_resume: str | None = None
    #: The wording actually added to the résumé. Not always the raw keyword:
    #: several extracted n-grams can name one capability, and the cleanest
    #: surface the posting used is the one worth printing.
    presentation: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["verdict"] = self.verdict.value
        return out


@dataclass
class AlignmentPlan:
    decisions: list[KeywordDecision] = field(default_factory=list)
    #: Keywords whose wording was added to the skills index.
    added: list[str] = field(default_factory=list)
    #: Keywords with no supporting evidence. These are the real gaps.
    gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_verdict(self, verdict: KeywordVerdict) -> list[KeywordDecision]:
        return [d for d in self.decisions if d.verdict is verdict]

    @property
    def scored(self) -> list[KeywordDecision]:
        return [d for d in self.decisions if d.verdict.counts_for_ats]

    @property
    def coverage(self) -> float:
        """Share of real keywords the résumé now carries, 0-100."""
        scored = self.scored
        if not scored:
            return 100.0
        covered = sum(
            1 for d in scored if d.verdict in (KeywordVerdict.PRESENT, KeywordVerdict.ALIGNED)
        )
        return round(100.0 * covered / len(scored), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "counts": {
                verdict.value: len(self.by_verdict(verdict)) for verdict in KeywordVerdict
            },
            "added": self.added,
            "gaps": self.gaps,
            "notes": self.notes,
            "decisions": [d.to_dict() for d in self.decisions],
        }


def _is_never(keyword: str) -> bool:
    return bool(_NEVER.search(keyword))


def _is_boilerplate(keyword: str) -> bool:
    # content_tokens keeps trailing punctuation, so "candidates." would miss
    # the lookup and a plainly boilerplate phrase would be reported as a gap.
    tokens = [re.sub(r"[^a-z0-9+#.-]", "", t).strip(".-") for t in content_tokens(keyword)]
    tokens = [t for t in tokens if t]
    if not tokens:
        return True
    return all(token in _BOILERPLATE for token in tokens)


class AtsAligner:
    """Decides, per posting keyword, whether the résumé may carry its wording."""

    def __init__(self, scanner: SkillScanner | None = None) -> None:
        self.scanner = scanner or default_scanner()

    # -- what the evidence supports ----------------------------------------
    def _evidence_index(
        self, evidence: Sequence[EvidenceRecord]
    ) -> tuple[dict[str, list[int]], str, dict[str, list[int]]]:
        """(skill_id -> evidence ids, all evidence text, token -> evidence ids)."""
        by_skill: dict[str, list[int]] = {}
        by_token: dict[str, list[int]] = {}
        parts: list[str] = []

        for item in evidence:
            blob = " ".join(
                filter(
                    None,
                    [
                        item.text or "",
                        " ".join(item.technologies or ()),
                        " ".join(item.skills or ()),
                        item.role_title or "",
                        item.project or "",
                    ],
                )
            )
            parts.append(blob)

            # Only what the scanner can re-derive from the *words* counts.
            # An evidence row's declared skill ids are not enough: the
            # factuality checker re-derives entities from text, so trusting a
            # declared id here would let alignment add a term the gate then
            # rejects -- which is exactly what happened before this line said
            # `scan(blob)` alone.
            for skill_id in self.scanner.scan(blob):
                by_skill.setdefault(skill_id, []).append(item.id)
            for token in content_tokens(blob):
                by_token.setdefault(token, []).append(item.id)

        return by_skill, normalize(" ".join(parts)), by_token

    def decide(
        self,
        keyword: str,
        rank: int,
        *,
        resume_norm: str,
        evidence_skills: dict[str, list[int]],
        evidence_norm: str,
        evidence_tokens: dict[str, list[int]],
    ) -> KeywordDecision:
        cleaned = " ".join((keyword or "").split())
        if not cleaned:
            return KeywordDecision(keyword, KeywordVerdict.NOT_A_CLAIM, rank, note="empty")

        if _is_never(cleaned):
            return KeywordDecision(
                cleaned,
                KeywordVerdict.NOT_A_CLAIM,
                rank,
                note=(
                    "Education, work authorization and clearance are stated from your own "
                    "records, never by matching a phrase."
                ),
            )
        if _is_boilerplate(cleaned):
            return KeywordDecision(
                cleaned, KeywordVerdict.NOT_A_CLAIM, rank, note="requirement grammar, not a capability"
            )

        if not is_presentable(cleaned):
            return KeywordDecision(
                cleaned,
                KeywordVerdict.FRAGMENT,
                rank,
                note=(
                    "an n-gram that ran across a phrase boundary. Not scored, and never "
                    "added -- it would read as gibberish to whoever opens the résumé."
                ),
            )

        normalised = normalize(cleaned)
        if normalised and normalised in resume_norm:
            return KeywordDecision(
                cleaned, KeywordVerdict.PRESENT, rank, surface_in_resume=cleaned,
                note="already in the résumé as the posting words it",
            )

        # Every skill the *printed words* derive has to be supported, not just
        # the one the phrase resolves to. "ospf troubleshooting" resolves to
        # network_monitoring but its words also yield routing_switching, and
        # the factuality checker re-derives both from the line -- so checking
        # only the resolved id would add a term the gate then rejects.
        skill_id = self.scanner.resolve(cleaned)
        derived = set(self.scanner.scan(cleaned))
        if skill_id:
            derived.add(skill_id)

        if derived:
            unsupported = sorted(derived - set(evidence_skills))
            if not unsupported:
                return KeywordDecision(
                    cleaned,
                    KeywordVerdict.ALIGNED,
                    rank,
                    skill_id=skill_id,
                    evidence_ids=sorted(
                        {i for s in derived for i in evidence_skills.get(s, ())}
                    ),
                    note=(
                        "your evidence supports "
                        + ", ".join(sorted(derived))
                        + "; adding the posting's wording"
                    ),
                )
            return KeywordDecision(
                cleaned,
                KeywordVerdict.UNSUPPORTED,
                rank,
                skill_id=skill_id,
                note=(
                    "these words would claim "
                    + ", ".join(unsupported)
                    + ", which your evidence does not support. Left off rather than typed in — "
                    "the fix is the experience, not the word."
                ),
            )

        # A multi-word phrase the graph does not know can still be supported
        # verbatim by the evidence -- "vlan design" appearing in a bullet.
        if normalised and normalised in evidence_norm:
            tokens = [t for t in content_tokens(cleaned) if t in evidence_tokens]
            ids = sorted({i for t in tokens for i in evidence_tokens[t]})
            return KeywordDecision(
                cleaned,
                KeywordVerdict.ALIGNED,
                rank,
                evidence_ids=ids,
                note="your evidence uses this phrase, the résumé draft did not",
            )

        return KeywordDecision(
            cleaned,
            KeywordVerdict.UNSUPPORTED,
            rank,
            skill_id=skill_id,
            note=(
                "no evidence supports this. It is left off rather than typed in — "
                "the fix is the experience, not the word."
            ),
        )

    # -- the pass -----------------------------------------------------------
    def align(
        self,
        resume: Resume,
        evidence: Sequence[EvidenceRecord],
        keywords: Sequence[str],
        *,
        max_added: int = 14,
        apply: bool = True,
    ) -> AlignmentPlan:
        """Decide every keyword and, when `apply`, add the supported wording.

        Only the skills index gains vocabulary. Bullets are left alone on
        purpose: a bullet is a specific claim about something the user did, and
        rewording it to match a posting risks changing what it says. A skills
        line is an index of capabilities the evidence supports, so stating them
        in the employer's words is the same claim in their vocabulary.
        """
        plan = AlignmentPlan()
        cited = resume.cited_evidence_ids()
        pool = [e for e in evidence if not cited or e.id in cited] or list(evidence)

        evidence_skills, evidence_norm, evidence_tokens = self._evidence_index(pool)
        resume_norm = normalize(_resume_text(resume))

        for rank, keyword in enumerate(keywords):
            plan.decisions.append(
                self.decide(
                    keyword,
                    rank,
                    resume_norm=resume_norm,
                    evidence_skills=evidence_skills,
                    evidence_norm=evidence_norm,
                    evidence_tokens=evidence_tokens,
                )
            )

        # Collapsing runs either way, so a report and a résumé agree on what
        # alignment means.
        alignable = _one_per_capability(plan.by_verdict(KeywordVerdict.ALIGNED))[:max_added]
        plan.added = [d.presentation or d.keyword for d in alignable]
        plan.gaps = [d.keyword for d in plan.by_verdict(KeywordVerdict.UNSUPPORTED)]

        if apply and alignable:
            _add_to_skills(resume, alignable)
            plan.notes.append(
                f"Added {len(alignable)} term(s) from the posting's own wording, each supported by "
                f"evidence: {', '.join(plan.added)}."
            )
        elif alignable:
            plan.notes.append(
                f"{len(alignable)} term(s) would be added from the posting's own wording: "
                f"{', '.join(plan.added)}."
            )

        if plan.gaps:
            plan.notes.append(
                f"{len(plan.gaps)} posting term(s) left off because nothing in your evidence "
                f"supports them: {', '.join(plan.gaps[:8])}"
                + ("…" if len(plan.gaps) > 8 else "")
            )
        skipped = plan.by_verdict(KeywordVerdict.NOT_A_CLAIM)
        if skipped:
            plan.notes.append(
                f"{len(skipped)} posting term(s) are requirements rather than capabilities and are "
                "not scored against the résumé."
            )
        fragments = plan.by_verdict(KeywordVerdict.FRAGMENT)
        if fragments:
            plan.notes.append(
                f"{len(fragments)} extracted phrase(s) were n-gram artifacts, not real terms, and "
                "are excluded from both the résumé and the score."
            )
        return plan


def _one_per_capability(decisions: Sequence[KeywordDecision]) -> list[KeywordDecision]:
    """Collapse the n-grams that name the same capability into one clean term.

    A posting yields "palo alto", "administration palo alto" and "firewall
    administration palo" for a single skill. All three would match an ATS; only
    the first reads like something a person wrote. So the decisions are grouped
    by the capability they resolve to and the shortest surface wins, with the
    others recorded as covered by it rather than printed.
    """
    groups: dict[str, list[KeywordDecision]] = {}
    for decision in decisions:
        key = decision.skill_id or normalize(decision.keyword)
        groups.setdefault(key, []).append(decision)

    chosen: list[KeywordDecision] = []
    for key, members in groups.items():
        # Fewest words first, then shortest, so "palo alto" beats
        # "firewall administration palo".
        best = min(members, key=lambda d: (len(d.keyword.split()), len(d.keyword)))
        best.presentation = best.keyword
        best.evidence_ids = sorted({i for m in members for i in m.evidence_ids})
        if len(members) > 1:
            others = [m.keyword for m in members if m is not best]
            best.note += f" (also covers: {', '.join(others)})"
            for member in members:
                if member is not best:
                    member.presentation = best.keyword
        chosen.append(best)
    return chosen


_ALIGNED_HEADING = "Also stated as in the posting"


def _resume_text(resume: Resume) -> str:
    parts: list[str] = []
    for section in resume.sections:
        parts.append(section.name or "")
        for entry in section.entries:
            parts.extend(filter(None, [entry.heading, entry.subheading, entry.meta]))
            parts.extend(block.text or "" for block in entry.blocks)
    return "\n".join(parts)


def _add_to_skills(resume: Resume, decisions: Sequence[KeywordDecision]) -> None:
    """Append one line to the skills section, citing the evidence behind it.

    A separate line rather than editing the existing ones: the reader can see
    exactly which terms are the posting's vocabulary, and the factuality
    checker gets one claim to verify rather than several rewritten ones.
    """
    section = next((s for s in resume.sections if s.kind == "skills"), None)
    if section is None:
        section = ResumeSection(name="Skills", kind="skills")
        # After the summary, before experience -- where a reader expects it.
        insert_at = 1 if resume.sections else 0
        resume.sections.insert(insert_at, section)
    if not section.entries:
        section.entries.append(ResumeEntry(blocks=[]))

    terms = [d.presentation or d.keyword for d in decisions]
    evidence_ids = sorted({i for d in decisions for i in d.evidence_ids})
    skills = sorted({d.skill_id for d in decisions if d.skill_id})

    section.entries[-1].blocks.append(
        ResumeBlock(
            text=f"{_ALIGNED_HEADING}: {', '.join(terms)}",
            evidence_ids=evidence_ids,
            skills=skills,
        )
    )
