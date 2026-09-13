"""Work authorization: never assume sponsorship, always show the source."""

from __future__ import annotations

from careeros.engines.workauth import assess_eligibility
from careeros.enums import AuthVerdict


def test_silence_means_unknown_not_yes():
    """The single most important rule in this engine."""
    result = assess_eligibility("Great role, apply now.", "US", "h1b")
    assert result.verdict is AuthVerdict.UNKNOWN
    assert "NOT assumed" in " ".join(result.reasons)


def test_explicit_refusal_blocks_a_candidate_who_needs_sponsorship():
    result = assess_eligibility("We are unable to sponsor visas.", "US", "h1b")
    assert result.verdict is AuthVerdict.NOT_COMPATIBLE


def test_explicit_refusal_is_harmless_to_a_green_card_holder():
    result = assess_eligibility("We are unable to sponsor visas.", "US", "green_card")
    assert result.verdict is AuthVerdict.COMPATIBLE


def test_explicit_sponsorship_yields_potentially_compatible_never_certain():
    result = assess_eligibility("H1B transfer welcome.", "US", "h1b")
    assert result.verdict is AuthVerdict.POTENTIALLY_COMPATIBLE
    assert "petition" in " ".join(result.reasons)


def test_citizenship_requirement_blocks_permanent_resident():
    result = assess_eligibility("Must be a US Citizen.", "US", "green_card")
    assert result.verdict is AuthVerdict.NOT_COMPATIBLE


def test_permanent_status_requirement_blocks_opt():
    result = assess_eligibility("US citizens or permanent residents only.", "US", "opt")
    assert result.verdict is AuthVerdict.NOT_COMPATIBLE


def test_clearance_blocks_unless_profile_records_one():
    text = "An active security clearance is required."
    assert assess_eligibility(text, "US", "us_citizen").verdict is AuthVerdict.NOT_COMPATIBLE
    assert assess_eligibility(text, "US", "us_citizen", has_clearance=True).verdict is AuthVerdict.COMPATIBLE


def test_every_verdict_cites_its_source():
    result = assess_eligibility("We are unable to sponsor visas.", "US", "h1b")
    assert result.evidence
    assert "unable to sponsor" in result.evidence[0]["quote"]


def test_disclaimer_is_always_attached():
    result = assess_eligibility("anything", "US", "h1b")
    assert "verify with employer/recruiter" in result.disclaimer


def test_employment_type_is_a_preference_not_a_block():
    result = assess_eligibility(
        "H1B sponsorship available. Corp to corp accepted.", "US", "h1b", employment_preferences=["w2"]
    )
    assert result.detected_employment_type == "c2c"
    assert result.employment_type_match is False
    assert result.verdict is AuthVerdict.POTENTIALLY_COMPATIBLE   # still not a block
    assert result.score < 75


def test_india_pack_works_the_same_way():
    result = assess_eligibility(
        "Candidates must be authorized to work in India. Full-time permanent role.",
        "IN",
        "indian_citizen",
    )
    assert result.verdict is AuthVerdict.COMPATIBLE
    assert result.detected_employment_type == "full_time"
