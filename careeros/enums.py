"""Enumerations shared by the engines, the ORM and the API.

Everything here is deliberately *open at the edges*: the values that describe
the world (career domain, job function, employment type, work-authorization
status) are stored as plain strings sourced from the config packs, so a new
domain or a new country never requires editing this file. What is closed here
is the small set of values the system's own logic branches on.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """`str` mixin so values serialise cleanly to JSON and SQLite."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# --------------------------------------------------------------------------
# Work authorization
# --------------------------------------------------------------------------
class AuthVerdict(StrEnum):
    """Compatibility of a job with the user's work-authorization status.

    `UNKNOWN` is the default and the honest answer for most postings: absence
    of a sponsorship statement is not evidence of sponsorship either way.
    """

    COMPATIBLE = "compatible"
    POTENTIALLY_COMPATIBLE = "potentially_compatible"
    UNKNOWN = "unknown"
    NOT_COMPATIBLE = "not_compatible"


# --------------------------------------------------------------------------
# Skill matching
# --------------------------------------------------------------------------
class MatchKind(StrEnum):
    DIRECT = "direct"
    RELATED = "related"          # transferable - adjacent in the skill graph
    PARTIAL = "partial"          # holds a broader/narrower form
    MISSING = "missing"
    UNKNOWN = "unknown"          # requirement could not be parsed


class RequirementLevel(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    UNSPECIFIED = "unspecified"


# --------------------------------------------------------------------------
# Priority
# --------------------------------------------------------------------------
class DeadlineBucket(StrEnum):
    TODAY = "today"
    WITHIN_48H = "within_48h"
    WITHIN_7D = "within_7d"
    FUTURE = "future"
    NONE = "none"
    EXPIRED = "expired"

    @property
    def emoji(self) -> str:
        return {
            "today": "\U0001F534",        # red
            "within_48h": "\U0001F7E0",   # orange
            "within_7d": "\U0001F7E1",    # yellow
            "future": "\U0001F7E2",       # green
            "none": "⚪",             # white
            "expired": "⚫",          # black
        }[self.value]

    @property
    def label(self) -> str:
        return {
            "today": "DEADLINE TODAY",
            "within_48h": "DEADLINE WITHIN 48 HOURS",
            "within_7d": "DEADLINE WITHIN 7 DAYS",
            "future": "FUTURE",
            "none": "NO DEADLINE",
            "expired": "EXPIRED",
        }[self.value]


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------
class EvidenceKind(StrEnum):
    """Atomic, verified facts in the Career Evidence Database.

    A resume is *assembled from* these; nothing may appear on a generated
    resume that does not trace back to one of them.
    """

    RESPONSIBILITY = "responsibility"
    ACHIEVEMENT = "achievement"
    PROJECT = "project"
    TECHNOLOGY = "technology"
    CERTIFICATION = "certification"
    EDUCATION = "education"
    TITLE = "title"
    EMPLOYMENT = "employment"
    SUMMARY_FACT = "summary_fact"


class VerificationState(StrEnum):
    VERIFIED = "verified"          # user confirmed
    UNVERIFIED = "unverified"      # imported, not yet confirmed
    DISPUTED = "disputed"          # user flagged as inaccurate


# --------------------------------------------------------------------------
# Resumes
# --------------------------------------------------------------------------
class ResumeKind(StrEnum):
    MASTER = "master"
    TRACK = "track"
    TAILORED = "tailored"


class ClaimVerdict(StrEnum):
    SUPPORTED = "supported"            # traces to verified evidence
    WEAKLY_SUPPORTED = "weakly_supported"   # traces to unverified evidence
    UNSUPPORTED = "unsupported"        # no evidence - must not ship
    REPHRASED = "rephrased"            # wording changed, facts preserved


# --------------------------------------------------------------------------
# Application lifecycle
# --------------------------------------------------------------------------
class ApplicationStatus(StrEnum):
    DISCOVERED = "discovered"
    SAVED = "saved"
    ANALYZED = "analyzed"
    RESUME_READY = "resume_ready"
    APPLICATION_READY = "application_ready"
    APPLIED = "applied"
    APPLICATION_RECEIVED = "application_received"
    RECRUITER_CONTACT = "recruiter_contact"
    RECRUITER_SCREEN = "recruiter_screen"
    INTERVIEW = "interview"
    TECHNICAL_INTERVIEW = "technical_interview"
    ASSESSMENT = "assessment"
    HIRING_MANAGER = "hiring_manager"
    FINAL_ROUND = "final_round"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    NO_RESPONSE = "no_response"
    EXPIRED = "expired"


#: Ordered pipeline used for funnel analytics. Terminal states are excluded.
APPLICATION_FUNNEL: tuple[ApplicationStatus, ...] = (
    ApplicationStatus.DISCOVERED,
    ApplicationStatus.SAVED,
    ApplicationStatus.ANALYZED,
    ApplicationStatus.RESUME_READY,
    ApplicationStatus.APPLICATION_READY,
    ApplicationStatus.APPLIED,
    ApplicationStatus.APPLICATION_RECEIVED,
    ApplicationStatus.RECRUITER_CONTACT,
    ApplicationStatus.RECRUITER_SCREEN,
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.TECHNICAL_INTERVIEW,
    ApplicationStatus.ASSESSMENT,
    ApplicationStatus.HIRING_MANAGER,
    ApplicationStatus.FINAL_ROUND,
    ApplicationStatus.OFFER,
    ApplicationStatus.ACCEPTED,
)

TERMINAL_STATUSES = frozenset(
    {
        ApplicationStatus.ACCEPTED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.WITHDRAWN,
        ApplicationStatus.NO_RESPONSE,
        ApplicationStatus.EXPIRED,
    }
)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
class EmailCategory(StrEnum):
    RECRUITER = "recruiter"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"
    APPLICATION_CONFIRMATION = "application_confirmation"
    REJECTION = "rejection"
    OFFER = "offer"
    SPONSORSHIP = "sponsorship"
    FOLLOW_UP = "follow_up"
    OTHER = "other"


class DraftState(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SENT = "sent"
    DISCARDED = "discarded"
    BLOCKED_HIGH_IMPACT = "blocked_high_impact"


#: Topics that may never be auto-sent, regardless of automation mode.
HIGH_IMPACT_TOPICS = (
    "immigration",
    "sponsorship_commitment",
    "salary_negotiation",
    "contract",
    "offer",
    "resignation",
    "legal",
)


# --------------------------------------------------------------------------
# Automation
# --------------------------------------------------------------------------
class AutomationMode(StrEnum):
    MANUAL = "manual"
    ASSISTED = "assisted"     # default
    AUTOMATED = "automated"


class ReasonConfidence(StrEnum):
    """Used to keep an explicit rejection reason separate from a guess."""

    EXPLICIT = "explicit"     # stated by the employer
    INFERRED = "inferred"     # the system's hypothesis - never shown as fact
