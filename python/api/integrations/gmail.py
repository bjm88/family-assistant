"""Thin Gmail send adapter.

Sends RFC-822 email through the assistant's connected Google account.
The complementary read/scan operations would live here too if/when we
add them; for now the only outbound capability is :func:`send_email`.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)


class GmailSendError(RuntimeError):
    """Raised when Gmail rejects the send (e.g. missing scope, quota)."""


def send_email(
    creds: Credentials,
    *,
    to: str,
    subject: str,
    body: str,
    sender: Optional[str] = None,
) -> str:
    """Send a plain-text email and return Gmail's message id.

    Parameters
    ----------
    creds
        Authorised credentials with the ``gmail.send`` scope.
    to
        Recipient email address.
    subject, body
        Plain-text content. HTML alternatives can be added by passing
        a custom :class:`EmailMessage` once we need them.
    sender
        Optional ``From`` override. Defaults to the special ``me``
        token, which Gmail expands to the authenticated user.
    """
    msg = EmailMessage()
    msg.set_content(body)
    msg["To"] = to
    msg["From"] = sender or "me"
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        # cache_discovery=False keeps tests/dev fast and avoids the
        # noisy "file_cache is unavailable" warning when google-auth
        # can't write its discovery cache.
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        sent = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
    except HttpError as exc:
        raise GmailSendError(_summarise_http_error(exc)) from exc

    message_id = sent.get("id", "")
    logger.info("Gmail send ok message_id=%s to=%s", message_id, to)
    return message_id


def _summarise_http_error(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", None) if exc.resp else None
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        message = payload.get("error", {}).get("message") or str(exc)
    except Exception:  # noqa: BLE001 - fall back to raw
        message = str(exc)
    return f"Gmail HTTP {status}: {message}"
