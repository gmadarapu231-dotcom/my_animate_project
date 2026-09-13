"""Email classification, extraction and the high-impact send guard."""

from __future__ import annotations

from careeros.ai.provider import get_provider
from careeros.enums import ApplicationStatus, AutomationMode, DraftState, EmailCategory
from careeros.gmail.classify import EmailClassifier, parse_action_date
from careeros.gmail.drafts import DraftGenerator, detect_high_impact


def classifier():
    return EmailClassifier(provider=get_provider("off"))


def test_categories():
    c = classifier()
    cases = [
        ("Interview invitation", "Can we schedule a call on 2026-09-18 14:00?", EmailCategory.INTERVIEW),
        ("Application received", "Thank you for applying. We received your application.",
         EmailCategory.APPLICATION_CONFIRMATION),
        ("Update", "Unfortunately we are not moving forward.", EmailCategory.REJECTION),
        ("Assessment", "Please complete the HackerRank assessment by 2026-09-20.", EmailCategory.ASSESSMENT),
        ("Offer", "We are pleased to offer you the position.", EmailCategory.OFFER),
        ("Question", "Will you require H1B sponsorship?", EmailCategory.SPONSORSHIP),
    ]
    for subject, body, expected in cases:
        assert c.analyze(subject, body).category is expected, subject


def test_rejection_beats_a_polite_closing():
    """"we'll keep your resume on file" does not make it not a rejection."""
    result = classifier().analyze(
        "Update", "Unfortunately we are not moving forward, but we will keep your resume on file."
    )
    assert result.category is EmailCategory.REJECTION


def test_explicit_reason_only_when_stated():
    c = classifier()
    stated = c.analyze("Update", "Unfortunately, because we require direct SAP GRC experience, we declined.")
    assert stated.rejection_reason_explicit
    assert "sap grc" in stated.rejection_reason_explicit.lower()

    silent = c.analyze("Update", "Unfortunately we are not moving forward at this time.")
    assert silent.rejection_reason_explicit is None      # no reason invented


def test_status_hints_drive_the_lifecycle():
    assert classifier().analyze("x", "We are pleased to offer you").status_hint is ApplicationStatus.OFFER


def test_dates_are_extracted():
    result = classifier().analyze("Interview", "Are you free on 2026-09-18 14:00 CT?")
    assert result.interview_datetime.startswith("2026-09-18")
    assert parse_action_date(result.interview_datetime).isoformat() == "2026-09-18"


def test_high_impact_topics_detected():
    assert "immigration" in detect_high_impact("Will you need H1B sponsorship?")
    assert "salary_negotiation" in detect_high_impact("What base salary are you targeting?")
    assert "offer" in detect_high_impact("Please review the formal offer.")
    assert detect_high_impact("Are you free Tuesday?") == []


def test_high_impact_reply_is_blocked_even_in_automated_mode():
    draft = DraftGenerator(provider=get_provider("off")).generate(
        category=EmailCategory.SPONSORSHIP,
        subject="Sponsorship",
        body="Will you require H1B sponsorship now or in the future?",
        candidate_first_name="Priya",
        automation_mode=AutomationMode.AUTOMATED,
    )
    assert draft.state is DraftState.BLOCKED_HIGH_IMPACT
    assert draft.may_auto_send is False


def test_ordinary_reply_still_requires_approval():
    draft = DraftGenerator(provider=get_provider("off")).generate(
        category=EmailCategory.INTERVIEW,
        subject="Interview invitation",
        body="Are you available Tuesday at 2pm?",
        candidate_first_name="Priya",
    )
    assert draft.state is DraftState.AWAITING_APPROVAL
    assert draft.may_auto_send is False      # DRAFT -> APPROVAL -> SEND, always


def test_every_template_renders_without_known_fields():
    generator = DraftGenerator(provider=get_provider("off"))
    for category in EmailCategory:
        draft = generator.generate(category, "Subject", "Body text.", "Priya")
        assert "{" not in draft.body


def test_local_client_never_sends(tmp_path):
    from careeros.gmail.client import LocalMailboxClient
    import pytest

    client = LocalMailboxClient(tmp_path / "mail.json")
    assert client.can_send is False
    with pytest.raises(PermissionError):
        client.send(None, "a@b.com", "s", "b")
