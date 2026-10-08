"""`taxvault verify` -- the acceptance check a deployment runs on itself.

These tests check the checker. A verification command that silently passes
when something is broken is worse than no verification at all, so each test
below breaks one thing and asserts that `verify` notices.
"""

from __future__ import annotations

import pytest

from taxvault.verify import Report, _Runner, run


def test_every_check_passes_on_a_working_installation(tax_env, capsys):
    """The whole point: a correct installation reports a clean pass."""
    code = run()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ALL" in out and "CHECKS PASSED" in out
    assert "FAIL" not in out


def test_the_groups_a_deployment_cares_about_are_all_covered(tax_env, capsys):
    run()
    out = capsys.readouterr().out
    for group in ("Installation", "Tax parameters", "Arithmetic", "Encryption",
                  "Reading forms", "The agent", "Getting paid",
                  "This environment"):
        assert group in out, f"{group} is not verified"


def test_it_says_filing_still_needs_an_efin(tax_env, capsys):
    """Nobody should read a clean pass as permission to file."""
    run()
    out = capsys.readouterr().out
    assert "cannot FILE" in out
    assert "EFIN" in out


def test_a_broken_check_fails_the_run_and_is_named(capsys):
    """The checker has to fail when a check fails."""
    runner = _Runner()
    runner.section("Deliberate")

    @runner.check("this one is fine")
    def _() -> str:
        return "ok"

    @runner.check("this one is broken", remedy="do the thing")
    def _() -> str:
        raise AssertionError("2 + 2 came out as 5")

    from taxvault.verify import _print

    code = _print(runner.report, verbose=False, trace=False)
    out = capsys.readouterr().out
    assert code == 1
    assert "1 OF 2 CHECKS FAILED" in out
    assert "this one is broken" in out
    assert "2 + 2 came out as 5" in out
    assert "do the thing" in out
    assert "Do not take a real client's return" in out


def test_an_exception_is_caught_rather_than_crashing_the_run(capsys):
    """One broken check must not stop the others from reporting."""
    runner = _Runner()
    runner.section("Deliberate")

    @runner.check("explodes")
    def _() -> str:
        raise RuntimeError("the database fell over")

    @runner.check("still runs")
    def _() -> str:
        return "reached"

    assert [c.name for c in runner.report.checks] == ["explodes", "still runs"]
    assert runner.report.checks[0].passed is False
    assert "the database fell over" in runner.report.checks[0].detail
    assert runner.report.checks[1].passed is True


def test_a_skip_is_not_a_failure(capsys):
    from taxvault.verify import _Skip, _print

    runner = _Runner()
    runner.section("Deliberate")

    @runner.check("not applicable here")
    def _() -> str:
        raise _Skip("no alembic installed")

    code = _print(runner.report, verbose=False, trace=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "skip" in out
    assert runner.report.ok is True


def test_a_misconfigured_production_fails_verification(tax_env, monkeypatch, capsys):
    """The check that matters most: production without its secrets."""
    monkeypatch.setenv("TAXVAULT_ENV", "production")
    monkeypatch.delenv("TAXVAULT_MASTER_KEY", raising=False)
    monkeypatch.delenv("TAXVAULT_SESSION_SECRET", raising=False)

    code = run()
    out = capsys.readouterr().out
    assert code == 1, out
    assert "blocking setting" in out
    assert "master_key" in out


def test_the_report_counts_what_it_says_it_counts():
    from taxvault.verify import Check

    report = Report(checks=[
        Check(name="a", group="g", passed=True),
        Check(name="b", group="g", passed=False),
        Check(name="c", group="g", skipped=True),
    ])
    assert len(report.passed) == 1
    assert len(report.failed) == 1
    assert len(report.skipped) == 1
    assert report.ok is False
