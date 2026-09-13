"""Pull job mail, classify it, and advance the application lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.db.models import (
    Application,
    ApplicationEvent,
    EmailDraft,
    EmailMessage,
    Job,
    RejectionAnalysis,
    User,
)
from careeros.engines.textutil import normalize
from careeros.enums import ApplicationStatus, AutomationMode, EmailCategory, ReasonConfidence
from careeros.gmail.classify import EmailAnalysis, EmailClassifier, parse_action_date
from careeros.gmail.client import DEFAULT_QUERY, GmailClient
from careeros.gmail.drafts import DraftGenerator

logger = logging.getLogger(__name__)

#: Statuses that must never be walked back by an inferred email signal.
_TERMINAL = {
    ApplicationStatus.ACCEPTED.value,
    ApplicationStatus.REJECTED.value,
    ApplicationStatus.WITHDRAWN.value,
}


@dataclass
class SyncStats:
    fetched: int = 0
    stored: int = 0
    linked: int = 0
    advanced: int = 0
    drafted: int = 0
    blocked: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched, "stored": self.stored, "linked": self.linked,
            "advanced": self.advanced, "drafted": self.drafted, "blocked": self.blocked,
            "errors": self.errors,
        }


class GmailSync:
    def __init__(
        self,
        session: Session,
        client: GmailClient,
        classifier: EmailClassifier | None = None,
        drafter: DraftGenerator | None = None,
    ) -> None:
        self.session = session
        self.client = client
        self.classifier = classifier or EmailClassifier()
        self.drafter = drafter or DraftGenerator()

    # -- linking ------------------------------------------------------------
    def _find_application(self, user: User, analysis: EmailAnalysis, text: str) -> Application | None:
        """Attach the message to an application by company, then by title.

        Deliberately conservative: a wrong link corrupts the funnel analytics,
        so an unmatched email stays unlinked rather than being guessed onto the
        nearest application.
        """
        applications = self.session.execute(
            select(Application, Job).join(Job, Job.id == Application.job_id)
            .where(Application.user_id == user.id)
        ).all()

        norm = normalize(text)
        company = normalize(analysis.company or "")
        position = normalize(analysis.position or "")

        best: tuple[int, Application] | None = None
        for application, job in applications:
            score = 0
            job_company = normalize(job.company)
            if job_company and (job_company in norm or (company and company in job_company)):
                score += 3
            job_title = normalize(job.title)
            if job_title and (job_title in norm or (position and position in job_title)):
                score += 2
            if analysis.job_url and job.url and analysis.job_url.strip() == job.url.strip():
                score += 5
            if score >= 3 and (best is None or score > best[0]):
                best = (score, application)
        return best[1] if best else None

    def _advance(self, application: Application, analysis: EmailAnalysis, stats: SyncStats) -> None:
        target = analysis.status_hint
        if target is None or application.status in _TERMINAL:
            return
        if application.status == target.value:
            return
        event = ApplicationEvent(
            application_id=application.id,
            from_status=application.status,
            to_status=target.value,
            source="email",
            detail=f"{analysis.category.value} email (confidence {analysis.confidence:.2f})",
        )
        application.status = target.value
        application.last_activity_on = date.today()
        self.session.add(event)
        stats.advanced += 1

    def _record_rejection(self, application: Application, analysis: EmailAnalysis) -> None:
        """Keep the stated reason and the system's hypotheses strictly apart."""
        row = self.session.scalars(
            select(RejectionAnalysis).where(RejectionAnalysis.application_id == application.id)
        ).first() or RejectionAnalysis(application_id=application.id)
        row.stage = application.status
        row.explicit_reason = analysis.rejection_reason_explicit
        row.explicit_quote = analysis.rejection_reason_quote
        row.confidence = (
            ReasonConfidence.EXPLICIT.value
            if analysis.rejection_reason_explicit
            else ReasonConfidence.INFERRED.value
        )
        row.possible_reasons = self._hypotheses(application)
        if row.id is None:
            self.session.add(row)

    def _hypotheses(self, application: Application) -> list[dict[str, Any]]:
        """Hypotheses, labelled as such, derived from the scores we already hold."""
        job = self.session.get(Job, application.job_id)
        out: list[dict[str, Any]] = []
        if job is None:
            return out
        priority = job.priority
        if priority and priority.match_score < 60:
            out.append({
                "reason": "Skill match below the level that typically converts",
                "basis": f"match score {priority.match_score:.0f}",
                "confidence": "low",
            })
        if priority and priority.eligibility_score < 60:
            out.append({
                "reason": "Work-authorization fit was uncertain or unfavourable",
                "basis": f"eligibility score {priority.eligibility_score:.0f}",
                "confidence": "low",
            })
        if job.applicant_count and job.applicant_count > 150:
            out.append({
                "reason": "High applicant volume",
                "basis": f"{job.applicant_count} applicants recorded",
                "confidence": "low",
            })
        for item in out:
            item["disclaimer"] = "Hypothesis generated by CareerOS - not a reason given by the employer."
        return out

    # -- main ---------------------------------------------------------------
    def sync(
        self,
        user: User,
        query: str = DEFAULT_QUERY,
        limit: int = 50,
        generate_drafts: bool = True,
        use_ai: bool = True,
    ) -> SyncStats:
        stats = SyncStats()
        for message in self.client.list_messages(query=query, limit=limit):
            stats.fetched += 1
            try:
                existing = self.session.scalars(
                    select(EmailMessage).where(EmailMessage.gmail_id == message.id)
                ).first()
                if existing:
                    continue

                analysis = self.classifier.analyze(
                    message.subject or "", message.body or "", message.sender, use_ai=use_ai
                )
                application = self._find_application(
                    user, analysis, f"{message.subject}\n{message.body}"
                )

                row = EmailMessage(
                    user_id=user.id,
                    gmail_id=message.id,
                    thread_id=message.thread_id,
                    received_at=message.received_at,
                    sender=message.sender,
                    subject=message.subject,
                    snippet=message.snippet,
                    category=analysis.category.value,
                    confidence=analysis.confidence,
                    extracted=analysis.to_dict(),
                    application_id=application.id if application else None,
                    requires_action=analysis.requires_action,
                    action_due_on=parse_action_date(
                        analysis.assessment_deadline or analysis.interview_datetime
                    ),
                )
                self.session.add(row)
                self.session.flush()
                stats.stored += 1

                if application:
                    stats.linked += 1
                    self._advance(application, analysis, stats)
                    if analysis.category is EmailCategory.REJECTION:
                        self._record_rejection(application, analysis)

                if generate_drafts and analysis.requires_action:
                    draft = self.drafter.generate(
                        category=analysis.category,
                        subject=message.subject or "",
                        body=message.body or "",
                        candidate_first_name=(user.full_name or "").split()[0] or "there",
                        recruiter_name=analysis.recruiter_name,
                        company=analysis.company,
                        position=analysis.position,
                        interview_datetime=analysis.interview_datetime,
                        assessment_deadline=analysis.assessment_deadline,
                        automation_mode=AutomationMode(user.automation_mode),
                        use_ai=use_ai,
                    )
                    self.session.add(
                        EmailDraft(
                            user_id=user.id,
                            email_id=row.id,
                            application_id=application.id if application else None,
                            intent=draft.intent,
                            subject=draft.subject,
                            body=draft.body,
                            state=draft.state.value,
                            high_impact_topics=draft.high_impact_topics,
                        )
                    )
                    stats.drafted += 1
                    if draft.high_impact_topics:
                        stats.blocked += 1
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"email[{message.id}]: {type(exc).__name__}: {exc}")
                logger.exception("email sync failed for %s", message.id)
        self.session.flush()
        return stats
