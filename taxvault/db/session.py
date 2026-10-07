"""Database engine and session handling.

SQLite by default so the system runs with no infrastructure; set
`TAXVAULT_DATABASE_URL` to a managed PostgreSQL DSN for a cloud deployment. The
schema is portable SQLAlchemy -- JSON columns map to `jsonb`, and every column
holding personal data holds ciphertext, so the hosting provider's at-rest
encryption is a second layer rather than the only one.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy import text as text_clause
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from taxvault.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def default_db_path() -> Path:
    return Path(os.getenv("TAXVAULT_HOME", Path.home() / ".taxvault")) / "taxvault.db"


def database_url() -> str:
    url = os.getenv("TAXVAULT_DATABASE_URL")
    if url:
        return url
    path = default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        url = database_url()
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # Managed PostgreSQL drops idle connections, and a pooled
            # connection that died quietly surfaces as a 500 on whatever
            # request happened to pick it up. pre_ping costs one round trip
            # and removes the whole class of failure.
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = int(os.getenv("TAXVAULT_DB_POOL_SIZE", "5"))
            kwargs["max_overflow"] = int(os.getenv("TAXVAULT_DB_MAX_OVERFLOW", "5"))
            kwargs["pool_recycle"] = int(os.getenv("TAXVAULT_DB_POOL_RECYCLE", "1800"))
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Tests point at a fresh database per run."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine, _SessionFactory = None, None


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


def new_session() -> Session:
    return get_session_factory()()


def init_db() -> None:
    """Create any missing tables.

    This creates tables; it does NOT alter existing ones. A column added to a
    model after go-live will not appear, and SQLAlchemy will not say so -- it
    will fail on the first query that selects it. Schema changes go through
    Alembic (`alembic upgrade head`); see `migrations/README.md`.
    """
    Base.metadata.create_all(get_engine())


def ping() -> tuple[bool, str]:
    """Can we actually reach the database? Returns (ok, detail).

    A health check that does not touch the database reports "ok" while the
    database is unreachable, which is worse than no health check: the load
    balancer keeps sending traffic to a process that cannot serve it.
    """
    try:
        with get_engine().connect() as connection:
            connection.execute(text_clause("SELECT 1"))
        return True, "reachable"
    except Exception as exc:  # pragma: no cover - needs a broken database
        return False, f"{type(exc).__name__}: {exc}"


@contextmanager
def session_scope() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
