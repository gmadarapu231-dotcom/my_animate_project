"""AI email replies.

Default flow is DRAFT -> USER APPROVAL -> SEND, and some topics never leave
draft state at all. `HIGH_IMPACT_TOPICS` (immigration, sponsorship
commitments, salary negotiation, contracts, offers, resignation, legal) are
detected on both the incoming message and the generated reply; when any fires,
the draft is marked `BLOCKED_HIGH_IMPACT` and the send path refuses it
regardless of the user's automation mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from careeros.ai.provider import LLMProvider, get_provider
from careeros.enums import AutomationMode, DraftState, EmailCategory

#: Topic -> phrases that mark a message as high impact.
HIGH_IMPACT_SIGNATURES: dict[str, tuple[str, ...]] = {
    "immigration": ("visa", "h1b", "h-1b", "green card", "immigration", "work permit", "uscis", "i-140", "i-983"),
    "sponsorship_commitment": ("sponsor", "sponsorship", "cap-exempt", "transfer my petition"),
    "salary_negotiation": ("salary", "compensation", "rate", "pay range", "counter offer", "counteroffer", "base pay"),
    "contract": ("contract", "agreement", "msa", "statement of work", "terms and conditions", "non-compete"),
    "offer": ("offer letter", "formal offer", "accept the offer", "decline the offer"),
    "resignation": ("resign", "resignation", "notice period", "last working day"),
    "legal": ("legal", "attorney", "lawyer", "litigation", "nda", "non-disclosure"),
}

#: Reply intents the system knows how to draft.
INTENTS = {
    EmailCategory.RECRUITER: "job_interest_response",
    EmailCategory.INTERVIEW: "interview_availability",
    EmailCategory.ASSESSMENT: "assessment_acknowledgement",
    EmailCategory.APPLICATION_CONFIRMATION: "application_acknowledgement",
    EmailCategory.FOLLOW_UP: "follow_up",
    EmailCategory.OFFER: "offer_acknowledgement",
    EmailCategory.SPONSORSHIP: "sponsorship_enquiry_response",
    EmailCategory.REJECTION: "thank_you_after_rejection",
}

SYSTEM_PROMPT = """You draft short, professional replies for a job seeker.

Rules:
- Never state, confirm or negotiate a visa status, sponsorship arrangement, \
salary figure, contract term or offer decision. If the incoming email raises \
one, acknowledge it and say the candidate will follow up directly.
- Never invent availability, dates, experience, certifications or notice \
periods that were not given to you.
- Keep it under 150 words, plain text, no markdown.
- Match the sender's level of formality. Sign off with the candidate's first name."""


@dataclass
class DraftResult:
    intent: str
    subject: str
    body: str
    state: DraftState
    high_impact_topics: list[str] = field(default_factory=list)
    method: str = "template"
    rationale: str | None = None

    @property
    def may_auto_send(self) -> bool:
        return self.state is DraftState.APPROVED and not self.high_impact_topics

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "subject": self.subject,
            "body": self.body,
            "state": self.state.value,
            "high_impact_topics": self.high_impact_topics,
            "method": self.method,
            "rationale": self.rationale,
        }


def detect_high_impact(text: str) -> list[str]:
    low = (text or "").lower()
    found = []
    for topic, needles in HIGH_IMPACT_SIGNATURES.items():
        if any(re.search(rf"(?<![a-z]){re.escape(n)}", low) for n in needles):
            found.append(topic)
    return found


_TEMPLATES = {
    "job_interest_response": (
        "Hi {recruiter},\n\nThank you for reaching out about {position_phrase}"
        "{company_clause}. I am interested and would be glad to learn more.\n\n"
        "Could you share the job description, the team's focus and the next step in "
        "your process? I am generally available for an introductory call this week "
        "and next.\n\nBest regards,\n{first_name}"
    ),
    "interview_availability": (
        "Hi {recruiter},\n\nThank you for the invitation to interview for {position_phrase} "
        "role{company_clause}. I am glad to move forward.\n\n"
        "{slot_clause}If another time suits the panel better, I am happy to work around "
        "their calendar - please send the invitation and I will confirm.\n\n"
        "Best regards,\n{first_name}"
    ),
    "assessment_acknowledgement": (
        "Hi {recruiter},\n\nThank you - I have received the assessment for {position_phrase} "
        "role{company_clause}. {deadline_clause}I will complete it and confirm once it is "
        "submitted.\n\nBest regards,\n{first_name}"
    ),
    "application_acknowledgement": (
        "Hi {recruiter},\n\nThank you for confirming receipt of my application for "
        "{position_phrase}{company_clause}. I look forward to hearing about next steps, and "
        "I am happy to provide anything further that would help your review.\n\n"
        "Best regards,\n{first_name}"
    ),
    "follow_up": (
        "Hi {recruiter},\n\nI wanted to follow up on my application for {position_phrase} "
        "role{company_clause}. I remain very interested and would welcome an update on "
        "where things stand.\n\nThank you for your time.\n\nBest regards,\n{first_name}"
    ),
    "thank_you_after_rejection": (
        "Hi {recruiter},\n\nThank you for letting me know about {position_phrase}"
        "{company_clause}, and for the time your team spent on my application.\n\n"
        "If you are open to it, I would value any feedback on where I fell short - it "
        "helps me focus. I would also be glad to be considered for future openings.\n\n"
        "Best regards,\n{first_name}"
    ),
    "offer_acknowledgement": (
        "Hi {recruiter},\n\nThank you very much for the offer for {position_phrase} role"
        "{company_clause}. I appreciate the team's time throughout the process.\n\n"
        "I would like to review the details properly and will come back to you shortly "
        "with any questions.\n\nBest regards,\n{first_name}"
    ),
    "sponsorship_enquiry_response": (
        "Hi {recruiter},\n\nThank you for your question regarding {position_phrase}"
        "{company_clause}. I would prefer to confirm those details with you directly "
        "rather than over email - could we set up a short call?\n\n"
        "Best regards,\n{first_name}"
    ),
}


class DraftGenerator:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or get_provider()

    def generate(
        self,
        category: EmailCategory,
        subject: str,
        body: str,
        candidate_first_name: str,
        recruiter_name: str | None = None,
        company: str | None = None,
        position: str | None = None,
        interview_datetime: str | None = None,
        assessment_deadline: str | None = None,
        automation_mode: AutomationMode = AutomationMode.ASSISTED,
        use_ai: bool = True,
    ) -> DraftResult:
        intent = INTENTS.get(category, "follow_up")
        topics = detect_high_impact(f"{subject}\n{body}")

        fields = {
            "recruiter": (recruiter_name or "there").split()[0] if recruiter_name else "there",
            "first_name": candidate_first_name,
            "position_phrase": f"the {position} role" if position else "this role",
            "company_clause": f" at {company}" if company else "",
            "slot_clause": (
                f"The proposed time ({interview_datetime}) works for me. "
                if interview_datetime else ""
            ),
            "deadline_clause": (
                f"I have noted the {assessment_deadline} deadline. " if assessment_deadline else ""
            ),
        }
        draft_body = _TEMPLATES[intent].format(**fields)
        method = "template"
        rationale = None

        if use_ai and getattr(self.provider, "available", False):
            generated = self.provider.text(
                system=SYSTEM_PROMPT,
                prompt=(
                    f"CANDIDATE FIRST NAME: {candidate_first_name}\n"
                    f"INTENT: {intent}\n"
                    f"KNOWN FACTS: position={position or 'unknown'}, company={company or 'unknown'}, "
                    f"proposed_time={interview_datetime or 'none given'}, "
                    f"deadline={assessment_deadline or 'none given'}\n"
                    f"HIGH-IMPACT TOPICS PRESENT: {', '.join(topics) or 'none'}\n\n"
                    f"INCOMING EMAIL\nSubject: {subject}\n\n{body}\n\n"
                    "Draft the reply."
                ),
                max_tokens=800,
            )
            if generated:
                draft_body = generated
                method = "llm"
                # The reply itself may raise a high-impact topic even when the
                # incoming message did not.
                topics = sorted(set(topics) | set(detect_high_impact(generated)))

        if topics:
            state = DraftState.BLOCKED_HIGH_IMPACT
            rationale = (
                "Held for manual review - touches " + ", ".join(topics) + ". "
                "CareerOS never auto-sends on these topics."
            )
        elif automation_mode is AutomationMode.AUTOMATED:
            state = DraftState.DRAFT
            rationale = "Automated mode: still requires approval before sending."
        else:
            state = DraftState.AWAITING_APPROVAL

        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        return DraftResult(
            intent=intent,
            subject=reply_subject,
            body=draft_body.strip(),
            state=state,
            high_impact_topics=topics,
            method=method,
            rationale=rationale,
        )
