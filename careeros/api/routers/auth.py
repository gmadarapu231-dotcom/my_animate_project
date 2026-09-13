"""Sign-in endpoints.

Every route here is public by necessity -- you cannot present a session before
you have one -- so each carries its own protection: OAuth state is single-use
and server-issued, emailed codes are hashed and attempt-capped, and no route
accepts a password because none exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from careeros.api.deps import bearer, get_db
from careeros.auth import (
    AuthError,
    describe_auth,
    resolve_session,
    sign_in_with_code,
    sign_in_with_google,
    start_email_code,
)
from careeros.auth.service import start_google
from careeros.auth.tokens import looks_like_session_token, read_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class GoogleStart(BaseModel):
    include_gmail: bool = Field(
        default=False,
        description="Also ask for Gmail read/compose, so one consent covers sign-in and mail.",
    )
    redirect_uri: str | None = None
    login_hint: str | None = None


class GoogleExchange(BaseModel):
    code: str
    state: str


class EmailStart(BaseModel):
    email: str


class EmailVerify(BaseModel):
    email: str
    code: str


@router.get("/describe")
def describe() -> dict[str, Any]:
    """What sign-in methods this server supports, for the login screen."""
    return describe_auth()


@router.post("/google/start")
def google_start(body: GoogleStart, session: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return start_google(
            session,
            include_gmail=body.include_gmail,
            redirect_uri=body.redirect_uri,
            login_hint=body.login_hint,
        )
    except AuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/google/exchange")
def google_exchange(body: GoogleExchange, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Finish the flow from a client that captured the redirect itself."""
    try:
        return sign_in_with_google(session, code=body.code, state=body.state).to_dict()
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/google/callback", response_class=HTMLResponse)
def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Where Google sends the browser back.

    Renders the token into a page the user copies into the app, and posts it to
    an opener window when there is one. A redirect carrying the token in a URL
    would leave it in history and in any referrer.
    """
    if error:
        return HTMLResponse(_page("Sign-in cancelled", error, token=None), status_code=400)
    if not code or not state:
        return HTMLResponse(
            _page("Missing details", "Google did not send a code and state.", token=None),
            status_code=400,
        )
    try:
        result = sign_in_with_google(session, code=code, state=state)
    except AuthError as exc:
        return HTMLResponse(_page("Could not sign you in", str(exc), token=None), status_code=401)

    hello = result.user.full_name or result.user.email
    note = "Account created." if result.created else "Welcome back."
    if result.granted_gmail:
        note += " Gmail access granted."
    return HTMLResponse(_page(f"Signed in as {hello}", note, token=result.token))


@router.post("/email/start")
def email_start(body: EmailStart, session: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return start_email_code(session, body.email)
    except AuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/email/verify")
def email_verify(body: EmailVerify, session: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return sign_in_with_code(session, body.email, body.code).to_dict()
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/session")
def whoami(request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Who the presented token belongs to, and how long it has left."""
    token = bearer(request)
    if not token or not looks_like_session_token(token):
        return {"signed_in": False, "reason": "no session token presented"}
    try:
        parsed = read_token(token)
        user = resolve_session(session, token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {
        "signed_in": True,
        "method": parsed.method,
        "expires_in_seconds": parsed.seconds_remaining,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "picture_url": user.picture_url,
            "home_country": user.home_country,
            "has_profile": bool(user.current_title or user.tracks),
            "evidence_ready": bool(user.tracks),
        },
    }


def _page(heading: str, message: str, token: str | None) -> str:
    """A deliberately plain page: it exists for ten seconds."""
    safe_heading = _escape(heading)
    safe_message = _escape(message)
    body = f"<h1>{safe_heading}</h1><p>{safe_message}</p>"
    if token:
        body += (
            "<p>Paste this into the app to finish signing in:</p>"
            f"<textarea readonly rows=4 onclick='this.select()'>{_escape(token)}</textarea>"
            "<p class=small>It is a session token. Treat it like a password and do not share it.</p>"
            "<script>try{if(window.opener){window.opener.postMessage("
            f"{{type:'careeros-auth',token:{_json(token)}}},'*');"
            "setTimeout(function(){window.close()},1200);}}catch(e){}</script>"
        )
    return (
        "<!doctype html><meta charset=utf-8><title>CareerOS sign-in</title>"
        "<style>:root{color-scheme:light dark}"
        "body{font:15px/1.5 system-ui,sans-serif;max-width:34rem;margin:0 auto;padding:3rem 1.25rem}"
        "h1{font-size:1.4rem;margin:0 0 .5rem}"
        "textarea{width:100%;font:12px ui-monospace,monospace;padding:.6rem;"
        "border:1px solid #8884;border-radius:6px;background:transparent;color:inherit}"
        ".small{opacity:.7;font-size:.82rem}</style>" + body
    )


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _json(value: str) -> str:
    import json

    return json.dumps(value)
