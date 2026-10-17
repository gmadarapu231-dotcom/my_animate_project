"""The cycle: find, prep, apply, track -- on a timer.

One pass is `run_once()`: discover from every ready provider, classify and
rank, tailor a résumé for the top candidates, walk them in priority order and
apply where the guardrails allow, then read the mailbox so replies move each
application along. Every job produces a verdict, so the
report answers "why did it not apply to that one?" for all of them, not just
the ones it acted on.

Three properties make a 4-hour loop safe to leave running:

**A lock file.** Two overlapping passes would double-apply. The lock carries
the pid and start time and is treated as stale after an hour, so a killed
process does not wedge the loop forever.

**Every run recorded.** A `PipelineRun` row per pass with counts and errors, so
the history is inspectable rather than a feeling.

**Off and dry by default.** `AutopilotPolicy.enabled` is false and `dry_run` is
true. The first thing a user gets is a list of what it *would* have sent. Going
live still queues each application for a yes or no (`require_approval`); only
switching that off as well makes the cycle fully hands-off.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.apply import channels as channel_module
from careeros.apply.guardrails import (
    AutopilotPolicy,
    applications_today,
    evaluate,
    in_quiet_hours,
)
from careeros.apply.packet import build as build_packet
from careeros.apply.submit import Submission, submit
from careeros.db.models import (
    Application,
    ApplicationEvent,
    AtsAssessment,
    CoverLetter,
    EligibilityAssessment,
    EvidenceItem,
    Job,
    MatchAssessment,
    PipelineRun,
    PriorityScore,
    ResumeDocument,
    User,
)
from careeros.enums import ApplicationStatus, VerificationState
from careeros.sources.base import SourceRegistry
from careeros.sources.http import HttpClient

logger = logging.getLogger(__name__)

DEFAULT_TAILOR_LIMIT = 12
DEFAULT_CONSIDER_LIMIT = 40
LOCK_STALE_AFTER = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Policy storage
# ---------------------------------------------------------------------------
def load_policy(user: User) -> AutopilotPolicy:
    return AutopilotPolicy.from_dict(getattr(user, "autopilot", None))


def save_policy(session: Session, user: User, policy: AutopilotPolicy) -> AutopilotPolicy:
    user.autopilot = policy.to_dict()
    session.flush()
    return policy


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
def lock_path() -> Path:
    home = Path(os.getenv("CAREEROS_HOME", Path.home() / ".careeros"))
    return home / "autopilot.lock"


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    """Refuses to start a second pass while one is in flight."""

    def __init__(self, path: Path | None = None, stale_after: timedelta = LOCK_STALE_AFTER) -> None:
        self.path = path or lock_path()
        self.stale_after = stale_after
        self.held = False

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def stale(self) -> bool:
        existing = self._read()
        if not existing:
            return True
        try:
            started = datetime.fromisoformat(existing["started_at"])
        except (KeyError, ValueError):
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return _now() - started > self.stale_after

    def __enter__(self) -> "RunLock":
        if self.path.exists() and not self.stale():
            existing = self._read() or {}
            raise AlreadyRunning(
                f"A pass started at {existing.get('started_at')} "
                f"(pid {existing.get('pid')}) is still running. "
                f"Delete {self.path} if that process is gone."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": _now().isoformat()}),
            encoding="utf-8",
        )
        self.held = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------
@dataclass
class JobVerdict:
    job_id: int
    title: str
    company: str
    tier: str
    allowed: bool
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    submission: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "tier": self.tier,
            "allowed": self.allowed,
            "blockers": self.blockers,
            "notes": self.notes,
            "submission": self.submission,
        }


@dataclass
class PassReport:
    started_at: datetime
    finished_at: datetime | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    discovery: dict[str, Any] | None = None
    classified: int = 0
    tailored: int = 0
    considered: int = 0
    submitted: int = 0
    would_submit: int = 0
    assisted: int = 0
    awaiting_approval: int = 0
    tracking: dict[str, Any] | None = None
    verdicts: list[JobVerdict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def blocked_counts(self) -> dict[str, int]:
        """Why applications did not go out, most common first."""
        counts: dict[str, int] = {}
        for verdict in self.verdicts:
            for blocker in verdict.blockers:
                key = blocker.split("(")[0].strip().rstrip(":")
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def stages(self) -> list[dict[str, Any]]:
        """The pass as the four steps a person thinks in: find, prep, apply, track."""
        found = ((self.discovery or {}).get("ingest") or {}).get("inserted")
        tracked = self.tracking or {}
        return [
            {
                "key": "find",
                "label": "Find",
                "done": self.discovery is not None,
                "detail": (
                    f"{found} new posting(s), {self.classified} classified and ranked"
                    if found is not None
                    else f"search skipped, {self.classified} classified"
                ),
            },
            {
                "key": "prep",
                "label": "Prep",
                "done": True,
                "detail": f"{self.tailored} résumé(s) tailored and fact-checked",
            },
            {
                "key": "apply",
                "label": "Apply",
                "done": True,
                "detail": (
                    f"{self.submitted} sent, {self.awaiting_approval} waiting for you, "
                    f"{self.would_submit} dry-run, {self.assisted} need a human"
                ),
            },
            {
                "key": "track",
                "label": "Track",
                "done": self.tracking is not None and "skipped" not in tracked,
                "detail": (
                    tracked["skipped"]
                    if "skipped" in tracked
                    else f"{tracked.get('fetched', 0)} email(s) read, "
                    f"{tracked.get('advanced', 0)} application(s) moved"
                    if self.tracking is not None
                    else "not run"
                ),
            },
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": (
                round((self.finished_at - self.started_at).total_seconds(), 1)
                if self.finished_at
                else None
            ),
            "policy": self.policy,
            "skipped_reason": self.skipped_reason,
            "discovery": self.discovery,
            "counts": {
                "classified": self.classified,
                "tailored": self.tailored,
                "considered": self.considered,
                "submitted": self.submitted,
                "would_submit": self.would_submit,
                "assisted": self.assisted,
                "awaiting_approval": self.awaiting_approval,
            },
            "stages": self.stages(),
            "tracking": self.tracking,
            "blocked_by": self.blocked_counts,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "errors": self.errors,
        }


def _verified_evidence(session: Session, user: User) -> set[int]:
    rows = session.scalars(
        select(EvidenceItem.id).where(
            EvidenceItem.user_id == user.id,
            EvidenceItem.verification == VerificationState.VERIFIED.value,
        )
    ).all()
    return {int(i) for i in rows}


def _latest_resume(session: Session, user: User, job_id: int) -> ResumeDocument | None:
    return session.scalars(
        select(ResumeDocument)
        .where(ResumeDocument.user_id == user.id, ResumeDocument.job_id == job_id)
        .order_by(ResumeDocument.version.desc(), ResumeDocument.id.desc())
    ).first()


def _assessments(
    session: Session, user: User, job_id: int
) -> tuple[EligibilityAssessment | None, MatchAssessment | None, PriorityScore | None]:
    """The three per-(user, job) assessments, best match first.

    A job can have one match row per career track, so the strongest is the one
    that matters -- applying is a decision about the job, not about a track.
    """
    eligibility = session.scalars(
        select(EligibilityAssessment).where(
            EligibilityAssessment.user_id == user.id,
            EligibilityAssessment.job_id == job_id,
        )
    ).first()
    matches = session.scalars(
        select(MatchAssessment).where(MatchAssessment.job_id == job_id)
    ).all()
    best = max(matches, key=lambda m: m.match_score) if matches else None
    priority = session.scalars(
        select(PriorityScore).where(PriorityScore.job_id == job_id)
    ).first()
    return eligibility, best, priority


def _application_for(session: Session, user: User, job_id: int) -> Application | None:
    return session.scalars(
        select(Application).where(Application.user_id == user.id, Application.job_id == job_id)
    ).first()


def _record(
    session: Session,
    user: User,
    job: Job,
    resume: ResumeDocument | None,
    result: Submission,
) -> Application:
    """Write the outcome onto the application row and its event log."""
    application = _application_for(session, user, job.id)
    if application is None:
        application = Application(user_id=user.id, job_id=job.id)
        session.add(application)
        session.flush()

    previous = application.status
    application.resume_id = resume.id if resume else application.resume_id
    application.submission_mode = "automatic" if result.ok and not result.dry_run else "assisted"

    if result.ok and not result.dry_run:
        application.status = ApplicationStatus.APPLIED.value
        application.applied_on = date.today()
        application.last_activity_on = date.today()
        detail = f"Submitted via {result.tier} ({result.provider})"
        if result.reference:
            detail += f", reference {result.reference}"
        coverage = (result.artifacts or {}).get("ats_keyword_match")
        if coverage is not None:
            detail += f", ATS keyword match {coverage}%"
    elif result.ok and result.dry_run:
        application.status = ApplicationStatus.APPLICATION_READY.value
        detail = f"Dry run: {result.message}"
    else:
        application.status = ApplicationStatus.APPLICATION_READY.value
        detail = result.message

    session.add(
        ApplicationEvent(
            application_id=application.id,
            from_status=previous,
            to_status=application.status,
            at=_now(),
            detail=detail[:2000],
            source="autopilot",
        )
    )
    session.flush()
    return application


def _default_mailbox() -> Any | None:
    """The mailbox to watch for replies, only when one is actually connected."""
    from careeros.gmail.client import OAuthGmailClient, get_client

    if os.getenv("CAREEROS_LOCAL_MAILBOX"):
        return get_client()
    oauth = OAuthGmailClient()
    return oauth if oauth.available else None


def _queue(
    session: Session, user: User, job: Job, resume: ResumeDocument | None
) -> Application:
    """Park a ready application until the user says yes or no."""
    application = _application_for(session, user, job.id)
    if application is None:
        application = Application(user_id=user.id, job_id=job.id)
        session.add(application)
        session.flush()
    if application.approval == "pending":
        return application
    previous = application.status
    application.status = ApplicationStatus.APPLICATION_READY.value
    application.resume_id = resume.id if resume else application.resume_id
    application.approval = "pending"
    session.add(
        ApplicationEvent(
            application_id=application.id,
            from_status=previous,
            to_status=application.status,
            at=_now(),
            detail="Ready to send. Waiting for your approval.",
            source="autopilot",
        )
    )
    session.flush()
    return application


def _consider(
    session: Session,
    user: User,
    job: Job,
    policy: AutopilotPolicy,
    *,
    verified_evidence_ids: set[int],
    submitted_this_run: int,
    submitted_today: int,
    client: HttpClient | None = None,
    send_email: Callable[[dict[str, str], tuple[str, bytes]], str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> tuple[JobVerdict, str]:
    """Check one job against the guardrails and act on it.

    The outcome is one of blocked, assisted, queued, would_submit, submitted,
    failed or error. On error the message is the verdict's last note.
    """
    channel = channel_module.detect(
        url=job.url,
        description=job.description,
        source=job.source,
        company=job.company,
    )
    resume = _latest_resume(session, user, job.id)
    factuality = None
    if resume is not None and getattr(resume, "factuality", None):
        factuality = resume.factuality[-1]

    eligibility, match, priority = _assessments(session, user, job.id)
    application = _application_for(session, user, job.id)
    decision = evaluate(
        policy=policy,
        job=job,
        channel=channel,
        resume=resume,
        factuality=factuality,
        eligibility=eligibility,
        match=match,
        priority=priority,
        verified_evidence_ids=verified_evidence_ids,
        application=application,
        submitted_this_run=submitted_this_run,
        submitted_today=submitted_today,
        today=today,
        now=now,
    )
    verdict = JobVerdict(
        job_id=job.id,
        title=job.title,
        company=job.company,
        tier=channel.tier.value,
        allowed=decision.allowed,
        blockers=decision.blockers,
        notes=decision.notes,
    )

    if not decision.allowed:
        if channel.tier.value == "assisted" or decision.tier == "blocked":
            return verdict, "assisted"
        return verdict, "blocked"

    approved = application is not None and application.approval == "approved"
    if policy.submits_for_real and policy.require_approval and not approved:
        _queue(session, user, job, resume)
        verdict.notes.append("Waiting for your approval before it is sent.")
        return verdict, "queued"

    cover = session.scalars(
        select(CoverLetter).where(CoverLetter.user_id == user.id, CoverLetter.job_id == job.id)
    ).first()
    ats = (
        session.scalars(
            select(AtsAssessment).where(
                AtsAssessment.job_id == job.id, AtsAssessment.resume_id == resume.id
            )
        ).first()
        if resume is not None
        else None
    )
    packet = build_packet(
        user=user,
        job=job,
        channel=channel,
        resume=resume,
        cover_letter=cover,
        eligibility=eligibility,
        ats=ats,
    )

    try:
        result = submit(packet, dry_run=policy.dry_run, client=client, send=send_email)
    except Exception as exc:  # noqa: BLE001 - one bad submission is not the run
        logger.exception("submission failed for job %s", job.id)
        verdict.notes.append(f"submit[job {job.id}]: {type(exc).__name__}: {exc}")
        return verdict, "error"

    verdict.submission = result.to_dict()
    _record(session, user, job, resume, result)

    if result.ok and result.dry_run:
        return verdict, "would_submit"
    if result.ok:
        return verdict, "submitted"
    verdict.allowed = False
    verdict.blockers.append(result.message)
    return verdict, "assisted"


# ---------------------------------------------------------------------------
# The approval queue
# ---------------------------------------------------------------------------
class NotQueued(LookupError):
    pass


def approval_queue(session: Session, user: User) -> list[dict[str, Any]]:
    """Applications the autopilot has ready and is holding for a yes or no."""
    rows = session.execute(
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Application.user_id == user.id, Application.approval == "pending")
        .order_by(Application.id)
    ).all()
    out: list[dict[str, Any]] = []
    for application, job in rows:
        _eligibility, match, priority = _assessments(session, user, job.id)
        channel = channel_module.detect(
            url=job.url, description=job.description, source=job.source, company=job.company
        )
        out.append(
            {
                "job_id": job.id,
                "application_id": application.id,
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "tier": channel.tier.value,
                "resume_id": application.resume_id,
                "match_score": round(match.match_score, 1) if match else None,
                "priority": round(priority.overall, 1) if priority else None,
                "deadline_on": job.deadline_on.isoformat() if job.deadline_on else None,
            }
        )
    return out


def _pending(session: Session, user: User, job_id: int) -> tuple[Application, Job]:
    application = _application_for(session, user, job_id)
    job = session.get(Job, job_id)
    if application is None or job is None or application.approval != "pending":
        raise NotQueued(f"Job {job_id} is not waiting for approval.")
    return application, job


def approve(
    session: Session,
    user: User,
    job_id: int,
    *,
    client: HttpClient | None = None,
    send_email: Callable[[dict[str, str], tuple[str, bytes]], str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Say yes to one queued application: it is re-checked, then sent.

    The guardrails run again at this moment rather than trusting the earlier
    pass, so a deadline that passed or a cap that filled in the meantime still
    stops it.
    """
    application, job = _pending(session, user, job_id)
    application.approval = "approved"
    session.flush()

    existing = session.scalars(select(Application).where(Application.user_id == user.id)).all()
    verdict, outcome = _consider(
        session,
        user,
        job,
        load_policy(user),
        verified_evidence_ids=_verified_evidence(session, user),
        submitted_this_run=0,
        submitted_today=applications_today(existing, today),
        client=client,
        send_email=send_email,
        today=today,
        now=now,
    )
    if outcome not in ("submitted", "would_submit"):
        # Not sent after all: back in the queue, with the reason on record.
        application.approval = "pending"
        session.flush()
    return {"outcome": outcome, "verdict": verdict.to_dict()}


def decline(session: Session, user: User, job_id: int) -> dict[str, Any]:
    """Say no: the autopilot will not queue or send this one again."""
    application, _job = _pending(session, user, job_id)
    application.approval = "declined"
    session.add(
        ApplicationEvent(
            application_id=application.id,
            from_status=application.status,
            to_status=application.status,
            at=_now(),
            detail="You passed on this one. The autopilot will not send it.",
            source="user",
        )
    )
    session.flush()
    return {"outcome": "declined", "job_id": job_id}


def run_once(
    session: Session,
    user: User | None = None,
    *,
    policy: AutopilotPolicy | None = None,
    registry: SourceRegistry | None = None,
    client: HttpClient | None = None,
    send_email: Callable[[dict[str, str], tuple[str, bytes]], str] | None = None,
    discover_jobs: bool = True,
    track_email: bool = True,
    mail_client: Any | None = None,
    tailor_limit: int = DEFAULT_TAILOR_LIMIT,
    consider_limit: int = DEFAULT_CONSIDER_LIMIT,
    use_ai: bool = True,
    today: date | None = None,
    now: datetime | None = None,
) -> PassReport:
    """One full pass. Never raises for a single job or a single provider."""
    from careeros.pipeline import Pipeline
    from careeros.services import load_user
    from careeros.sources import build_registry
    from careeros.sources.discover import discover

    report = PassReport(started_at=_now())
    user = user or load_user(session)
    if user is None:
        report.skipped_reason = "no profile on this server yet"
        report.finished_at = _now()
        return report

    active = policy or load_policy(user)
    report.policy = active.to_dict()

    if not active.enabled:
        report.skipped_reason = (
            "autopilot is off — `careeros autopilot enable` turns it on (dry run first)"
        )
        report.finished_at = _now()
        return report

    window = in_quiet_hours(active, now)
    if window:
        report.skipped_reason = f"inside quiet hours {window}"
        report.finished_at = _now()
        return report

    pipeline = Pipeline(session)
    built = registry or build_registry(into=SourceRegistry(), client=client)

    # -- 1. find jobs ------------------------------------------------------
    if discover_jobs:
        try:
            report.discovery = discover(session, user, registry=built, client=client)
        except Exception as exc:  # noqa: BLE001 - discovery must not end the pass
            report.errors.append(f"discover: {type(exc).__name__}: {exc}")
            logger.exception("discovery failed")

    # -- 2. classify and rank ---------------------------------------------
    try:
        stats = pipeline.classify_jobs()
        report.classified = getattr(stats, "classified", 0) or 0
        pipeline.assess_for_user(user, today=today)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"assess: {type(exc).__name__}: {exc}")
        logger.exception("assessment failed")

    ranked = pipeline.ranked_jobs(limit=consider_limit)

    # -- 3. tailor for the strongest candidates ---------------------------
    for job in ranked[:tailor_limit]:
        if job.priority and job.priority.eligibility_score <= 0:
            continue
        if _latest_resume(session, user, job.id) is not None:
            continue
        try:
            pipeline.tailor_for_job(user, job, use_ai=use_ai)
            report.tailored += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"tailor[job {job.id}]: {type(exc).__name__}: {exc}")

    # -- 4. apply, in priority order --------------------------------------
    verified = _verified_evidence(session, user)
    existing = session.scalars(select(Application).where(Application.user_id == user.id)).all()
    submitted_today = applications_today(existing, today)
    submitted_this_run = 0

    for job in ranked:
        report.considered += 1
        verdict, outcome = _consider(
            session,
            user,
            job,
            active,
            verified_evidence_ids=verified,
            submitted_this_run=submitted_this_run,
            submitted_today=submitted_today,
            client=client,
            send_email=send_email,
            today=today,
            now=now,
        )
        if outcome == "error":
            report.errors.append(verdict.notes.pop())
        elif outcome == "assisted":
            report.assisted += 1
        elif outcome == "queued":
            report.awaiting_approval += 1
            # A queued job holds its place under the run cap, so approving the
            # whole queue can never exceed what one pass would have sent.
            submitted_this_run += 1
        elif outcome == "would_submit":
            report.would_submit += 1
            submitted_this_run += 1
        elif outcome == "submitted":
            report.submitted += 1
            submitted_this_run += 1
            submitted_today += 1
        report.verdicts.append(verdict)

    # -- 5. track replies -------------------------------------------------
    if track_email:
        mailbox = mail_client if mail_client is not None else _default_mailbox()
        if mailbox is None:
            report.tracking = {"skipped": "no mailbox connected"}
        else:
            try:
                from careeros.gmail.sync import GmailSync

                stats = GmailSync(session, mailbox).sync(user, use_ai=use_ai)
                report.tracking = stats.to_dict()
            except Exception as exc:  # noqa: BLE001 - mail must not end the pass
                report.errors.append(f"track: {type(exc).__name__}: {exc}")
                logger.exception("email tracking failed")

    report.finished_at = _now()

    run = PipelineRun(
        started_at=report.started_at,
        finished_at=report.finished_at,
        ok=not report.errors,
        stats=report.to_dict()["counts"],
        errors=report.errors,
    )
    session.add(run)
    session.flush()
    return report


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def run_forever(
    session_factory: Callable[[], Session],
    *,
    interval_hours: float | None = None,
    max_passes: int | None = None,
    on_report: Callable[[PassReport], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    **pass_options: Any,
) -> list[PassReport]:
    """Run a pass, sleep, repeat -- the in-process version of the 4-hour cycle.

    Fine for a laptop or a long-lived container. For a machine that reboots,
    `careeros autopilot once` under cron or a systemd timer is sturdier, and
    `careeros autopilot install` prints both.

    `pass_options` go straight through to `run_once`, so the loop honours the
    same switches as a single pass -- `discover_jobs=False` and `use_ai=False`
    included.
    """
    reports: list[PassReport] = []
    passes = 0

    while max_passes is None or passes < max_passes:
        passes += 1
        session: Session | None = None
        try:
            # Opening the session is inside the try on purpose: a database that
            # is briefly unreachable is exactly when the loop must survive.
            session = session_factory()
            with RunLock():
                report = run_once(session, **pass_options)
                session.commit()
        except AlreadyRunning as exc:
            report = PassReport(started_at=_now(), finished_at=_now())
            report.skipped_reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - the loop outlives one bad pass
            report = PassReport(started_at=_now(), finished_at=_now())
            report.errors.append(f"{type(exc).__name__}: {exc}")
            logger.exception("autopilot pass failed")
            if session is not None:
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - already failing
                    pass
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass

        reports.append(report)
        if on_report:
            on_report(report)

        if max_passes is not None and passes >= max_passes:
            break

        hours = interval_hours or (report.policy.get("interval_hours") or 4.0)
        sleeper(max(60.0, float(hours) * 3600.0))

    return reports
