"""Job prioritisation: match + eligibility + urgency + competition.

The brief's hard rule is implemented literally: *if today is the final
application date, the job appears at the top*. That is not left to a weighted
score to approximate -- `sort_key` puts a deadline-today job in its own tier
above everything else, and the weighted score only orders jobs within a tier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from careeros.enums import AuthVerdict, DeadlineBucket

WEIGHTS = {"match": 0.35, "eligibility": 0.30, "urgency": 0.25, "competition": 0.10}

#: Tier order used by `sort_key`. Lower sorts first.
BUCKET_TIER = {
    DeadlineBucket.TODAY: 0,
    DeadlineBucket.WITHIN_48H: 1,
    DeadlineBucket.WITHIN_7D: 2,
    DeadlineBucket.FUTURE: 3,
    DeadlineBucket.NONE: 3,
    DeadlineBucket.EXPIRED: 9,
}

URGENCY_BY_BUCKET = {
    DeadlineBucket.TODAY: 100.0,
    DeadlineBucket.WITHIN_48H: 90.0,
    DeadlineBucket.WITHIN_7D: 70.0,
    DeadlineBucket.FUTURE: 40.0,
    DeadlineBucket.NONE: 30.0,
    DeadlineBucket.EXPIRED: 0.0,
}

#: Applied when the work-authorization verdict rules the job out. The job stays
#: visible (the verdict may be wrong, and the user may want to challenge it)
#: but it must not outrank anything actionable.
INELIGIBLE_DAMPING = 0.25


@dataclass
class PriorityResult:
    match_score: float = 0.0
    eligibility_score: float = 0.0
    urgency_score: float = 0.0
    competition_score: float = 0.0
    overall: float = 0.0
    deadline_bucket: DeadlineBucket = DeadlineBucket.NONE
    days_to_deadline: int | None = None
    days_since_posted: int | None = None
    explanation: list[str] = field(default_factory=list)

    @property
    def flag(self) -> str:
        return f"{self.deadline_bucket.emoji} {self.deadline_bucket.label}"

    def sort_key(self) -> tuple[int, float, int]:
        """Deadline tier first, then score. Deadline-today always floats up."""
        tier = BUCKET_TIER[self.deadline_bucket]
        if self.eligibility_score <= 0 and tier < 9:
            tier = 8   # ruled out: below every actionable job, above expired
        return (tier, -self.overall, self.days_to_deadline if self.days_to_deadline is not None else 9999)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["deadline_bucket"] = self.deadline_bucket.value
        d["flag"] = self.flag
        return d


def classify_deadline(deadline_on: date | None, today: date | None = None) -> tuple[DeadlineBucket, int | None]:
    today = today or date.today()
    if deadline_on is None:
        return DeadlineBucket.NONE, None
    days = (deadline_on - today).days
    if days < 0:
        return DeadlineBucket.EXPIRED, days
    if days == 0:
        return DeadlineBucket.TODAY, 0
    if days <= 2:
        return DeadlineBucket.WITHIN_48H, days
    if days <= 7:
        return DeadlineBucket.WITHIN_7D, days
    return DeadlineBucket.FUTURE, days


def competition_score(applicant_count: int | None) -> float:
    """Fewer applicants is a better bet. Unknown is neutral, not optimistic."""
    if applicant_count is None:
        return 50.0
    if applicant_count <= 10:
        return 95.0
    if applicant_count <= 25:
        return 85.0
    if applicant_count <= 50:
        return 70.0
    if applicant_count <= 100:
        return 55.0
    if applicant_count <= 250:
        return 40.0
    if applicant_count <= 500:
        return 25.0
    return 12.0


class PriorityEngine:
    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = weights or WEIGHTS

    def score(
        self,
        match_score: float,
        eligibility_score: float,
        verdict: AuthVerdict | None = None,
        deadline_on: date | None = None,
        posted_on: date | None = None,
        applicant_count: int | None = None,
        today: date | None = None,
    ) -> PriorityResult:
        today = today or date.today()
        bucket, days_left = classify_deadline(deadline_on, today)

        urgency = URGENCY_BY_BUCKET[bucket]
        days_since_posted = (today - posted_on).days if posted_on else None
        if bucket in (DeadlineBucket.NONE, DeadlineBucket.FUTURE) and days_since_posted is not None:
            # No stated deadline: freshness is the proxy for urgency, since the
            # early applicants to a new posting are the ones a recruiter reads.
            if days_since_posted <= 1:
                urgency = max(urgency, 75.0)
            elif days_since_posted <= 3:
                urgency = max(urgency, 65.0)
            elif days_since_posted <= 7:
                urgency = max(urgency, 50.0)
            elif days_since_posted > 30:
                urgency = min(urgency, 20.0)

        competition = competition_score(applicant_count)

        overall = (
            self.weights["match"] * match_score
            + self.weights["eligibility"] * eligibility_score
            + self.weights["urgency"] * urgency
            + self.weights["competition"] * competition
        )

        explanation: list[str] = [
            f"Match {match_score:.0f}/100 (weight {self.weights['match']:.0%})",
            f"Eligibility {eligibility_score:.0f}/100 (weight {self.weights['eligibility']:.0%})",
            f"Urgency {urgency:.0f}/100 - {bucket.label.lower()}"
            + (f", {days_left} day(s) left" if days_left is not None and days_left >= 0 else ""),
            f"Competition {competition:.0f}/100"
            + (f" - {applicant_count} applicants" if applicant_count is not None else " - applicant count unknown"),
        ]

        if bucket is DeadlineBucket.EXPIRED:
            overall = 0.0
            explanation.append("Deadline has passed - archived from the actionable list.")
        elif eligibility_score <= 0:
            overall *= INELIGIBLE_DAMPING
            explanation.append(
                f"Work-authorization verdict is '{(verdict or AuthVerdict.NOT_COMPATIBLE).value}' - "
                "de-prioritised but kept visible for review."
            )
        elif bucket is DeadlineBucket.TODAY:
            explanation.append("FINAL APPLICATION DATE IS TODAY - pinned to the top of the list.")

        return PriorityResult(
            match_score=round(match_score, 1),
            eligibility_score=round(eligibility_score, 1),
            urgency_score=round(urgency, 1),
            competition_score=round(competition, 1),
            overall=round(overall, 1),
            deadline_bucket=bucket,
            days_to_deadline=days_left,
            days_since_posted=days_since_posted,
            explanation=explanation,
        )
