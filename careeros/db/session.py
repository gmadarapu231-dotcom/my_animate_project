"""Database engine/session management.

SQLite by default so Phase 1 runs with no infrastructure; set
`CAREEROS_DATABASE_URL` to a PostgreSQL DSN for Phase 2 onwards (the schema is
plain SQLAlchemy and portable -- JSON columns map to `jsonb`).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from careeros.config import taxonomy
from careeros.db.models import Base, CareerDomain

logger = logging.getLogger(__name__)

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
    add_missing_columns()
    if seed_domains:
        sync_domain_taxonomy()


def add_missing_columns() -> None:
    """Additive-only migration for databases created by an earlier version.

    `create_all` adds new *tables* but never new *columns*, so a database that
    predates the identity columns on `user` would fail on first query. This
    walks the models, compares them with the live schema and issues ALTER TABLE
    for anything missing. Additive only: nothing here drops or retypes a
    column, so it cannot lose data, and a full migration tool can take over
    later without undoing it.
    """
    engine = get_engine()
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable and column.default is None and column.server_default is None:
                # Cannot be added to a populated table without a value; leave
                # it to a real migration rather than guessing one.
                logger.warning(
                    "cannot add required column %s.%s automatically", table.name, column.name
                )
                continue
            ddl = column.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'))
            logger.info("added column %s.%s", table.name, column.name)


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
