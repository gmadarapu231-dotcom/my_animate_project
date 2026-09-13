"""Work-authorization and employment-type eligibility.

Design rules, taken straight from the product brief and enforced in code:

* **Never assume sponsorship.** Silence in a JD yields `UNKNOWN`, not a guess.
* **Always show the source.** Every verdict carries the matched phrase and a
  quotable excerpt from the posting.
* **No hard-coded immigration law.** Statuses, the phrases that matter and what
  each phrase implies all live in the country pack. This module only applies
  the pack's rules; it encodes no legal conclusions of its own.
* **Always disclaim.** Output is an AI assessment, and says so.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from careeros.config import AuthStatus, CountryPack, countries
from careeros.engines.textutil import find_phrase, normalize
from careeros.enums import AuthVerdict

DISCLAIMER = "AI assessment - verify with employer/recruiter."

#: Verdict -> eligibility score fed to the priority engine.
VERDICT_SCORES = {
    AuthVerdict.COMPATIBLE: 100.0,
    AuthVerdict.POTENTIALLY_COMPATIBLE: 75.0,
    AuthVerdict.UNKNOWN: 50.0,
    AuthVerdict.NOT_COMPATIBLE: 0.0,
}


@dataclass
class SignalHit:
    signal: str
    severity: str
    pattern: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EligibilityResult:
    verdict: AuthVerdict
    score: float
    user_status_id: str
    user_status_label: str
    country_code: str
    employment_type_match: bool = True
    detected_employment_type: str | None = None
    signals: list[SignalHit] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    @property
    def evidence(self) -> list[dict[str, str]]:
        """The JD excerpts behind the verdict -- rendered next to it in the UI."""
        return [{"signal": s.signal, "quote": s.excerpt} for s in self.signals]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "score": self.score,
            "user_status_id": self.user_status_id,
            "user_status_label": self.user_status_label,
            "country_code": self.country_code,
            "employment_type_match": self.employment_type_match,
            "detected_employment_type": self.detected_employment_type,
            "signals": [s.to_dict() for s in self.signals],
            "evidence": self.evidence,
            "reasons": self.reasons,
            "disclaimer": self.disclaimer,
        }


class WorkAuthorizationEngine:
    """Evaluates one job against one user status, using one country pack."""

    def __init__(self, pack: CountryPack) -> None:
        self.pack = pack

    # -- signal detection ---------------------------------------------------
    def detect_signals(self, description: str) -> list[SignalHit]:
        norm = normalize(description)
        hits: list[SignalHit] = []
        for signal, spec in self.pack.jd_signals.items():
            if signal == "employment_type_hints":
                continue
            severity = spec.get("severity", "informational")
            for pattern in spec.get("patterns", []):
                hit = find_phrase(norm, pattern)
                if hit:
                    hits.append(
                        SignalHit(
                            signal=signal,
                            severity=severity,
                            pattern=hit.phrase,
                            excerpt=hit.excerpt,
                        )
                    )
                    break   # one quote per signal is enough evidence
        return hits

    def detect_employment_type(self, description: str, declared: str | None = None) -> str | None:
        if declared:
            return declared
        norm = normalize(description)
        hints = self.pack.jd_signals.get("employment_type_hints", {})
        best: tuple[int, str] | None = None
        for emp_type, patterns in hints.items():
            for pattern in patterns:
                p = normalize(pattern)
                if p and p in norm:
                    cand = (len(p), emp_type)
                    if best is None or cand > best:
                        best = cand
        return best[1] if best else None

    # -- the verdict --------------------------------------------------------
    def assess(
        self,
        description: str,
        user_status_id: str,
        employment_preferences: list[str] | None = None,
        declared_employment_type: str | None = None,
        has_clearance: bool = False,
    ) -> EligibilityResult:
        status: AuthStatus = self.pack.status(user_status_id)
        signals = self.detect_signals(description)
        by_signal = {s.signal: s for s in signals}
        reasons: list[str] = []

        verdict = AuthVerdict.UNKNOWN

        # 1. Hard status requirements stated by the employer.
        blocked = False
        for signal, spec in self.pack.jd_signals.items():
            if signal not in by_signal or spec.get("severity") != "blocking":
                continue
            requirement = spec.get("requires")
            if requirement and requirement not in status.satisfies:
                verdict = AuthVerdict.NOT_COMPATIBLE
                blocked = True
                reasons.append(
                    f"Posting states a '{requirement}' requirement; your status "
                    f"({status.label}) does not satisfy it."
                )

        # 2. Clearance is only blocking when the user does not hold one. The
        #    system cannot verify a clearance, so it asks rather than assumes.
        clearance = by_signal.get("clearance_required")
        if clearance and not blocked:
            if has_clearance:
                reasons.append("Clearance required; your profile records an active clearance.")
            else:
                verdict = AuthVerdict.NOT_COMPATIBLE
                blocked = True
                reasons.append(
                    "Posting requires a security clearance and your profile does not record one."
                )

        # 3. Sponsorship.
        if not blocked:
            unavailable = by_signal.get("sponsorship_unavailable")
            available = by_signal.get("sponsorship_available")

            if status.needs_sponsorship_now:
                if unavailable:
                    verdict = AuthVerdict.NOT_COMPATIBLE
                    reasons.append(
                        "Employer explicitly states sponsorship is not available and your "
                        "status requires it."
                    )
                elif available:
                    verdict = AuthVerdict.POTENTIALLY_COMPATIBLE
                    reasons.append(
                        "Employer explicitly mentions sponsorship/transfer; "
                        + ("an H1B transfer petition would still be required."
                           if status.transfer_required
                           else "a petition would still be required.")
                    )
                else:
                    verdict = AuthVerdict.UNKNOWN
                    reasons.append(
                        "Posting says nothing about sponsorship. Sponsorship is NOT assumed - "
                        "status unknown until confirmed with the employer."
                    )
            else:
                if unavailable:
                    verdict = AuthVerdict.COMPATIBLE
                    reasons.append(
                        "Employer does not sponsor, but your status does not require sponsorship."
                    )
                else:
                    verdict = AuthVerdict.COMPATIBLE
                    reasons.append(f"{status.label} does not require employer sponsorship to start.")
                if status.needs_sponsorship_future:
                    reasons.append(
                        "Note: your status has a future expiry - confirm the employer's "
                        "willingness to support an extension or change of status later."
                    )

        # 4. Employment type is a preference mismatch, never a hard block.
        detected = self.detect_employment_type(description, declared_employment_type)
        prefs = [p for p in (employment_preferences or []) if p]
        emp_match = True
        if detected and prefs and detected not in prefs:
            emp_match = False
            reasons.append(
                f"Employment type '{detected}' is outside your stated preferences "
                f"({', '.join(prefs)})."
            )

        score = VERDICT_SCORES[verdict]
        if not emp_match and score > 0:
            score = max(0.0, score - 15.0)

        return EligibilityResult(
            verdict=verdict,
            score=score,
            user_status_id=status.id,
            user_status_label=status.label,
            country_code=self.pack.code,
            employment_type_match=emp_match,
            detected_employment_type=detected,
            signals=signals,
            reasons=reasons,
        )


def assess_eligibility(
    description: str,
    country: str,
    user_status_id: str,
    employment_preferences: list[str] | None = None,
    declared_employment_type: str | None = None,
    has_clearance: bool = False,
) -> EligibilityResult:
    pack = countries().require(country)
    return WorkAuthorizationEngine(pack).assess(
        description=description,
        user_status_id=user_status_id,
        employment_preferences=employment_preferences,
        declared_employment_type=declared_employment_type,
        has_clearance=has_clearance,
    )
