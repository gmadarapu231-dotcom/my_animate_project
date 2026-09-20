"""FastAPI dependencies: the database session, and who is asking."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from taxos.auth import AuthError, resolve_session
from taxos.auth.tokens import looks_like_session_token
from taxos.db.models import Account, Taxpayer
from taxos.db.session import new_session


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


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_account(request: Request, session: Session = Depends(get_db)) -> Account:
    """The signed-in account. Every data route depends on this, never on a
    user id in the request body -- which is how horizontal privilege bugs
    happen."""
    token = bearer(request)
    if not token or not looks_like_session_token(token):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    try:
        account, _ = resolve_session(session, token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return account


def verified_account(request: Request, session: Session = Depends(get_db)) -> Account:
    """An account that has also proved SSN, email and mobile.

    Anything that touches tax data goes through this rather than
    `current_account`: signing in proves an email address, which is not enough
    to read somebody's return.
    """
    token = bearer(request)
    if not token or not looks_like_session_token(token):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    try:
        account, verified = resolve_session(session, token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not verified:
        raise HTTPException(
            status_code=403,
            detail=(
                "Confirm your identity first: your Social Security number, plus codes "
                "sent to your email address and mobile number."
            ),
        )
    return account


def current_taxpayer(
    account: Account = Depends(verified_account), session: Session = Depends(get_db)
) -> Taxpayer:
    taxpayer = session.scalars(
        select(Taxpayer).where(
            Taxpayer.account_id == account.id,
            Taxpayer.relationship_to_filer == "self",
        )
    ).first()
    if taxpayer is None:
        raise HTTPException(status_code=404, detail="No taxpayer record on this account yet.")
    return taxpayer


def owned_taxpayer(
    taxpayer_id: int, account: Account = Depends(verified_account),
    session: Session = Depends(get_db),
) -> Taxpayer:
    """Load a taxpayer by id, but only one this account owns."""
    taxpayer = session.get(Taxpayer, taxpayer_id)
    # 404 rather than 403 for someone else's record: confirming that an id
    # exists is itself a small leak.
    if taxpayer is None or taxpayer.account_id != account.id:
        raise HTTPException(status_code=404, detail="No such taxpayer on this account.")
    return taxpayer
