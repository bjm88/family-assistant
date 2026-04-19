"""Gmail send + inbox-read adapters.

The send half is used by the ``gmail_send`` agent tool; the read half
backs the email-inbox poller (:mod:`api.services.email_inbox`) that
reacts to inbound mail from registered family members. Both halves
share the same :class:`Credentials` plumbing — ``gmail.modify`` (a
superset of ``gmail.send`` + ``gmail.readonly``) is the recommended
scope so the poller can list, fetch, and mark messages as read with a
single OAuth grant.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)


class GmailSendError(RuntimeError):
    """Raised when Gmail rejects the send (e.g. missing scope, quota)."""


class GmailReadError(RuntimeError):
    """Raised when an inbox list / get / modify call fails."""


def _service(creds: Credentials):
    """Build a cached-discovery-free Gmail service object."""
    # cache_discovery=False keeps tests/dev fast and avoids the noisy
    # "file_cache is unavailable" warning when google-auth can't write
    # its discovery cache.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


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
        sent = _service(creds).users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
    except HttpError as exc:
        raise GmailSendError(_summarise_http_error(exc)) from exc

    message_id = sent.get("id", "")
    logger.info("Gmail send ok message_id=%s to=%s", message_id, to)
    return message_id


def send_reply(
    creds: Credentials,
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str,
    references: Optional[str],
    thread_id: str,
    sender: Optional[str] = None,
) -> str:
    """Send a plain-text reply that threads on an existing Gmail thread.

    Setting ``In-Reply-To`` + ``References`` is what makes Gmail (and
    every other RFC-compliant client) collapse the new message under
    the original thread instead of starting a fresh one. Passing
    ``threadId`` to the Gmail API on top of that is belt-and-suspenders
    — it tells the server side to thread the message even if a client
    later clobbers the headers.
    """
    msg = EmailMessage()
    msg.set_content(body)
    msg["To"] = to
    msg["From"] = sender or "me"
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["In-Reply-To"] = in_reply_to
    # References should chain. If the original had its own References
    # header, append the message id; otherwise start the chain at the
    # message id itself.
    msg["References"] = (
        f"{references} {in_reply_to}".strip() if references else in_reply_to
    )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    try:
        sent = _service(creds).users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id},
        ).execute()
    except HttpError as exc:
        raise GmailSendError(_summarise_http_error(exc)) from exc

    message_id = sent.get("id", "")
    logger.info(
        "Gmail reply ok message_id=%s thread_id=%s to=%s",
        message_id,
        thread_id,
        to,
    )
    return message_id


# ---------------------------------------------------------------------------
# Inbox read helpers (used by the email-inbox poller)
# ---------------------------------------------------------------------------


def list_unread_inbox_message_ids(
    creds: Credentials, *, max_results: int = 25
) -> List[str]:
    """Return Gmail message ids for unread INBOX messages.

    Filters with the same query a human would type into the Gmail
    search bar — ``in:inbox is:unread`` — so promotional / spam mail
    that Gmail already filed elsewhere never reaches Avi.
    """
    try:
        resp = (
            _service(creds)
            .users()
            .messages()
            .list(
                userId="me",
                q="in:inbox is:unread",
                maxResults=max_results,
            )
            .execute()
        )
    except HttpError as exc:
        raise GmailReadError(_summarise_http_error(exc)) from exc
    msgs = resp.get("messages") or []
    return [m["id"] for m in msgs if m.get("id")]


def fetch_message(creds: Credentials, message_id: str) -> "FetchedMessage":
    """Fetch a single message, decode the plain-text body, parse headers."""
    try:
        raw = (
            _service(creds)
            .users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as exc:
        raise GmailReadError(_summarise_http_error(exc)) from exc

    headers = {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in (raw.get("payload", {}).get("headers") or [])
    }
    sender_name, sender_email = parseaddr(headers.get("from", ""))
    body_text = _extract_plain_text(raw.get("payload") or {})
    received_at: Optional[datetime] = None
    if raw.get("internalDate"):
        try:
            # Gmail's internalDate is epoch milliseconds (UTC).
            received_at = datetime.fromtimestamp(
                int(raw["internalDate"]) / 1000, tz=timezone.utc
            )
        except (TypeError, ValueError):
            received_at = None

    return FetchedMessage(
        message_id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        sender_email=(sender_email or "").strip().lower(),
        sender_name=(sender_name or "").strip() or None,
        subject=headers.get("subject"),
        body_text=body_text,
        in_reply_to_header=headers.get("message-id"),
        references_header=headers.get("references"),
        list_id_header=headers.get("list-id"),
        precedence_header=headers.get("precedence"),
        auto_submitted_header=headers.get("auto-submitted"),
        received_at=received_at,
        label_ids=list(raw.get("labelIds") or []),
    )


def mark_message_read(creds: Credentials, message_id: str) -> None:
    """Remove the UNREAD label so the next poll skips this message."""
    try:
        (
            _service(creds)
            .users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            )
            .execute()
        )
    except HttpError as exc:
        raise GmailReadError(_summarise_http_error(exc)) from exc


# ---------------------------------------------------------------------------
# Internal data structures + body extractor
# ---------------------------------------------------------------------------


class FetchedMessage:
    """Plain-data container for a parsed Gmail message.

    A small dataclass would do the job too but we keep this as a
    regular class so the docstring stays attached to the type and the
    poller has a clean ``.foo`` rather than ``.foo`` on a ``dict``.
    """

    __slots__ = (
        "message_id",
        "thread_id",
        "sender_email",
        "sender_name",
        "subject",
        "body_text",
        "in_reply_to_header",
        "references_header",
        "list_id_header",
        "precedence_header",
        "auto_submitted_header",
        "received_at",
        "label_ids",
    )

    def __init__(
        self,
        *,
        message_id: str,
        thread_id: str,
        sender_email: str,
        sender_name: Optional[str],
        subject: Optional[str],
        body_text: str,
        in_reply_to_header: Optional[str],
        references_header: Optional[str],
        list_id_header: Optional[str],
        precedence_header: Optional[str],
        auto_submitted_header: Optional[str],
        received_at: Optional[datetime],
        label_ids: List[str],
    ) -> None:
        self.message_id = message_id
        self.thread_id = thread_id
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.subject = subject
        self.body_text = body_text
        self.in_reply_to_header = in_reply_to_header
        self.references_header = references_header
        self.list_id_header = list_id_header
        self.precedence_header = precedence_header
        self.auto_submitted_header = auto_submitted_header
        self.received_at = received_at
        self.label_ids = label_ids


def _extract_plain_text(payload: dict, _depth: int = 0) -> str:
    """Walk a Gmail payload tree and concatenate every text/plain part.

    Falls back to text/html (very crudely stripped of tags) if no
    plain part exists. We intentionally cap recursion depth — Gmail
    sometimes nests multipart/related inside multipart/alternative
    inside multipart/mixed — but it's never very deep in practice.
    """
    if _depth > 6:
        return ""
    mime_type = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    data = body.get("data")

    if mime_type.startswith("text/plain") and data:
        return _b64url_decode(data)

    parts = payload.get("parts") or []
    if parts:
        # Prefer text/plain, then text/html, then anything.
        for part in parts:
            if (part.get("mimeType") or "").lower().startswith("text/plain"):
                inner = _extract_plain_text(part, _depth + 1)
                if inner:
                    return inner
        for part in parts:
            if (part.get("mimeType") or "").lower().startswith("text/html"):
                inner_html = _b64url_decode((part.get("body") or {}).get("data") or "")
                if inner_html:
                    return _strip_html(inner_html)
        for part in parts:
            inner = _extract_plain_text(part, _depth + 1)
            if inner:
                return inner

    if mime_type.startswith("text/html") and data:
        return _strip_html(_b64url_decode(data))
    return ""


def _b64url_decode(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001 - defensive
        return ""


def _strip_html(html: str) -> str:
    """Crudest possible HTML → text. Good enough for the LLM context."""
    import re

    # Remove script/style blocks first.
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL
    )
    # Strip every remaining tag.
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Collapse whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _summarise_http_error(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", None) if exc.resp else None
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        message = payload.get("error", {}).get("message") or str(exc)
    except Exception:  # noqa: BLE001 - fall back to raw
        message = str(exc)
    return f"Gmail HTTP {status}: {message}"


__all__ = [
    "GmailSendError",
    "GmailReadError",
    "FetchedMessage",
    "send_email",
    "send_reply",
    "list_unread_inbox_message_ids",
    "fetch_message",
    "mark_message_read",
]
