"""Database engine and session handling.

SQLite by default so the system runs with no infrastructure; set
`TAXOS_DATABASE_URL` to a managed PostgreSQL DSN for a cloud deployment. The
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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from taxos.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def default_db_path() -> Path:
    return Path(os.getenv("TAXOS_HOME", Path.home() / ".taxos")) / "taxos.db"


def database_url() -> str:
    url = os.getenv("TAXOS_DATABASE_URL")
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
    Base.metadata.create_all(get_engine())


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
