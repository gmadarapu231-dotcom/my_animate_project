"""FastAPI dependencies."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from careeros.ai.provider import LLMProvider, get_provider
from careeros.auth import AuthError, AuthMode, auth_mode, resolve_session
from careeros.auth.tokens import looks_like_session_token
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


def bearer(request: Request) -> str | None:
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    return presented.strip() if scheme.lower() == "bearer" and presented.strip() else None


def current_user(request: Request, session: Session = Depends(get_db)) -> User:
    """The signed-in account, or the single local profile when auth is open.

    A session token names its account, so this is where multi-account scoping
    actually happens. The service token (and open mode) deliberately fall back
    to the one loaded profile: that is the CLI and dashboard case, where there
    is exactly one person and no sign-in to do.
    """
    token = bearer(request)
    if token and looks_like_session_token(token):
        try:
            return resolve_session(session, token)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    if auth_mode() is AuthMode.REQUIRED and not token:
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    user = load_user(session)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No profile on this server yet. Sign in to open an account, or "
                "run `careeros load-profile <file.yaml>`."
            ),
        )
    return user


def optional_user(request: Request, session: Session = Depends(get_db)) -> User | None:
    """For endpoints that work with or without an account, such as browsing jobs."""
    try:
        return current_user(request, session)
    except HTTPException:
        return None


def llm() -> LLMProvider:
    return get_provider()


def pipeline(session: Session = Depends(get_db)) -> Pipeline:
    return Pipeline(session)
