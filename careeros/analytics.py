"""Career intelligence: funnel analytics, per-track outcomes, and the learning loop.

The learning loop closes the circle the brief describes: *which career tracks
are actually producing results, and what should change because of it.*

Two honesty rules hold throughout:

* A rate computed from a handful of applications is labelled low-confidence
  rather than presented as a finding. Six applications do not tell you your
  SAP track is dead.
* Recommendations may suggest a skill to learn, a certification to consider, a
  resume change or a different job target. They never suggest claiming a
  qualification the user does not hold.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.db.models import (
    Application,
    CareerTrack,
    EligibilityAssessment,
    Job,
    JobClassification,
    MatchAssessment,
    RejectionAnalysis,
)
from careeros.enums import APPLICATION_FUNNEL, ApplicationStatus

#: Below this many applications, a conversion rate is noise.
MIN_SAMPLE_FOR_CONFIDENCE = 10

_RESPONDED = {
    ApplicationStatus.APPLICATION_RECEIVED, ApplicationStatus.RECRUITER_CONTACT,
    ApplicationStatus.RECRUITER_SCREEN, ApplicationStatus.INTERVIEW,
    ApplicationStatus.TECHNICAL_INTERVIEW, ApplicationStatus.ASSESSMENT,
    ApplicationStatus.HIRING_MANAGER, ApplicationStatus.FINAL_ROUND,
    ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED,
}
_INTERVIEWED = {
    ApplicationStatus.INTERVIEW, ApplicationStatus.TECHNICAL_INTERVIEW,
    ApplicationStatus.HIRING_MANAGER, ApplicationStatus.FINAL_ROUND,
    ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED,
}
_OFFERED = {ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED}

#: `applied` onwards -- a discovered job that was never submitted is not a
#: failed application and must not drag the response rate down.
_SUBMITTED_FROM = APPLICATION_FUNNEL.index(ApplicationStatus.APPLIED)
_SUBMITTED = set(APPLICATION_FUNNEL[_SUBMITTED_FROM:]) | {
    ApplicationStatus.REJECTED, ApplicationStatus.NO_RESPONSE
}


@dataclass
class FunnelStats:
    label: str
    applications: int = 0
    responses: int = 0
    interviews: int = 0
    assessments: int = 0
    offers: int = 0
    rejections: int = 0

    @property
    def response_rate(self) -> float:
        return round(100 * self.responses / self.applications, 1) if self.applications else 0.0

    @property
    def interview_rate(self) -> float:
        return round(100 * self.interviews / self.applications, 1) if self.applications else 0.0

    @property
    def offer_rate(self) -> float:
        return round(100 * self.offers / self.applications, 1) if self.applications else 0.0

    @property
    def confident(self) -> bool:
        return self.applications >= MIN_SAMPLE_FOR_CONFIDENCE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            response_rate=self.response_rate,
            interview_rate=self.interview_rate,
            offer_rate=self.offer_rate,
            confident=self.confident,
            note=None if self.confident else
            f"Only {self.applications} application(s) - rates are indicative, not conclusive.",
        )
        return d


@dataclass
class LearningSignal:
    kind: str            # skill_to_learn | certification | resume_change | targeting
    subject: str
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _status(application: Application) -> ApplicationStatus:
    try:
        return ApplicationStatus(application.status)
    except ValueError:
        return ApplicationStatus.DISCOVERED


def _accumulate(stats: FunnelStats, application: Application) -> None:
    status = _status(application)
    if status not in _SUBMITTED:
        return
    stats.applications += 1
    if status in _RESPONDED:
        stats.responses += 1
    if status in _INTERVIEWED:
        stats.interviews += 1
    if status is ApplicationStatus.ASSESSMENT:
        stats.assessments += 1
    if status in _OFFERED:
        stats.offers += 1
    if status is ApplicationStatus.REJECTED:
        stats.rejections += 1


class Analytics:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _rows(self, user_id: int) -> list[tuple[Application, Job, JobClassification | None]]:
        return [
            (a, j, j.classification)
            for a, j in self.session.execute(
                select(Application, Job).join(Job, Job.id == Application.job_id)
                .where(Application.user_id == user_id)
            ).all()
        ]

    # -- headline -----------------------------------------------------------
    def overall(self, user_id: int) -> dict[str, Any]:
        stats = FunnelStats(label="All")
        pipeline = Counter()
        for application, _job, _cl in self._rows(user_id):
            _accumulate(stats, application)
            pipeline[application.status] += 1
        payload = stats.to_dict()
        payload["pipeline"] = dict(pipeline)
        return payload

    def _grouped(self, user_id: int, key_fn, label_fn=None) -> list[dict[str, Any]]:
        buckets: dict[Any, FunnelStats] = {}
        for application, job, classification in self._rows(user_id):
            key = key_fn(application, job, classification)
            if key is None:
                continue
            label = label_fn(key) if label_fn else str(key)
            stats = buckets.setdefault(key, FunnelStats(label=label))
            _accumulate(stats, application)
        return sorted(
            (s.to_dict() for s in buckets.values()),
            key=lambda d: (-d["offers"], -d["interviews"], -d["applications"]),
        )

    # -- breakdowns ---------------------------------------------------------
    def by_track(self, user_id: int) -> list[dict[str, Any]]:
        names = {
            t.id: t.name
            for t in self.session.scalars(select(CareerTrack).where(CareerTrack.user_id == user_id)).all()
        }
        return self._grouped(
            user_id,
            lambda a, j, c: a.track_id,
            lambda tid: names.get(tid, f"Track {tid}"),
        )

    def by_domain(self, user_id: int) -> list[dict[str, Any]]:
        return self._grouped(user_id, lambda a, j, c: c.domain_label if c else None)

    def by_country(self, user_id: int) -> list[dict[str, Any]]:
        return self._grouped(user_id, lambda a, j, c: j.country_code)

    def by_company(self, user_id: int) -> list[dict[str, Any]]:
        return self._grouped(user_id, lambda a, j, c: j.company)

    def by_source(self, user_id: int) -> list[dict[str, Any]]:
        return self._grouped(user_id, lambda a, j, c: j.source)

    def by_employment_type(self, user_id: int) -> list[dict[str, Any]]:
        return self._grouped(user_id, lambda a, j, c: j.employment_type)

    def by_match_band(self, user_id: int) -> list[dict[str, Any]]:
        def band(_a, job, _c):
            score = job.priority.match_score if job.priority else None
            if score is None:
                return None
            floor = int(score // 10) * 10
            return f"{floor}-{floor + 9}%"

        return self._grouped(user_id, band)

    def by_visa_verdict(self, user_id: int) -> list[dict[str, Any]]:
        verdicts = {
            r.job_id: r.verdict
            for r in self.session.scalars(
                select(EligibilityAssessment).where(EligibilityAssessment.user_id == user_id)
            ).all()
        }
        return self._grouped(user_id, lambda a, j, c: verdicts.get(j.id))

    def rejection_reasons(self, user_id: int) -> dict[str, Any]:
        rows = self.session.execute(
            select(RejectionAnalysis, Application)
            .join(Application, Application.id == RejectionAnalysis.application_id)
            .where(Application.user_id == user_id)
        ).all()
        explicit = [r.explicit_reason for r, _a in rows if r.explicit_reason]
        hypotheses = Counter(
            item["reason"] for r, _a in rows for item in (r.possible_reasons or [])
        )
        return {
            "total": len(rows),
            "explicit_reasons": explicit,
            "explicit_count": len(explicit),
            "hypotheses": [
                {"reason": reason, "count": count,
                 "disclaimer": "CareerOS hypothesis - not stated by the employer."}
                for reason, count in hypotheses.most_common()
            ],
            "by_stage": dict(Counter(r.stage for r, _a in rows if r.stage)),
        }

    # -- learning loop ------------------------------------------------------
    def learning_signals(self, user_id: int, top_n: int = 8) -> list[LearningSignal]:
        """Turn outcome history into concrete, honest next actions."""
        signals: list[LearningSignal] = []
        rows = self._rows(user_id)

        # 1. Requirements that keep appearing as gaps on the jobs being targeted.
        #    Counted per distinct JOB: the same gap is recorded once per career
        #    track, and counting rows would multiply every gap by the number of
        #    tracks the user runs.
        gap_jobs: dict[str, set[int]] = defaultdict(set)
        gap_tracks: dict[str, set[str]] = defaultdict(set)
        track_names = {
            t.id: t.name
            for t in self.session.scalars(select(CareerTrack).where(CareerTrack.user_id == user_id)).all()
        }
        job_ids = [j.id for _a, j, _c in rows]
        if job_ids:
            for match in self.session.scalars(
                select(MatchAssessment).where(MatchAssessment.job_id.in_(job_ids))
            ).all():
                for gap in match.gaps or []:
                    gap_jobs[gap].add(match.job_id)
                    gap_tracks[gap].add(track_names.get(match.track_id, ""))

        ranked_gaps = sorted(gap_jobs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for gap, jobs_with_gap in ranked_gaps[:top_n]:
            count = len(jobs_with_gap)
            if count < 2:
                continue
            signals.append(
                LearningSignal(
                    kind="skill_to_learn",
                    subject=gap,
                    rationale=(
                        f"Named as a requirement on {count} targeted job(s) with no supporting "
                        f"evidence in your Career Evidence Database."
                    ),
                    evidence={
                        "jobs": count,
                        "job_ids": sorted(jobs_with_gap),
                        "tracks": sorted(t for t in gap_tracks[gap] if t),
                    },
                    confidence="medium" if count >= 4 else "low",
                )
            )

        # 2. Certifications the postings keep asking for -- excluding any the
        #    user already holds, which would otherwise be the loudest signal.
        from careeros.db.models import Certification
        from careeros.engines.textutil import normalize

        held = {
            normalize(c.name)
            for c in self.session.scalars(
                select(Certification).where(Certification.user_id == user_id)
            ).all()
        }
        cert_counter: Counter[str] = Counter()
        for _a, _j, classification in rows:
            for cert in (classification.required_certifications if classification else []) or []:
                key = normalize(cert)
                if any(key in h or h in key for h in held):
                    continue
                cert_counter[cert] += 1
        for cert, count in cert_counter.most_common(3):
            if count < 2:
                continue
            signals.append(
                LearningSignal(
                    kind="certification",
                    subject=cert.upper(),
                    rationale=f"Requested by {count} targeted posting(s).",
                    evidence={"jobs": count},
                    confidence="medium" if count >= 4 else "low",
                )
            )

        # 3. Track performance -- only once there is enough history to mean it.
        track_stats = self.by_track(user_id)
        confident = [t for t in track_stats if t["confident"]]
        if len(confident) >= 2:
            best, worst = confident[0], confident[-1]
            if best["interview_rate"] > worst["interview_rate"] + 15:
                signals.append(
                    LearningSignal(
                        kind="targeting",
                        subject=best["label"],
                        rationale=(
                            f"{best['label']} converts at {best['interview_rate']:.0f}% to interview "
                            f"vs {worst['interview_rate']:.0f}% for {worst['label']}. Weight your "
                            f"daily applications towards it."
                        ),
                        evidence={"best": best["label"], "worst": worst["label"]},
                        confidence="medium",
                    )
                )

        # 4. Resume/ATS signal: applications that never got a response despite
        #    a strong match point at the document, not at the fit.
        silent_strong = [
            j for a, j, _c in rows
            if _status(a) in {ApplicationStatus.APPLIED, ApplicationStatus.NO_RESPONSE}
            and j.priority and j.priority.match_score >= 75
        ]
        if len(silent_strong) >= 3:
            signals.append(
                LearningSignal(
                    kind="resume_change",
                    subject="ATS keyword coverage",
                    rationale=(
                        f"{len(silent_strong)} strong-match application(s) drew no response. "
                        "Check the ATS keyword gaps on those jobs before widening your search."
                    ),
                    evidence={"jobs": [j.id for j in silent_strong][:10]},
                    confidence="low",
                )
            )
        return signals[:top_n]

    def dashboard(self, user_id: int) -> dict[str, Any]:
        return {
            "overall": self.overall(user_id),
            "by_track": self.by_track(user_id),
            "by_domain": self.by_domain(user_id),
            "by_country": self.by_country(user_id),
            "by_company": self.by_company(user_id)[:10],
            "by_source": self.by_source(user_id),
            "by_employment_type": self.by_employment_type(user_id),
            "by_match_band": self.by_match_band(user_id),
            "by_visa_verdict": self.by_visa_verdict(user_id),
            "rejections": self.rejection_reasons(user_id),
            "learning_signals": [s.to_dict() for s in self.learning_signals(user_id)],
        }
