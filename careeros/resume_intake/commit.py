"""Turning résumé proposals into rows -- and not into verified facts.

The whole résumé layer rests on one invariant: a generated résumé may only
claim things that trace back to evidence the user has verified. A parser that
wrote verified evidence would break it silently, and the user would find out
when a recruiter asked about a bullet they never wrote.

So this module is deliberately asymmetric:

* Contact details, titles and years of experience are *descriptive* -- they go
  straight onto the profile, because nothing is claimed on their strength.
* Employers are created as containers, which assert nothing on their own.
* Evidence lands as `unverified`, always, whatever the parser's confidence.
  `verify()` is a separate call that only a human action reaches -- and the
  agent has no tool for it (`FORBIDDEN_CAPABILITIES`).

The résumé file itself is recorded as the `source` on every row, so a year
later it is clear where a fact came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.db.models import Certification, Employer, EvidenceItem, User
from careeros.enums import EvidenceKind, VerificationState
from careeros.resume_intake.parse import EvidenceProposal, ParsedResume

logger = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    employers_created: int = 0
    employers_matched: int = 0
    evidence_created: int = 0
    evidence_skipped: int = 0
    certifications_created: int = 0
    profile_fields_set: list[str] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "employers_created": self.employers_created,
            "employers_matched": self.employers_matched,
            "evidence_created": self.evidence_created,
            "evidence_skipped": self.evidence_skipped,
            "certifications_created": self.certifications_created,
            "profile_fields_set": self.profile_fields_set,
            "evidence_ids": self.evidence_ids,
            "notes": self.notes,
            "verification_state": VerificationState.UNVERIFIED.value,
            "next_step": (
                f"{self.evidence_created} item(s) are stored unverified. Confirm the ones "
                "that are accurate before any résumé can cite them."
            ),
        }


def _normalise_company(name: str) -> str:
    """For matching only. The stored name keeps the user's own spelling."""
    import re

    cleaned = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    cleaned = re.sub(
        r"\b(inc|llc|ltd|limited|corp|corporation|plc|gmbh|pvt|private|co|company|"
        r"technologies|technology|solutions|systems|group|holdings|labs)\b",
        " ",
        cleaned,
    )
    return " ".join(cleaned.split())


def _find_employer(session: Session, user_id: int, name: str) -> Employer | None:
    target = _normalise_company(name)
    if not target:
        return None
    rows = session.scalars(select(Employer).where(Employer.user_id == user_id)).all()
    for row in rows:
        if _normalise_company(row.name) == target:
            return row
    return None


def _fingerprint(text: str) -> str:
    """Loose identity for a bullet, so a re-upload does not duplicate it."""
    import re

    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return " ".join(words[:18])


def apply_profile(user: User, parsed: ParsedResume, *, overwrite: bool = False) -> list[str]:
    """Copy the descriptive fields onto the profile.

    `overwrite=False` fills only what is empty, which is what you want on a
    second upload: the user may have corrected something by hand since.
    """
    changed: list[str] = []

    def put(attribute: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        current = getattr(user, attribute, None)
        if current in (None, "", [], {}, 0, 0.0) or overwrite:
            setattr(user, attribute, value)
            changed.append(attribute)

    put("full_name", parsed.full_name)
    put("email", parsed.email)
    put("phone", parsed.phone)
    put("current_title", parsed.current_title)
    put("total_experience_years", parsed.total_experience_years)
    if parsed.links:
        merged = {**(user.links or {}), **parsed.links}
        if merged != (user.links or {}):
            user.links = merged
            changed.append("links")
    return changed


def commit_parsed(
    session: Session,
    user: User,
    parsed: ParsedResume,
    *,
    source: str = "résumé upload",
    overwrite_profile: bool = False,
    min_confidence: float = 0.0,
) -> IntakeResult:
    """Store the proposals. Evidence is unverified; nothing here verifies it."""
    result = IntakeResult()
    result.profile_fields_set = apply_profile(user, parsed, overwrite=overwrite_profile)
    session.flush()

    # -- employers ---------------------------------------------------------
    employer_rows: dict[str, Employer] = {}
    for proposal in parsed.employers:
        existing = _find_employer(session, user.id, proposal.name)
        if existing is None:
            existing = Employer(
                user_id=user.id,
                name=proposal.name,
                location=proposal.location,
                start_date=proposal.start_date,
                end_date=proposal.end_date,
            )
            session.add(existing)
            result.employers_created += 1
        else:
            result.employers_matched += 1
            # Fill gaps only; the user's own edits win.
            existing.location = existing.location or proposal.location
            existing.start_date = existing.start_date or proposal.start_date
            existing.end_date = existing.end_date or proposal.end_date
        employer_rows[proposal.name] = existing
    session.flush()

    # -- evidence ----------------------------------------------------------
    known = {
        _fingerprint(row.text)
        for row in session.scalars(
            select(EvidenceItem).where(EvidenceItem.user_id == user.id)
        ).all()
    }

    for proposal in parsed.evidence:
        if proposal.confidence < min_confidence:
            result.evidence_skipped += 1
            continue
        key = _fingerprint(proposal.text)
        if key in known:
            result.evidence_skipped += 1
            continue
        known.add(key)

        employer = employer_rows.get(proposal.employer_name or "")
        item = EvidenceItem(
            user_id=user.id,
            employer_id=employer.id if employer else None,
            kind=proposal.kind,
            text=proposal.text,
            role_title=proposal.role_title,
            technologies=proposal.technologies,
            skills=proposal.skills,
            domains=[],
            start_date=proposal.start_date,
            end_date=proposal.end_date,
            metrics=proposal.metrics,
            # The parser's confidence is recorded as strength, but the
            # verification state is not negotiable.
            verification=VerificationState.UNVERIFIED.value,
            source=source,
            strength=round(min(max(proposal.confidence, 0.1), 0.9), 2),
            tags=["from-resume"],
        )
        session.add(item)
        result.evidence_created += 1
    session.flush()
    result.evidence_ids = [
        row.id
        for row in session.scalars(
            select(EvidenceItem).where(
                EvidenceItem.user_id == user.id, EvidenceItem.source == source
            )
        ).all()
    ]

    # -- certifications ----------------------------------------------------
    existing_certs = {
        (row.name or "").strip().lower()
        for row in session.scalars(
            select(Certification).where(Certification.user_id == user.id)
        ).all()
    }
    for line in parsed.certifications:
        name, issuer, issued = _split_certification(line)
        if not name or name.lower() in existing_certs:
            continue
        existing_certs.add(name.lower())
        session.add(
            Certification(user_id=user.id, name=name, issuer=issuer, issued_on=issued)
        )
        result.certifications_created += 1
    session.flush()

    if parsed.warnings:
        result.notes.extend(parsed.warnings)
    return result


def _split_certification(line: str) -> tuple[str, str | None, date | None]:
    """"CCNP Enterprise — Cisco, 2021" -> ("CCNP Enterprise", "Cisco", 2021-01-01)."""
    import re

    year = None
    match = re.search(r"\b(19|20)\d{2}\b", line)
    if match:
        year = date(int(match.group(0)), 1, 1)
        line = line[: match.start()] + line[match.end() :]

    parts = [p.strip(" ,–—-|·•") for p in re.split(r"\s+[–—|·•]\s+|,\s+", line) if p.strip(" ,–—-|·•")]
    if not parts:
        return "", None, year
    name = parts[0]
    issuer = parts[1] if len(parts) > 1 and len(parts[1]) < 48 else None
    return name[:200], issuer, year


def verify(
    session: Session, user: User, evidence_ids: Iterable[int], *, verified: bool = True
) -> int:
    """Mark evidence verified. Only a human action reaches this.

    There is no agent tool that calls it -- `verify_evidence` is in
    `FORBIDDEN_CAPABILITIES` -- and that is the point: verification is the user
    asserting a fact is true, which is not something a model can do on their
    behalf.
    """
    wanted = {int(i) for i in evidence_ids}
    if not wanted:
        return 0
    rows = session.scalars(
        select(EvidenceItem).where(
            EvidenceItem.user_id == user.id, EvidenceItem.id.in_(wanted)
        )
    ).all()
    state = VerificationState.VERIFIED if verified else VerificationState.UNVERIFIED
    for row in rows:
        row.verification = state.value
    session.flush()
    return len(rows)
