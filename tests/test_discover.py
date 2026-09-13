"""Discovery: planning searches from a profile, running them, and ingesting.

Offline throughout. The providers are driven by a fake transport, so what is
under test is the planning, the fan-out, the failure isolation and the join
into the pipeline -- not anyone's API being up.
"""

from __future__ import annotations

import json

import pytest

from careeros.sources import build_registry
from careeros.sources.base import SourceRegistry
from careeros.sources.connectors import SearchQuery
from careeros.sources.discover import discover, run_plan
from careeros.sources.http import HttpClient, Response
from careeros.sources.plan import build_plan, career_page_query, search_countries, search_terms
from careeros.sources.spec import load_specs

REMOTIVE = {
    "jobs": [
        {
            "id": 900,
            "title": "Senior Network Engineer",
            "company_name": "Northwind Systems",
            "description": "BGP, OSPF, SD-WAN. Remote across the US.",
            "url": "https://remotive.com/remote-jobs/900",
            "candidate_required_location": "USA",
            "job_type": "full_time",
            "publication_date": "2026-09-05T00:00:00",
        }
    ]
}
THEMUSE = {
    "results": [
        {
            "id": 7001,
            "name": "Cybersecurity Analyst",
            "company": {"name": "Vertex Health"},
            "contents": "<p>SIEM, incident response, Splunk.</p>",
            "refs": {"landing_page": "https://www.themuse.com/jobs/vertex/7001"},
            "locations": [{"name": "Austin, TX"}],
            "publication_date": "2026-09-06T00:00:00Z",
        }
    ]
}


class Router:
    def __init__(self, table: dict[str, object], fail: tuple[str, ...] = ()) -> None:
        self.table = table
        self.fail = fail
        self.urls: list[str] = []

    def request(self, method, url, *, headers, body, timeout):
        self.urls.append(url)
        for needle in self.fail:
            if needle in url:
                return Response(status=500, body="boom", url=url, headers={})
        for needle, payload in self.table.items():
            if needle in url:
                text = payload if isinstance(payload, str) else json.dumps(payload)
                return Response(status=200, body=text, url=url, headers={})
        return Response(status=404, body="no route", url=url, headers={})


@pytest.fixture
def client_factory():
    def make(table, fail=()):
        transport = Router(table, fail)
        return HttpClient(transport=transport, rate_limit_per_minute=0), transport

    return make


# ---------------------------------------------------------------------------
# planning comes from the person, not a hard-coded list
# ---------------------------------------------------------------------------
def test_search_terms_come_from_the_users_own_career_tracks(user):
    terms = search_terms(user)
    assert terms, "a profile with career tracks must produce search terms"
    lowered = " ".join(terms).lower()
    # The sample profile's tracks are networking/security/cloud/SAP. Nothing in
    # the source layer knows those words; they come from the profile.
    assert any(word in lowered for word in ("network", "security", "cloud", "sap"))


def test_searches_only_run_where_the_user_can_work(user):
    countries = search_countries(user)
    assert set(countries) <= {"US", "IN"}
    assert "US" in countries and "IN" in countries

    plan = build_plan(user, load_specs())
    for search in plan.searches:
        assert search.query.country in countries


def test_a_provider_that_covers_no_authorised_country_is_not_planned(user):
    specs = [s for s in load_specs() if s.id == "usajobs"]   # US only
    plan = build_plan(user, specs, countries=["IN"])
    assert plan.searches == []


def test_board_providers_get_one_unfiltered_pass(user):
    specs = [s for s in load_specs() if s.id == "greenhouse"]
    plan = build_plan(user, specs)
    assert len(plan.searches) == 1
    assert plan.searches[0].query.query == ""   # the connector filters instead


def test_the_career_page_query_steers_away_from_the_aggregators():
    query = career_page_query("network engineer", "US")
    assert '"network engineer"' in query
    for board in ("linkedin.com", "indeed.com", "naukri.com", "dice.com"):
        assert f"-site:{board}" in query


# ---------------------------------------------------------------------------
# running a plan
# ---------------------------------------------------------------------------
def test_a_run_collects_from_every_ready_provider(user, client_factory):
    client, transport = client_factory({"remotive.com": REMOTIVE, "themuse.com": THEMUSE})
    registry = build_registry(into=SourceRegistry(), client=client)
    plan = build_plan(user, [s for s in load_specs() if s.available])

    result = run_plan(plan, registry, only=["remotive", "themuse"])

    titles = {job.title for job in result.jobs}
    assert "Senior Network Engineer" in titles
    assert "Cybersecurity Analyst" in titles
    assert result.by_board  # every posting is attributed to a board
    assert all(outcome.provider_id in ("remotive", "themuse") for outcome in result.outcomes)


def test_a_provider_without_credentials_is_skipped_with_a_reason(user, client_factory, monkeypatch):
    monkeypatch.delenv("CAREEROS_ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("CAREEROS_ADZUNA_APP_KEY", raising=False)
    client, _ = client_factory({})
    registry = build_registry(into=SourceRegistry(), client=client)
    plan = build_plan(user, load_specs())

    result = run_plan(plan, registry, only=["adzuna"])
    outcome = next(o for o in result.outcomes if o.provider_id == "adzuna")
    assert outcome.found == 0
    assert "CAREEROS_ADZUNA_APP_ID" in (outcome.skipped_reason or "")


def test_a_prohibited_board_is_skipped_and_says_why(user, client_factory):
    client, _ = client_factory({})
    registry = build_registry(into=SourceRegistry(), client=client)
    plan = build_plan(user, load_specs())
    # The planner still plans it; the runner is what refuses, and explains.
    result = run_plan(plan, registry, only=["linkedin_direct"])
    outcome = next(o for o in result.outcomes if o.provider_id == "linkedin_direct")
    assert outcome.found == 0
    assert "google_jobs" in (outcome.skipped_reason or "")


def test_one_failing_provider_does_not_sink_the_run(user, client_factory):
    client, _ = client_factory(
        {"remotive.com": REMOTIVE, "themuse.com": THEMUSE}, fail=("themuse.com",)
    )
    registry = build_registry(into=SourceRegistry(), client=client)
    plan = build_plan(user, [s for s in load_specs() if s.available])

    result = run_plan(plan, registry, only=["remotive", "themuse"])
    assert [j.title for j in result.jobs] == ["Senior Network Engineer"]
    failed = next(o for o in result.outcomes if o.provider_id == "themuse")
    assert failed.errors and failed.found == 0


def test_a_challenged_host_is_dropped_for_the_rest_of_the_run(user, client_factory):
    client, transport = client_factory(
        {"remotive.com": "<html>Attention Required! | Cloudflare</html>"}
    )
    registry = build_registry(into=SourceRegistry(), client=client)
    plan = build_plan(user, load_specs())

    result = run_plan(plan, registry, only=["remotive"])
    outcome = next(o for o in result.outcomes if o.provider_id == "remotive")
    assert outcome.found == 0 and outcome.errors
    assert len([u for u in transport.urls if "remotive.com" in u]) == 1


def test_the_per_provider_cap_is_honoured(user, client_factory):
    # Distinct companies, or the fingerprint would correctly collapse them --
    # one-character title differences are not different jobs.
    many = {
        "jobs": [
            dict(REMOTIVE["jobs"][0], id=i, company_name=f"Employer {i:02d} Holdings")
            for i in range(40)
        ]
    }
    client, _ = client_factory({"remotive.com": many})
    registry = build_registry(into=SourceRegistry(), client=client)
    plan = build_plan(user, load_specs())

    result = run_plan(plan, registry, per_provider=5, only=["remotive"])
    assert len(result.jobs) == 5


# ---------------------------------------------------------------------------
# the join into the pipeline
# ---------------------------------------------------------------------------
def test_discovered_postings_are_ingested_and_deduplicated(db, user, client_factory):
    from careeros.db.models import Job

    client, _ = client_factory({"remotive.com": REMOTIVE, "themuse.com": THEMUSE})
    registry = build_registry(into=SourceRegistry(), client=client)

    report = discover(db, user, registry=registry, only=["remotive", "themuse"])
    assert report["ingest"]["inserted"] >= 2
    assert report["by_board"]

    stored = {job.title for job in db.query(Job).all()}
    assert "Senior Network Engineer" in stored

    # Running again finds the same postings and inserts nothing new.
    again = discover(db, user, registry=registry, only=["remotive", "themuse"])
    assert again["ingest"]["inserted"] == 0
    assert again["ingest"]["duplicates"] >= 2


def test_a_dry_run_saves_nothing(db, user, client_factory):
    from careeros.db.models import Job

    before = db.query(Job).count()
    client, _ = client_factory({"remotive.com": REMOTIVE})
    registry = build_registry(into=SourceRegistry(), client=client)

    report = discover(db, user, registry=registry, only=["remotive"], ingest=False)
    assert report["found"] >= 1
    assert "ingest" not in report
    assert db.query(Job).count() == before


def test_discovered_jobs_go_through_the_same_stages_as_any_other(db, user, pipeline, client_factory):
    """A sourced posting must be classified and ranked like a pasted one."""
    from careeros.db.models import Job

    client, _ = client_factory({"remotive.com": REMOTIVE})
    registry = build_registry(into=SourceRegistry(), client=client)
    discover(db, user, registry=registry, only=["remotive"])

    job = db.query(Job).filter(Job.source == "remotive").first()
    assert job is not None

    pipeline.classify_jobs([job])
    db.flush()
    assert job.classification is not None
    assert job.classification.domain_id      # a domain was assigned, seeded or new
