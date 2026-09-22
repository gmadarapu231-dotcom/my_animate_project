"""Whether auto-apply may fire for one job, and if not, exactly why.

Automatic submission is the one action in this system that reaches a stranger
and cannot be taken back. So the decision is deterministic, ordered, and
explains itself: every refusal names the rule it failed, which is what makes a
4-hour cycle auditable rather than alarming.

Two defaults matter. `enabled` is false and `dry_run` is true, so installing
this changes nothing until the user says so -- and the first thing they get is
a report of what it *would* have sent.

Three gates are not configurable, because switching them off would be a
different product:

* the tailored résumé must have passed the factuality checker;
* it must cite at least one **verified** evidence item -- a résumé assembled
  from unconfirmed parse output must never be sent to an employer;
* a work-authorization verdict of `not_compatible` stops the application, since
  applying anyway wastes everyone's time and misrepresents the applicant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from careeros.apply.channels import Channel, SubmitTier
from careeros.enums import ApplicationStatus, AuthVerdict

#: Verdicts that stop an application outright, whatever the policy says.
HARD_BLOCK_VERDICTS = (AuthVerdict.NOT_COMPATIBLE.value,)


@dataclass
class AutopilotPolicy:
    """The user's rules. Stored as JSON on their profile."""

    enabled: bool = False
    #: Assemble and report, submit nothing. The honest default for a new user.
    dry_run: bool = True

    interval_hours: float = 4.0
    max_per_run: int = 5
    max_per_day: int = 15

    min_match_score: float = 70.0
    min_priority_score: float = 0.0
    #: Which submission tiers may be automated. ASSISTED is never automatable.
    channels: tuple[str, ...] = (SubmitTier.API.value, SubmitTier.EMAIL.value)

    #: An UNKNOWN sponsorship verdict is the common case, and refusing it would
    #: rule out most postings. It is allowed, and the application says nothing
    #: about status that the profile does not.
    allow_unknown_verdict: bool = True
    require_deadline_open: bool = True

    company_blocklist: tuple[str, ...] = ()
    company_allowlist: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    min_salary: float | None = None
    skip_if_applicants_over: int | None = None

    #: Local hours during which nothing is submitted, as "HH:MM-HH:MM".
    quiet_hours: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AutopilotPolicy":
        raw = dict(raw or {})
        fields = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
        for name in (
            "channels", "company_blocklist", "company_allowlist",
            "domains", "countries", "quiet_hours",
        ):
            if name in fields and fields[name] is not None:
                fields[name] = tuple(fields[name])
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, tuple):
                out[key] = list(value)
        return out

    @property
    def submits_for_real(self) -> bool:
        return self.enabled and not self.dry_run

    def summary(self) -> str:
        if not self.enabled:
            return "Autopilot is off. It searches nothing and applies to nothing."
        mode = "assembling only (dry run)" if self.dry_run else "submitting"
        return (
            f"Every {self.interval_hours:g}h: search, rank, then {mode} up to "
            f"{self.max_per_run} per run and {self.max_per_day} per day, at "
            f"match {self.min_match_score:g}+ via {', '.join(self.channels) or 'no channel'}."
        )


@dataclass
class Decision:
    """Whether this job may be auto-applied to, and the reasoning either way."""

    allowed: bool
    job_id: int | None = None
    tier: str | None = None
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "job_id": self.job_id,
            "tier": self.tier,
            "blockers": self.blockers,
            "notes": self.notes,
        }


def cited_evidence_ids(factuality: Any | None) -> set[int]:
    """Every evidence id the finished résumé's claims rest on.

    The résumé row does not carry these; the factuality report does, because
    the checker is what traced each claim back to its support. Reading them
    from there rather than from the document is the point -- it is the same
    set the gate was computed from.
    """
    out: set[int] = set()
    for claim in getattr(factuality, "claims", None) or []:
        if isinstance(claim, dict):
            for value in claim.get("evidence_ids") or []:
                try:
                    out.add(int(value))
                except (TypeError, ValueError):
                    continue
    return out


def _name_matches(name: str | None, patterns: Iterable[str]) -> bool:
    target = (name or "").strip().lower()
    if not target:
        return False
    return any(p.strip().lower() in target for p in patterns if p.strip())


def in_quiet_hours(policy: AutopilotPolicy, now: datetime | None = None) -> str | None:
    """The quiet window currently in force, if any."""
    if not policy.quiet_hours:
        return None
    moment = (now or datetime.now()).time()
    for window in policy.quiet_hours:
        try:
            start_text, end_text = window.split("-", 1)
            start = datetime.strptime(start_text.strip(), "%H:%M").time()
            end = datetime.strptime(end_text.strip(), "%H:%M").time()
        except ValueError:
            continue
        inside = start <= moment < end if start <= end else (moment >= start or moment < end)
        if inside:
            return window
    return None


def applications_today(applications: Sequence[Any], today: date | None = None) -> int:
    day = today or date.today()
    return sum(
        1
        for row in applications
        if getattr(row, "applied_on", None) == day
        and getattr(row, "status", "") not in ("", ApplicationStatus.DISCOVERED.value)
    )


def evaluate(
    *,
    policy: AutopilotPolicy,
    job: Any,
    channel: Channel,
    resume: Any | None,
    factuality: Any | None,
    eligibility: Any | None = None,
    match: Any | None = None,
    priority: Any | None = None,
    verified_evidence_ids: set[int] | None = None,
    application: Any | None = None,
    submitted_this_run: int = 0,
    submitted_today: int = 0,
    today: date | None = None,
    now: datetime | None = None,
) -> Decision:
    """The full check, in order. Every refusal names its rule."""
    decision = Decision(allowed=False, job_id=getattr(job, "id", None), tier=channel.tier.value)
    blockers = decision.blockers
    day = today or date.today()

    # -- switches ----------------------------------------------------------
    if not policy.enabled:
        blockers.append("autopilot is off")
        return decision

    window = in_quiet_hours(policy, now)
    if window:
        blockers.append(f"inside quiet hours {window}")

    # -- caps --------------------------------------------------------------
    if submitted_this_run >= policy.max_per_run:
        blockers.append(f"run cap reached ({policy.max_per_run})")
    if submitted_today >= policy.max_per_day:
        blockers.append(f"daily cap reached ({policy.max_per_day})")

    # -- already handled ---------------------------------------------------
    status = getattr(application, "status", None)
    terminal = {
        ApplicationStatus.APPLIED.value,
        ApplicationStatus.APPLICATION_RECEIVED.value,
        ApplicationStatus.WITHDRAWN.value,
        ApplicationStatus.REJECTED.value,
        ApplicationStatus.ACCEPTED.value,
        ApplicationStatus.OFFER.value,
    }
    if status in terminal:
        blockers.append(f"already {status}")

    # -- channel -----------------------------------------------------------
    if not channel.tier.automatable:
        blockers.append(f"{channel.tier.value}: {channel.reason or channel.tier.label}")
    elif channel.tier.value not in policy.channels:
        blockers.append(f"channel {channel.tier.value} not enabled in your policy")

    # -- work authorization: not negotiable --------------------------------
    # Eligibility, match and priority live in their own tables keyed by
    # (user, job), so they are passed in rather than read off the Job.
    verdict = getattr(eligibility, "verdict", None)
    if verdict in HARD_BLOCK_VERDICTS:
        blockers.append(f"work-authorization verdict is {verdict}")
    elif verdict == AuthVerdict.UNKNOWN.value and not policy.allow_unknown_verdict:
        blockers.append("sponsorship is unknown and your policy requires a clear verdict")
    elif verdict == AuthVerdict.UNKNOWN.value:
        decision.notes.append(
            "Sponsorship is not stated in this posting. The application says nothing "
            "about your status beyond what your profile holds — verify with the employer."
        )

    # -- résumé integrity: not negotiable ----------------------------------
    if resume is None:
        blockers.append("no tailored résumé for this job yet")
    else:
        if not getattr(resume, "is_final", False):
            blockers.append("the tailored résumé has not passed the factuality check")
        if factuality is not None and not getattr(factuality, "passed", False):
            unsupported = getattr(factuality, "unsupported_count", 0)
            blockers.append(f"factuality check failed ({unsupported} unsupported claim(s))")
        cited = cited_evidence_ids(factuality)
        if verified_evidence_ids is not None:
            if not cited:
                blockers.append("the résumé cites no evidence")
            elif not (cited & verified_evidence_ids):
                blockers.append(
                    "every claim on this résumé rests on unverified evidence — confirm it first "
                    "(careeros verify-evidence --list)"
                )
            elif cited - verified_evidence_ids:
                decision.notes.append(
                    f"{len(cited - verified_evidence_ids)} cited item(s) are still unverified."
                )

    # -- scores ------------------------------------------------------------
    score = getattr(match, "match_score", None)
    if score is None:
        blockers.append("no match score yet")
    elif score < policy.min_match_score:
        blockers.append(f"match {score:.1f} is below your floor of {policy.min_match_score:g}")

    priority_score = getattr(priority, "overall", None)
    if policy.min_priority_score and (
        priority_score is None or priority_score < policy.min_priority_score
    ):
        blockers.append(
            f"priority {priority_score} is below your floor of {policy.min_priority_score:g}"
        )

    # -- the posting itself ------------------------------------------------
    deadline = getattr(job, "deadline_on", None)
    if policy.require_deadline_open and deadline and deadline < day:
        blockers.append(f"the deadline passed on {deadline}")

    company = getattr(job, "company", None)
    if policy.company_blocklist and _name_matches(company, policy.company_blocklist):
        blockers.append(f"{company} is on your blocklist")
    if policy.company_allowlist and not _name_matches(company, policy.company_allowlist):
        blockers.append(f"{company} is not on your allowlist")

    if policy.domains:
        domain = getattr(getattr(job, "classification", None), "domain_id", None)
        if domain not in policy.domains:
            blockers.append(f"domain {domain} is not in your allowed set")

    if policy.countries:
        # Job.country_code is the normalised ISO code; there is no `country`.
        country = getattr(job, "country_code", None)
        if (country or "").upper() not in {c.upper() for c in policy.countries}:
            blockers.append(f"country {country} is not in your allowed set")

    if policy.min_salary is not None:
        top = getattr(job, "salary_max", None) or getattr(job, "salary_min", None)
        if top is None:
            decision.notes.append("No salary published, so your floor could not be applied.")
        elif top < policy.min_salary:
            blockers.append(f"salary {top:.0f} is below your floor of {policy.min_salary:.0f}")

    if policy.skip_if_applicants_over is not None:
        applicants = getattr(job, "applicant_count", None)
        if applicants and applicants > policy.skip_if_applicants_over:
            blockers.append(
                f"{applicants} applicants, over your limit of {policy.skip_if_applicants_over}"
            )

    decision.allowed = not blockers
    if decision.allowed and policy.dry_run:
        decision.notes.append("Dry run: this would be submitted, but nothing is sent.")
    return decision
