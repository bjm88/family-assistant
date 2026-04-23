"""Live AI-assistant session endpoints.

All routes live under the ``/api/aiassistant`` prefix applied in
``main.py``. The router exposes:

* ``POST /sessions/ensure-active`` — used by the live page on mount to
  ensure a session exists (and to sweep stale ones). Returns the
  full header, including participants + last activity time.
* ``GET  /sessions``                — session history list.
* ``GET  /sessions/{id}``           — session detail (participants +
  full message transcript).
* ``POST /sessions/{id}/end``       — manual close from the UI.
* ``GET  /sessions/active?family_id=`` — lightweight poll endpoint the
  live page uses every few seconds to keep the participant pill fresh.

The ``/greet``, ``/followup``, and ``/chat`` routes in ``ai_chat.py``
now accept an optional ``live_session_id`` and share the same session
helpers from ``api.ai.session`` so bookkeeping stays single-sourced.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..ai import session as live_session
from ..auth import (
    CurrentUser,
    require_family_member,
    require_family_member_from_request,
    require_user,
)
from ..db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["ai_sessions"])


# ---------- helpers --------------------------------------------------------


def _require_session(
    db: Session, live_session_id: int
) -> models.LiveSession:
    session = db.get(models.LiveSession, live_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Live session not found")
    return session


# ---------- ensure / fetch -------------------------------------------------


@router.post("/ensure-active", response_model=schemas.LiveSessionRead)
def ensure_active(
    payload: schemas.EnsureActiveSessionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    require_family_member(payload.family_id, request)
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    session, created = live_session.ensure_active_session(
        db,
        payload.family_id,
        start_context=payload.start_context,
    )
    # Log a system note the first time we open a session so the transcript
    # is self-explanatory in the history view.
    if created:
        live_session.log_message(
            db,
            session,
            role="system",
            content="Session started",
            meta={"start_context": payload.start_context},
        )
    db.flush()
    db.refresh(session)
    return live_session.session_header_dict(db, session)


@router.get(
    "/active",
    response_model=Optional[schemas.LiveSessionRead],
    dependencies=[Depends(require_family_member_from_request)],
)
def active_session(
    family_id: int = Query(...),
    db: Session = Depends(get_db),
) -> Optional[dict]:
    """Lightweight poll endpoint.

    Sweeps stale sessions and returns the family's current active session
    if one exists, else ``null``. The live page polls this every few
    seconds to keep the participants pill fresh without pulling the full
    transcript down.
    """
    if db.get(models.Family, family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    live_session.close_stale_sessions(db, family_id=family_id)
    session = live_session.get_active_session(db, family_id)
    if session is None:
        return None
    return live_session.session_header_dict(db, session)


# ---------- list / detail --------------------------------------------------


@router.get("", response_model=List[schemas.LiveSessionRead])
def list_sessions(
    request: Request,
    family_id: int = Query(...),
    limit: int = Query(50, ge=1, le=200),
    include_active: bool = Query(True),
    db: Session = Depends(get_db),
) -> List[dict]:
    """History list (newest first). Closes stale sessions so the list
    reflects an accurate active/ended state.

    For non-admin members the result is automatically narrowed to
    sessions they actually participated in (any surface — live face
    rec, SMS, email, Telegram all upsert participants the same way).
    """
    user = require_family_member(family_id, request)
    if db.get(models.Family, family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    live_session.close_stale_sessions(db, family_id=family_id)

    stmt = (
        select(models.LiveSession)
        .where(models.LiveSession.family_id == family_id)
        .order_by(models.LiveSession.started_at.desc())
        .limit(limit)
    )
    if not include_active:
        stmt = stmt.where(models.LiveSession.ended_at.is_not(None))
    if not user.is_admin and user.person_id is not None:
        # Members see only sessions where they appear as a
        # participant. Use a correlated EXISTS so the LIMIT still
        # applies cleanly (a JOIN + DISTINCT would interact poorly
        # with ORDER BY started_at DESC LIMIT N when a session
        # has multiple participant rows).
        stmt = stmt.where(
            select(models.LiveSessionParticipant.live_session_participant_id)
            .where(
                models.LiveSessionParticipant.live_session_id
                == models.LiveSession.live_session_id
            )
            .where(
                models.LiveSessionParticipant.person_id == user.person_id
            )
            .exists()
        )
    rows = list(db.execute(stmt).scalars())
    return [live_session.session_header_dict(db, s) for s in rows]


@router.get("/{live_session_id}", response_model=schemas.LiveSessionDetail)
def get_session(
    live_session_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
) -> dict:
    session = _require_session(db, live_session_id)
    if not user.is_admin:
        # Wrong family or not a participant → pretend the session
        # doesn't exist (404, never 403) so members can't probe for
        # IDs that belong to other families or sessions they weren't
        # in.
        if user.family_id != session.family_id:
            raise HTTPException(status_code=404, detail="Live session not found")
        if user.person_id is None:
            raise HTTPException(status_code=404, detail="Live session not found")
        is_participant = (
            db.query(models.LiveSessionParticipant)
            .filter(
                models.LiveSessionParticipant.live_session_id
                == session.live_session_id,
                models.LiveSessionParticipant.person_id == user.person_id,
            )
            .first()
            is not None
        )
        if not is_participant:
            raise HTTPException(status_code=404, detail="Live session not found")
    participants = list(session.participants)
    messages = list(session.messages)
    header = live_session.session_header_dict(
        db, session, participants=participants, messages=messages
    )
    header["participants"] = [
        live_session.participant_read_dict(p) for p in participants
    ]
    header["messages"] = [
        live_session.message_read_dict(m) for m in messages
    ]
    return header


# ---------- manual close ---------------------------------------------------


@router.post("/{live_session_id}/end", response_model=schemas.LiveSessionRead)
def end(
    live_session_id: int,
    payload: schemas.EndSessionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    session = _require_session(db, live_session_id)
    require_family_member(session.family_id, request)
    live_session.end_session(db, session, reason=payload.end_reason)
    db.flush()
    db.refresh(session)
    return live_session.session_header_dict(db, session)
