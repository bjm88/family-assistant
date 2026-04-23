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
from dataclasses import dataclass
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
    """Fetch a single message, decode the plain-text body, walk attachments.

    Attachments come back inline (in ``body.data``) for small parts and
    via a separate ``users.messages.attachments.get`` call for larger
    ones (Gmail's threshold is ~5 MB but it isn't documented as a hard
    number). We resolve both here so the caller gets a flat list of
    :class:`FetchedAttachment` with ``data`` already populated. Failure
    on any single attachment is logged and skipped — a busted PDF must
    never drop the rest of the message.
    """
    service = _service(creds)
    try:
        raw = (
            service
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
    attachments = _collect_attachments(
        service, message_id=message_id, payload=raw.get("payload") or {}
    )
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
        attachments=attachments,
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


@dataclass(frozen=True)
class FetchedAttachment:
    """One attachment extracted from a Gmail message.

    ``data`` is the raw decoded bytes — always populated, even when
    Gmail required a follow-up ``attachments.get`` call. ``filename``
    comes from the part's Content-Disposition header (Gmail surfaces
    it as ``filename`` on the body); inline images sometimes lack one,
    in which case we fall back to ``"attachment-<index>"`` upstream.
    """

    media_index: int
    filename: Optional[str]
    mime_type: str
    size_bytes: int
    gmail_attachment_id: Optional[str]
    data: bytes


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
        "attachments",
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
        attachments: Optional[List[FetchedAttachment]] = None,
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
        self.attachments: List[FetchedAttachment] = list(attachments or [])

    @property
    def num_media(self) -> int:
        return len(self.attachments)


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


def _collect_attachments(
    service, *, message_id: str, payload: dict
) -> List[FetchedAttachment]:
    """Walk the payload tree and pull every non-text attachment as bytes.

    Gmail signals an attachment in two ways:

    1. The part has a ``filename`` (anything ending up as a real file
       in a normal mail client). This is the ground truth — we use it
       even when the MIME type is also text/* (e.g. an attached .txt
       log file the user does want analysed).
    2. The MIME type does not start with ``text/`` AND it is not a
       ``multipart/*`` container. This catches inline images that came
       in with no Content-Disposition header, which is common from
       phone mail clients.

    Each match is decoded right here — ``body.data`` is base64url'd
    when the file is small, otherwise we pull the bytes via
    ``users.messages.attachments.get(id=…)``. Failures on a single
    attachment are logged and skipped so the rest of the message still
    flows through.
    """
    out: List[FetchedAttachment] = []
    counter = [0]  # mutable so the recursive walker can bump it

    def visit(part: dict) -> None:
        mime_type = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or None
        body = part.get("body") or {}
        sub_parts = part.get("parts") or []

        is_attachment_part = bool(filename) or (
            not mime_type.startswith("text/")
            and not mime_type.startswith("multipart/")
            and (body.get("data") or body.get("attachmentId"))
        )

        if is_attachment_part:
            counter[0] += 1
            idx = counter[0]
            try:
                data = _decode_part_data(
                    service,
                    message_id=message_id,
                    body=body,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Gmail attachment fetch failed message=%s part_idx=%s "
                    "filename=%r mime=%s: %s",
                    message_id, idx, filename, mime_type, exc,
                )
                return
            if not data:
                return
            out.append(
                FetchedAttachment(
                    media_index=idx,
                    filename=filename,
                    mime_type=mime_type or "application/octet-stream",
                    size_bytes=len(data),
                    gmail_attachment_id=body.get("attachmentId"),
                    data=data,
                )
            )
            return

        for sub in sub_parts:
            visit(sub)

    visit(payload)
    return out


def _decode_part_data(service, *, message_id: str, body: dict) -> bytes:
    """Return the raw bytes for a part body, fetching via the API if needed."""
    inline = body.get("data")
    if inline:
        return base64.urlsafe_b64decode(inline.encode("utf-8"))
    attachment_id = body.get("attachmentId")
    if not attachment_id:
        return b""
    resp = (
        service
        .users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    enc = resp.get("data") or ""
    if not enc:
        return b""
    return base64.urlsafe_b64decode(enc.encode("utf-8"))


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
    "FetchedAttachment",
    "FetchedMessage",
    "send_email",
    "send_reply",
    "list_unread_inbox_message_ids",
    "fetch_message",
    "mark_message_read",
]
