"""Sign-in and identity endpoints.

Every route here is public by necessity -- you cannot present a session before
you have one -- so each carries its own protection: codes are hashed and
attempt-capped, the rate limiter is tightest on exactly these paths, and no
route accepts a password because none exists.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from taxvault.api.deps import bearer, client_ip, current_account, get_db
from taxvault.auth import (
    AuthError,
    DeliveryError,
    complete_mobile_verification,
    complete_sign_in,
    describe_auth,
    mask_email,
    mask_mobile,
    resolve_session,
    start_mobile_verification,
    start_sign_in,
    verify_identity,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignInStart(BaseModel):
    email: str = Field(description="Where the one-time code is sent.")


class SignInVerify(BaseModel):
    email: str
    code: str


class MobileStart(BaseModel):
    mobile: str = Field(description="US mobile, 10 digits, or a full +country number.")


class MobileVerify(BaseModel):
    code: str


class IdentityCheck(BaseModel):
    """The three factors, submitted together.

    The SSN is accepted here and nowhere else, and it is never echoed back:
    responses carry the last four digits only.
    """

    ssn: str
    email: str
    mobile: str
    first_name: str = ""
    last_name: str = ""
    date_of_birth: date | None = None
    resident_state: str = ""


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/describe")
def describe() -> dict[str, Any]:
    """What this server supports, for the sign-in screen."""
    return describe_auth()


@router.post("/sign-in")
def sign_in(body: SignInStart, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Send a one-time code to an email address.

    The response is the same whether or not an account exists, so this cannot
    be used to discover who has one.
    """
    try:
        return start_sign_in(session, body.email)
    except (AuthError, DeliveryError, ValueError) as exc:
        raise _fail(exc) from exc


@router.post("/verify")
def verify(body: SignInVerify, request: Request,
           session: Session = Depends(get_db)) -> dict[str, Any]:
    """Exchange a code for a session token."""
    try:
        result = complete_sign_in(session, body.email, body.code, ip_address=client_ip(request))
    except (AuthError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return result.to_dict()


@router.post("/mobile/start")
def mobile_start(body: MobileStart, account=Depends(current_account),
                 session: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return start_mobile_verification(session, account, body.mobile)
    except (AuthError, DeliveryError, ValueError) as exc:
        raise _fail(exc) from exc


@router.post("/mobile/verify")
def mobile_verify(body: MobileVerify, request: Request, account=Depends(current_account),
                  session: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return complete_mobile_verification(
            session, account, body.code, ip_address=client_ip(request)
        )
    except (AuthError, ValueError) as exc:
        raise _fail(exc) from exc


@router.post("/identity")
def identity(body: IdentityCheck, request: Request, account=Depends(current_account),
             session: Session = Depends(get_db)) -> dict[str, Any]:
    """Bind an SSN to this account, once email and mobile are both proven."""
    try:
        return verify_identity(
            session, account,
            ssn=body.ssn, email=body.email, mobile=body.mobile,
            first_name=body.first_name, last_name=body.last_name,
            date_of_birth=body.date_of_birth, resident_state=body.resident_state,
            ip_address=client_ip(request),
        )
    except (AuthError, ValueError) as exc:
        raise _fail(exc) from exc


@router.get("/session")
def session_info(request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    """What the current token is good for. Used by the client on every launch."""
    token = bearer(request)
    if not token:
        return {"signed_in": False, "identity_verified": False}
    try:
        account, verified = resolve_session(session, token)
    except AuthError as exc:
        return {"signed_in": False, "identity_verified": False, "reason": str(exc)}
    taxpayer = next(
        (t for t in account.taxpayers if t.relationship_to_filer == "self"), None
    )
    return {
        "signed_in": True,
        "identity_verified": verified,
        "account": {
            "id": account.id,
            "email": account.email,
            "email_masked": mask_email(account.email),
            "full_name": account.full_name,
            "mobile": mask_mobile(account.mobile_e164 or ""),
            "mobile_verified": account.mobile_verified_at is not None,
            "role": account.role,
        },
        "taxpayer": None if taxpayer is None else {
            "id": taxpayer.id,
            "name": taxpayer.display_name,
            "ssn": f"***-**-{taxpayer.ssn_last4}" if taxpayer.ssn_last4 else None,
            "resident_state": taxpayer.resident_state,
        },
    }
