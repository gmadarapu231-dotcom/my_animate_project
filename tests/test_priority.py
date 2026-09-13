"""Prioritisation, with the deadline-today rule as a hard guarantee."""

from __future__ import annotations

from datetime import date, timedelta

from careeros.engines.priority import PriorityEngine, classify_deadline, competition_score
from careeros.enums import AuthVerdict, DeadlineBucket

TODAY = date(2026, 9, 13)


def test_deadline_buckets():
    assert classify_deadline(TODAY, TODAY)[0] is DeadlineBucket.TODAY
    assert classify_deadline(TODAY + timedelta(days=2), TODAY)[0] is DeadlineBucket.WITHIN_48H
    assert classify_deadline(TODAY + timedelta(days=5), TODAY)[0] is DeadlineBucket.WITHIN_7D
    assert classify_deadline(TODAY + timedelta(days=30), TODAY)[0] is DeadlineBucket.FUTURE
    assert classify_deadline(None, TODAY)[0] is DeadlineBucket.NONE
    assert classify_deadline(TODAY - timedelta(days=1), TODAY)[0] is DeadlineBucket.EXPIRED


def test_every_bucket_has_a_flag():
    for bucket in DeadlineBucket:
        assert bucket.emoji and bucket.label


def test_deadline_today_outranks_a_better_scoring_job():
    """The brief's hard rule: if today is the final date, it goes to the top."""
    engine = PriorityEngine()
    mediocre_today = engine.score(55, 50, deadline_on=TODAY, today=TODAY)
    excellent_later = engine.score(98, 100, deadline_on=TODAY + timedelta(days=20), today=TODAY)

    assert excellent_later.overall > mediocre_today.overall      # better on points...
    ranked = sorted([excellent_later, mediocre_today], key=lambda r: r.sort_key())
    assert ranked[0] is mediocre_today                           # ...but still ranks second


def test_expired_scores_zero_and_sinks():
    engine = PriorityEngine()
    expired = engine.score(99, 100, deadline_on=TODAY - timedelta(days=1), today=TODAY)
    assert expired.overall == 0.0
    assert expired.sort_key()[0] == 9


def test_ineligible_job_is_damped_but_still_visible():
    engine = PriorityEngine()
    result = engine.score(95, 0, verdict=AuthVerdict.NOT_COMPATIBLE, today=TODAY)
    assert 0 < result.overall < 30
    assert result.sort_key()[0] == 8


def test_competition_prefers_fewer_applicants():
    assert competition_score(5) > competition_score(60) > competition_score(600)
    assert competition_score(None) == 50.0      # unknown is neutral, not optimistic


def test_freshness_drives_urgency_when_no_deadline():
    engine = PriorityEngine()
    fresh = engine.score(70, 100, posted_on=TODAY, today=TODAY)
    stale = engine.score(70, 100, posted_on=TODAY - timedelta(days=45), today=TODAY)
    assert fresh.urgency_score > stale.urgency_score


def test_explanation_is_always_populated():
    result = PriorityEngine().score(70, 100, deadline_on=TODAY, today=TODAY)
    assert len(result.explanation) >= 4
    assert any("FINAL APPLICATION DATE IS TODAY" in line for line in result.explanation)
