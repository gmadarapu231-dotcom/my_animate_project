"""Test fixtures.

Every test runs with `CAREEROS_LLM=off`, so results are deterministic even on a
machine that has Anthropic credentials configured. The point is to prove the
product works without the model, not to mock it.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("CAREEROS_LLM", "off")


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setenv("CAREEROS_LLM", "off")
    from careeros.ai.provider import reset_provider

    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh SQLite database per test."""
    monkeypatch.setenv("CAREEROS_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    from careeros.db.session import init_db, new_session, reset_engine

    reset_engine()
    init_db()
    session = new_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()
        reset_engine()


@pytest.fixture
def user(db):
    from careeros.profile_io import load_profile

    user = load_profile(db, "data/sample_profile.yaml")
    db.commit()
    return user


@pytest.fixture
def pipeline(db):
    from careeros.ai.provider import get_provider
    from careeros.pipeline import Pipeline

    return Pipeline(db, provider=get_provider("off"))


@pytest.fixture
def loaded(db, user, pipeline):
    """Profile + jobs ingested, classified and scored."""
    from careeros.sources.jsonfile import JsonFileSource

    stats = pipeline.ingest([JsonFileSource("data/sample_jobs.json")])
    pipeline.classify_jobs(stats=stats)
    pipeline.assess_for_user(user, stats=stats)
    db.commit()
    return {"user": user, "pipeline": pipeline, "stats": stats}


# The tax system's fixtures live in their own module so the two products'
# fixtures cannot shadow each other; importing them here registers them.
pytest_plugins = ["tests.conftest_taxvault"]
