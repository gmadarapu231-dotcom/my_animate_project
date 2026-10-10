"""The bridge from `submit_via_email` to Gmail.

`submit_via_email` takes a `send(message, attachment)` callable so it never
imports Gmail. This builds that callable when, and only when, the user has
granted the send scope -- otherwise it returns None and the email channel says
"sending is not connected" instead of failing.
"""

from __future__ import annotations

import os
from typing import Callable

Sender = Callable[[dict[str, str], tuple[str, bytes]], str]


def gmail_sender() -> Sender | None:
    if os.getenv("CAREEROS_LOCAL_MAILBOX"):
        return None  # a local mailbox never sends
    from careeros.gmail.client import OAuthGmailClient

    client = OAuthGmailClient(allow_send=True)
    if not client.available:
        return None

    def send(message: dict[str, str], attachment: tuple[str, bytes]) -> str:
        return client.send_with_attachment(
            message["to"], message["subject"], message["body"], attachment
        )

    return send
