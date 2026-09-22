"""Applying: channel detection, guardrails, packets and submission.

The tests that matter most are the refusals. Automatic submission is the one
action here that reaches a stranger and cannot be undone, so each gate gets its
own test, and the defaults are asserted rather than assumed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from careeros.apply.channels import SubmitTier, detect, find_application_email
from careeros.apply.guardrails import (
    AutopilotPolicy,
    cited_evidence_ids,
    evaluate,
    in_quiet_hours,
)
from careeros.apply.packet import build as build_packet
from careeros.apply.submit import (
    application_email,
    prepare_assisted,
    submit,
    submit_via_api,
    submit_via_email,
)
from careeros.enums import ApplicationStatus, AuthVerdict
from careeros.sources.http import HttpClient, Response


# ---------------------------------------------------------------------------
# stand-ins, so these tests exercise the rules rather than the ORM
# ---------------------------------------------------------------------------
class FakeUser:
    full_name = "Priya Raman"
    email = "priya@example.com"
    phone = "+1 512 555 0134"
    links = {"linkedin": "linkedin.com/in/priyaraman"}
    home_city = "Austin"
    home_region = "TX"
    total_experience_years = 8.5
    current_title = "Senior Network Security Engineer"
    remote_preference = "hybrid"
    open_to_relocation = True

    def __init__(self, country="US", needs_sponsorship=True):
        self.work_auth = [
            type(
                "Auth",
                (),
                {"country_code": country, "status_id": "h1b", "needs_sponsorship": needs_sponsorship},
            )()
        ]


class FakeJob:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.title = kwargs.get("title", "Senior Network Security Engineer")
        self.company = kwargs.get("company", "Cobalt Bank")
        self.country_code = kwargs.get("country_code", "US")
        self.url = kwargs.get("url", "https://boards.greenhouse.io/cobalt/jobs/5550001")
        self.description = kwargs.get("description", "BGP, OSPF, Cisco ISE.")
        self.source = kwargs.get("source", "greenhouse")
        self.deadline_on = kwargs.get("deadline_on")
        self.applicant_count = kwargs.get("applicant_count")
        self.salary_min = kwargs.get("salary_min")
        self.salary_max = kwargs.get("salary_max")
        self.classification = type("C", (), {"domain_id": kwargs.get("domain", "networking")})()


class FakeResume:
    def __init__(self, is_final=True, text="PRIYA RAMAN\nSenior Network Security Engineer\n..."):
        self.id = 7
        self.is_final = is_final
        self.rendered_text = text


def fake_factuality(passed=True, evidence_ids=(1, 2, 3), unsupported=0):
    return type(
        "F",
        (),
        {
            "passed": passed,
            "unsupported_count": unsupported,
            "claims": [{"text": "x", "verdict": "supported", "evidence_ids": list(evidence_ids)}],
        },
    )()


def elig(verdict=AuthVerdict.POTENTIALLY_COMPATIBLE.value):
    return type("E", (), {"verdict": verdict})()


def match(score=88.0):
    return type("M", (), {"match_score": score})()


def ready_policy(**overrides):
    base = {"enabled": True, "dry_run": True, "min_match_score": 70.0}
    base.update(overrides)
    return AutopilotPolicy(**base)


def allow(**overrides):
    """A decision that should pass every gate unless a test breaks one."""
    kwargs = {
        "policy": ready_policy(),
        "job": FakeJob(),
        "channel": detect(url="https://boards.greenhouse.io/cobalt/jobs/5550001"),
        "resume": FakeResume(),
        "factuality": fake_factuality(),
        "eligibility": elig(),
        "match": match(),
        "verified_evidence_ids": {1, 2, 3},
    }
    kwargs.update(overrides)
    return evaluate(**kwargs)


# ---------------------------------------------------------------------------
# channel detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,tier",
    [
        ("https://boards.greenhouse.io/acme/jobs/5550001", SubmitTier.API),
        ("https://jobs.lever.co/northwind/9c1f2e3a", SubmitTier.API),
        ("https://acme.myworkdayjobs.com/en-US/careers/job/R-9912", SubmitTier.ASSISTED),
        ("https://careers.example.com/req/1", SubmitTier.ASSISTED),
        ("https://www.linkedin.com/jobs/view/412", SubmitTier.BLOCKED),
        ("https://www.indeed.com/viewjob?jk=abc", SubmitTier.BLOCKED),
        ("https://in.naukri.com/job-listings-x-1", SubmitTier.BLOCKED),
        ("https://www.glassdoor.com/job-listing/x", SubmitTier.BLOCKED),
        ("https://www.ziprecruiter.com/c/x/Job/y", SubmitTier.BLOCKED),
        ("https://www.dice.com/job-detail/abc", SubmitTier.BLOCKED),
    ],
)
def test_each_board_lands_in_the_right_tier(url, tier):
    assert detect(url=url).tier is tier


def test_a_blocked_board_explains_itself_and_is_never_automatable():
    channel = detect(url="https://www.linkedin.com/jobs/view/412")
    assert not channel.tier.automatable
    assert "authenticated session" in channel.reason
    # And it names what to do instead.
    assert "Apply through the link" in channel.reason


def test_an_application_address_beats_a_login_wall():
    """The employer published the address; using it is not a workaround."""
    channel = detect(
        url="https://www.linkedin.com/jobs/view/412",
        description="Apply by sending your CV to careers@cobaltbank.com",
    )
    assert channel.tier is SubmitTier.EMAIL
    assert channel.email == "careers@cobaltbank.com"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Apply: mailto:jobs@example.com", "jobs@example.com"),
        ("Send your resume to careers@example.com", "careers@example.com"),
        ("Email hiring@example.com to apply", "hiring@example.com"),
        ("Questions to privacy@example.com", None),
        ("Contact no-reply@example.com", None),
        ("Recruiter: Dana Wu, dana.wu@example.com", None),
        ("Our careers inbox: careers@example.com", "careers@example.com"),
        ("", None),
    ],
)
def test_only_an_application_address_counts(text, expected):
    assert find_application_email(text) == expected


def test_a_recognised_board_with_an_unreadable_id_falls_back_rather_than_guessing():
    channel = detect(url="https://boards.greenhouse.io/acme")
    assert channel.tier is SubmitTier.ASSISTED
    assert "posting id could not be read" in channel.reason


def test_an_unexercised_endpoint_is_marked_as_such():
    assert detect(url="https://jobs.ashbyhq.com/northwind/abc-123").verified is False
    assert detect(url="https://boards.greenhouse.io/acme/jobs/1").verified is True


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------
def test_autopilot_is_off_and_dry_by_default():
    policy = AutopilotPolicy()
    assert policy.enabled is False
    assert policy.dry_run is True
    assert policy.submits_for_real is False
    assert "off" in policy.summary()


def test_enabling_alone_does_not_submit():
    assert AutopilotPolicy(enabled=True).submits_for_real is False
    assert AutopilotPolicy(enabled=True, dry_run=False).submits_for_real is True


def test_assisted_is_never_an_automatable_channel():
    assert SubmitTier.ASSISTED.value not in AutopilotPolicy().channels
    assert not SubmitTier.ASSISTED.automatable
    assert not SubmitTier.BLOCKED.automatable


def test_a_policy_round_trips_through_json():
    policy = AutopilotPolicy(
        enabled=True, channels=("email",), company_blocklist=("Acme",), quiet_hours=("22:00-07:00",)
    )
    restored = AutopilotPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
    assert restored == policy


# ---------------------------------------------------------------------------
# guardrails: the gates that are not configurable
# ---------------------------------------------------------------------------
def test_everything_lined_up_is_allowed():
    decision = allow()
    assert decision.allowed, decision.blockers


def test_nothing_happens_while_autopilot_is_off():
    decision = allow(policy=AutopilotPolicy())
    assert not decision.allowed
    assert decision.blockers == ["autopilot is off"]


def test_a_resume_that_failed_the_factuality_check_is_never_sent():
    decision = allow(factuality=fake_factuality(passed=False, unsupported=2))
    assert not decision.allowed
    assert any("factuality check failed" in b for b in decision.blockers)


def test_a_resume_not_marked_final_is_never_sent():
    decision = allow(resume=FakeResume(is_final=False))
    assert not decision.allowed
    assert any("not passed the factuality check" in b for b in decision.blockers)


def test_a_resume_resting_only_on_unverified_evidence_is_never_sent():
    """The résumé parser produces unverified evidence. It must not ship."""
    decision = allow(verified_evidence_ids=set())
    assert not decision.allowed
    assert any("unverified evidence" in b for b in decision.blockers)
    assert any("verify-evidence" in b for b in decision.blockers)


def test_partially_verified_evidence_is_allowed_but_noted():
    decision = allow(factuality=fake_factuality(evidence_ids=(1, 2, 99)), verified_evidence_ids={1, 2})
    assert decision.allowed
    assert any("still unverified" in n for n in decision.notes)


def test_an_incompatible_work_authorization_verdict_stops_the_application():
    decision = allow(eligibility=elig(AuthVerdict.NOT_COMPATIBLE.value))
    assert not decision.allowed
    assert any("not_compatible" in b for b in decision.blockers)


def test_an_unknown_verdict_is_allowed_with_the_disclaimer():
    decision = allow(eligibility=elig(AuthVerdict.UNKNOWN.value))
    assert decision.allowed
    assert any("not stated in this posting" in n for n in decision.notes)
    assert any("verify with the employer" in n for n in decision.notes)


def test_an_unknown_verdict_can_be_refused_by_policy():
    decision = allow(
        policy=ready_policy(allow_unknown_verdict=False), eligibility=elig(AuthVerdict.UNKNOWN.value)
    )
    assert not decision.allowed


def test_cited_evidence_comes_from_the_factuality_report():
    assert cited_evidence_ids(fake_factuality(evidence_ids=(4, 5))) == {4, 5}
    assert cited_evidence_ids(None) == set()


# ---------------------------------------------------------------------------
# guardrails: the configurable rules
# ---------------------------------------------------------------------------
def test_a_match_below_the_floor_is_refused():
    decision = allow(match=match(61.0))
    assert not decision.allowed
    assert any("below your floor of 70" in b for b in decision.blockers)


def test_a_job_with_no_match_score_yet_is_refused():
    decision = allow(match=None)
    assert not decision.allowed
    assert "no match score yet" in decision.blockers


def test_the_run_and_daily_caps_hold():
    assert not allow(submitted_this_run=5).allowed
    assert any("run cap" in b for b in allow(submitted_this_run=5).blockers)
    assert any("daily cap" in b for b in allow(submitted_today=15).blockers)


def test_a_blocked_channel_is_refused_with_its_reason():
    decision = allow(channel=detect(url="https://www.linkedin.com/jobs/view/1"))
    assert not decision.allowed
    assert any("authenticated session" in b for b in decision.blockers)


def test_a_channel_the_policy_disables_is_refused():
    decision = allow(policy=ready_policy(channels=("email",)))
    assert not decision.allowed
    assert any("not enabled in your policy" in b for b in decision.blockers)


def test_an_expired_deadline_is_refused():
    decision = allow(job=FakeJob(deadline_on=date.today() - timedelta(days=1)))
    assert not decision.allowed
    assert any("deadline passed" in b for b in decision.blockers)


def test_a_blocklisted_company_is_refused():
    decision = allow(policy=ready_policy(company_blocklist=("cobalt",)))
    assert not decision.allowed
    assert any("blocklist" in b for b in decision.blockers)


def test_an_allowlist_excludes_everything_else():
    assert not allow(policy=ready_policy(company_allowlist=("Northwind",))).allowed
    assert allow(policy=ready_policy(company_allowlist=("Cobalt Bank",))).allowed


def test_a_domain_or_country_restriction_holds():
    assert not allow(policy=ready_policy(domains=("healthcare",))).allowed
    assert not allow(policy=ready_policy(countries=("IN",))).allowed
    assert allow(policy=ready_policy(countries=("us",))).allowed


def test_a_salary_floor_holds_and_says_when_it_could_not_be_applied():
    low = allow(policy=ready_policy(min_salary=200_000), job=FakeJob(salary_max=150_000))
    assert not low.allowed

    silent = allow(policy=ready_policy(min_salary=200_000), job=FakeJob())
    assert silent.allowed
    assert any("No salary published" in n for n in silent.notes)


def test_a_crowded_posting_can_be_skipped():
    decision = allow(
        policy=ready_policy(skip_if_applicants_over=100), job=FakeJob(applicant_count=220)
    )
    assert not decision.allowed
    assert any("220 applicants" in b for b in decision.blockers)


def test_an_already_submitted_application_is_not_repeated():
    applied = type("A", (), {"status": ApplicationStatus.APPLIED.value})()
    decision = allow(application=applied)
    assert not decision.allowed
    assert any("already applied" in b for b in decision.blockers)


@pytest.mark.parametrize("hour,blocked", [(23, True), (3, True), (12, False)])
def test_quiet_hours_hold_across_midnight(hour, blocked):
    policy = ready_policy(quiet_hours=("22:00-07:00",))
    moment = datetime(2026, 9, 22, hour, 0)
    assert bool(in_quiet_hours(policy, moment)) is blocked
    assert allow(policy=policy, now=moment).allowed is not blocked


def test_a_malformed_quiet_window_is_ignored_not_fatal():
    assert in_quiet_hours(ready_policy(quiet_hours=("nonsense",)), datetime(2026, 9, 22, 3)) is None


# ---------------------------------------------------------------------------
# packets
# ---------------------------------------------------------------------------
def test_a_packet_answers_from_the_profile_and_nothing_else():
    packet = build_packet(
        user=FakeUser(), job=FakeJob(), channel=detect(url=FakeJob().url), resume=FakeResume()
    )
    assert packet.complete
    answers = {a.question: a for a in packet.answers}
    sponsorship = answers["Will you now or in the future require sponsorship?"]
    assert sponsorship.value == "Yes"
    assert "work authorization" in sponsorship.source
    assert all(a.source in ("profile", "you") or "work authorization" in a.source for a in packet.answers)


def test_a_country_the_profile_is_silent_about_blocks_automatic_submission():
    """Answering "yes, authorised" for an unknown country would be a lie."""
    packet = build_packet(
        user=FakeUser(country="US"),
        job=FakeJob(country_code="IN"),
        channel=detect(url=FakeJob().url),
        resume=FakeResume(),
    )
    assert not packet.complete
    assert any("authorised" in q for q in packet.unanswered)


def test_an_incomplete_packet_is_not_submitted_automatically():
    packet = build_packet(
        user=FakeUser(country="US"),
        job=FakeJob(country_code="IN"),
        channel=detect(url=FakeJob().url),
        resume=FakeResume(),
    )
    result = submit(packet, dry_run=True)
    assert not result.ok
    assert result.downgraded_to == SubmitTier.ASSISTED.value
    assert "incomplete" in result.message


def test_a_packet_without_a_resume_is_incomplete():
    packet = build_packet(
        user=FakeUser(), job=FakeJob(), channel=detect(url=FakeJob().url), resume=None
    )
    assert not packet.complete
    assert any("résumé" in q for q in packet.unanswered)


def test_the_packet_carries_the_authorization_disclaimer():
    packet = build_packet(
        user=FakeUser(),
        job=FakeJob(),
        channel=detect(url=FakeJob().url),
        resume=FakeResume(),
        eligibility=elig(),
    )
    assert any("verify with the employer" in d for d in packet.disclaimers)


def test_the_resume_filename_names_the_person_and_the_role():
    packet = build_packet(
        user=FakeUser(), job=FakeJob(), channel=detect(url=FakeJob().url), resume=FakeResume()
    )
    assert packet.resume_filename.startswith("priya-raman-")
    assert "network-security-engineer" in packet.resume_filename


# ---------------------------------------------------------------------------
# submission
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self, status=200, body='{"success": true, "id": "abc123"}'):
        self.status = status
        self.body = body
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        return Response(status=self.status, body=self.body, url=url, headers={})


def api_packet():
    return build_packet(
        user=FakeUser(), job=FakeJob(), channel=detect(url=FakeJob().url), resume=FakeResume()
    )


def test_a_dry_run_sends_nothing_at_all():
    transport = Recorder()
    client = HttpClient(transport=transport, rate_limit_per_minute=0)
    result = submit_via_api(api_packet(), client=client, dry_run=True)
    assert result.ok and result.dry_run
    assert transport.calls == []
    assert "Would POST" in result.message


def test_a_live_api_submission_posts_the_resume_as_multipart():
    transport = Recorder()
    client = HttpClient(transport=transport, rate_limit_per_minute=0)
    result = submit_via_api(api_packet(), client=client, dry_run=False)

    assert result.ok and not result.dry_run
    assert result.reference in ("abc123", "True", "true")
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/v1/boards/cobalt/jobs/5550001")
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="resume"; filename="priya-raman' in body
    assert b"priya@example.com" in body


def test_a_captcha_in_the_response_downgrades_instead_of_being_solved():
    transport = Recorder(body='{"error":"please complete the reCAPTCHA"}')
    client = HttpClient(transport=transport, rate_limit_per_minute=0)
    result = submit_via_api(api_packet(), client=client, dry_run=False)

    assert not result.ok
    assert result.downgraded_to == SubmitTier.ASSISTED.value
    assert "recaptcha" in result.message.lower()
    assert len(transport.calls) == 1, "a challenge must not be retried"


def test_a_403_is_taken_as_final():
    transport = Recorder(status=403, body="Forbidden")
    client = HttpClient(transport=transport, rate_limit_per_minute=0)
    result = submit_via_api(api_packet(), client=client, dry_run=False)
    assert not result.ok
    assert result.downgraded_to == SubmitTier.ASSISTED.value
    assert len(transport.calls) == 1


def test_a_platform_with_no_configured_endpoint_hands_over():
    packet = build_packet(
        user=FakeUser(),
        job=FakeJob(url="https://jobs.ashbyhq.com/northwind/abc-123"),
        channel=detect(url="https://jobs.ashbyhq.com/northwind/abc-123"),
        resume=FakeResume(),
    )
    result = submit_via_api(packet, dry_run=False)
    assert not result.ok
    assert result.downgraded_to == SubmitTier.ASSISTED.value
    assert "No application endpoint" in result.message


def email_packet():
    job = FakeJob(url="https://careers.example.com/r/1", description="Email careers@example.com to apply")
    return build_packet(
        user=FakeUser(), job=job, channel=detect(url=job.url, description=job.description),
        resume=FakeResume(),
    )


def test_an_emailed_application_addresses_and_signs_itself():
    message = application_email(email_packet())
    assert message["to"] == "careers@example.com"
    assert "Senior Network Security Engineer" in message["subject"]
    assert "Cobalt Bank" in message["subject"]
    assert "Priya Raman" in message["body"]
    assert "priya@example.com" in message["body"]


def test_an_email_dry_run_sends_nothing():
    sent: list = []
    result = submit_via_email(email_packet(), send=lambda m, a: sent.append(m) or "id", dry_run=True)
    assert result.ok and result.dry_run and sent == []


def test_a_live_email_attaches_the_resume():
    captured: dict = {}

    def send(message, attachment):
        captured["message"] = message
        captured["attachment"] = attachment
        return "gmail-draft-1"

    result = submit_via_email(email_packet(), send=send, dry_run=False)
    assert result.ok and result.reference == "gmail-draft-1"
    filename, payload = captured["attachment"]
    assert filename.startswith("priya-raman-")
    assert b"PRIYA RAMAN" in payload


def test_email_without_a_sender_connected_says_so_rather_than_failing_silently():
    result = submit_via_email(email_packet(), send=None, dry_run=False)
    assert not result.ok
    assert "gmail-auth" in result.message
    assert result.downgraded_to == SubmitTier.ASSISTED.value


def test_a_send_failure_is_reported_and_credentials_stay_out_of_the_message():
    def send(message, attachment):
        raise RuntimeError("SMTP auth failed for api_key=super-secret")

    result = submit_via_email(email_packet(), send=send, dry_run=False)
    assert not result.ok
    assert "super-secret" not in result.message


def test_an_assisted_handoff_is_a_success_not_a_failure():
    job = FakeJob(url="https://acme.myworkdayjobs.com/job/R-1")
    packet = build_packet(
        user=FakeUser(), job=job, channel=detect(url=job.url), resume=FakeResume()
    )
    result = prepare_assisted(packet)
    assert result.ok
    assert result.artifacts["apply_url"] == job.url
    assert result.artifacts["resume_characters"] > 0


def test_a_blocked_board_routes_to_assisted_with_the_reason():
    job = FakeJob(url="https://www.linkedin.com/jobs/view/1", description="")
    packet = build_packet(
        user=FakeUser(), job=job, channel=detect(url=job.url), resume=FakeResume()
    )
    result = submit(packet, dry_run=False)
    assert not result.ok
    assert result.downgraded_to == SubmitTier.ASSISTED.value
    assert "authenticated session" in result.message
