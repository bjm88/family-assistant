"""Twilio SMS / MMS / WhatsApp adapter.

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
   our import time and lock-step our Python version. Both SMS and
   WhatsApp share the exact same endpoint — WhatsApp simply requires a
   ``whatsapp:`` prefix on the ``From`` and ``To`` fields, which
   :func:`send_whatsapp` adds for you.
3. **Download** an MMS / WhatsApp attachment from a Twilio Media URL.
   The URLs are protected by HTTP Basic with the same ``account_sid``
   / ``auth_token`` as the REST API and stop working a few minutes
   after the message arrives, so we always copy them to local storage
   immediately.

Nothing in this module looks at the database — it's pure adapter code.
The orchestration (dedup, person lookup, agent loop, reply) lives in
:mod:`api.services.sms_inbox` for both SMS and WhatsApp; the inbound
form parser sets :attr:`InboundSms.channel` based on the ``whatsapp:``
prefix on ``From`` so the service can branch on the right surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import mimetypes
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Mapping, Optional, Tuple

import httpx


logger = logging.getLogger(__name__)


class TwilioSendError(RuntimeError):
    """Raised when Twilio's REST API rejects the outbound message."""


class TwilioReadError(RuntimeError):
    """Raised when listing inbound messages from Twilio fails."""


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


# Twilio's WhatsApp surface uses the same Programmable Messaging
# endpoint as SMS but tags every party with this prefix on both inbound
# (``From=whatsapp:+1...``) and outbound (the API rejects unprefixed
# numbers when sending from a WhatsApp sender).
WHATSAPP_PREFIX = "whatsapp:"


def is_whatsapp_address(addr: Optional[str]) -> bool:
    """True iff ``addr`` is a Twilio WhatsApp-style address.

    Used by the webhook router / inbox service to decide whether the
    inbound form belongs to the SMS branch or the WhatsApp branch
    without needing a separate webhook URL or ``?senderType=`` query
    param.
    """
    return bool(addr) and addr.startswith(WHATSAPP_PREFIX)


def strip_whatsapp_prefix(addr: Optional[str]) -> str:
    """Return ``addr`` with any ``whatsapp:`` prefix removed.

    Useful for handing the bare E.164 phone number to anything that
    expects an SMS-shaped string (e.g. ``utils.phone.normalize_phone``,
    person-lookup, audit log fields).
    """
    if not addr:
        return ""
    if addr.startswith(WHATSAPP_PREFIX):
        return addr[len(WHATSAPP_PREFIX):]
    return addr


def with_whatsapp_prefix(addr: str) -> str:
    """Return ``addr`` guaranteed to carry the ``whatsapp:`` prefix.

    Idempotent so callers can safely pass either ``+15551234567`` or
    ``whatsapp:+15551234567`` without double-prefixing.
    """
    if addr.startswith(WHATSAPP_PREFIX):
        return addr
    return f"{WHATSAPP_PREFIX}{addr}"


def send_sms(
    *,
    account_sid: str,
    auth_token: str,
    from_phone: str,
    to_phone: str,
    body: str,
    media_urls: Optional[Iterable[str]] = None,
    messaging_service_sid: Optional[str] = None,
    status_callback: Optional[str] = None,
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
    messaging_service_sid
        A2P 10DLC Messaging Service (``MG…``). Required for US-bound
        SMS from a 10-digit long code — sending with a bare ``From``
        is accepted by the API then blocked as error 30034.
    status_callback
        Public URL Twilio should POST delivery receipts to. Without
        this we only see the create-time ``queued`` status and miss
        later ``undelivered`` / ``failed`` outcomes.
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
    if messaging_service_sid:
        fields.append(("MessagingServiceSid", messaging_service_sid))
    if status_callback:
        fields.append(("StatusCallback", status_callback))
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
    status = payload.get("status")
    error_code = payload.get("error_code")
    logger.info(
        "Twilio SMS accepted sid=%s to=%s from=%s body_len=%d "
        "status=%s error_code=%s messaging_service_sid=%s",
        sid,
        to_phone,
        from_phone,
        len(body),
        status,
        error_code,
        messaging_service_sid,
    )
    if status in {"failed", "undelivered"}:
        raise TwilioSendError(
            f"Twilio {status} sid={sid} error_code={error_code} "
            f"error_message={payload.get('error_message')}"
        )
    return sid


def send_whatsapp(
    *,
    account_sid: str,
    auth_token: str,
    from_phone: str,
    to_phone: str,
    body: str,
    media_urls: Optional[Iterable[str]] = None,
    status_callback: Optional[str] = None,
    timeout_seconds: float = 15.0,
) -> str:
    """Send a single WhatsApp reply via Twilio and return the MessageSid.

    Thin wrapper around :func:`send_sms` that adds the ``whatsapp:``
    prefix Twilio requires on both ``From`` and ``To`` for WhatsApp
    business sends. ``from_phone`` should be your approved WhatsApp
    sender number (E.164 — e.g. ``+14155238886`` for the Twilio
    sandbox); ``to_phone`` should be the user's WhatsApp-registered
    phone number, also E.164. Either may already carry the prefix
    (e.g. when the inbound webhook's ``From`` is forwarded straight
    through) — :func:`with_whatsapp_prefix` is idempotent.

    Twilio's REST behaviour:
    * ``Body`` may be up to 4096 chars for free-form replies inside
      the 24-hour customer-care window.
    * Outside that window the API returns HTTP 400 / Twilio error
      63016 ("Failed to send freeform message because you are outside
      the allowed window") and the call must use a pre-approved
      template instead. We surface that as :class:`TwilioSendError`
      verbatim — the caller can decide whether to retry with a
      template or just record the failure.
    """
    return send_sms(
        account_sid=account_sid,
        auth_token=auth_token,
        from_phone=with_whatsapp_prefix(from_phone),
        to_phone=with_whatsapp_prefix(to_phone),
        body=body,
        media_urls=media_urls,
        status_callback=status_callback,
        timeout_seconds=timeout_seconds,
    )


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
    raw_from = form.get("From", "")
    raw_to = form.get("To", "")
    # Twilio prefixes both From and To with `whatsapp:` for WhatsApp
    # messages on the same Programmable Messaging webhook used for SMS.
    # Detecting on `From` is sufficient because Twilio always tags both
    # ends consistently — but we look at both so a misconfigured
    # template can never silently route a WhatsApp message into the SMS
    # branch (or vice versa) just because one field happens to match.
    channel = "whatsapp" if (
        is_whatsapp_address(raw_from) or is_whatsapp_address(raw_to)
    ) else "sms"
    return InboundSms(
        message_sid=form.get("MessageSid") or form.get("SmsMessageSid") or "",
        messaging_service_sid=form.get("MessagingServiceSid"),
        account_sid=form.get("AccountSid", ""),
        from_phone=raw_from,
        to_phone=raw_to,
        body=body,
        num_media=num_media,
        media=media,
        channel=channel,
    )


@dataclass
class InboundSms:
    """A parsed Twilio inbound SMS / MMS / WhatsApp webhook."""

    message_sid: str
    messaging_service_sid: Optional[str]
    account_sid: str
    from_phone: str
    to_phone: str
    body: str
    num_media: int
    media: List[Tuple[int, str, str]]  # (index, url, content-type)
    # 'sms' for plain SMS / MMS; 'whatsapp' when From/To carry the
    # ``whatsapp:`` prefix Twilio uses for its WhatsApp Programmable
    # Messaging surface. Defaulted so older callers and tests that
    # construct InboundSms by hand keep working unchanged.
    channel: str = "sms"


# ---------------------------------------------------------------------------
# Inbox listing (downtime recovery — webhooks Twilio gave up on)
# ---------------------------------------------------------------------------


def _parse_twilio_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _message_resource_to_inbound(item: Mapping[str, object]) -> Optional[InboundSms]:
    """Map one Twilio Message resource onto :class:`InboundSms`.

    Returns None for outbound traffic. Media URLs are filled separately
    — the list endpoint does not inline them.
    """
    direction = str(item.get("direction") or "")
    if not direction.startswith("inbound"):
        return None
    sid = str(item.get("sid") or "")
    if not sid:
        return None
    raw_from = str(item.get("from") or "")
    raw_to = str(item.get("to") or "")
    try:
        num_media = int(item.get("num_media") or 0)
    except (TypeError, ValueError):
        num_media = 0
    channel = (
        "whatsapp"
        if is_whatsapp_address(raw_from) or is_whatsapp_address(raw_to)
        else "sms"
    )
    messaging_service_sid = item.get("messaging_service_sid")
    return InboundSms(
        message_sid=sid,
        messaging_service_sid=(
            str(messaging_service_sid) if messaging_service_sid else None
        ),
        account_sid=str(item.get("account_sid") or ""),
        from_phone=raw_from,
        to_phone=raw_to,
        body=str(item.get("body") or ""),
        num_media=num_media,
        media=[],
        channel=channel,
    )


def _attach_media_urls(
    inbound: InboundSms,
    *,
    account_sid: str,
    auth_token: str,
    timeout_seconds: float,
) -> None:
    """Best-effort Media list. A failure leaves the text body usable."""
    if inbound.num_media <= 0 or not inbound.message_sid:
        return
    url = (
        f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Messages/"
        f"{inbound.message_sid}/Media.json"
    )
    try:
        resp = httpx.get(
            url, auth=(account_sid, auth_token), timeout=timeout_seconds
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "Twilio media list failed for %s: %s", inbound.message_sid, exc
        )
        return
    if resp.status_code >= 400:
        logger.warning(
            "Twilio media list HTTP %s for %s",
            resp.status_code,
            inbound.message_sid,
        )
        return
    media: List[Tuple[int, str, str]] = []
    for index, item in enumerate(resp.json().get("media_list") or []):
        uri = str(item.get("uri") or "")
        if not uri:
            continue
        if uri.endswith(".json"):
            uri = uri[: -len(".json")]
        content_url = uri if uri.startswith("http") else f"https://api.twilio.com{uri}"
        media.append(
            (index, content_url, str(item.get("content_type") or "application/octet-stream"))
        )
    inbound.media = media


def list_inbound_messages(
    *,
    account_sid: str,
    auth_token: str,
    to_phone: str,
    sent_after: datetime,
    page_size: int = 50,
    max_pages: int = 20,
    timeout_seconds: float = 20.0,
) -> List[Tuple[datetime, InboundSms]]:
    """List inbound messages Twilio delivered to ``to_phone`` since ``sent_after``.

    Pages newest-first and stops once a page crosses the cutoff. Each
    item is ``(sent_at, inbound)``. Media URLs are attached when Twilio
    still has them; a media-list failure does not drop the message.
    """
    if not account_sid or not auth_token:
        raise TwilioReadError("Twilio credentials missing.")
    if sent_after.tzinfo is None:
        sent_after = sent_after.replace(tzinfo=timezone.utc)

    url: Optional[str] = f"{_TWILIO_API_BASE}/Accounts/{account_sid}/Messages.json"
    params: Optional[dict] = {"To": to_phone, "PageSize": int(page_size)}
    found: List[Tuple[datetime, InboundSms]] = []

    for _page in range(max_pages):
        if not url:
            break
        try:
            resp = httpx.get(
                url,
                params=params,
                auth=(account_sid, auth_token),
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TwilioReadError(f"transport error: {exc}") from exc
        # next_page_uri already carries the query string.
        params = None
        if resp.status_code >= 400:
            raise TwilioReadError(
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        payload = resp.json()
        stop = False
        for item in payload.get("messages") or []:
            sent_at = _parse_twilio_timestamp(
                item.get("date_sent") or item.get("date_created")
            )
            if sent_at is not None and sent_at < sent_after:
                stop = True
                continue
            inbound = _message_resource_to_inbound(item)
            if inbound is None:
                continue
            _attach_media_urls(
                inbound,
                account_sid=account_sid,
                auth_token=auth_token,
                timeout_seconds=timeout_seconds,
            )
            found.append((sent_at or sent_after, inbound))
        if stop:
            break
        next_uri = payload.get("next_page_uri")
        if not next_uri:
            break
        next_uri = str(next_uri)
        url = (
            next_uri
            if next_uri.startswith("http")
            else f"https://api.twilio.com{next_uri}"
        )
    return found


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
    "TwilioMediaError",
    "TwilioReadError",
    "TwilioSendError",
    "list_inbound_messages",
    "WHATSAPP_PREFIX",
    "compute_twilio_signature",
    "download_media",
    "empty_twiml",
    "extension_for_mime",
    "is_whatsapp_address",
    "parse_inbound_form",
    "send_sms",
    "send_whatsapp",
    "strip_whatsapp_prefix",
    "truncate_for_sms",
    "verify_twilio_signature",
    "with_whatsapp_prefix",
]
