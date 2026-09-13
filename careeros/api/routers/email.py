"""Gmail sync, classified messages and AI reply drafts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from careeros.api.deps import current_user, get_db
from careeros.db.models import EmailDraft, EmailMessage, User
from careeros.enums import DraftState
from careeros.gmail.client import DEFAULT_QUERY, get_client
from careeros.gmail.sync import GmailSync

router = APIRouter(prefix="/api/email", tags=["email"])


class SyncRequest(BaseModel):
    query: str = DEFAULT_QUERY
    limit: int = 50
    mailbox: str | None = None     # local JSON mailbox for demo/testing
    generate_drafts: bool = True
    use_ai: bool = True


class DraftEdit(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.post("/sync")
def sync(
    payload: SyncRequest,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    client = get_client(payload.mailbox)
    stats = GmailSync(session, client).sync(
        user,
        query=payload.query,
        limit=payload.limit,
        generate_drafts=payload.generate_drafts,
        use_ai=payload.use_ai,
    )
    return stats.to_dict()


@router.get("/messages")
def messages(
    category: str | None = None,
    requires_action: bool | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    stmt = select(EmailMessage).where(EmailMessage.user_id == user.id)
    if category:
        stmt = stmt.where(EmailMessage.category == category)
    if requires_action is not None:
        stmt = stmt.where(EmailMessage.requires_action.is_(requires_action))
    rows = session.scalars(stmt.order_by(EmailMessage.received_at.desc())).all()
    return {
        "count": len(rows),
        "messages": [
            {
                "id": m.id,
                "gmail_id": m.gmail_id,
                "sender": m.sender,
                "subject": m.subject,
                "snippet": m.snippet,
                "received_at": m.received_at.isoformat() if m.received_at else None,
                "category": m.category,
                "confidence": m.confidence,
                "extracted": m.extracted,
                "application_id": m.application_id,
                "requires_action": m.requires_action,
                "action_due_on": m.action_due_on.isoformat() if m.action_due_on else None,
            }
            for m in rows
        ],
    }


@router.get("/drafts")
def drafts(
    state: str | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    stmt = select(EmailDraft).where(EmailDraft.user_id == user.id)
    if state:
        stmt = stmt.where(EmailDraft.state == state)
    rows = session.scalars(stmt.order_by(EmailDraft.id.desc())).all()
    return {
        "count": len(rows),
        "drafts": [
            {
                "id": d.id,
                "email_id": d.email_id,
                "application_id": d.application_id,
                "intent": d.intent,
                "subject": d.subject,
                "body": d.body,
                "state": d.state,
                "high_impact_topics": d.high_impact_topics,
                "may_send": d.state == DraftState.APPROVED.value and not d.high_impact_topics,
            }
            for d in rows
        ],
    }


@router.post("/drafts/{draft_id}/edit")
def edit_draft(
    draft_id: int,
    payload: DraftEdit,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    draft = session.get(EmailDraft, draft_id)
    if draft is None or draft.user_id != user.id:
        raise HTTPException(404, "Draft not found")
    if payload.subject is not None:
        draft.subject = payload.subject
    if payload.body is not None:
        draft.body = payload.body
        # Re-run the high-impact check: a human edit can introduce a topic that
        # must never be auto-sent.
        from careeros.gmail.drafts import detect_high_impact

        draft.high_impact_topics = detect_high_impact(payload.body)
        if draft.high_impact_topics:
            draft.state = DraftState.BLOCKED_HIGH_IMPACT.value
    return {"id": draft.id, "state": draft.state, "high_impact_topics": draft.high_impact_topics}


@router.post("/drafts/{draft_id}/approve")
def approve(
    draft_id: int, session: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict[str, Any]:
    draft = session.get(EmailDraft, draft_id)
    if draft is None or draft.user_id != user.id:
        raise HTTPException(404, "Draft not found")
    if draft.high_impact_topics:
        raise HTTPException(
            409,
            "This reply touches "
            + ", ".join(draft.high_impact_topics)
            + ". CareerOS does not approve these for sending - send it yourself after review.",
        )
    draft.state = DraftState.APPROVED.value
    draft.approved_at = datetime.now(timezone.utc)
    return {"id": draft.id, "state": draft.state}


@router.post("/drafts/{draft_id}/send")
def send(
    draft_id: int,
    mailbox: str | None = None,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Send an approved reply. Refuses anything high-impact, always."""
    draft = session.get(EmailDraft, draft_id)
    if draft is None or draft.user_id != user.id:
        raise HTTPException(404, "Draft not found")
    if draft.high_impact_topics:
        raise HTTPException(409, "High-impact reply - CareerOS will not send this.")
    if draft.state != DraftState.APPROVED.value:
        raise HTTPException(409, "Draft must be approved first (DRAFT -> APPROVAL -> SEND).")

    message = session.get(EmailMessage, draft.email_id) if draft.email_id else None
    recipient = (message.sender if message else None) or ""
    client = get_client(mailbox, allow_send=True)
    try:
        external_id = client.send(
            message.thread_id if message else None, recipient, draft.subject or "", draft.body
        )
    except (PermissionError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc
    draft.state = DraftState.SENT.value
    draft.sent_at = datetime.now(timezone.utc)
    return {"id": draft.id, "state": draft.state, "external_id": external_id}
