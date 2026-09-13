"""Gmail access.

Two hard rules, enforced by the shape of this module:

* **OAuth only.** There is no field anywhere for a Gmail password, and no code
  path that would accept one. The credential we hold is a refresh token issued
  by Google's consent screen, stored outside the database.
* **Least privilege.** The default scope set is read-only plus `gmail.compose`
  (create drafts). `gmail.send` is opt-in and off unless the user explicitly
  enables automated sending, which the draft workflow still gates.

`LocalMailboxClient` implements the same interface from a JSON file so the
classification and drafting layers can be developed and tested with no Google
project at all.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

logger = logging.getLogger(__name__)

#: Read + draft. Sending is deliberately NOT here.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: Query used for the daily sweep; narrow by design so the agent reads job mail
#: rather than the whole inbox.
DEFAULT_QUERY = (
    "newer_than:30d ("
    "subject:(application OR interview OR recruiter OR opportunity OR assessment OR offer OR position) "
    "OR from:(greenhouse.io OR lever.co OR myworkday.com OR icims.com OR ashbyhq.com OR smartrecruiters.com)"
    ")"
)


@dataclass
class MailMessage:
    id: str
    thread_id: str | None = None
    sender: str | None = None
    to: str | None = None
    subject: str | None = None
    body: str = ""
    snippet: str | None = None
    received_at: datetime | None = None
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "subject": self.subject,
            "snippet": self.snippet,
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }


class GmailClient(Protocol):
    name: str
    can_send: bool

    def list_messages(self, query: str = DEFAULT_QUERY, limit: int = 50) -> Iterator[MailMessage]: ...

    def create_draft(self, thread_id: str | None, to: str, subject: str, body: str) -> str: ...

    def send(self, thread_id: str | None, to: str, subject: str, body: str) -> str: ...


class LocalMailboxClient:
    """File-backed mailbox. Used by tests, the demo and offline development."""

    name = "local"
    can_send = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.drafts: list[dict[str, Any]] = []

    def list_messages(self, query: str = DEFAULT_QUERY, limit: int = 50) -> Iterator[MailMessage]:
        if not self.path.exists():
            return iter(())
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else payload.get("messages", [])
        out = []
        for rec in records[:limit]:
            received = rec.get("received_at")
            out.append(
                MailMessage(
                    id=str(rec.get("id")),
                    thread_id=rec.get("thread_id"),
                    sender=rec.get("from") or rec.get("sender"),
                    to=rec.get("to"),
                    subject=rec.get("subject"),
                    body=rec.get("body", ""),
                    snippet=(rec.get("body", "") or "")[:200],
                    received_at=datetime.fromisoformat(received) if received else None,
                    labels=rec.get("labels", []),
                )
            )
        return iter(out)

    def create_draft(self, thread_id: str | None, to: str, subject: str, body: str) -> str:
        draft_id = f"local-draft-{len(self.drafts) + 1}"
        self.drafts.append(
            {"id": draft_id, "thread_id": thread_id, "to": to, "subject": subject, "body": body}
        )
        return draft_id

    def send(self, thread_id: str | None, to: str, subject: str, body: str) -> str:
        raise PermissionError("LocalMailboxClient never sends mail.")


class OAuthGmailClient:
    """Real Gmail access via the Google API client.

    Credentials are resolved from a token file written by the OAuth consent
    flow (`CAREEROS_GMAIL_TOKEN`, default `~/.careeros/gmail_token.json`). The
    token file lives outside the application database and outside the repo.
    """

    name = "gmail"

    def __init__(self, token_path: str | Path | None = None, allow_send: bool = False) -> None:
        self.token_path = Path(
            token_path
            or os.getenv("CAREEROS_GMAIL_TOKEN", Path.home() / ".careeros" / "gmail_token.json")
        )
        self.scopes = list(DEFAULT_SCOPES) + ([SEND_SCOPE] if allow_send else [])
        self.can_send = allow_send
        self._service: Any = None

    @property
    def available(self) -> bool:
        return self.token_path.exists()

    def _connect(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from google.oauth2.credentials import Credentials  # noqa: PLC0415
            from googleapiclient.discovery import build  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Gmail support needs `google-api-python-client` and "
                "`google-auth-oauthlib`. Install the `gmail` extra."
            ) from exc
        if not self.token_path.exists():
            raise RuntimeError(
                f"No Gmail token at {self.token_path}. Run `careeros gmail-auth` to "
                "complete the OAuth consent flow. CareerOS never accepts a Gmail password."
            )
        creds = Credentials.from_authorized_user_file(str(self.token_path), self.scopes)
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    @staticmethod
    def _decode(payload: dict[str, Any]) -> str:
        import base64  # noqa: PLC0415

        def walk(part: dict[str, Any]) -> str:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return "".join(walk(p) for p in part.get("parts", []) or [])

        return walk(payload)

    def list_messages(self, query: str = DEFAULT_QUERY, limit: int = 50) -> Iterator[MailMessage]:
        service = self._connect()
        listing = service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        for ref in listing.get("messages", []):
            full = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
            timestamp = full.get("internalDate")
            yield MailMessage(
                id=full["id"],
                thread_id=full.get("threadId"),
                sender=headers.get("from"),
                to=headers.get("to"),
                subject=headers.get("subject"),
                body=self._decode(full.get("payload", {})),
                snippet=full.get("snippet"),
                received_at=datetime.fromtimestamp(int(timestamp) / 1000) if timestamp else None,
                labels=full.get("labelIds", []),
            )

    def _raw(self, to: str, subject: str, body: str) -> str:
        import base64  # noqa: PLC0415
        from email.message import EmailMessage  # noqa: PLC0415

        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    def create_draft(self, thread_id: str | None, to: str, subject: str, body: str) -> str:
        service = self._connect()
        message: dict[str, Any] = {"raw": self._raw(to, subject, body)}
        if thread_id:
            message["threadId"] = thread_id
        draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
        return draft["id"]

    def send(self, thread_id: str | None, to: str, subject: str, body: str) -> str:
        if not self.can_send:
            raise PermissionError(
                "Sending is disabled. Enable it explicitly (allow_send=True) and re-run the "
                "OAuth flow to grant the gmail.send scope."
            )
        service = self._connect()
        message: dict[str, Any] = {"raw": self._raw(to, subject, body)}
        if thread_id:
            message["threadId"] = thread_id
        sent = service.users().messages().send(userId="me", body=message).execute()
        return sent["id"]


def get_client(path: str | Path | None = None, allow_send: bool = False) -> GmailClient:
    """Local mailbox when one is configured, otherwise the OAuth client."""
    local = path or os.getenv("CAREEROS_LOCAL_MAILBOX")
    if local:
        return LocalMailboxClient(local)
    return OAuthGmailClient(allow_send=allow_send)
