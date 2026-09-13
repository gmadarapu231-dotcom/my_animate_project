"""Classify job-related email and extract the facts that drive the pipeline."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from careeros.ai.provider import LLMProvider, get_provider
from careeros.ai.schemas import EmailExtractionOut
from careeros.engines.textutil import normalize
from careeros.enums import ApplicationStatus, EmailCategory

#: Ordered: the first category whose signature fires wins, so a rejection that
#: also says "we will keep your resume on file" is still a rejection.
_CATEGORY_SIGNATURES: list[tuple[EmailCategory, tuple[str, ...]]] = [
    (EmailCategory.OFFER, ("offer letter", "we are pleased to offer", "formal offer", "offer of employment")),
    (EmailCategory.REJECTION, (
        "unfortunately", "we regret to inform", "not moving forward", "decided to move forward with other",
        "will not be proceeding", "were not selected", "pursuing other candidates", "no longer under consideration",
    )),
    (EmailCategory.ASSESSMENT, (
        "assessment", "coding challenge", "take-home", "hackerrank", "codility", "online test",
        "technical screen invitation", "complete the exercise",
    )),
    (EmailCategory.INTERVIEW, (
        "interview", "schedule a call", "schedule a time", "availability for a", "calendar invite",
        "meet the team", "onsite", "panel",
    )),
    (EmailCategory.APPLICATION_CONFIRMATION, (
        "we received your application", "thank you for applying", "application received",
        "your application has been submitted", "successfully applied",
    )),
    (EmailCategory.SPONSORSHIP, (
        "visa", "sponsorship", "h1b", "h-1b", "work authorization", "green card", "immigration",
    )),
    (EmailCategory.RECRUITER, (
        "came across your profile", "i am a recruiter", "opportunity that may interest",
        "reaching out about", "would you be open to", "exciting opportunity", "your background",
    )),
    (EmailCategory.FOLLOW_UP, ("following up", "checking in", "any update", "circling back")),
]

_STATUS_HINTS = {
    EmailCategory.APPLICATION_CONFIRMATION: ApplicationStatus.APPLICATION_RECEIVED,
    EmailCategory.RECRUITER: ApplicationStatus.RECRUITER_CONTACT,
    EmailCategory.INTERVIEW: ApplicationStatus.INTERVIEW,
    EmailCategory.ASSESSMENT: ApplicationStatus.ASSESSMENT,
    EmailCategory.OFFER: ApplicationStatus.OFFER,
    EmailCategory.REJECTION: ApplicationStatus.REJECTED,
}

_URL = re.compile(r"https?://[^\s<>\"')]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_DATETIME_PATTERNS = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?"),
    re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*(?:am|pm)?))?", re.I),
    re.compile(
        r"\b((?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2})"
        r"(?:[,\s]+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)))?",
        re.I,
    ),
]
_DEADLINE_CUE = re.compile(
    r"(?:due|deadline|complete (?:it |this )?by|expires?|submit by|within)\s+([^.\n]{3,60})", re.I
)
_POSITION_CUE = re.compile(
    r"(?:for the|regarding the|about the|role of|position of|opening for)\s+([A-Z][\w &/+-]{3,60}?)"
    r"\s*(?:role|position|opening|opportunity|\(|,|\.|$)"
)
_COMPANY_CUE = re.compile(r"\bat\s+([A-Z][\w&'-]*(?:\s+[A-Z][\w&'-]*){0,3})")


@dataclass
class EmailAnalysis:
    category: EmailCategory
    confidence: float = 0.0
    company: str | None = None
    position: str | None = None
    recruiter_name: str | None = None
    recruiter_email: str | None = None
    job_url: str | None = None
    interview_datetime: str | None = None
    assessment_deadline: str | None = None
    required_action: str | None = None
    status_hint: ApplicationStatus | None = None
    rejection_reason_explicit: str | None = None
    rejection_reason_quote: str | None = None
    possible_reasons: list[dict[str, Any]] = field(default_factory=list)
    method: str = "heuristic"

    @property
    def requires_action(self) -> bool:
        return self.category in {
            EmailCategory.INTERVIEW,
            EmailCategory.ASSESSMENT,
            EmailCategory.RECRUITER,
            EmailCategory.OFFER,
            EmailCategory.SPONSORSHIP,
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        d["status_hint"] = self.status_hint.value if self.status_hint else None
        d["requires_action"] = self.requires_action
        return d


SYSTEM_PROMPT = """You read one job-search email and extract structured facts.

Rules:
- Report only what the email states. Never infer a company, a date or a reason.
- `rejection_reason_explicit` is filled ONLY when the sender states a reason. \
If they do not, leave it null - a polite form rejection has no stated reason.
- `rejection_reason_quote` must be an exact substring of the email.
- Dates: return exactly what the email says, do not resolve relative dates."""


class EmailClassifier:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_provider()

    # -- heuristics ---------------------------------------------------------
    @staticmethod
    def _category(text_norm: str) -> tuple[EmailCategory, float]:
        for category, needles in _CATEGORY_SIGNATURES:
            hits = [n for n in needles if n in text_norm]
            if hits:
                # More independent signatures -> more confidence, capped.
                return category, min(0.95, 0.55 + 0.12 * len(hits))
        return EmailCategory.OTHER, 0.2

    @staticmethod
    def _first_datetime(text: str) -> str | None:
        for pattern in _DATETIME_PATTERNS:
            m = pattern.search(text)
            if m:
                return " ".join(part for part in m.groups() if part).strip()
        return None

    @staticmethod
    def _extract_rejection_reason(text: str) -> tuple[str | None, str | None]:
        """Only an explicitly stated reason counts. Everything else is a guess,
        and guesses are kept out of this field on purpose."""
        cues = [
            r"because\s+([^.\n]{10,180})",
            r"due to\s+([^.\n]{10,180})",
            r"reason[:\s]+([^.\n]{10,180})",
            r"we (?:are |were )?looking for\s+([^.\n]{10,180})",
            r"require[sd]?\s+([^.\n]{10,180})\s+which",
        ]
        for cue in cues:
            m = re.search(cue, text, re.I)
            if m:
                return m.group(1).strip(), m.group(0).strip()
        return None, None

    def heuristic_analyze(self, subject: str, body: str, sender: str | None = None) -> EmailAnalysis:
        text = f"{subject or ''}\n{body or ''}"
        norm = normalize(text)
        category, confidence = self._category(norm)

        emails = _EMAIL.findall(text)
        urls = _URL.findall(text)
        position = None
        m = _POSITION_CUE.search(text)
        if m:
            position = m.group(1).strip()
        company = None
        cm = _COMPANY_CUE.search(text)
        if cm:
            company = cm.group(1).strip().rstrip(".,;:")

        analysis = EmailAnalysis(
            category=category,
            confidence=confidence,
            company=company,
            position=position,
            recruiter_email=(sender and _EMAIL.search(sender) and _EMAIL.search(sender).group(0))
            or (emails[0] if emails else None),
            recruiter_name=(sender.split("<")[0].strip().strip('"') if sender and "<" in sender else None),
            job_url=urls[0] if urls else None,
            status_hint=_STATUS_HINTS.get(category),
        )

        if category is EmailCategory.INTERVIEW:
            analysis.interview_datetime = self._first_datetime(text)
            analysis.required_action = "Confirm availability / accept the invitation."
        elif category is EmailCategory.ASSESSMENT:
            dm = _DEADLINE_CUE.search(text)
            analysis.assessment_deadline = (dm.group(1).strip() if dm else None) or self._first_datetime(text)
            analysis.required_action = "Complete the assessment before the deadline."
        elif category is EmailCategory.RECRUITER:
            analysis.required_action = "Reply with interest / availability."
        elif category is EmailCategory.OFFER:
            analysis.required_action = "Review the offer. Do not reply automatically."
        elif category is EmailCategory.REJECTION:
            explicit, quote = self._extract_rejection_reason(text)
            analysis.rejection_reason_explicit = explicit
            analysis.rejection_reason_quote = quote
        elif category is EmailCategory.SPONSORSHIP:
            analysis.required_action = "Immigration topic - review personally before replying."
        return analysis

    # -- AI refinement ------------------------------------------------------
    def analyze(self, subject: str, body: str, sender: str | None = None, use_ai: bool = True) -> EmailAnalysis:
        base = self.heuristic_analyze(subject, body, sender)
        if not use_ai or not getattr(self.provider, "available", False):
            return base

        out = self.provider.structured(
            system=SYSTEM_PROMPT,
            prompt=f"FROM: {sender or 'unknown'}\nSUBJECT: {subject}\n\nBODY:\n{body}",
            schema=EmailExtractionOut,
        )
        if not out:
            return base
        return self._merge(base, out, body)

    @staticmethod
    def _merge(base: EmailAnalysis, out: EmailExtractionOut, body: str) -> EmailAnalysis:
        merged = EmailAnalysis(**{**asdict(base), "category": base.category})
        try:
            merged.category = EmailCategory(out.category)
        except ValueError:
            pass
        merged.confidence = max(base.confidence, float(out.confidence))
        merged.method = "hybrid"
        for attr in (
            "company", "position", "recruiter_name", "recruiter_email", "job_url",
            "interview_datetime", "assessment_deadline", "required_action",
        ):
            value = getattr(out, attr, None)
            if value:
                setattr(merged, attr, value)
        # Trust an explicit rejection reason only if the quote is really in the
        # email -- this is the field most likely to be confabulated.
        if out.rejection_reason_explicit and out.rejection_reason_quote:
            if normalize(out.rejection_reason_quote) in normalize(body):
                merged.rejection_reason_explicit = out.rejection_reason_explicit
                merged.rejection_reason_quote = out.rejection_reason_quote
        merged.status_hint = _STATUS_HINTS.get(merged.category)
        return merged


_LEADING_DATE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?)",
    re.I,
)


def parse_action_date(value: str | None) -> date | None:
    """Best-effort date from an extracted string; None when ambiguous.

    The extracted value usually carries a time as well ("2026-09-18 14:00"),
    so the date portion is isolated first.
    """
    if not value:
        return None
    candidates = [value.strip()]
    m = _LEADING_DATE.search(value)
    if m:
        candidates.insert(0, m.group(1))
    for candidate in candidates:
        for fmt in (
            "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
            "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
            "%B %d", "%b %d",
        ):
            try:
                parsed = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            if parsed.year == 1900:
                parsed = parsed.replace(year=date.today().year)
            return parsed.date()
    return None
