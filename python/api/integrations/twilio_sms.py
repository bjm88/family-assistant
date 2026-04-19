"""Twilio SMS / MMS adapter.

Three jobs, all small enough to live in one file:

1. **Verify** that an inbound webhook really came from Twilio. Twilio
   signs every request with HMAC-SHA1 over the (public-URL + sorted
   form params) using your account auth token. We re-compute the same
   signature and compare it against the ``X-Twilio-Signature`` header
   the request claims. Anything that doesn't match is dropped before
   we touch the database.
2. **Send** an outbound reply via Twilio's REST API. We use plain
   ``httpx`` (no Twilio SDK dependency) because the API is just two
   form fields against one URL and pulling in the SDK would balloon
   our import time and lock-step our Python version.
3. **Download** an MMS attachment from a Twilio Media URL. The URLs
   are protected by HTTP Basic with the same ``account_sid`` /
   ``auth_token`` as the REST API and stop working a few minutes
   after the message arrives, so we always copy them to local
   storage immediately.

Nothing in this module looks at the database — it's pure adapter code.
The orchestration (dedup, person lookup, agent loop, reply) lives in
:mod:`api.services.sms_inbox`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import urllib.parse
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Tuple

import httpx


logger = logging.getLogger(__name__)


class TwilioSendError(RuntimeError):
    """Raised when Twilio's REST API rejects the outbound message."""


class TwilioMediaError(RuntimeError):
    """Raised when fetching an MMS media URL fails."""


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def compute_twilio_signature(
    auth_token: str, url: str, params: Mapping[str, str]
) -> str:
    """Compute the value Twilio expects to find in ``X-Twilio-Signature``.

    Algorithm (per Twilio docs): take the full URL Twilio called, append
    each POST param's name+value sorted alphabetically by name (no
    separators between them), HMAC-SHA1 with the auth token, then
    base64-encode the digest. We treat all params as their string form.
    """
    payload = url
    for key in sorted(params):
        payload += key + str(params[key])
    digest = hmac.new(
        auth_token.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_signature(
    *,
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: Optional[str],
) -> bool:
    """Return True iff ``signature`` matches what Twilio would have sent.

    A missing or empty signature returns False — callers decide whether
    to fail closed or fall back to a "no auth token configured" log
    line.
    """
    if not signature:
        return False
    expected = compute_twilio_signature(auth_token, url, params)
    # ``hmac.compare_digest`` is constant-time so a forged signature
    # can't be brute-forced byte-by-byte via response timing.
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Outbound send
# ---------------------------------------------------------------------------


_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


def send_sms(
    *,
    account_sid: str,
    auth_token: str,
    from_phone: str,
    to_phone: str,
    body: str,
    media_urls: Optional[Iterable[str]] = None,
    timeout_seconds: float = 15.0,
) -> str:
    """Send a single SMS / MMS and return Twilio's MessageSid.

    Parameters
    ----------
    from_phone, to_phone
        Both must be in E.164 (``+14155551234``).
    body
        Up to 1600 chars (Twilio will fragment into segments behind
        the scenes). Empty string is allowed when ``media_urls`` is
        provided.
    media_urls
        Optional list of public-internet URLs Twilio should attach as
        MMS media. Each URL adds one ``MediaUrl`` form field.
    """
    if not account_sid or not auth_token:
        raise TwilioSendError(
            "Twilio credentials missing — set TWILIO_ACCOUNT_SID + "
            "TWILIO_AUTH_TOKEN in your .env."
        )
    url = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Messages.json"
    fields: list[Tuple[str, str]] = [
        ("From", from_phone),
        ("To", to_phone),
        ("Body", body),
    ]
    for media in media_urls or ():
        fields.append(("MediaUrl", media))

    # Encode the form body ourselves and pass it as ``content=`` rather
    # than ``data=fields``. httpx 0.28.1 has a bug where a list-of-tuples
    # ``data`` argument leaks raw tuples down to h11's body writer and
    # crashes with ``TypeError: sequence item 1: expected a bytes-like
    # object, tuple found``. Doing the urlencoding here side-steps the
    # broken form encoder while still letting us emit repeated keys
    # (which we need for multi-attachment MMS — Twilio expects multiple
    # ``MediaUrl=`` fields, not a single comma-separated value).
    body_bytes = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")

    try:
        resp = httpx.post(
            url,
            content=body_bytes,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(account_sid, auth_token),
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TwilioSendError(f"transport error: {exc}") from exc

    if resp.status_code >= 400:
        # Twilio errors come back as JSON like
        # {"code": 21211, "message": "Invalid 'To' Phone Number", ...}
        try:
            body_json = resp.json()
            detail = body_json.get("message") or body_json
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise TwilioSendError(f"HTTP {resp.status_code}: {detail}")

    payload = resp.json()
    sid = payload.get("sid", "")
    logger.info(
        "Twilio SMS sent sid=%s to=%s from=%s body_len=%d",
        sid,
        to_phone,
        from_phone,
        len(body),
    )
    return sid


# ---------------------------------------------------------------------------
# MMS media download
# ---------------------------------------------------------------------------


@dataclass
class DownloadedMedia:
    """File contents + metadata for a single MMS attachment."""

    media_index: int
    twilio_media_url: str
    mime_type: str
    file_bytes: bytes


def download_media(
    *,
    account_sid: str,
    auth_token: str,
    media_url: str,
    media_index: int = 0,
    timeout_seconds: float = 30.0,
) -> DownloadedMedia:
    """Pull an MMS media file off Twilio with HTTP Basic auth.

    Twilio media URLs return a 302 to the underlying CDN. ``httpx``
    follows redirects by default, but the redirect target uses signed
    query params instead of Basic auth, so we ask httpx to follow it
    *without* the Authorization header to avoid leaking creds to the
    CDN host.
    """
    if not account_sid or not auth_token:
        raise TwilioMediaError("Twilio credentials missing.")

    # Step 1 — call the original URL with Basic auth, no auto-follow.
    try:
        resp = httpx.get(
            media_url,
            auth=(account_sid, auth_token),
            timeout=timeout_seconds,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise TwilioMediaError(f"transport error: {exc}") from exc

    # If Twilio returns the file directly (rare today, common years
    # ago), use it as-is.
    if resp.status_code == 200:
        mime = resp.headers.get("content-type", "application/octet-stream")
        return DownloadedMedia(
            media_index=media_index,
            twilio_media_url=media_url,
            mime_type=mime.split(";", 1)[0].strip(),
            file_bytes=resp.content,
        )

    # Step 2 — follow the redirect *without* basic auth.
    if resp.status_code in (301, 302, 307):
        location = resp.headers.get("location")
        if not location:
            raise TwilioMediaError(
                f"Twilio returned {resp.status_code} but no Location header."
            )
        try:
            file_resp = httpx.get(
                location,
                timeout=timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise TwilioMediaError(f"transport error following media: {exc}") from exc
        if file_resp.status_code >= 400:
            raise TwilioMediaError(
                f"media CDN returned HTTP {file_resp.status_code}"
            )
        mime = file_resp.headers.get("content-type") or resp.headers.get(
            "content-type", "application/octet-stream"
        )
        return DownloadedMedia(
            media_index=media_index,
            twilio_media_url=media_url,
            mime_type=mime.split(";", 1)[0].strip(),
            file_bytes=file_resp.content,
        )

    raise TwilioMediaError(f"HTTP {resp.status_code}: {resp.text[:300]}")


def extension_for_mime(mime: str) -> str:
    """Best-effort file extension for a MIME type ('image/jpeg' → '.jpg')."""
    ext = mimetypes.guess_extension(mime or "")
    if ext == ".jpe":  # mimetypes returns this on some platforms
        return ".jpg"
    return ext or ".bin"


def parse_inbound_form(form: Mapping[str, str]) -> "InboundSms":
    """Parse a Twilio inbound webhook ``application/x-www-form-urlencoded`` body.

    Twilio sends ~30 form fields per inbound message; we pluck the
    handful we actually use and gather the ``MediaUrl<i>`` /
    ``MediaContentType<i>`` pairs into a list. Unknown fields are
    ignored — they're available on the raw form for ad-hoc debugging
    via the audit row's status_reason.
    """
    body = form.get("Body") or ""
    num_media = int(form.get("NumMedia") or "0")
    media: List[Tuple[int, str, str]] = []
    for i in range(num_media):
        url = form.get(f"MediaUrl{i}")
        ctype = form.get(f"MediaContentType{i}") or "application/octet-stream"
        if url:
            media.append((i, url, ctype))
    return InboundSms(
        message_sid=form.get("MessageSid") or form.get("SmsMessageSid") or "",
        messaging_service_sid=form.get("MessagingServiceSid"),
        account_sid=form.get("AccountSid", ""),
        from_phone=form.get("From", ""),
        to_phone=form.get("To", ""),
        body=body,
        num_media=num_media,
        media=media,
    )


@dataclass
class InboundSms:
    """A parsed Twilio inbound SMS/MMS webhook."""

    message_sid: str
    messaging_service_sid: Optional[str]
    account_sid: str
    from_phone: str
    to_phone: str
    body: str
    num_media: int
    media: List[Tuple[int, str, str]]  # (index, url, content-type)


# ---------------------------------------------------------------------------
# TwiML helpers
# ---------------------------------------------------------------------------


def empty_twiml() -> str:
    """Return the canonical empty TwiML response.

    Twilio expects the webhook to respond with TwiML so it knows
    whether to send a synchronous reply. We always return empty TwiML
    and instead send our reply asynchronously via the REST API — that
    way the agent loop can take 30+ seconds without keeping Twilio's
    HTTP socket open.
    """
    return '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def truncate_for_sms(text: str, *, max_chars: int) -> str:
    """Trim ``text`` so it fits in a single SMS-friendly reply.

    Cuts at the last whitespace before ``max_chars`` so we don't slice
    a word in half, then appends an ellipsis if anything was dropped.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


__all__ = [
    "DownloadedMedia",
    "InboundSms",
    "TwilioSendError",
    "TwilioMediaError",
    "compute_twilio_signature",
    "download_media",
    "empty_twiml",
    "extension_for_mime",
    "parse_inbound_form",
    "send_sms",
    "truncate_for_sms",
    "verify_twilio_signature",
]
