"""End-to-end pipeline, dedupe, search and analytics against the sample data."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from careeros.db.models import (
    Application,
    CareerDomain,
    EligibilityAssessment,
    Job,
    ResumeDocument,
)
from careeros.enums import AuthVerdict, DeadlineBucket
from careeros.search import QueryParser, run_search
from careeros.sources.base import RawJob


def test_cross_source_dedupe_by_fingerprint():
    a = RawJob("linkedin", "1", "Sr. Network Engineer", "Acme Corp", "x", city="Austin")
    b = RawJob("dice", "99", "Senior Network Engineer", "Acme  Corp", "y", city="Austin")
    c = RawJob("dice", "98", "Senior Network Engineer", "Acme Corp", "y", city="Dallas")
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_ingest_deduplicates(loaded):
    stats = loaded["stats"]
    assert stats.fetched == 11
    assert stats.inserted == 10
    assert stats.duplicates == 1
    assert stats.errors == []


def test_every_job_is_classified_and_scored(loaded, db):
    jobs = db.scalars(select(Job)).all()
    for job in jobs:
        assert job.classification is not None, job.title
        assert job.priority is not None, job.title


def test_multiple_domains_and_countries_are_represented(loaded, db):
    domains = {j.classification.domain_id for j in db.scalars(select(Job)).all()}
    countries = {j.country_code for j in db.scalars(select(Job)).all()}
    assert len(domains) >= 5
    assert countries == {"US", "IN"}


def test_unfamiliar_profession_lands_in_other_not_a_tech_bucket(loaded, db):
    job = db.scalars(select(Job).where(Job.title.like("Veterinary%"))).first()
    assert job.classification.domain_id == "other"


def test_indian_salary_is_normalised_and_original_kept(loaded, db):
    job = db.scalars(select(Job).where(Job.company == "Sahyadri Technologies")).first()
    assert job.salary_currency == "INR"
    assert job.salary_min == 1_800_000        # 18 LPA
    assert "LPA" in job.salary_raw            # original preserved verbatim
    assert "ctc" in (job.salary_components or [])


def test_sponsorship_verdicts_follow_the_posting(loaded, db):
    user = loaded["user"]

    def verdict(company: str) -> str:
        job = db.scalars(select(Job).where(Job.company == company)).first()
        row = db.scalars(
            select(EligibilityAssessment).where(
                EligibilityAssessment.user_id == user.id, EligibilityAssessment.job_id == job.id
            )
        ).first()
        return row.verdict

    # "not able to sponsor now or in the future" + H1B holder
    assert verdict("Vertex Health") == AuthVerdict.NOT_COMPATIBLE.value
    # "US citizens or permanent residents only"
    assert verdict("Orrin Logistics") == AuthVerdict.NOT_COMPATIBLE.value
    # "We will sponsor and transfer H1B visas"
    assert verdict("Cobalt Bank") == AuthVerdict.POTENTIALLY_COMPATIBLE.value
    # Indian citizen applying in India
    assert verdict("Sahyadri Technologies") == AuthVerdict.COMPATIBLE.value


def test_expired_job_is_archived(loaded, db):
    job = db.scalars(select(Job).where(Job.company == "Lumen Grid Utilities")).first()
    assert job.archived is True
    assert job.priority.deadline_bucket == DeadlineBucket.EXPIRED.value


def test_deadline_today_ranks_first(loaded):
    ranked = loaded["pipeline"].ranked_jobs()
    assert ranked[0].priority.deadline_bucket == DeadlineBucket.TODAY.value


def test_ineligible_jobs_rank_below_actionable_ones(loaded):
    ranked = loaded["pipeline"].ranked_jobs()
    eligible_positions = [i for i, j in enumerate(ranked) if j.priority.eligibility_score > 0]
    ineligible_positions = [i for i, j in enumerate(ranked) if j.priority.eligibility_score == 0]
    assert max(eligible_positions) < min(ineligible_positions)


def test_tailoring_produces_a_factual_resume(loaded, db):
    pipe, user = loaded["pipeline"], loaded["user"]
    job = pipe.ranked_jobs(limit=1)[0]
    doc, report = pipe.tailor_for_job(user, job, use_ai=False)
    db.flush()
    assert report.passed, report.summary
    assert doc.is_final is True
    assert doc.storage_path.startswith("/resumes/tailored/")
    assert doc.rendered_text


def test_full_daily_run_is_clean(db, user, pipeline):
    from careeros.sources.jsonfile import JsonFileSource

    run = pipeline.run_daily([JsonFileSource("data/sample_jobs.json")], user=user, use_ai=False)
    db.flush()
    assert run.ok, run.errors
    assert run.stats["tailored"] > 0
    assert db.scalars(select(Application)).all()
    assert db.scalars(select(ResumeDocument)).all()


def test_daily_run_is_idempotent(db, user, pipeline):
    from careeros.sources.jsonfile import JsonFileSource

    source = JsonFileSource("data/sample_jobs.json")
    pipeline.run_daily([source], user=user, use_ai=False)
    db.flush()
    first = db.scalars(select(Job)).all()
    pipeline.run_daily([source], user=user, use_ai=False)
    db.flush()
    second = db.scalars(select(Job)).all()
    assert len(first) == len(second), "re-running the daily pipeline must not duplicate jobs"


def test_recommendation_answers_the_daily_question(loaded, db):
    rec = loaded["pipeline"].recommend(loaded["user"], for_date=date.today())
    db.flush()
    assert "TOP CAREER TRACK TODAY" in rec.narrative
    assert rec.top_jobs


def test_natural_language_search(loaded, db):
    user = loaded["user"]
    parser = QueryParser()

    sap = run_search(db, parser.parse("Find SAP Security jobs", use_ai=False), user_id=user.id)
    assert sap and all(j.classification.domain_id == "sap" for j in sap)

    india = run_search(db, parser.parse("Find jobs in India", use_ai=False), user_id=user.id)
    assert india and all(j.country_code == "IN" for j in india)

    today = run_search(db, parser.parse("jobs with deadline today", use_ai=False), user_id=user.id)
    assert today and all(j.deadline_on == date.today() for j in today)

    strong = run_search(
        db, parser.parse("jobs where my match is above 85%", use_ai=False), user_id=user.id
    )
    assert all(j.priority.match_score >= 85 for j in strong)


def test_search_for_a_non_technical_role(loaded, db):
    jobs = run_search(
        db, QueryParser().parse("veterinary practice manager", use_ai=False),
        user_id=loaded["user"].id,
    )
    assert any("Veterinary" in j.title for j in jobs)


def test_analytics_reports_low_confidence_on_small_samples(loaded, db):
    from careeros.analytics import Analytics

    data = Analytics(db).dashboard(loaded["user"].id)
    assert "overall" in data and "learning_signals" in data
    if data["overall"]["applications"] < 10:
        assert data["overall"]["note"]


def test_a_new_domain_would_be_persisted(loaded, db):
    """The classifier's escape hatch: an inferred domain becomes a real row."""
    from careeros.engines.classifier import ClassificationResult
    from careeros.pipeline import StageStats

    job = db.scalars(select(Job)).first()
    result = ClassificationResult(domain_id="marine_biology", domain_label="Marine Biology")
    stats = StageStats()
    loaded["pipeline"]._persist_classification(job, result, stats)
    db.flush()
    row = db.get(CareerDomain, "marine_biology")
    assert row is not None and row.origin == "inferred"
    assert "marine_biology" in stats.new_domains
