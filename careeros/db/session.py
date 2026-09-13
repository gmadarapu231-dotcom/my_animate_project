"""Database engine/session management.

SQLite by default so Phase 1 runs with no infrastructure; set
`CAREEROS_DATABASE_URL` to a PostgreSQL DSN for Phase 2 onwards (the schema is
plain SQLAlchemy and portable -- JSON columns map to `jsonb`).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from careeros.config import taxonomy
from careeros.db.models import Base, CareerDomain

DEFAULT_DB_PATH = Path(os.getenv("CAREEROS_HOME", Path.home() / ".careeros")) / "careeros.db"

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def database_url() -> str:
    url = os.getenv("CAREEROS_DATABASE_URL")
    if url:
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        url = database_url()
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # Foreign keys are off by default in SQLite; the schema relies on
            # ON DELETE CASCADE for evidence and application cleanup.
            @event.listens_for(_engine, "connect")
            def _fk_on(dbapi_conn, _record):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


def new_session() -> Session:
    return get_session_factory()()


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


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests switching DB URLs)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def init_db(seed_domains: bool = True) -> None:
    Base.metadata.create_all(get_engine())
    if seed_domains:
        sync_domain_taxonomy()


def sync_domain_taxonomy() -> None:
    """Mirror `domains.yaml` into the `career_domain` table.

    Rows inserted by the classifier (origin='inferred') are left untouched --
    the YAML is a seed, not a source of truth that overwrites learning.
    """
    tax = taxonomy()
    with session_scope() as session:
        existing = {d.id for d in session.query(CareerDomain).all()}
        for spec in tax.domains:
            if spec["id"] in existing:
                continue
            session.add(
                CareerDomain(
                    id=spec["id"],
                    label=spec["label"],
                    origin="seed",
                    functions=spec.get("functions", []),
                    title_patterns=spec.get("title_patterns", []),
                    keywords=spec.get("keywords", {}),
                )
            )
