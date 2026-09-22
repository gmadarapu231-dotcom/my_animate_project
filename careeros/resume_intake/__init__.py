"""Résumé in, searches and evidence out.

    extract_text  -> plain text from .txt/.md/.docx/.pdf
    parse_resume  -> employers, bullets, skills, dates as *proposals*
    commit_parsed -> rows in the database, evidence always unverified
    verify        -> the human confirming facts, the one gate that matters
    terms_from_resume -> what to search for, derived from the résumé itself

`intake_file` runs the whole chain for a path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from careeros.db.models import User
from careeros.resume_intake.commit import (
    IntakeResult,
    apply_profile,
    commit_parsed,
    verify,
)
from careeros.resume_intake.extract import SUPPORTED, ExtractionError, extract_text, normalise
from careeros.resume_intake.parse import (
    EmployerProposal,
    EvidenceProposal,
    ParsedResume,
    parse_resume,
)

__all__ = [
    "EmployerProposal",
    "EvidenceProposal",
    "ExtractionError",
    "IntakeResult",
    "ParsedResume",
    "SUPPORTED",
    "apply_profile",
    "commit_parsed",
    "extract_text",
    "intake_file",
    "intake_text",
    "normalise",
    "parse_resume",
    "terms_from_resume",
    "verify",
]

#: A title is a better query than a skill: "Network Security Engineer" matches
#: postings, "zero_trust" matches noise. Skills are the fallback when the
#: résumé's titles are too generic or absent.
DEFAULT_TERM_LIMIT = 6


def terms_from_resume(parsed: ParsedResume, *, limit: int = DEFAULT_TERM_LIMIT) -> list[str]:
    """Search terms taken from the résumé, most recent role first.

    Nothing in here knows what field the résumé is in. The titles are whatever
    the person actually wrote, and the skill labels come from the same graph
    the classifier uses -- so a veterinary résumé produces veterinary searches
    without anyone adding that domain.
    """
    from careeros.config import taxonomy

    out: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        cleaned = " ".join((value or "").split())
        if not cleaned or cleaned.lower() in seen or len(cleaned) < 3:
            return
        seen.add(cleaned.lower())
        out.append(cleaned)

    add(parsed.current_title)
    for employer in parsed.employers:
        add(employer.title)
    for title in parsed.titles:
        add(title)

    if len(out) < limit and parsed.skills:
        tax = taxonomy()
        strongest = sorted(parsed.skills.items(), key=lambda kv: -kv[1])
        for skill_id, _score in strongest:
            node = tax.skills.get(skill_id)
            add(str(getattr(node, "label", None) or skill_id.replace("_", " ")))
            if len(out) >= limit:
                break

    return out[:limit]


def intake_text(
    session: Session,
    user: User,
    text: str,
    *,
    source: str = "résumé text",
    commit: bool = True,
    overwrite_profile: bool = False,
) -> dict[str, Any]:
    """Parse text and optionally store it. Returns a report, not rows."""
    parsed = parse_resume(text)
    report: dict[str, Any] = {
        "parsed": parsed.to_dict(),
        "search_terms": terms_from_resume(parsed),
        "committed": False,
    }
    if commit:
        result = commit_parsed(
            session, user, parsed, source=source, overwrite_profile=overwrite_profile
        )
        report["committed"] = True
        report["intake"] = result.to_dict()
    return report


def intake_file(
    session: Session,
    user: User,
    path: str | Path,
    *,
    commit: bool = True,
    overwrite_profile: bool = False,
) -> dict[str, Any]:
    file = Path(path)
    text = extract_text(file)
    report = intake_text(
        session,
        user,
        text,
        source=f"résumé: {file.name}",
        commit=commit,
        overwrite_profile=overwrite_profile,
    )
    report["file"] = {"name": file.name, "bytes": file.stat().st_size, "characters": len(text)}
    return report
