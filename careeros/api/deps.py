"""FastAPI dependencies."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from careeros.ai.provider import LLMProvider, get_provider
from careeros.db.models import User
from careeros.db.session import new_session
from careeros.pipeline import Pipeline
from careeros.services import load_user


def get_db() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(session: Session = Depends(get_db)) -> User:
    """Phase 1 is single-user; this is the seam where auth lands in Phase 2."""
    user = load_user(session)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail="No profile loaded. Run `careeros load-profile <file.yaml>` first.",
        )
    return user


def llm() -> LLMProvider:
    return get_provider()


def pipeline(session: Session = Depends(get_db)) -> Pipeline:
    return Pipeline(session)
