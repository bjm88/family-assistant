"""Slack adapter — Events API in, Web API out.

Four jobs, all plain ``httpx`` (no Slack SDK):

1. **Verify** an Events API request against the app's signing secret.
2. **Parse** an event payload (or a ``conversations.history`` row, for
   the downtime catch-up) into an :class:`InboundSlackMessage`, or a
   :class:`SkippedEvent` saying why Avi shouldn't answer it.
3. **Send** a reply with ``chat.postMessage``.
4. **Download** a shared file with the token that can see it.

Avi can have two identities in a workspace: the app's bot user (people
DM the app) and a regular member account (people DM Avi like anyone
else). ``via`` on every inbound says which one received the message —
``"bot"`` or ``"user"`` — and the reply must go out with that same
identity's token.

Nothing here touches the database. Orchestration lives in
:mod:`api.services.slack_inbox`.

Web API reference: https://api.slack.com/web
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse

import httpx


logger = logging.getLogger(__name__)


_SLACK_API_BASE = "https://slack.com/api"

VIA_BOT = "bot"
VIA_USER = "user"


class SlackError(RuntimeError):
    """A Slack call failed. ``code`` is Slack's own error token, if any."""

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class SlackSendError(SlackError):
    """``chat.postMessage`` was rejected or unreachable."""


class SlackReadError(SlackError):
    """A read call (auth.test, users.info, history, file download) failed."""


# ---------------------------------------------------------------------------
# Request signing
# ---------------------------------------------------------------------------


def verify_signature(
    *,
    signing_secret: Optional[str],
    timestamp: Optional[str],
    body: bytes,
    signature: Optional[str],
    now: Optional[float] = None,
    tolerance_seconds: int = 300,
) -> bool:
    """True iff ``signature`` is Slack's HMAC for this exact body.

    Slack signs ``v0:<timestamp>:<raw body>`` with the app's signing
    secret. Requests older than five minutes are refused so a captured
    request can't be replayed later.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - sent_at) > tolerance_seconds:
        return False
    expected = sign(signing_secret=signing_secret, timestamp=timestamp, body=body)
    return hmac.compare_digest(expected, signature)


def sign(*, signing_secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-Slack-Signature`` value Slack would send for this request."""
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256)
    return "v0=" + digest.hexdigest()


# ---------------------------------------------------------------------------
# Web API plumbing
# ---------------------------------------------------------------------------


def _call(
    token: str,
    method: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    json_body: Optional[Mapping[str, Any]] = None,
    error_cls: type[SlackError] = SlackReadError,
    timeout_seconds: float = 15.0,
) -> dict:
    """One Web API call. Raises ``error_cls`` unless Slack says ``ok``."""
    if not token:
        raise error_cls(f"{method}: no Slack token configured.")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{_SLACK_API_BASE}/{method}"
    resp: Optional[httpx.Response] = None
    for attempt in (1, 2):
        try:
            if json_body is not None:
                resp = httpx.post(
                    url, headers=headers, json=dict(json_body), timeout=timeout_seconds
                )
            else:
                resp = httpx.get(
                    url, headers=headers, params=dict(params or {}), timeout=timeout_seconds
                )
        except httpx.HTTPError as exc:
            raise error_cls(f"{method}: transport error: {exc}") from exc
        if resp.status_code != 429 or attempt == 2:
            break
        # Rate limited: Slack says how long to wait. One short retry.
        try:
            wait = float(resp.headers.get("Retry-After") or 1)
        except ValueError:
            wait = 1.0
        time.sleep(min(max(wait, 0.5), 5.0))
    assert resp is not None
    if resp.status_code >= 400:
        raise error_cls(f"{method}: HTTP {resp.status_code}", code="http_error")
    try:
        body = resp.json()
    except ValueError as exc:
        raise error_cls(f"{method}: response was not JSON") from exc
    if not body.get("ok"):
        code = str(body.get("error") or "unknown_error")
        needed = body.get("needed")
        detail = f" (missing scope: {needed})" if needed else ""
        raise error_cls(f"{method}: {code}{detail}", code=code)
    return body


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackIdentity:
    """Who a token acts as (``auth.test``)."""

    team_id: str
    team_name: Optional[str]
    user_id: str
    user_name: Optional[str]
    bot_id: Optional[str]
    url: Optional[str]


def auth_test(token: str, *, timeout_seconds: float = 10.0) -> SlackIdentity:
    body = _call(token, "auth.test", json_body={}, timeout_seconds=timeout_seconds)
    return SlackIdentity(
        team_id=str(body.get("team_id") or ""),
        team_name=body.get("team"),
        user_id=str(body.get("user_id") or ""),
        user_name=body.get("user"),
        bot_id=body.get("bot_id"),
        url=body.get("url"),
    )


_IDENTITY_CACHE: dict[str, SlackIdentity] = {}
_CACHE_LOCK = threading.Lock()


def auth_test_cached(token: str) -> SlackIdentity:
    """:func:`auth_test`, remembered for the life of the process.

    A token's team and user never change, and the inbox needs them on
    every event to recognise Avi's own messages.
    """
    with _CACHE_LOCK:
        cached = _IDENTITY_CACHE.get(token)
    if cached is not None:
        return cached
    fresh = auth_test(token)
    with _CACHE_LOCK:
        _IDENTITY_CACHE[token] = fresh
    return fresh


@dataclass(frozen=True)
class SlackUser:
    """The parts of ``users.info`` the inbox decides on."""

    user_id: str
    team_id: Optional[str]
    email: Optional[str]
    display_name: Optional[str]
    is_bot: bool = False
    deleted: bool = False


def get_user(token: str, user_id: str) -> SlackUser:
    """Look a member up. ``email`` needs the ``users:read.email`` scope."""
    body = _call(token, "users.info", params={"user": user_id})
    user = body.get("user") or {}
    profile = user.get("profile") or {}
    name = (
        profile.get("real_name")
        or user.get("real_name")
        or profile.get("display_name")
        or user.get("name")
    )
    return SlackUser(
        user_id=str(user.get("id") or user_id),
        team_id=user.get("team_id"),
        email=(profile.get("email") or None),
        display_name=name or None,
        is_bot=bool(user.get("is_bot")),
        deleted=bool(user.get("deleted")),
    )


_USER_CACHE: dict[str, Tuple[SlackUser, float]] = {}
_USER_TTL_SECONDS = 10 * 60


def get_user_cached(token: str, user_id: str) -> SlackUser:
    """:func:`get_user` with a ten-minute memory.

    Short on purpose: the email on the Slack account is what decides
    who the sender is, so a changed or deactivated account has to be
    noticed quickly.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _USER_CACHE.get(user_id)
    if cached is not None and now - cached[1] < _USER_TTL_SECONDS:
        return cached[0]
    fresh = get_user(token, user_id)
    with _CACHE_LOCK:
        _USER_CACHE[user_id] = (fresh, now)
    return fresh


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


def post_message(
    *,
    token: str,
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
    timeout_seconds: float = 15.0,
) -> str:
    """Post ``text`` and return the new message's ``ts``.

    ``text`` must already be Slack-safe — run it through
    :func:`format_outbound` first.
    """
    payload: dict[str, Any] = {
        "channel": channel,
        "text": text,
        # Avi writes the answer himself; link preview cards are noise.
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    body = _call(
        token,
        "chat.postMessage",
        json_body=payload,
        error_cls=SlackSendError,
        timeout_seconds=timeout_seconds,
    )
    ts = str(body.get("ts") or "")
    logger.info(
        "Slack chat.postMessage channel=%s ts=%s thread=%s body_len=%d",
        channel,
        ts,
        thread_ts or "-",
        len(text),
    )
    return ts


_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_MD_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^([ \t]*)[*-][ \t]+", re.MULTILINE)


def format_outbound(text: str, *, max_chars: int) -> str:
    """Make a model-written reply safe and readable as a Slack message.

    Slack treats ``<...>`` as markup: ``<!channel>`` pings everyone and
    ``<@U123>`` pings a member. Every ``&``, ``<`` and ``>`` is escaped
    so nothing the model writes can do that. The few Markdown habits
    models keep regardless of instructions are translated to Slack's
    own format — ``**bold**``, ``[label](url)``, ``# headings`` and
    ``- bullets`` — so they don't show up as stray symbols.
    """
    text = truncate_for_slack(text, max_chars=max_chars)
    links: List[Tuple[str, str]] = []

    def _stash(match: re.Match) -> str:
        links.append((match.group(1), match.group(2)))
        return f"\x00{len(links) - 1}\x00"

    text = _MD_LINK_RE.sub(_stash, text)
    text = _escape(text)
    text = _MD_HEADING_RE.sub(lambda m: f"*{m.group(1).strip('*_ ')}*", text)
    text = _MD_BOLD_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _MD_BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)

    def _restore(match: re.Match) -> str:
        label, url = links[int(match.group(1))]
        url = url.replace("&", "&amp;").replace("<", "%3C").replace(">", "%3E")
        return f"<{url}|{_escape(label).replace('|', '/')}>"

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate_for_slack(text: str, *, max_chars: int) -> str:
    """Trim ``text`` to one comfortably-sized Slack message."""
    text = (text or "").strip()
    cap = max(64, min(int(max_chars), 12_000))
    if len(text) <= cap:
        return text
    cut = text[: cap - 1]
    last_space = cut.rfind(" ")
    if last_space > cap * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@dataclass
class DownloadedFile:
    mime_type: str
    file_bytes: bytes


def download_file(
    *,
    token: str,
    url: str,
    hint_mime: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> DownloadedFile:
    """Fetch a shared file. Needs the ``files:read`` scope on ``token``."""
    if not token:
        raise SlackReadError("file download: no Slack token configured.")
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or not (
        host == "slack.com" or host.endswith(".slack.com")
    ):
        # The token goes out as a bearer header; only ever to Slack.
        raise SlackReadError(f"file download: refusing non-Slack URL host {host!r}.")
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise SlackReadError(f"file download: transport error: {exc}") from exc
    if resp.status_code >= 400:
        raise SlackReadError(f"file download: HTTP {resp.status_code}")
    mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    wanted = (hint_mime or "").split(";", 1)[0].strip().lower()
    if mime == "text/html" and wanted != "text/html":
        # Without files:read Slack answers 200 with its sign-in page.
        raise SlackReadError(
            "file download: Slack returned a sign-in page instead of the file "
            "— the token is missing the files:read scope.",
            code="missing_scope",
        )
    if not mime or mime == "application/octet-stream":
        mime = wanted or mimetypes.guess_type(urlparse(url).path)[0] or "application/octet-stream"
    return DownloadedFile(mime_type=mime, file_bytes=resp.content)


def extension_for_mime(mime: str) -> str:
    ext = mimetypes.guess_extension(mime or "")
    if ext == ".jpe":
        return ".jpg"
    return ext or ".bin"


# ---------------------------------------------------------------------------
# History (downtime catch-up)
# ---------------------------------------------------------------------------


def conversations_history(
    *, token: str, channel: str, oldest: str, limit: int = 100
) -> List[Mapping[str, Any]]:
    """Messages in ``channel`` newer than ``oldest``, oldest first."""
    body = _call(
        token,
        "conversations.history",
        params={
            "channel": channel,
            "oldest": oldest,
            "inclusive": "false",
            "limit": max(1, min(int(limit), 200)),
        },
    )
    messages = [m for m in (body.get("messages") or []) if isinstance(m, Mapping)]
    return sorted(messages, key=lambda m: _ts_key(str(m.get("ts") or "0")))


def _ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Inbound parsing
# ---------------------------------------------------------------------------


@dataclass
class InboundFile:
    """One file shared with a message."""

    media_index: int  # 1-based, matches "[Attachment N: …]" in the prompt
    file_id: str
    name: str
    mime_type: str
    size_bytes: Optional[int]
    url: Optional[str]


@dataclass
class InboundSlackMessage:
    """A Slack message Avi should consider answering."""

    team_id: str
    channel_id: str
    channel_type: str  # 'im' | 'mpim' | 'channel' | 'group'
    ts: str
    thread_ts: Optional[str]
    user_id: str
    body: str
    via: str  # VIA_BOT | VIA_USER — which of Avi's identities received it
    event_id: Optional[str] = None
    files: List[InboundFile] = field(default_factory=list)

    @property
    def num_media(self) -> int:
        return len(self.files)

    @property
    def is_direct(self) -> bool:
        return self.channel_type == "im"

    @property
    def reply_thread_ts(self) -> Optional[str]:
        """Where the reply goes: a DM is answered inline like any chat,
        a thread in its thread, a channel mention in a new thread under
        it so the channel stays readable."""
        if self.thread_ts:
            return self.thread_ts
        return None if self.is_direct else self.ts


@dataclass(frozen=True)
class SkippedEvent:
    """An event Avi must not answer, and why."""

    reason: str


# ``file_share`` is an ordinary message with files; ``thread_broadcast``
# is a thread reply also shown in the channel. Every other subtype is an
# edit, deletion, join notice or bot post — nothing a person just said.
_SPEAKING_SUBTYPES = frozenset({"", "file_share", "thread_broadcast"})


def parse_event(
    payload: Mapping[str, Any],
    *,
    bot_user_id: Optional[str],
    human_user_id: Optional[str],
    app_id: Optional[str] = None,
    team_id: Optional[str] = None,
    resolve_user: Optional[Callable[[str], Optional[str]]] = None,
) -> Union[InboundSlackMessage, SkippedEvent]:
    """Turn an Events API ``event_callback`` into a message or a skip.

    ``bot_user_id`` / ``human_user_id`` are Avi's own Slack user ids
    (either may be ``None`` when that identity isn't configured).
    """
    if payload.get("type") != "event_callback":
        return SkippedEvent(f"not an event_callback ({payload.get('type')!r})")
    if app_id and payload.get("api_app_id") and payload.get("api_app_id") != app_id:
        return SkippedEvent("event is for a different Slack app")
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return SkippedEvent("no event body")
    event_team = str(payload.get("team_id") or event.get("team") or "")
    if team_id and event_team and event_team != team_id:
        return SkippedEvent("event is from a different workspace")

    via = _via_for(payload, bot_user_id=bot_user_id, human_user_id=human_user_id)
    if isinstance(via, SkippedEvent):
        return via

    return parse_message(
        event,
        team_id=event_team or (team_id or ""),
        via=via,
        bot_user_id=bot_user_id,
        human_user_id=human_user_id,
        app_id=app_id,
        event_id=payload.get("event_id"),
        resolve_user=resolve_user,
    )


def _via_for(
    payload: Mapping[str, Any],
    *,
    bot_user_id: Optional[str],
    human_user_id: Optional[str],
) -> Union[str, SkippedEvent]:
    """Which of Avi's identities this event was delivered for.

    An app can also be authorised by other members, and Slack then
    delivers *their* private messages too. Those are not addressed to
    Avi, so anything not delivered for one of Avi's own identities is
    dropped.
    """
    auths = payload.get("authorizations") or []
    auth = auths[0] if auths and isinstance(auths[0], Mapping) else None
    if auth is None:
        return VIA_BOT if bot_user_id else VIA_USER
    auth_user = auth.get("user_id")
    if auth.get("is_bot"):
        if bot_user_id and auth_user and auth_user != bot_user_id:
            return SkippedEvent("event was delivered for a different bot user")
        return VIA_BOT
    if human_user_id and auth_user == human_user_id:
        return VIA_USER
    return SkippedEvent("event was delivered for another member's account, not Avi's")


def parse_message(
    event: Mapping[str, Any],
    *,
    team_id: str,
    via: str,
    bot_user_id: Optional[str],
    human_user_id: Optional[str],
    app_id: Optional[str] = None,
    event_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    channel_type: Optional[str] = None,
    resolve_user: Optional[Callable[[str], Optional[str]]] = None,
) -> Union[InboundSlackMessage, SkippedEvent]:
    """Parse one message object (from an event or from history)."""
    event_type = str(event.get("type") or "message")
    if event_type not in ("message", "app_mention"):
        return SkippedEvent(f"event type {event_type!r} is not a message")
    subtype = str(event.get("subtype") or "")
    if subtype not in _SPEAKING_SUBTYPES:
        return SkippedEvent(f"message subtype {subtype!r}")
    if event.get("hidden"):
        return SkippedEvent("hidden message")

    self_ids = {uid for uid in (bot_user_id, human_user_id) if uid}
    user_id = str(event.get("user") or "")
    if not user_id:
        return SkippedEvent("message has no human sender")
    if user_id in self_ids:
        return SkippedEvent("Avi's own message")
    if event.get("bot_id") or (app_id and event.get("app_id") == app_id):
        return SkippedEvent("message was posted by an app")

    channel = str(channel_id or event.get("channel") or "")
    ts = str(event.get("ts") or "")
    if not channel or not ts:
        return SkippedEvent("message has no channel or timestamp")

    raw_text = str(event.get("text") or "")
    kind = str(channel_type or event.get("channel_type") or "")
    if not kind:
        kind = "im" if channel.startswith("D") else "channel"
    if kind != "im":
        # Outside a DM Avi only answers when he is addressed by name,
        # and he answers as whichever of his identities was named.
        # Slack reports a single authorization per event even when both
        # identities can see the channel, so the one it was delivered
        # for is not always the one that was mentioned.
        named = {
            VIA_BOT: bool(bot_user_id) and _mentions(raw_text, bot_user_id),
            VIA_USER: bool(human_user_id) and _mentions(raw_text, human_user_id),
        }
        if event_type == "app_mention":
            via = VIA_BOT
        elif not named[via]:
            other = VIA_USER if via == VIA_BOT else VIA_BOT
            if not named[other]:
                return SkippedEvent("channel message that doesn't mention Avi")
            via = other

    body = clean_text(raw_text, strip_user_ids=self_ids, resolve_user=resolve_user)
    shared = _shared_messages(event, resolve_user=resolve_user)
    if shared:
        body = f"{body}\n\n{shared}".strip()

    thread_ts = str(event.get("thread_ts") or "") or None
    return InboundSlackMessage(
        team_id=team_id,
        channel_id=channel,
        channel_type=kind,
        ts=ts,
        thread_ts=thread_ts if thread_ts != ts else None,
        user_id=user_id,
        body=body,
        via=via,
        event_id=str(event_id) if event_id else None,
        files=_files(event),
    )


def _mentions(text: str, user_id: str) -> bool:
    return f"<@{user_id}>" in text or f"<@{user_id}|" in text


def _files(event: Mapping[str, Any]) -> List[InboundFile]:
    out: List[InboundFile] = []
    for item in event.get("files") or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        if item.get("mode") == "tombstone":  # deleted before we saw it
            continue
        size = item.get("size")
        out.append(
            InboundFile(
                media_index=len(out) + 1,
                file_id=str(item["id"]),
                name=str(item.get("name") or item.get("title") or f"file-{len(out) + 1}"),
                mime_type=str(item.get("mimetype") or "application/octet-stream"),
                size_bytes=int(size) if isinstance(size, (int, float)) else None,
                url=item.get("url_private_download") or item.get("url_private"),
            )
        )
    return out


def _shared_messages(
    event: Mapping[str, Any],
    *,
    resolve_user: Optional[Callable[[str], Optional[str]]],
) -> str:
    """Text of any message the sender forwarded along with theirs."""
    blocks: List[str] = []
    for att in event.get("attachments") or []:
        if not isinstance(att, Mapping):
            continue
        if not (att.get("is_share") or att.get("is_msg_unfurl")):
            continue
        text = clean_text(
            str(att.get("text") or att.get("fallback") or ""),
            resolve_user=resolve_user,
        )
        if not text:
            continue
        author = att.get("author_name") or att.get("author_subname")
        header = f"[Forwarded message from {author}]" if author else "[Forwarded message]"
        blocks.append(f"{header}\n{text}")
    return "\n\n".join(blocks)


_ANGLE_RE = re.compile(r"<([^<>\n]+)>")


def clean_text(
    text: str,
    *,
    strip_user_ids: Optional[set[str]] = None,
    resolve_user: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Slack's wire format → the text the person actually typed.

    Slack wraps links, mentions and channels in ``<...>`` and
    HTML-escapes ``& < >``. Mentions of Avi himself are dropped (they
    only say "this is for you"); other mentions become ``@Name``.
    """
    strip_user_ids = strip_user_ids or set()
    addressed = False

    def _swap(match: re.Match) -> str:
        nonlocal addressed
        inner = match.group(1)
        target, _, label = inner.partition("|")
        if target.startswith("@"):
            uid = target[1:]
            if uid in strip_user_ids:
                addressed = True
                return ""
            name = label or (resolve_user(uid) if resolve_user else None)
            return f"@{name or uid}"
        if target.startswith("#"):
            return f"#{label or target[1:]}"
        if target.startswith("!"):
            return f"@{label or target[1:].split('^')[0]}"
        if target.startswith(("mailto:", "tel:")):
            return label or target.split(":", 1)[1]
        if label and label not in (target, target.split("://", 1)[-1]):
            return f"{label} ({target})"
        return target

    cleaned = _ANGLE_RE.sub(_swap, text or "")
    cleaned = (
        cleaned.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if addressed:
        # "@Avi, what's for dinner" -> "what's for dinner"
        cleaned = re.sub(r"^[,:]\s+", "", cleaned)
    return cleaned


__all__ = [
    "DownloadedFile",
    "InboundFile",
    "InboundSlackMessage",
    "SkippedEvent",
    "SlackError",
    "SlackIdentity",
    "SlackReadError",
    "SlackSendError",
    "SlackUser",
    "VIA_BOT",
    "VIA_USER",
    "auth_test",
    "auth_test_cached",
    "clean_text",
    "conversations_history",
    "download_file",
    "extension_for_mime",
    "format_outbound",
    "get_user",
    "get_user_cached",
    "parse_event",
    "parse_message",
    "post_message",
    "sign",
    "truncate_for_slack",
    "verify_signature",
]
