"""API surface smoke tests, including the guards that must never be bypassable."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(loaded, monkeypatch):
    from careeros.api import deps
    from careeros.api.app import app

    session = loaded["pipeline"].session

    def _session_override():
        yield session

    app.dependency_overrides[deps.get_db] = _session_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def mailbox(tmp_path):
    path = tmp_path / "mailbox.json"
    path.write_text(json.dumps({"messages": [
        {"id": "m1", "thread_id": "t1", "from": "Dana <dana@cobaltbank.com>",
         "subject": "Interview invitation - Senior Network Security Engineer",
         "received_at": "2026-09-12T15:04:00",
         "body": "We would like to schedule a call for the Senior Network Security Engineer "
                 "role at Cobalt Bank. Are you free on 2026-09-18 14:00?"},
        {"id": "m2", "thread_id": "t2", "from": "hr@orrin.com",
         "subject": "Quick question", "received_at": "2026-09-13T08:00:00",
         "body": "Will you require H1B sponsorship now or in the future?"},
    ]}), encoding="utf-8")
    return str(path)


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert set(body["countries"]) == {"US", "IN"}


def test_country_config_is_served_from_the_packs(client):
    body = client.get("/api/config/countries").json()
    codes = {c["code"] for c in body["countries"]}
    assert codes == {"US", "IN"}
    india = next(c for c in body["countries"] if c["code"] == "IN")
    assert india["currency"] == "INR"
    assert any(e["id"] == "c2c" for c in body["countries"] if c["code"] == "US"
               for e in c["employment_types"])


def test_taxonomy_config_says_it_is_only_a_seed(client):
    body = client.get("/api/config/taxonomy").json()
    assert body["skill_count"] > 50
    assert "no code change" in body["note"]


def test_job_list_and_card_shape(client):
    body = client.get("/api/jobs").json()
    assert body["count"] > 0
    card = body["jobs"][0]
    for key in ("title", "company", "country", "location", "salary", "scores",
                "priority", "classification", "eligibility", "application"):
        assert key in card, key
    assert card["priority"]["flag"]
    assert card["salary"]["display"]


def test_eligibility_shows_its_source_and_disclaimer(client):
    jobs = client.get("/api/jobs").json()["jobs"]
    vertex = next(j for j in jobs if j["company"] == "Vertex Health")
    assert vertex["eligibility"]["verdict"] == "not_compatible"
    assert vertex["eligibility"]["evidence"][0]["quote"]
    assert "verify with employer" in vertex["eligibility"]["disclaimer"]


def test_facets_reflect_the_data(client):
    body = client.get("/api/jobs/facets").json()
    assert {c["code"] for c in body["countries"]} == {"US", "IN"}
    assert any(d["id"] == "other" for d in body["domains"])


def test_natural_language_search_endpoint(client):
    body = client.post("/api/jobs/search", json={"query": "SAP jobs", "use_ai": False}).json()
    assert body["count"] >= 1
    assert "sap" in body["filters_description"]


def test_master_and_tailored_resume(client):
    master = client.post("/api/resumes/master").json()
    assert master["is_final"] is True
    assert master["factuality"] is None or master["factuality"]["passed"]

    job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
    tailored = client.post("/api/resumes/tailor", json={"job_id": job_id, "use_ai": False}).json()
    assert tailored["factuality"]["passed"] is True
    assert tailored["is_final"] is True
    assert tailored["rendered_text"]


def test_cover_letter_reports_unclaimed_gaps(client):
    jobs = client.get("/api/jobs").json()["jobs"]
    sap = next(j for j in jobs if j["company"] == "Halden Consulting")
    body = client.post(
        "/api/resumes/cover-letter", json={"job_id": sap["id"], "use_ai": False}
    ).json()
    # Naming the role applied for is fine; claiming SAP *experience* is not.
    experience = body["body"].lower().split("relevant experience:", 1)[-1]
    assert "sap" not in experience
    assert "pfcg" not in body["body"].lower()
    assert body["unclaimed_gaps"]


def test_application_lifecycle(client):
    # An application row is created by `prepare` (or by the daily run).
    job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
    client.post("/api/applications/prepare", json={"job_id": job_id})

    apps = client.get("/api/applications").json()
    assert apps["count"] > 0
    app_id = apps["applications"][0]["id"]
    assert client.post(f"/api/applications/{app_id}/status",
                       json={"status": "applied"}).json()["status"] == "applied"
    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["events"][-1]["to"] == "applied"

    bad = client.post(f"/api/applications/{app_id}/status", json={"status": "teleported"})
    assert bad.status_code == 422


def test_prepare_is_assisted_and_says_so(client):
    job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
    client.post("/api/resumes/tailor", json={"job_id": job_id, "use_ai": False})
    body = client.post("/api/applications/prepare", json={"job_id": job_id}).json()
    assert body["submission_mode"] == "assisted"
    assert "never bypasses CAPTCHA" in body["notice"]


def test_email_sync_classifies_and_blocks_high_impact_drafts(client, mailbox):
    stats = client.post(
        "/api/email/sync", json={"mailbox": mailbox, "use_ai": False}
    ).json()
    assert stats["stored"] == 2
    assert stats["blocked"] == 1

    drafts = client.get("/api/email/drafts").json()["drafts"]
    blocked = next(d for d in drafts if d["high_impact_topics"])
    assert blocked["may_send"] is False

    refused = client.post(f"/api/email/drafts/{blocked['id']}/approve")
    assert refused.status_code == 409
    assert "does not approve" in refused.json()["detail"]

    ordinary = next(d for d in drafts if not d["high_impact_topics"])
    assert client.post(f"/api/email/drafts/{ordinary['id']}/approve").status_code == 200


def test_editing_a_draft_re_runs_the_high_impact_check(client, mailbox):
    client.post("/api/email/sync", json={"mailbox": mailbox, "use_ai": False})
    drafts = client.get("/api/email/drafts").json()["drafts"]
    ordinary = next(d for d in drafts if not d["high_impact_topics"])
    body = client.post(
        f"/api/email/drafts/{ordinary['id']}/edit",
        json={"body": "Happy to discuss - my target base salary is $180,000."},
    ).json()
    assert body["state"] == "blocked_high_impact"
    assert "salary_negotiation" in body["high_impact_topics"]


def test_recommendations_and_analytics(client):
    rec = client.get("/api/recommendations?refresh=true").json()
    assert "TOP CAREER TRACK TODAY" in rec["narrative"]

    analytics = client.get("/api/analytics").json()
    assert "overall" in analytics and "by_track" in analytics

    learning = client.get("/api/analytics/learning").json()
    assert "never recommends claiming" in learning["disclaimer"]


def test_profile_and_evidence_endpoints(client):
    profile = client.get("/api/profile").json()
    assert profile["evidence_count"] > 0
    assert len(profile["career_tracks"]) == 4
    assert {a["country"] for a in profile["work_authorization"]} == {"US", "IN"}

    evidence = client.get("/api/profile/evidence").json()
    assert evidence["count"] == profile["evidence_count"]
    assert all("text" in e for e in evidence["evidence"])


def test_dashboard_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "CareerOS" in response.text


# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------
def test_agent_tools_endpoint_publishes_the_surface_and_the_withholdings(client):
    body = client.get("/api/agent/tools").json()
    assert body["count"] >= 15
    names = {t["name"] for t in body["tools"]}
    assert "tailor_resume" in names and "get_match_detail" in names

    withheld = {w["name"] for w in body["withheld_capabilities"]}
    assert {"add_evidence", "send_email", "mark_resume_final"} <= withheld
    assert names.isdisjoint(withheld), "a withheld capability is exposed as a tool"
    assert all(w["reason"] for w in body["withheld_capabilities"])


def test_agent_ask_returns_503_without_model_access(client, monkeypatch):
    """The deterministic pipeline works offline; the reasoning loop cannot."""
    response = client.post("/api/agent/ask", json={"prompt": "what should I do today?"})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_agent_ask_validates_input(client):
    assert client.post("/api/agent/ask", json={"prompt": ""}).status_code == 422
    assert client.post(
        "/api/agent/ask", json={"prompt": "x", "max_turns": 99}
    ).status_code == 422
    assert client.post(
        "/api/agent/ask", json={"prompt": "x", "effort": "turbo"}
    ).status_code == 422


def test_agent_runs_endpoint(client):
    body = client.get("/api/agent/runs").json()
    assert "runs" in body and "count" in body


# ===========================================================================
# Sign-in and sourcing
# ===========================================================================
@pytest.fixture
def signed_in_client(client, monkeypatch, tmp_path):
    """A client that must present a session token, with Google configured."""
    monkeypatch.setenv("CAREEROS_HOME", str(tmp_path))
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("CAREEROS_GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("CAREEROS_AUTH", "required")
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    return client


def test_describe_is_reachable_without_signing_in(signed_in_client):
    """The login screen has to be able to ask what methods exist."""
    response = signed_in_client.get("/api/auth/describe")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "required"
    assert "google" in body["methods"]
    assert body["passwords"] == "never stored or requested"


def test_protected_endpoints_refuse_and_point_at_sign_in(signed_in_client):
    response = signed_in_client.get("/api/jobs")
    assert response.status_code == 401
    assert response.json()["sign_in"] == "/api/auth/describe"


def test_a_session_token_opens_the_api(signed_in_client, loaded):
    from careeros.auth.tokens import issue_token

    user = loaded["user"]
    token = issue_token(user.id, user.email, method="google")
    response = signed_in_client.get("/api/jobs", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["count"] >= 1


def test_whoami_reports_the_signed_in_account(signed_in_client, loaded):
    from careeros.auth.tokens import issue_token

    user = loaded["user"]
    token = issue_token(user.id, user.email, method="email_code")
    response = signed_in_client.get("/api/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["signed_in"] is True
    assert body["method"] == "email_code"
    assert body["user"]["email"] == user.email
    assert body["user"]["has_profile"] is True


def test_a_forged_token_is_refused(signed_in_client):
    response = signed_in_client.get(
        "/api/jobs", headers={"Authorization": "Bearer cos1.forged.signature"}
    )
    assert response.status_code == 401


def test_the_google_start_endpoint_returns_a_consent_url(signed_in_client):
    response = signed_in_client.post("/api/auth/google/start", json={"include_gmail": True})
    assert response.status_code == 200
    body = response.json()
    assert body["authorization_url"].startswith("https://accounts.google.com/")
    assert "gmail.compose" in body["authorization_url"]
    assert "gmail.send" not in body["authorization_url"]


def test_google_start_says_what_is_missing_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("CAREEROS_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CAREEROS_GOOGLE_CLIENT_SECRET", raising=False)
    response = client.post("/api/auth/google/start", json={})
    assert response.status_code == 503
    assert "CAREEROS_GOOGLE_CLIENT_ID" in response.json()["detail"]


def test_the_sources_list_explains_every_board(client):
    response = client.get("/api/sources")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 20

    rows = {row["id"]: row for row in body["providers"]}
    # Google Jobs is the route to the big boards.
    assert rows["google_jobs"]["access"] == "licensed_aggregator"
    # And the boards that are not scraped say so, and say what replaces them.
    for provider_id in ("linkedin_direct", "indeed_direct", "naukri_direct"):
        row = rows[provider_id]
        assert row["state"] == "not_permitted"
        assert row["reason"]
        assert row["use_instead"]


def test_the_sources_list_never_contains_a_credential(client, monkeypatch):
    monkeypatch.setenv("CAREEROS_SERPAPI_KEY", "sk-do-not-leak-me")
    response = client.get("/api/sources")
    assert "sk-do-not-leak-me" not in response.text


def test_sources_can_be_scoped_to_a_country(client):
    india = client.get("/api/sources", params={"country": "IN"}).json()
    ids = {row["id"] for row in india["providers"]}
    assert "naukri_direct" in ids       # India's biggest board, explained
    assert "usajobs" not in ids         # US federal only


def test_the_plan_endpoint_derives_searches_from_the_profile(client):
    response = client.get("/api/sources/plan")
    assert response.status_code == 200
    body = response.json()
    assert set(body["countries"]) <= {"US", "IN"}
    assert body["terms"], "a loaded profile must yield search terms"
    assert "hard-coded" in body["note"]
