"""Transport security and web serving.

A mobile app changes the threat model: the server stops being a localhost-only
process and starts listening on a network a phone is on. These tests cover the
two things that protects -- a bearer token and a CORS policy -- plus the
routing rules for serving the universal app's web build.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from careeros.api import security


@pytest.fixture
def app_module(monkeypatch):
    """Reload the app so import-time decisions (auth, web build) are re-made."""

    def _load():
        import careeros.api.app as module

        return importlib.reload(module)

    return _load


# ---------------------------------------------------------------------------
# Token auth
# ---------------------------------------------------------------------------
def test_open_by_default(monkeypatch, app_module, loaded):
    """No token configured -- the single-user desktop case stays frictionless."""
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    module = app_module()
    with TestClient(module.app) as client:
        assert client.get("/api/health").json()["auth_required"] is False
        assert client.get("/api/jobs").status_code == 200


def test_token_required_when_configured(monkeypatch, app_module, loaded):
    monkeypatch.setenv("CAREEROS_API_TOKEN", "tok-abc")
    module = app_module()
    with TestClient(module.app) as client:
        # Health stays public so a client can discover that auth is on.
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["auth_required"] is True

        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/jobs", headers={"Authorization": "tok-abc"}).status_code == 401
        assert (
            client.get("/api/jobs", headers={"Authorization": "Bearer tok-abc"}).status_code == 200
        )


def test_401_explains_how_to_fix_it(monkeypatch, app_module, loaded):
    monkeypatch.setenv("CAREEROS_API_TOKEN", "tok-abc")
    module = app_module()
    with TestClient(module.app) as client:
        response = client.get("/api/jobs")
        assert "CAREEROS_API_TOKEN" in response.json()["detail"]
        assert response.headers["www-authenticate"] == "Bearer"


def test_write_endpoints_are_covered_too(monkeypatch, app_module, loaded):
    """Middleware covers every path by construction, not endpoint by endpoint."""
    monkeypatch.setenv("CAREEROS_API_TOKEN", "tok-abc")
    module = app_module()
    with TestClient(module.app) as client:
        assert client.post("/api/jobs/search", json={"query": "x"}).status_code == 401
        assert client.post("/api/resumes/master").status_code == 401
        assert client.post("/api/agent/ask", json={"prompt": "x"}).status_code == 401


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
def test_dev_origins_by_default(monkeypatch):
    monkeypatch.delenv("CAREEROS_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    origins = security.allowed_origins()
    assert "http://localhost:8081" in origins      # the Expo web dev server
    assert "*" not in origins


def test_wildcard_cors_is_refused_without_a_token(monkeypatch):
    """An open API plus `*` would let any page the user visits read their data."""
    monkeypatch.setenv("CAREEROS_CORS_ORIGINS", "*")
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    assert "*" not in security.allowed_origins()


def test_wildcard_cors_allowed_once_a_token_is_set(monkeypatch):
    monkeypatch.setenv("CAREEROS_CORS_ORIGINS", "*")
    monkeypatch.setenv("CAREEROS_API_TOKEN", "tok-abc")
    assert security.allowed_origins() == ["*"]


def test_explicit_origins_are_honoured(monkeypatch):
    monkeypatch.setenv("CAREEROS_CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    assert security.allowed_origins() == ["https://a.example", "https://b.example"]


# ---------------------------------------------------------------------------
# Serving the universal app's web build
# ---------------------------------------------------------------------------
def test_classic_dashboard_needs_no_build_step(monkeypatch, app_module, loaded):
    """The zero-dependency dashboard stays available whether or not npm ran."""
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    module = app_module()
    with TestClient(module.app) as client:
        response = client.get("/classic")
        assert response.status_code == 200
        assert "CareerOS" in response.text


def test_root_serves_a_page(monkeypatch, app_module, loaded):
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    module = app_module()
    with TestClient(module.app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "<html" in response.text.lower()


def test_unmatched_api_path_is_a_json_404_not_the_spa(monkeypatch, app_module, loaded):
    """The SPA fallback must never answer for /api: a client typo has to fail
    loudly rather than receive HTML with status 200."""
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    module = app_module()
    if not module.EXPO_WEB_DIR.exists():
        pytest.skip("web build not present; run `npm --prefix clients/app run export:web`")
    with TestClient(module.app) as client:
        response = client.get("/api/definitely-not-a-route")
        assert response.status_code == 404
        assert response.json()["detail"] == "Not Found"
        assert "<html" not in response.text.lower()


def test_deep_links_fall_back_to_the_spa(monkeypatch, app_module, loaded):
    """A refresh on /insights or /job/1 must not 404 -- those are client routes."""
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    module = app_module()
    if not module.EXPO_WEB_DIR.exists():
        pytest.skip("web build not present; run `npm --prefix clients/app run export:web`")
    with TestClient(module.app) as client:
        for path in ("/today", "/insights", "/job/1", "/settings", "/anything-else"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "<html" in response.text.lower(), path


def test_static_serving_cannot_escape_the_build_directory(monkeypatch, app_module, loaded):
    monkeypatch.delenv("CAREEROS_API_TOKEN", raising=False)
    module = app_module()
    if not module.EXPO_WEB_DIR.exists():
        pytest.skip("web build not present")
    with TestClient(module.app) as client:
        response = client.get("/../../careeros/cli.py")
        assert "def main(" not in response.text
        assert "ANTHROPIC" not in response.text
