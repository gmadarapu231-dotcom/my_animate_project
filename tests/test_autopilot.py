"""The 4-hour cycle: one pass, the lock, and what it records.

These run against a real database and the real pipeline, with discovery turned
off and the AI layer off, so what is under test is the cycle's own logic: which
jobs it acts on, what it writes down, and what it refuses.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from careeros.sources.http import Response

from careeros.apply.guardrails import AutopilotPolicy
from careeros.autopilot import (
    AlreadyRunning,
    NotQueued,
    PassReport,
    approval_queue,
    approve,
    decline,
    RunLock,
    load_policy,
    run_forever,
    run_once,
    save_policy,
)
from careeros.db.models import Application, ApplicationEvent, EvidenceItem, Job, PipelineRun
from careeros.enums import ApplicationStatus, VerificationState


@pytest.fixture
def ready(db, user):
    """A profile, ingested jobs, verified evidence, and two automatable channels."""
    from careeros.pipeline import Pipeline
    from careeros.sources.jsonfile import JsonFileSource

    pipeline = Pipeline(db)
    pipeline.ingest([JsonFileSource("data/sample_jobs.json")])
    db.flush()

    cobalt = db.query(Job).filter(Job.company == "Cobalt Bank").first()
    cobalt.url = "https://boards.greenhouse.io/cobalt/jobs/5550001"
    halden = db.query(Job).filter(Job.company == "Halden Consulting").first()
    halden.description = (halden.description or "") + (
        "\n\nTo apply, email your CV to careers@halden.example.com"
    )
    for item in db.query(EvidenceItem).filter(EvidenceItem.user_id == user.id).all():
        item.verification = VerificationState.VERIFIED.value
    db.flush()
    return {"db": db, "user": user, "cobalt": cobalt, "halden": halden}


def enable(db, user, **overrides):
    settings = {"enabled": True, "dry_run": True, "min_match_score": 50.0}
    settings.update(overrides)
    return save_policy(db, user, AutopilotPolicy(**settings))


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------
def test_a_fresh_profile_does_nothing(ready):
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)
    assert report.skipped_reason is not None
    assert "off" in report.skipped_reason
    assert report.considered == 0
    assert report.submitted == 0


def test_the_policy_is_stored_on_the_profile_and_read_back(ready):
    enable(ready["db"], ready["user"], max_per_run=3, quiet_hours=("22:00-07:00",))
    reloaded = load_policy(ready["user"])
    assert reloaded.enabled and reloaded.dry_run
    assert reloaded.max_per_run == 3
    assert reloaded.quiet_hours == ("22:00-07:00",)
    # And it survives a JSON round trip, because that is how it is stored.
    assert json.loads(json.dumps(ready["user"].autopilot))["max_per_run"] == 3


def test_quiet_hours_skip_the_whole_pass(ready):
    enable(ready["db"], ready["user"], quiet_hours=("22:00-07:00",))
    report = run_once(
        ready["db"],
        ready["user"],
        discover_jobs=False,
        use_ai=False,
        now=datetime(2026, 9, 22, 23, 30),
    )
    assert report.skipped_reason == "inside quiet hours 22:00-07:00"
    assert report.considered == 0


def test_no_profile_is_reported_not_crashed(db):
    report = run_once(db, None, discover_jobs=False, use_ai=False)
    assert report.skipped_reason == "no profile on this server yet"


# ---------------------------------------------------------------------------
# a dry pass
# ---------------------------------------------------------------------------
def test_a_dry_pass_reports_what_it_would_send_and_sends_nothing(ready):
    enable(ready["db"], ready["user"])
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)

    assert report.would_submit >= 1
    assert report.submitted == 0
    assert all(
        (v.submission or {}).get("dry_run") is True for v in report.verdicts if v.submission
    )

    applied = [v for v in report.verdicts if v.allowed]
    assert {v.tier for v in applied} <= {"api", "email"}
    for verdict in applied:
        assert "Would " in verdict.submission["message"]


def test_a_dry_pass_marks_applications_ready_rather_than_applied(ready):
    enable(ready["db"], ready["user"])
    run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)

    rows = ready["db"].query(Application).all()
    assert rows
    assert ApplicationStatus.APPLIED.value not in {r.status for r in rows}
    assert ApplicationStatus.APPLICATION_READY.value in {r.status for r in rows}
    assert all(r.applied_on is None for r in rows)


def test_every_job_gets_a_verdict_so_the_report_explains_the_whole_list(ready):
    enable(ready["db"], ready["user"])
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)

    assert len(report.verdicts) == report.considered
    for verdict in report.verdicts:
        assert verdict.allowed or verdict.blockers, f"job {verdict.job_id} has no reason"
    assert report.blocked_counts


def test_an_assisted_posting_is_counted_not_submitted(ready):
    enable(ready["db"], ready["user"])
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)
    assisted = [v for v in report.verdicts if v.tier == "assisted"]
    assert assisted
    assert all(not v.allowed for v in assisted)
    assert report.assisted >= len(assisted)


# ---------------------------------------------------------------------------
# a live pass
# ---------------------------------------------------------------------------
def test_a_live_pass_submits_and_records_the_reference(ready):
    from careeros.sources.http import HttpClient, Response

    class Accepts:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, headers, body, timeout):
            self.calls.append(url)
            return Response(status=200, body='{"id": "gh-777"}', url=url, headers={})

    transport = Accepts()
    sent: list = []
    enable(ready["db"], ready["user"], dry_run=False, require_approval=False, max_per_run=2)

    report = run_once(
        ready["db"],
        ready["user"],
        discover_jobs=False,
        use_ai=False,
        client=HttpClient(transport=transport, rate_limit_per_minute=0),
        send_email=lambda message, attachment: sent.append(message) or "mail-1",
    )

    assert report.submitted >= 1
    assert report.would_submit == 0
    assert transport.calls or sent

    applied = ready["db"].query(Application).filter(
        Application.status == ApplicationStatus.APPLIED.value
    ).all()
    assert applied
    assert all(row.applied_on == date.today() for row in applied)
    assert all(row.submission_mode == "automatic" for row in applied)

    events = ready["db"].query(ApplicationEvent).filter(
        ApplicationEvent.source == "autopilot"
    ).all()
    assert events
    assert any("Submitted via" in (e.detail or "") for e in events)
    assert any("reference" in (e.detail or "") for e in events)


def test_the_run_cap_holds_in_a_live_pass(ready):
    from careeros.sources.http import HttpClient, Response

    class Accepts:
        def request(self, method, url, *, headers, body, timeout):
            return Response(status=200, body="{}", url=url, headers={})

    enable(ready["db"], ready["user"], dry_run=False, require_approval=False, max_per_run=1)
    report = run_once(
        ready["db"],
        ready["user"],
        discover_jobs=False,
        use_ai=False,
        client=HttpClient(transport=Accepts(), rate_limit_per_minute=0),
        send_email=lambda m, a: "mail-1",
    )
    assert report.submitted == 1
    assert any("run cap reached" in b for v in report.verdicts for b in v.blockers)


def test_a_submission_failure_does_not_end_the_pass(ready):
    from careeros.sources.http import HttpClient, Response

    class Refuses:
        def request(self, method, url, *, headers, body, timeout):
            return Response(status=500, body="server error", url=url, headers={})

    enable(ready["db"], ready["user"], dry_run=False, require_approval=False)
    report = run_once(
        ready["db"],
        ready["user"],
        discover_jobs=False,
        use_ai=False,
        client=HttpClient(transport=Refuses(), rate_limit_per_minute=0),
        send_email=lambda m, a: "mail-1",
    )
    # The email one still went; the API one failed and was handed back.
    assert report.considered > 0
    assert report.finished_at is not None


def test_unverified_evidence_stops_a_live_pass_entirely(ready):
    """The résumé parser produces unverified evidence; it must not be sent."""
    for item in ready["db"].query(EvidenceItem).all():
        item.verification = VerificationState.UNVERIFIED.value
    ready["db"].flush()

    enable(ready["db"], ready["user"], dry_run=False, require_approval=False)
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)

    assert report.submitted == 0
    assert any(
        "unverified evidence" in b for v in report.verdicts for b in v.blockers
    ), report.blocked_counts


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
def test_each_pass_is_recorded(ready):
    before = ready["db"].query(PipelineRun).count()
    enable(ready["db"], ready["user"])
    run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)
    rows = ready["db"].query(PipelineRun).all()
    assert len(rows) == before + 1
    assert rows[-1].finished_at is not None
    assert "considered" in (rows[-1].stats or {})


def test_a_report_serialises_for_the_api(ready):
    enable(ready["db"], ready["user"])
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)
    payload = json.loads(json.dumps(report.to_dict(), default=str))
    assert payload["counts"]["considered"] >= 1
    assert isinstance(payload["blocked_by"], dict)
    assert payload["policy"]["dry_run"] is True


def test_a_second_pass_does_not_re_apply(ready):
    from careeros.sources.http import HttpClient, Response

    class Accepts:
        def __init__(self):
            self.count = 0

        def request(self, method, url, *, headers, body, timeout):
            self.count += 1
            return Response(status=200, body="{}", url=url, headers={})

    transport = Accepts()
    enable(ready["db"], ready["user"], dry_run=False, require_approval=False)
    client = HttpClient(transport=transport, rate_limit_per_minute=0)

    first = run_once(
        ready["db"], ready["user"], discover_jobs=False, use_ai=False,
        client=client, send_email=lambda m, a: "mail-1",
    )
    second = run_once(
        ready["db"], ready["user"], discover_jobs=False, use_ai=False,
        client=client, send_email=lambda m, a: "mail-1",
    )
    assert first.submitted >= 1
    assert second.submitted == 0
    assert any("already applied" in b for v in second.verdicts for b in v.blockers)


# ---------------------------------------------------------------------------
# the approval queue
# ---------------------------------------------------------------------------
class _Accepts:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append(url)
        return Response(status=200, body='{"id": "gh-1"}', url=url, headers={})


def _live_pass(ready, transport, sent, **policy):
    from careeros.sources.http import HttpClient

    enable(ready["db"], ready["user"], dry_run=False, **policy)
    return run_once(
        ready["db"],
        ready["user"],
        discover_jobs=False,
        use_ai=False,
        client=HttpClient(transport=transport, rate_limit_per_minute=0),
        send_email=lambda message, attachment: sent.append(message) or "mail-1",
    )


def test_live_is_still_gated_on_approval_by_default():
    assert AutopilotPolicy().require_approval is True
    live = AutopilotPolicy(enabled=True, dry_run=False)
    assert "approval" in live.summary()


def test_a_live_pass_queues_instead_of_sending(ready):
    transport, sent = _Accepts(), []
    report = _live_pass(ready, transport, sent)

    assert report.submitted == 0
    assert report.awaiting_approval >= 1
    assert not transport.calls and not sent

    queue = approval_queue(ready["db"], ready["user"])
    assert len(queue) == report.awaiting_approval
    assert all(row["tier"] in ("api", "email") for row in queue)


def test_a_second_pass_does_not_double_queue(ready):
    transport, sent = _Accepts(), []
    first = _live_pass(ready, transport, sent)
    _live_pass(ready, transport, sent)
    assert len(approval_queue(ready["db"], ready["user"])) == first.awaiting_approval
    waiting = ready["db"].query(ApplicationEvent).filter(
        ApplicationEvent.detail.like("%Waiting for your approval%")
    ).count()
    assert waiting == first.awaiting_approval


def test_approving_sends_that_one_application(ready):
    from careeros.sources.http import HttpClient

    transport, sent = _Accepts(), []
    _live_pass(ready, transport, sent)
    job_id = approval_queue(ready["db"], ready["user"])[0]["job_id"]

    result = approve(
        ready["db"],
        ready["user"],
        job_id,
        client=HttpClient(transport=transport, rate_limit_per_minute=0),
        send_email=lambda message, attachment: sent.append(message) or "mail-1",
    )
    assert result["outcome"] == "submitted"
    assert transport.calls or sent
    row = ready["db"].query(Application).filter(Application.job_id == job_id).one()
    assert row.status == ApplicationStatus.APPLIED.value
    assert row.approval == "approved"
    assert job_id not in [q["job_id"] for q in approval_queue(ready["db"], ready["user"])]


def test_declining_keeps_it_out_of_every_later_pass(ready):
    transport, sent = _Accepts(), []
    _live_pass(ready, transport, sent)
    job_id = approval_queue(ready["db"], ready["user"])[0]["job_id"]
    decline(ready["db"], ready["user"], job_id)

    later = _live_pass(ready, transport, sent, require_approval=False)
    verdict = next(v for v in later.verdicts if v.job_id == job_id)
    assert not verdict.allowed
    assert "you passed on this one" in verdict.blockers


def test_only_queued_jobs_can_be_approved(ready):
    with pytest.raises(NotQueued):
        approve(ready["db"], ready["user"], ready["cobalt"].id)
    with pytest.raises(NotQueued):
        decline(ready["db"], ready["user"], ready["cobalt"].id)


def test_an_approval_the_guardrails_now_refuse_goes_back_in_the_queue(ready):
    transport, sent = _Accepts(), []
    _live_pass(ready, transport, sent)
    job_id = approval_queue(ready["db"], ready["user"])[0]["job_id"]
    # The user tightened the rules after the pass queued it.
    enable(ready["db"], ready["user"], dry_run=False, min_match_score=100.0)

    result = approve(ready["db"], ready["user"], job_id)
    assert result["outcome"] == "blocked"
    assert job_id in [q["job_id"] for q in approval_queue(ready["db"], ready["user"])]


# ---------------------------------------------------------------------------
# tracking replies
# ---------------------------------------------------------------------------
def test_the_pass_reads_the_mailbox_and_moves_applications(ready, tmp_path):
    from careeros.gmail.client import LocalMailboxClient

    transport, sent = _Accepts(), []
    _live_pass(ready, transport, sent, require_approval=False)
    applied = ready["db"].query(Application).filter(
        Application.status == ApplicationStatus.APPLIED.value
    ).first()
    company = applied.job.company

    mailbox = tmp_path / "mail.json"
    mailbox.write_text(json.dumps([{
        "id": "m1",
        "from": "talent@example.com",
        "subject": f"Interview invitation - {company}",
        "body": f"Thanks for applying to {company}. Can we schedule an interview on 2026-11-02 14:00?",
        "received_at": "2026-10-17T09:00:00",
    }]))

    enable(ready["db"], ready["user"])
    report = run_once(
        ready["db"], ready["user"], discover_jobs=False, use_ai=False,
        mail_client=LocalMailboxClient(mailbox),
    )
    assert report.tracking["fetched"] == 1
    assert report.tracking["advanced"] == 1
    ready["db"].refresh(applied)
    assert applied.status == ApplicationStatus.INTERVIEW.value
    track = next(s for s in report.to_dict()["stages"] if s["key"] == "track")
    assert track["done"] and "1 application(s) moved" in track["detail"]


def test_no_mailbox_is_reported_as_a_skipped_stage(ready, monkeypatch, tmp_path):
    monkeypatch.delenv("CAREEROS_LOCAL_MAILBOX", raising=False)
    monkeypatch.setenv("CAREEROS_GMAIL_TOKEN", str(tmp_path / "absent.json"))
    enable(ready["db"], ready["user"])
    report = run_once(ready["db"], ready["user"], discover_jobs=False, use_ai=False)
    assert report.tracking == {"skipped": "no mailbox connected"}
    stages = report.to_dict()["stages"]
    assert [s["key"] for s in stages] == ["find", "prep", "apply", "track"]
    assert stages[-1]["done"] is False


# ---------------------------------------------------------------------------
# the lock
# ---------------------------------------------------------------------------
def test_two_passes_cannot_overlap(tmp_path):
    path = tmp_path / "autopilot.lock"
    with RunLock(path):
        assert path.exists()
        with pytest.raises(AlreadyRunning, match="still running"):
            with RunLock(path):
                pass
    assert not path.exists()


def test_a_stale_lock_is_taken_over(tmp_path):
    path = tmp_path / "autopilot.lock"
    path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "started_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    lock = RunLock(path)
    assert lock.stale()
    with lock:
        assert json.loads(path.read_text())["pid"] != 999999


def test_a_corrupt_lock_is_treated_as_stale(tmp_path):
    path = tmp_path / "autopilot.lock"
    path.write_text("not json", encoding="utf-8")
    assert RunLock(path).stale()
    with RunLock(path):
        pass


def test_the_lock_is_released_even_when_a_pass_raises(tmp_path):
    path = tmp_path / "autopilot.lock"
    with pytest.raises(RuntimeError):
        with RunLock(path):
            raise RuntimeError("boom")
    assert not path.exists()


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
def test_the_loop_passes_its_options_through_to_each_pass(db, user, monkeypatch, tmp_path):
    """`--no-discover` on the loop has to actually reach run_once."""
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    save_policy(db, user, AutopilotPolicy(enabled=True, dry_run=True))
    db.commit()

    import careeros.autopilot as module

    seen: list[dict] = []
    real = module.run_once
    monkeypatch.setattr(
        module,
        "run_once",
        lambda session, **kwargs: seen.append(kwargs) or real(session, **kwargs),
    )
    run_forever(
        lambda: db, max_passes=1, sleeper=lambda _s: None, discover_jobs=False, use_ai=False
    )
    assert seen == [{"discover_jobs": False, "use_ai": False}]


def test_the_loop_runs_a_pass_and_sleeps_the_policy_interval(db, user, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    save_policy(db, user, AutopilotPolicy(enabled=True, dry_run=True, interval_hours=4.0))
    db.commit()

    slept: list[float] = []
    reports = run_forever(
        lambda: db,
        max_passes=2,
        on_report=lambda r: None,
        sleeper=slept.append,
        discover_jobs=False,
        use_ai=False,
    )
    assert len(reports) == 2
    assert slept == [4 * 3600.0]      # one sleep between two passes


def test_the_loop_outlives_a_failing_pass(db, user, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))

    def explode():
        raise RuntimeError("database gone")

    reports = run_forever(explode, max_passes=2, sleeper=lambda _s: None)
    assert len(reports) == 2
    assert all(r.errors for r in reports)
    assert "database gone" in reports[0].errors[0]


def test_a_custom_interval_overrides_the_policy(db, user, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    save_policy(db, user, AutopilotPolicy(enabled=True, interval_hours=4.0))
    db.commit()

    slept: list[float] = []
    run_forever(
        lambda: db,
        interval_hours=1.5,
        max_passes=2,
        sleeper=slept.append,
        discover_jobs=False,
        use_ai=False,
    )
    assert slept == [1.5 * 3600.0]


def test_the_sleep_is_never_shorter_than_a_minute(db, user, monkeypatch, tmp_path):
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    slept: list[float] = []
    run_forever(
        lambda: db,
        interval_hours=0.0001,
        max_passes=2,
        sleeper=slept.append,
        discover_jobs=False,
        use_ai=False,
    )
    assert slept == [60.0]
