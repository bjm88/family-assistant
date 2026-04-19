"""Session lifecycle helpers for the live AI assistant.

Centralises the logic that:

* finds or creates the family's currently-active :class:`LiveSession`,
* sweeps idle sessions to ``ended_at = now()`` after the configured
  timeout,
* upserts :class:`LiveSessionParticipant` rows and flips
  ``greeted_already`` atomically,
* appends :class:`LiveSessionMessage` rows and bumps
  ``last_activity_at`` in the same transaction.

Keeping these helpers in one place means both the dedicated session
router (``routers/live_sessions.py``) and the existing chat/greet
endpoints (``routers/ai_chat.py``) call exactly the same code path —
no chance of the two diverging on timeout maths or activity tracking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _idle_cutoff() -> datetime:
    minutes = max(1, int(get_settings().AI_LIVE_SESSION_IDLE_MINUTES))
    return _now() - timedelta(minutes=minutes)


# ---------- idle sweep ------------------------------------------------------


def close_stale_sessions(
    db: Session, family_id: Optional[int] = None
) -> int:
    """Close any sessions whose ``last_activity_at`` is older than the
    configured idle window.

    Called on every :func:`ensure_active_session` and whenever the history
    list is fetched, so the client never has to think about timeouts —
    the backend keeps the books automatically. Returns the number of
    sessions closed so callers can log it if useful.
    """
    stmt = (
        update(models.LiveSession)
        .where(models.LiveSession.ended_at.is_(None))
        .where(models.LiveSession.last_activity_at < _idle_cutoff())
        .values(ended_at=_now(), end_reason="timeout")
    )
    if family_id is not None:
        stmt = stmt.where(models.LiveSession.family_id == family_id)
    result = db.execute(stmt)
    count = result.rowcount or 0
    if count:
        logger.info(
            "Closed %d stale live session(s) (idle threshold %d min)",
            count,
            get_settings().AI_LIVE_SESSION_IDLE_MINUTES,
        )
    return count


# ---------- ensure + fetch --------------------------------------------------


def get_active_session(
    db: Session, family_id: int
) -> Optional[models.LiveSession]:
    """Return the currently-active session for a family, if any.

    Does *not* sweep stale sessions — callers that need a guaranteed-
    fresh read should use :func:`ensure_active_session` instead.
    """
    return db.execute(
        select(models.LiveSession)
        .where(models.LiveSession.family_id == family_id)
        .where(models.LiveSession.ended_at.is_(None))
        .order_by(models.LiveSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def ensure_active_session(
    db: Session,
    family_id: int,
    *,
    start_context: Optional[str] = None,
) -> Tuple[models.LiveSession, bool]:
    """Return ``(session, created)`` for the family's active live session.

    "Live" means ``source='live'`` — we deliberately ignore email-thread
    sessions here so a long-running email conversation doesn't get
    mistaken for an active in-room interaction.

    Sweeps idle sessions first so we never accidentally reuse one that
    has expired by a few seconds. If an active session exists we return
    it unchanged (``created=False``); otherwise we create a fresh one
    stamped with ``start_context`` (``created=True``). Either way the
    session's ``last_activity_at`` is bumped to now so the idle timer
    restarts.
    """
    close_stale_sessions(db, family_id=family_id)
    existing = get_active_session(db, family_id)
    # Only reuse a session when it's a live one — email threads have
    # their own lookup path keyed on the thread id.
    if existing is not None and existing.source == "live":
        existing.last_activity_at = _now()
        return existing, False

    session = models.LiveSession(
        family_id=family_id,
        start_context=start_context,
        source="live",
    )
    db.add(session)
    db.flush()
    return session, True


def find_or_create_email_session(
    db: Session,
    *,
    family_id: int,
    external_thread_id: str,
    subject: Optional[str] = None,
) -> Tuple[models.LiveSession, bool]:
    """Return ``(session, created)`` for a Gmail thread.

    Looks up the existing email-source session for the thread first
    (so a multi-message conversation accretes into a single transcript)
    and creates a new one when none exists. Email threads do NOT
    auto-close after 30 minutes the way live sessions do — a back and
    forth that takes a week is still one session — so we skip the idle
    sweep entirely on this path.
    """
    existing = db.execute(
        select(models.LiveSession)
        .where(models.LiveSession.family_id == family_id)
        .where(models.LiveSession.source == "email")
        .where(models.LiveSession.external_thread_id == external_thread_id)
        .order_by(models.LiveSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_activity_at = _now()
        # If the user re-opens an old, ended thread, reactivate it so
        # the new reply doesn't look orphaned in the history view.
        if existing.ended_at is not None:
            existing.ended_at = None
            existing.end_reason = None
        return existing, False

    session = models.LiveSession(
        family_id=family_id,
        source="email",
        external_thread_id=external_thread_id,
        start_context=(
            f"email_thread:{subject[:80]}" if subject else "email_thread"
        ),
    )
    db.add(session)
    db.flush()
    return session, True


def find_or_create_sms_session(
    db: Session,
    *,
    family_id: int,
    counterparty_phone: str,
) -> Tuple[models.LiveSession, bool]:
    """Return ``(session, created)`` for an inbound SMS thread.

    Mirrors :func:`find_or_create_email_session` but keys on the
    counterparty's E.164 phone number rather than a Gmail thread id.
    SMS conversations are inherently one-on-one: every text from
    ``+14155551234`` lands in the same session row + transcript so
    Avi sees the whole back-and-forth on the next turn rather than
    starting from scratch.

    Like email threads, SMS sessions deliberately skip the live-page
    idle sweep — a text exchange that takes a week is still one
    session.
    """
    existing = db.execute(
        select(models.LiveSession)
        .where(models.LiveSession.family_id == family_id)
        .where(models.LiveSession.source == "sms")
        .where(models.LiveSession.external_thread_id == counterparty_phone)
        .order_by(models.LiveSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_activity_at = _now()
        if existing.ended_at is not None:
            existing.ended_at = None
            existing.end_reason = None
        return existing, False

    session = models.LiveSession(
        family_id=family_id,
        source="sms",
        external_thread_id=counterparty_phone,
        start_context=f"sms_thread:{counterparty_phone}",
    )
    db.add(session)
    db.flush()
    return session, True


def find_or_create_telegram_session(
    db: Session,
    *,
    family_id: int,
    chat_id: int,
) -> Tuple[models.LiveSession, bool]:
    """Return ``(session, created)`` for an inbound Telegram chat.

    Mirrors :func:`find_or_create_sms_session` but keys on the
    Telegram numeric chat id (stringified to share the
    ``external_thread_id`` column). For private chats the chat id
    equals the user's id, so every message from one human accretes
    into a single session row + transcript.

    Like email and SMS threads, Telegram sessions deliberately skip
    the live-page idle sweep — a back-and-forth that takes a week is
    still one session.
    """
    key = str(chat_id)
    existing = db.execute(
        select(models.LiveSession)
        .where(models.LiveSession.family_id == family_id)
        .where(models.LiveSession.source == "telegram")
        .where(models.LiveSession.external_thread_id == key)
        .order_by(models.LiveSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_activity_at = _now()
        if existing.ended_at is not None:
            existing.ended_at = None
            existing.end_reason = None
        return existing, False

    session = models.LiveSession(
        family_id=family_id,
        source="telegram",
        external_thread_id=key,
        start_context=f"telegram_thread:{key}",
    )
    db.add(session)
    db.flush()
    return session, True


def touch_session(
    db: Session, session: models.LiveSession
) -> None:
    """Bump ``last_activity_at`` to now.

    Called on every message append and participant upsert. Cheap enough
    to run unconditionally — the UPDATE only touches one row.
    """
    session.last_activity_at = _now()


def end_session(
    db: Session,
    session: models.LiveSession,
    *,
    reason: str = "manual",
) -> models.LiveSession:
    if session.ended_at is None:
        session.ended_at = _now()
        session.end_reason = reason
    return session


# ---------- participants ----------------------------------------------------


def upsert_participant(
    db: Session,
    session: models.LiveSession,
    *,
    person_id: int,
) -> models.LiveSessionParticipant:
    """Fetch the participant row for ``(session, person)``, creating it
    with ``greeted_already=False`` if missing.

    The ``uq_live_session_participant`` index makes this race-safe: two
    concurrent face-recognition ticks can both call this, and one of
    them will hit :class:`IntegrityError` and re-read the existing row.
    """
    existing = db.execute(
        select(models.LiveSessionParticipant)
        .where(models.LiveSessionParticipant.live_session_id == session.live_session_id)
        .where(models.LiveSessionParticipant.person_id == person_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = models.LiveSessionParticipant(
        live_session_id=session.live_session_id,
        person_id=person_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Another request inserted the row between our SELECT and
        # INSERT — re-read and return that one.
        existing = db.execute(
            select(models.LiveSessionParticipant)
            .where(models.LiveSessionParticipant.live_session_id == session.live_session_id)
            .where(models.LiveSessionParticipant.person_id == person_id)
        ).scalar_one()
        return existing
    touch_session(db, session)
    return row


def mark_greeted(
    db: Session,
    participant: models.LiveSessionParticipant,
) -> bool:
    """Atomically flip ``greeted_already`` from False to True.

    Returns ``True`` iff *we* were the caller that caused the flip — the
    race-losing caller gets ``False`` and should suppress its greeting.
    Using a conditional UPDATE guarantees at-most-once greeting even
    under rapid-fire recognition ticks.
    """
    result = db.execute(
        update(models.LiveSessionParticipant)
        .where(
            models.LiveSessionParticipant.live_session_participant_id
            == participant.live_session_participant_id
        )
        .where(models.LiveSessionParticipant.greeted_already.is_(False))
        .values(greeted_already=True)
    )
    flipped = (result.rowcount or 0) > 0
    if flipped:
        # Keep the in-memory object in sync so the caller doesn't have
        # to refresh just to read the new value.
        participant.greeted_already = True
    return flipped


# ---------- messages --------------------------------------------------------


def log_message(
    db: Session,
    session: models.LiveSession,
    *,
    role: str,
    content: str,
    person_id: Optional[int] = None,
    meta: Optional[dict[str, Any]] = None,
) -> models.LiveSessionMessage:
    """Append a message to the session and bump activity in one txn."""
    row = models.LiveSessionMessage(
        live_session_id=session.live_session_id,
        role=role,
        content=content,
        person_id=person_id,
        meta=meta,
    )
    db.add(row)
    touch_session(db, session)
    return row


# ---------- read helpers (shared by list + detail views) --------------------


def _display_name(p: Optional[models.Person]) -> Optional[str]:
    if p is None:
        return None
    return p.preferred_name or p.first_name or f"Person {p.person_id}"


def session_header_dict(
    db: Session,
    session: models.LiveSession,
    *,
    participants: Optional[Iterable[models.LiveSessionParticipant]] = None,
    messages: Optional[Iterable[models.LiveSessionMessage]] = None,
) -> dict[str, Any]:
    """Shape a :class:`LiveSession` into the dict that matches
    :class:`schemas.LiveSessionRead`.

    The ``participants`` / ``messages`` kwargs let callers that have
    already fetched those collections (e.g. the detail view) avoid a
    redundant database round-trip.
    """
    parts = list(participants) if participants is not None else list(session.participants)
    msgs = list(messages) if messages is not None else list(session.messages)
    preview_people = [_display_name(p.person) or f"Person {p.person_id}" for p in parts]
    last_msg = msgs[-1] if msgs else None
    return {
        "live_session_id": session.live_session_id,
        "family_id": session.family_id,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "last_activity_at": session.last_activity_at,
        "start_context": session.start_context,
        "end_reason": session.end_reason,
        "source": session.source,
        "external_thread_id": session.external_thread_id,
        "is_active": session.ended_at is None,
        "participant_count": len(parts),
        "message_count": len(msgs),
        "participants_preview": preview_people,
        "last_message_preview": (
            (last_msg.content or "").strip().splitlines()[0][:140]
            if last_msg
            else None
        ),
    }


def participant_read_dict(
    participant: models.LiveSessionParticipant,
) -> dict[str, Any]:
    return {
        "live_session_participant_id": participant.live_session_participant_id,
        "live_session_id": participant.live_session_id,
        "person_id": participant.person_id,
        "person_name": _display_name(participant.person),
        "joined_at": participant.joined_at,
        "greeted_already": participant.greeted_already,
    }


def message_read_dict(
    message: models.LiveSessionMessage,
) -> dict[str, Any]:
    return {
        "live_session_message_id": message.live_session_message_id,
        "live_session_id": message.live_session_id,
        "role": message.role,
        "person_id": message.person_id,
        "person_name": _display_name(message.person),
        "content": message.content,
        "meta": message.meta,
        "created_at": message.created_at,
    }
