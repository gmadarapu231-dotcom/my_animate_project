"""Actually sending an application.

One submitter per tier, all returning the same `Submission` so the caller does
not branch on channel. Two rules hold across all of them:

**A tier is a ceiling.** A board in the API tier that answers with a
human-verification challenge downgrades to ASSISTED and says so. Nothing here
solves a CAPTCHA, replays a token, or retries with different headers -- the
challenge is the site's answer and it is taken as final.

**Nothing is sent in a dry run.** `dry_run` is checked inside each submitter,
not by the caller, so a new submitter cannot forget it.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from careeros.apply.channels import Channel, SubmitTier
from careeros.apply.packet import Packet
from careeros.sources.http import HttpClient
from careeros.sources.policy import AccessTier, Challenge, SourceError, redact

logger = logging.getLogger(__name__)


@dataclass
class Submission:
    ok: bool
    tier: str
    provider: str
    dry_run: bool
    reference: str | None = None
    message: str = ""
    downgraded_to: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    submitted_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tier": self.tier,
            "provider": self.provider,
            "dry_run": self.dry_run,
            "reference": self.reference,
            "message": self.message,
            "downgraded_to": self.downgraded_to,
            "artifacts": self.artifacts,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Words in a response that mean "a human has to do this bit".
_CHALLENGE_WORDS = (
    "recaptcha",
    "captcha",
    "g-recaptcha-response",
    "human verification",
    "are you a robot",
    "hcaptcha",
    "turnstile",
)


def _needs_a_human(body: str) -> str | None:
    lowered = (body or "")[:4000].lower()
    for word in _CHALLENGE_WORDS:
        if word in lowered:
            return word
    return None


# ---------------------------------------------------------------------------
# multipart, without a dependency
# ---------------------------------------------------------------------------
def _multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes]]
) -> tuple[bytes, str]:
    boundary = f"----CareerOS{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for name, (filename, payload) in files.items():
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode("utf-8")
        )
        parts.append(payload)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# API submission
# ---------------------------------------------------------------------------
#: Where each ATS takes an application, and how it names the fields. Adding a
#: platform is an entry here.
API_ENDPOINTS: dict[str, dict[str, Any]] = {
    "greenhouse": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
        "fields": {
            "first_name": "first_name",
            "last_name": "last_name",
            "email": "email",
            "phone": "phone",
            "cover_letter_text": "cover_letter",
        },
        "resume_field": "resume",
        "docs": "https://developers.greenhouse.io/job-board.html#submit-an-application",
        "verified": False,
    },
    "lever": {
        "url": "https://api.lever.co/v0/postings/{site}/{posting_id}",
        "fields": {
            "name": "full_name",
            "email": "email",
            "phone": "phone",
            "comments": "cover_letter",
        },
        "resume_field": "resume",
        "docs": "https://github.com/lever/postings-api#apply-to-a-posting",
        "verified": False,
    },
    "smartrecruiters": {
        "url": "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}/candidates",
        "json": True,
        "docs": "https://dev.smartrecruiters.com/customer-api/posting-api/",
        "verified": False,
    },
}


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


def submit_via_api(
    packet: Packet, *, client: HttpClient | None = None, dry_run: bool = True
) -> Submission:
    spec = API_ENDPOINTS.get(packet.channel.provider)
    if spec is None:
        return Submission(
            ok=False,
            tier=SubmitTier.API.value,
            provider=packet.channel.provider,
            dry_run=dry_run,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=(
                f"No application endpoint is configured for {packet.channel.provider}. "
                "Submit through the link."
            ),
        )

    try:
        url = spec["url"].format(**packet.channel.parameters)
    except KeyError as missing:
        return Submission(
            ok=False,
            tier=SubmitTier.API.value,
            provider=packet.channel.provider,
            dry_run=dry_run,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=f"Cannot build the endpoint: {missing} is not in the posting URL.",
        )

    first, last = _split_name(packet.full_name)
    values = {
        "full_name": packet.full_name,
        "first_name": first,
        "last_name": last,
        "email": packet.email,
        "phone": packet.phone or "",
        "cover_letter": packet.cover_letter or "",
    }
    fields = {
        remote: values.get(local, "")
        for remote, local in (spec.get("fields") or {}).items()
        if values.get(local)
    }
    artifacts = {
        "endpoint": url,
        "fields": sorted(fields),
        "resume_filename": packet.resume_filename,
        "docs": spec.get("docs", ""),
    }

    if dry_run:
        return Submission(
            ok=True,
            tier=SubmitTier.API.value,
            provider=packet.channel.provider,
            dry_run=True,
            message=f"Would POST to {url}",
            artifacts=artifacts,
        )

    body: bytes
    headers: dict[str, str]
    if spec.get("json"):
        payload = {
            "firstName": first,
            "lastName": last,
            "email": packet.email,
            "phoneNumber": packet.phone,
            "attachments": [],
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
    else:
        body, content_type = _multipart(
            fields,
            {spec["resume_field"]: (packet.resume_filename, packet.resume_text.encode("utf-8"))},
        )
        headers = {"Content-Type": content_type, "Accept": "application/json"}

    http = client or HttpClient()
    try:
        response = http.fetch(
            url, method="POST", headers=headers, tier=AccessTier.OFFICIAL_API, raw_body=body
        )
    except Challenge as exc:
        # The board wants a human. That answer is respected, not worked around.
        return Submission(
            ok=False,
            tier=SubmitTier.API.value,
            provider=packet.channel.provider,
            dry_run=False,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=(
                f"{exc.host} asked for human verification"
                + (f" ({exc.args[0].split(': ', 1)[-1]})" if exc.args else "")
                + ", so this one is yours to submit. Nothing here bypasses that."
            ),
            artifacts=artifacts,
        )
    except SourceError as exc:
        return Submission(
            ok=False,
            tier=SubmitTier.API.value,
            provider=packet.channel.provider,
            dry_run=False,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=f"Submission failed: {redact(str(exc))}",
            artifacts=artifacts,
        )

    challenge = _needs_a_human(response.body)
    if challenge:
        return Submission(
            ok=False,
            tier=SubmitTier.API.value,
            provider=packet.channel.provider,
            dry_run=False,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=(
                f"This board requires {challenge}, so the application must be submitted by "
                "hand. The packet is ready."
            ),
            artifacts=artifacts,
        )

    reference = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            reference = str(
                payload.get("id") or payload.get("candidateId") or payload.get("success") or ""
            ) or None
    except SourceError:
        pass

    return Submission(
        ok=True,
        tier=SubmitTier.API.value,
        provider=packet.channel.provider,
        dry_run=False,
        reference=reference,
        message=f"Submitted to {packet.channel.provider}.",
        artifacts=artifacts,
        submitted_at=_now(),
    )


# ---------------------------------------------------------------------------
# Email submission
# ---------------------------------------------------------------------------
def application_email(packet: Packet) -> dict[str, str]:
    """Subject and body for an application sent by email."""
    subject = f"Application: {packet.job_title}"
    if packet.company:
        subject += f" — {packet.company}"

    lines = [f"Dear {packet.company or 'Hiring team'},", ""]
    if packet.cover_letter:
        lines.append(packet.cover_letter.strip())
    else:
        lines.append(
            f"I am applying for the {packet.job_title} role"
            + (f" at {packet.company}" if packet.company else "")
            + ". My résumé is attached."
        )
    lines += ["", "Kind regards,", packet.full_name]
    if packet.phone:
        lines.append(packet.phone)
    lines.append(packet.email)
    for label, url in (packet.links or {}).items():
        lines.append(f"{label}: {url}")

    return {"to": packet.channel.email or "", "subject": subject, "body": "\n".join(lines)}


def submit_via_email(
    packet: Packet,
    *,
    send: Callable[[dict[str, str], tuple[str, bytes]], str] | None = None,
    dry_run: bool = True,
) -> Submission:
    """Send the application to the address the posting named.

    `send` is injected so the Gmail layer stays out of this module and the
    tests never touch a mail server.
    """
    message = application_email(packet)
    attachment = (packet.resume_filename, packet.resume_text.encode("utf-8"))
    artifacts = {
        "to": message["to"],
        "subject": message["subject"],
        "body_characters": len(message["body"]),
        "attachment": packet.resume_filename,
    }

    if not message["to"]:
        return Submission(
            ok=False,
            tier=SubmitTier.EMAIL.value,
            provider="email",
            dry_run=dry_run,
            downgraded_to=SubmitTier.ASSISTED.value,
            message="No application address for this posting.",
        )

    if dry_run:
        return Submission(
            ok=True,
            tier=SubmitTier.EMAIL.value,
            provider="email",
            dry_run=True,
            message=f"Would email {message['to']}",
            artifacts=artifacts,
        )

    if send is None:
        return Submission(
            ok=False,
            tier=SubmitTier.EMAIL.value,
            provider="email",
            dry_run=False,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=(
                "Sending is not connected. Run `careeros gmail-auth` to grant send access, "
                "or send the drafted message yourself."
            ),
            artifacts=artifacts,
        )

    try:
        reference = send(message, attachment)
    except Exception as exc:  # noqa: BLE001 - a mail failure must not end a run
        return Submission(
            ok=False,
            tier=SubmitTier.EMAIL.value,
            provider="email",
            dry_run=False,
            message=f"Could not send: {redact(str(exc))}",
            artifacts=artifacts,
        )

    return Submission(
        ok=True,
        tier=SubmitTier.EMAIL.value,
        provider="email",
        dry_run=False,
        reference=reference,
        message=f"Emailed {message['to']}.",
        artifacts=artifacts,
        submitted_at=_now(),
    )


# ---------------------------------------------------------------------------
# Assisted hand-off
# ---------------------------------------------------------------------------
def prepare_assisted(packet: Packet) -> Submission:
    """Not a failure -- the packet is finished, the submission is the human's."""
    return Submission(
        ok=True,
        tier=SubmitTier.ASSISTED.value,
        provider=packet.channel.provider,
        dry_run=False,
        message=(
            f"Ready to submit at {packet.channel.apply_url or 'the posting'}. "
            f"{packet.channel.reason}"
        ),
        artifacts={
            "apply_url": packet.channel.apply_url,
            "resume_filename": packet.resume_filename,
            "resume_characters": len(packet.resume_text),
            "cover_letter": bool(packet.cover_letter),
            "answers": [a.to_dict() for a in packet.answers],
            "unanswered": packet.unanswered,
        },
    )


def submit(
    packet: Packet,
    *,
    dry_run: bool = True,
    client: HttpClient | None = None,
    send: Callable[[dict[str, str], tuple[str, bytes]], str] | None = None,
) -> Submission:
    """Route to the right submitter. The only entry point callers need."""
    tier = packet.channel.tier

    if not packet.complete and tier.automatable:
        return Submission(
            ok=False,
            tier=tier.value,
            provider=packet.channel.provider,
            dry_run=dry_run,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=(
                "The packet is incomplete, so it is not submitted automatically: "
                + "; ".join(packet.unanswered)
            ),
            artifacts={"unanswered": packet.unanswered},
        )

    if tier is SubmitTier.API:
        return submit_via_api(packet, client=client, dry_run=dry_run)
    if tier is SubmitTier.EMAIL:
        return submit_via_email(packet, send=send, dry_run=dry_run)
    if tier is SubmitTier.BLOCKED:
        return Submission(
            ok=False,
            tier=tier.value,
            provider=packet.channel.provider,
            dry_run=dry_run,
            downgraded_to=SubmitTier.ASSISTED.value,
            message=packet.channel.reason,
            artifacts={"apply_url": packet.channel.apply_url},
        )
    return prepare_assisted(packet)
