"""Fixtures for the tax system.

Each test gets its own database *and* its own master key directory, so nothing
leaks between tests -- and so a test run never touches a real ~/.taxvault.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tax_env(tmp_path, monkeypatch):
    """An isolated TaxVault: fresh database, fresh keys, codes in the response."""
    monkeypatch.setenv("TAXVAULT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TAXVAULT_DATABASE_URL", f"sqlite:///{tmp_path}/taxvault.db")
    monkeypatch.setenv("TAXVAULT_DEV_CODES", "1")
    monkeypatch.delenv("TAXVAULT_MASTER_KEY", raising=False)
    monkeypatch.delenv("TAXVAULT_SESSION_SECRET", raising=False)

    import taxvault.crypto as crypto
    from taxvault.api.security import reset_rate_limits
    from taxvault.db.session import init_db, reset_engine

    crypto.data_key.cache_clear() if hasattr(crypto.data_key, "cache_clear") else None
    reset_engine()
    reset_rate_limits()
    init_db()
    yield tmp_path
    reset_engine()
    reset_rate_limits()


@pytest.fixture
def tax_db(tax_env):
    from taxvault.db.session import new_session

    session = new_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(tax_env):
    """A TestClient against the real app, with the real middleware in place."""
    from fastapi.testclient import TestClient

    from taxvault.api.app import app

    with TestClient(app) as test_client:
        yield test_client
