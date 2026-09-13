"""The sourcing layer, exercised entirely offline.

Every test here drives a recorded payload through a fake transport. The suite
must never open a socket: a connector that reached the network in CI would make
the build depend on a third party's uptime and on keys nobody should need to
run the tests.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from careeros.sources.base import RawJob
from careeros.sources.connectors import SearchQuery, build
from careeros.sources.http import HttpClient, Response
from careeros.sources.jsonld import job_postings, strip_html, to_fields
from careeros.sources.mapping import apply_rule, map_item, resolve_path
from careeros.sources.policy import (
    AccessTier,
    Challenge,
    MissingCredentials,
    NotPermitted,
    looks_like_challenge,
    redact,
)
from careeros.sources.spec import load_specs


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------
class FakeTransport:
    """Answers from a routing table keyed by substring of the URL."""

    def __init__(self, routes: dict[str, object], status: int = 200) -> None:
        self.routes = routes
        self.status = status
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        for needle, payload in self.routes.items():
            if needle in url:
                text = payload if isinstance(payload, str) else json.dumps(payload)
                return Response(status=self.status, body=text, url=url, headers={})
        return Response(status=404, body="not found", url=url, headers={})


def client_for(routes, **kwargs) -> HttpClient:
    c = HttpClient(transport=FakeTransport(routes), rate_limit_per_minute=0, **kwargs)
    return c


def spec_by_id(provider_id: str):
    return next(s for s in load_specs() if s.id == provider_id)


# ---------------------------------------------------------------------------
# the provider file itself
# ---------------------------------------------------------------------------
def test_every_provider_builds_and_declares_a_tier():
    specs = load_specs()
    assert len(specs) >= 20
    for spec in specs:
        source = build(spec)
        assert source.id == spec.id
        assert isinstance(spec.access, AccessTier)
        # A keyed provider must name the variables it needs, or "needs
        # credentials" would be unactionable.
        if spec.requires_key:
            assert spec.required_slots, spec.id
        # Anything that fetches needs a field map or a resolve strategy.
        if spec.kind in ("rest", "ats_board", "serpapi_google_jobs"):
            assert spec.mapping, spec.id
        if spec.kind == "google_cse":
            assert spec.resolve.get("strategy") == "jsonld"


def test_the_boards_the_user_named_are_all_represented():
    """Each named board must resolve to a route, even when it is a refusal."""
    specs = {s.id: s for s in load_specs()}
    # Reachable directly, with the user's own key.
    for pid in ("google_jobs", "google_search_careers", "ziprecruiter", "careerbuilder", "dice", "monster"):
        assert specs[pid].access.may_fetch, pid
    # Not scraped -- and each one must say what to use instead.
    for pid in ("linkedin_direct", "indeed_direct", "glassdoor_direct", "naukri_direct"):
        spec = specs[pid]
        assert spec.access is AccessTier.PROHIBITED
        assert spec.reason.strip(), pid
        assert spec.use_instead, pid
        for alternative in spec.use_instead:
            assert alternative in specs, f"{pid} points at unknown provider {alternative}"
            assert specs[alternative].access.may_fetch


def test_prohibited_providers_refuse_and_name_the_alternative():
    source = build(spec_by_id("linkedin_direct"))
    with pytest.raises(NotPermitted) as excinfo:
        list(source.fetch())
    assert "google_jobs" in excinfo.value.use_instead
    assert "google_jobs" in str(excinfo.value)
    ok, message = source.healthcheck()
    assert not ok and "not permitted" in message


def test_missing_credentials_names_the_variable(monkeypatch):
    monkeypatch.delenv("CAREEROS_ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("CAREEROS_ADZUNA_APP_KEY", raising=False)
    source = build(spec_by_id("adzuna"))
    with pytest.raises(MissingCredentials) as excinfo:
        list(source.fetch(query=SearchQuery(query="network engineer", country="US")))
    assert set(excinfo.value.variables) == {"CAREEROS_ADZUNA_APP_ID", "CAREEROS_ADZUNA_APP_KEY"}


def test_providers_without_keys_are_ready_out_of_the_box():
    ready = [s.id for s in load_specs() if s.available]
    assert {"themuse", "remotive", "arbeitnow", "jobicy"} <= set(ready)


# ---------------------------------------------------------------------------
# rest connector
# ---------------------------------------------------------------------------
ADZUNA_PAGE = {
    "count": 2,
    "results": [
        {
            "id": 4412,
            "title": "Senior Network Engineer",
            "company": {"display_name": "Northwind Systems"},
            "description": "BGP, OSPF and SD-WAN across 14 sites. CCNP required.",
            "redirect_url": "https://www.adzuna.com/land/ad/4412",
            "location": {"display_name": "Austin, Texas"},
            "contract_time": "full_time",
            "created": "2026-09-04T09:12:00Z",
            "salary_min": 140000,
            "salary_max": 170000,
            "currency": "USD",
        },
        {
            "id": 4413,
            "title": "SAP Security Consultant",
            "company": {"display_name": "Halden Consulting"},
            "description": "GRC, SoD analysis, Fiori catalogues.",
            "redirect_url": "https://www.adzuna.com/land/ad/4413",
            "location": {"display_name": "Dallas, Texas"},
            "created": "2026-09-01T00:00:00Z",
        },
    ],
}


def test_rest_connector_maps_a_real_payload_shape(monkeypatch):
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_ID", "id-123")
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_KEY", "key-456")
    client = client_for({"api.adzuna.com": ADZUNA_PAGE})
    source = build(spec_by_id("adzuna"), client)

    jobs = list(source.fetch(limit=2, query=SearchQuery(query="network", country="US", max_pages=1)))
    assert [j.title for j in jobs] == ["Senior Network Engineer", "SAP Security Consultant"]

    first = jobs[0]
    assert first.company == "Northwind Systems"
    assert first.location == "Austin, Texas"
    assert first.posted_on == date(2026, 9, 4)
    assert first.salary_raw == "140000 - 170000 USD"
    assert first.employment_type == "full_time"
    assert first.source == "adzuna"
    assert first.raw["access"] == "official_api"
    # A row with no salary fields must come back as None, not "None - None".
    assert jobs[1].salary_raw is None


def test_rest_connector_puts_the_country_in_the_path_and_the_key_in_the_query(monkeypatch):
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_ID", "id-123")
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_KEY", "key-456")
    transport = FakeTransport({"api.adzuna.com": ADZUNA_PAGE})
    client = HttpClient(transport=transport, rate_limit_per_minute=0)
    source = build(spec_by_id("adzuna"), client)
    list(source.fetch(query=SearchQuery(query="network engineer", location="Austin", country="IN", max_pages=1)))

    _, url, _, _ = transport.calls[0]
    assert "/jobs/in/search/1" in url          # country pack drives the path
    assert "app_id=id-123" in url
    assert "what=network+engineer" in url
    assert "where=Austin" in url


def test_rest_connector_paginates_then_stops_on_an_empty_page(monkeypatch):
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_ID", "a")
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_KEY", "b")

    class Paged(FakeTransport):
        def request(self, method, url, *, headers, body, timeout):
            self.calls.append((method, url, headers, body))
            page = "/search/1" in url
            payload = ADZUNA_PAGE if page else {"count": 0, "results": []}
            return Response(status=200, body=json.dumps(payload), url=url, headers={})

    transport = Paged({})
    source = build(spec_by_id("adzuna"), HttpClient(transport=transport, rate_limit_per_minute=0))
    jobs = list(source.fetch(query=SearchQuery(query="x", country="US", max_pages=3)))
    assert len(jobs) == 2
    assert len(transport.calls) == 2  # stopped as soon as a page came back empty


def test_jooble_sends_its_key_in_the_path_and_its_query_as_json(monkeypatch):
    monkeypatch.setenv("CAREEROS_JOOBLE_KEY", "secret-key")
    payload = {
        "totalCount": 1,
        "jobs": [
            {
                "id": "77",
                "title": "Data Engineer",
                "company": "Sahyadri Technologies",
                "snippet": "Spark, Airflow, dbt.",
                "link": "https://in.jooble.org/jdp/77",
                "location": "Bengaluru",
                "salary": "₹28,00,000 per year",
                "type": "Full-time",
                "updated": "2026-09-08T00:00:00",
            }
        ],
    }
    transport = FakeTransport({"jooble.org/api": payload})
    source = build(spec_by_id("jooble"), HttpClient(transport=transport, rate_limit_per_minute=0))
    jobs = list(source.fetch(query=SearchQuery(query="data engineer", location="Bengaluru", country="IN", max_pages=1)))

    method, url, _, body = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/api/secret-key")
    assert json.loads(body)["keywords"] == "data engineer"
    assert jobs[0].salary_raw == "₹28,00,000 per year"   # left for the pipeline to parse
    assert jobs[0].country == "IN"


# ---------------------------------------------------------------------------
# google jobs
# ---------------------------------------------------------------------------
GOOGLE_JOBS_PAGE = {
    "jobs_results": [
        {
            "job_id": "eyJ2IjoxfQ",
            "title": "Senior Network Security Engineer",
            "company_name": "Cobalt Bank",
            "location": "Austin, TX",
            "via": "via LinkedIn",
            "description": "Palo Alto, Cisco ISE, microsegmentation. Will sponsor H1B transfers.",
            "share_link": "https://www.google.com/search?q=cobalt",
            "apply_options": [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/123"}],
            "detected_extensions": {"posted_at": "3 days ago", "schedule_type": "Full-time", "salary": "$140K - $165K a year"},
        },
        {
            "job_id": "eyJ2IjoyfQ",
            "title": "Network Engineer",
            "company_name": "Vertex Health",
            "location": "Remote",
            "via": "via Indeed",
            "description": "Campus and datacentre networking.",
            "apply_options": [{"link": "https://www.indeed.com/viewjob?jk=abc"}],
            "detected_extensions": {"posted_at": "Just posted"},
        },
    ]
}


def test_google_jobs_records_which_board_each_result_came_from(monkeypatch):
    monkeypatch.setenv("CAREEROS_SERPAPI_KEY", "serp-key")
    client = client_for({"serpapi.com": GOOGLE_JOBS_PAGE})
    source = build(spec_by_id("google_jobs"), client)
    jobs = list(source.fetch(query=SearchQuery(query="network engineer", location="Austin, TX", country="US", max_pages=1)))

    assert [j.raw["board"] for j in jobs] == ["LinkedIn", "Indeed"]
    assert jobs[0].url == "https://www.google.com/search?q=cobalt"
    assert jobs[0].employment_type == "Full-time"
    assert jobs[0].salary_raw == "$140K - $165K a year"
    # "3 days ago" is what Google actually returns; it has to become a date.
    assert jobs[0].posted_on == date.today() - timedelta(days=3)
    assert jobs[1].posted_on == date.today()
    # Falls back to the apply link when there is no share link.
    assert jobs[1].url == "https://www.indeed.com/viewjob?jk=abc"


# ---------------------------------------------------------------------------
# google search -> company career pages
# ---------------------------------------------------------------------------
CAREER_PAGE = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Staff Network Engineer","identifier":"REQ-9912",
 "hiringOrganization":{"@type":"Organization","name":"Northwind Systems"},
 "datePosted":"2026-09-02","validThrough":"2026-10-15",
 "employmentType":"FULL_TIME","url":"https://careers.northwind.example/req/9912",
 "description":"<p>Own BGP and OSPF across 14 sites.</p><ul><li>CCNP required</li></ul>",
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
   "value":{"@type":"QuantitativeValue","minValue":155000,"maxValue":185000,"unitText":"YEAR"}}}
</script></head><body>Apply now</body></html>"""

CSE_PAGE = {
    "items": [
        {"link": "https://careers.northwind.example/req/9912"},
        {"link": "https://blog.example.com/we-are-hiring"},
    ]
}


def test_google_search_reads_the_posting_from_the_pages_own_structured_data(monkeypatch):
    monkeypatch.setenv("CAREEROS_GOOGLE_CSE_KEY", "cse-key")
    monkeypatch.setenv("CAREEROS_GOOGLE_CSE_CX", "cse-cx")
    client = client_for({
        "googleapis.com/customsearch": CSE_PAGE,
        "careers.northwind.example": CAREER_PAGE,
        "blog.example.com": "<html><body>No structured data here</body></html>",
        "robots.txt": "User-agent: *\nAllow: /\n",
    })
    source = build(spec_by_id("google_search_careers"), client)
    jobs = list(source.fetch(query=SearchQuery(query='"network engineer" careers', country="US", max_pages=1)))

    # The blog post has no JobPosting, so it is skipped rather than guessed at.
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Staff Network Engineer"
    assert job.company == "Northwind Systems"
    assert job.external_id == "REQ-9912"
    assert job.city == "Austin" and job.region == "TX" and job.country == "US"
    assert job.deadline_on == date(2026, 10, 15)
    assert job.salary_raw == "USD 155000 - 185000 per year"
    assert "CCNP required" in job.description
    assert job.raw["board"] == "Company career site"
    assert job.raw["access"] == "public_page"


def test_a_career_page_disallowed_by_robots_is_not_fetched(monkeypatch):
    monkeypatch.setenv("CAREEROS_GOOGLE_CSE_KEY", "k")
    monkeypatch.setenv("CAREEROS_GOOGLE_CSE_CX", "x")
    transport = FakeTransport({
        "googleapis.com/customsearch": CSE_PAGE,
        "careers.northwind.example/robots.txt": "User-agent: *\nDisallow: /req/\n",
        "careers.northwind.example": CAREER_PAGE,
        "blog.example.com": "<html></html>",
    })
    source = build(spec_by_id("google_search_careers"), HttpClient(transport=transport, rate_limit_per_minute=0))
    assert list(source.fetch(query=SearchQuery(query="x", country="US", max_pages=1))) == []
    fetched = [url for _, url, _, _ in transport.calls]
    assert not any("/req/9912" in url and "robots" not in url for url in fetched)


# ---------------------------------------------------------------------------
# ats boards
# ---------------------------------------------------------------------------
GREENHOUSE_BOARD = {
    "jobs": [
        {
            "id": 5550001,
            "title": "Network Reliability Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/5550001",
            "location": {"name": "Austin, TX"},
            "updated_at": "2026-09-05T14:00:00-05:00",
            "content": "&lt;p&gt;Run the backbone. &lt;strong&gt;BGP&lt;/strong&gt; required.&lt;/p&gt;",
        },
        {
            "id": 5550002,
            "title": "Payroll Analyst",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/5550002",
            "location": {"name": "Remote"},
            "content": "Payroll for 400 people.",
        },
    ]
}


def test_ats_board_reads_one_employer_and_filters_locally(monkeypatch):
    monkeypatch.setenv("CAREEROS_GREENHOUSE_BOARDS", "acme, globex")
    client = client_for({"boards-api.greenhouse.io": GREENHOUSE_BOARD})
    source = build(spec_by_id("greenhouse"), client)

    every = list(source.fetch(query=SearchQuery()))
    assert len(every) == 4  # two boards, two roles each
    assert every[0].company == "acme" and every[2].company == "globex"
    assert "BGP" in every[0].description and "&lt;" not in every[0].description

    # Board APIs have no search parameter, so the connector filters.
    filtered = list(source.fetch(query=SearchQuery(query="payroll")))
    assert [j.title for j in filtered] == ["Payroll Analyst", "Payroll Analyst"]


def test_one_broken_board_token_does_not_sink_the_others(monkeypatch):
    monkeypatch.setenv("CAREEROS_GREENHOUSE_BOARDS", "missing,acme")

    class Partial(FakeTransport):
        def request(self, method, url, *, headers, body, timeout):
            self.calls.append((method, url, headers, body))
            if "/boards/missing/" in url:
                return Response(status=404, body="not found", url=url, headers={})
            return Response(status=200, body=json.dumps(GREENHOUSE_BOARD), url=url, headers={})

    source = build(spec_by_id("greenhouse"), HttpClient(transport=Partial({}), rate_limit_per_minute=0))
    jobs = list(source.fetch(query=SearchQuery()))
    assert [j.company for j in jobs] == ["acme", "acme"]


# ---------------------------------------------------------------------------
# rss
# ---------------------------------------------------------------------------
FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Senior Network Engineer at Northwind Systems</title>
 <link>https://example.com/jobs/1</link>
 <description>&lt;p&gt;BGP and OSPF.&lt;/p&gt;</description>
 <pubDate>2026-09-07</pubDate></item>
<item><title>Chef</title><link>https://example.com/jobs/2</link>
 <description>Kitchen work.</description></item>
</channel></rss>"""


def test_rss_splits_title_from_company_and_strips_markup(monkeypatch):
    monkeypatch.setenv("CAREEROS_RSS_FEEDS", "https://example.com/feed.xml")
    client = client_for({"example.com/feed.xml": FEED})
    source = build(spec_by_id("rss"), client)
    jobs = list(source.fetch(query=SearchQuery()))

    assert jobs[0].title == "Senior Network Engineer"
    assert jobs[0].company == "Northwind Systems"
    assert jobs[0].description == "BGP and OSPF."
    assert jobs[0].posted_on == date(2026, 9, 7)
    assert jobs[1].title == "Chef" and jobs[1].company == "Unknown"


# ---------------------------------------------------------------------------
# policy: this is the part that must not be talked around
# ---------------------------------------------------------------------------
def test_bot_protection_stops_the_host_and_is_never_retried(monkeypatch):
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_ID", "a")
    monkeypatch.setenv("CAREEROS_ADZUNA_APP_KEY", "b")
    transport = FakeTransport({"api.adzuna.com": "<html>Attention Required! | Cloudflare</html>"})
    client = HttpClient(transport=transport, rate_limit_per_minute=0)
    source = build(spec_by_id("adzuna"), client)

    with pytest.raises(Challenge):
        list(source.fetch(query=SearchQuery(query="x", country="US", max_pages=3)))
    calls = len(transport.calls)
    assert calls == 1, "a challenge must not be retried"

    # And the host stays blocked for the rest of the run.
    with pytest.raises(Challenge):
        client.fetch("https://api.adzuna.com/anything")
    assert len(transport.calls) == calls


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_every_gate_status_counts_as_a_challenge(status):
    challenged, _ = looks_like_challenge(status, "")
    assert challenged


def test_only_public_page_fetches_consult_robots():
    assert AccessTier.PUBLIC_PAGE.checks_robots
    for tier in (AccessTier.OFFICIAL_API, AccessTier.LICENSED_AGGREGATOR, AccessTier.PUBLIC_FEED):
        assert not tier.checks_robots


def test_credentials_never_survive_into_a_message():
    dirty = "GET https://api.adzuna.com/v1?app_id=abc123&app_key=s3cr3t&what=eng"
    clean = redact(dirty)
    assert "abc123" not in clean and "s3cr3t" not in clean
    assert "what=eng" in clean


def test_non_http_schemes_are_refused():
    from careeros.sources.policy import SourceError

    client = client_for({})
    with pytest.raises(SourceError):
        client.fetch("file:///etc/passwd")


def test_status_report_carries_no_secrets(monkeypatch):
    """`status()` is handed to the API, the dashboard and the agent."""
    monkeypatch.setenv("CAREEROS_SERPAPI_KEY", "super-secret-value")
    blob = json.dumps([s.status() for s in load_specs()])
    assert "super-secret-value" not in blob

    # The variable *name* is safe to publish and has to be, or the UI cannot
    # tell the user what to set. Only the value is a secret.
    monkeypatch.delenv("CAREEROS_SERPAPI_KEY")
    assert spec_by_id("google_jobs").status()["missing_env"] == ["CAREEROS_SERPAPI_KEY"]


# ---------------------------------------------------------------------------
# dedupe across all these sources
# ---------------------------------------------------------------------------
def test_the_same_role_from_three_boards_has_one_fingerprint():
    """The whole point of sourcing widely is that it must not multiply jobs."""
    via_linkedin = RawJob(
        source="google_jobs", external_id="1", title="Sr. Network Engineer",
        company="Northwind Systems", description="", location="Austin, TX", city="Austin",
    )
    via_adzuna = RawJob(
        source="adzuna", external_id="4412", title="Senior Network Engineer",
        company="Northwind  Systems", description="", location="Austin, Texas", city="Austin",
    )
    via_company = RawJob(
        source="google_search_careers", external_id="REQ-9912", title="Network Engineer, Senior",
        company="northwind systems", description="", location="Austin", city="Austin",
    )
    prints = {j.fingerprint() for j in (via_linkedin, via_adzuna, via_company)}
    assert len(prints) == 1

    # A different city is a different job and must not be collapsed.
    other_city = RawJob(
        source="adzuna", external_id="9", title="Senior Network Engineer",
        company="Northwind Systems", description="", location="Dallas, TX", city="Dallas",
    )
    assert other_city.fingerprint() not in prints
