"""The application packet: everything one submission needs, in one object.

Assembling this is separate from sending it, because the same packet serves all
four channels. An API submission posts its fields, an email attaches its files,
and an assisted hand-off shows the human exactly what to paste -- from one
source, so the three can never drift apart.

Screening answers come only from the profile. There is no model call here and
no inference: a question the profile cannot answer is returned as
`unanswered`, which is what stops an automatic submission from inventing a
salary expectation or a notice period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from careeros.apply.channels import Channel


@dataclass
class Answer:
    question: str
    value: str | None
    source: str
    confident: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "value": self.value,
            "source": self.source,
            "confident": self.confident,
        }


@dataclass
class Packet:
    job_id: int
    job_title: str
    company: str
    channel: Channel

    full_name: str = ""
    email: str = ""
    phone: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    location: str | None = None

    resume_id: int | None = None
    resume_text: str = ""
    resume_filename: str = "resume.txt"
    cover_letter: str | None = None

    answers: list[Answer] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    disclaimers: list[str] = field(default_factory=list)

    #: How this résumé stands against this posting's own keywords. Recorded on
    #: the packet so an application can be reviewed after the fact, and so a
    #: low-coverage submission can be spotted without re-deriving anything.
    ats_overall: float | None = None
    ats_keyword_match: float | None = None
    ats_matched: list[str] = field(default_factory=list)
    ats_missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Enough to submit without inventing anything."""
        return bool(self.full_name and self.email and self.resume_text and not self.unanswered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_title": self.job_title,
            "company": self.company,
            "channel": self.channel.to_dict(),
            "applicant": {
                "full_name": self.full_name,
                "email": self.email,
                "phone": self.phone,
                "links": self.links,
                "location": self.location,
            },
            "resume_id": self.resume_id,
            "resume_filename": self.resume_filename,
            "resume_characters": len(self.resume_text),
            "cover_letter_characters": len(self.cover_letter or ""),
            "answers": [a.to_dict() for a in self.answers],
            "unanswered": self.unanswered,
            "disclaimers": self.disclaimers,
            "complete": self.complete,
            "ats": {
                "overall": self.ats_overall,
                "keyword_match": self.ats_keyword_match,
                "matched": self.ats_matched,
                "missing": self.ats_missing,
            },
        }


#: Questions nearly every form asks, and where the answer legitimately comes
#: from. Anything not on this list is left to the human.
_PROFILE_ANSWERS: tuple[tuple[str, str], ...] = (
    ("Are you legally authorised to work in this country?", "work_authorization"),
    ("Will you now or in the future require sponsorship?", "needs_sponsorship"),
    ("Years of relevant experience", "total_experience_years"),
    ("Current job title", "current_title"),
    ("Preferred work arrangement", "remote_preference"),
    ("Are you open to relocation?", "open_to_relocation"),
)


def _slug(text: str, limit: int = 48) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return (cleaned[:limit] or "application").lower()


def _authorisation_answers(user: Any, country: str | None) -> list[Answer]:
    """Answered from the work-authorization rows, never guessed.

    A country the user has no row for produces no answer at all -- it becomes
    an `unanswered` question, which blocks automatic submission. Saying "yes,
    authorised" on a form for a country the profile is silent about would be
    exactly the misrepresentation this system exists to avoid.
    """
    rows = getattr(user, "work_auth", None) or []
    target = (country or "").upper()
    row = next((r for r in rows if (getattr(r, "country_code", "") or "").upper() == target), None)
    if row is None:
        return []

    needs_now = bool(getattr(row, "needs_sponsorship", False))
    return [
        Answer(
            "Are you legally authorised to work in this country?",
            "Yes" if not needs_now else "Requires sponsorship",
            f"work authorization: {getattr(row, 'status_id', 'unknown')} in {target}",
        ),
        Answer(
            "Will you now or in the future require sponsorship?",
            "Yes" if needs_now else "No",
            f"work authorization: {getattr(row, 'status_id', 'unknown')} in {target}",
        ),
    ]


def build(
    *,
    user: Any,
    job: Any,
    channel: Channel,
    resume: Any | None,
    cover_letter: Any | None = None,
    eligibility: Any | None = None,
    ats: Any | None = None,
    extra_answers: dict[str, str] | None = None,
) -> Packet:
    """Everything the submission needs, and a list of what is still missing."""
    packet = Packet(
        job_id=getattr(job, "id", 0),
        job_title=getattr(job, "title", "") or "",
        company=getattr(job, "company", "") or "",
        channel=channel,
        full_name=getattr(user, "full_name", "") or "",
        email=getattr(user, "email", "") or "",
        phone=getattr(user, "phone", None),
        links=dict(getattr(user, "links", None) or {}),
        location=", ".join(
            p for p in (getattr(user, "home_city", None), getattr(user, "home_region", None)) if p
        )
        or None,
        resume_id=getattr(resume, "id", None),
        resume_text=getattr(resume, "rendered_text", "") or "",
        cover_letter=getattr(cover_letter, "body", None),
    )
    packet.resume_filename = (
        f"{_slug(packet.full_name)}-{_slug(packet.job_title)}.txt"
        if packet.full_name
        else "resume.txt"
    )

    # Job.country_code, not `country` -- the normalised ISO code the country
    # pack keys off.
    answers = list(_authorisation_answers(user, getattr(job, "country_code", None)))
    answered = {a.question for a in answers}

    for question, attribute in _PROFILE_ANSWERS:
        if question in answered:
            continue
        value = getattr(user, attribute, None)
        if attribute == "total_experience_years" and value:
            answers.append(Answer(question, f"{float(value):g}", "profile"))
        elif attribute == "open_to_relocation":
            answers.append(Answer(question, "Yes" if value else "No", "profile"))
        elif isinstance(value, str) and value:
            answers.append(Answer(question, value, "profile"))
        elif attribute in ("work_authorization", "needs_sponsorship"):
            packet.unanswered.append(question)
        # Everything else simply goes unanswered rather than being invented.

    for question, value in (extra_answers or {}).items():
        answers.append(Answer(question, value, "you"))

    packet.answers = answers

    if not packet.full_name:
        packet.unanswered.append("Full name is missing from the profile")
    if not packet.email:
        packet.unanswered.append("Email is missing from the profile")
    if not packet.resume_text:
        packet.unanswered.append("No tailored résumé has been rendered for this job")

    if getattr(eligibility, "verdict", None):
        packet.disclaimers.append(
            "Work-authorization reading is an AI assessment — verify with the employer."
        )
    if getattr(resume, "is_final", False) is False and resume is not None:
        packet.disclaimers.append("This résumé has not passed the factuality check.")

    if ats is not None:
        packet.ats_overall = getattr(ats, "overall", None)
        packet.ats_keyword_match = getattr(ats, "keyword_match", None)
        packet.ats_matched = list(getattr(ats, "matched_keywords", None) or [])
        packet.ats_missing = list(getattr(ats, "missing_keywords", None) or [])

    return packet
