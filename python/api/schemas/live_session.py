"""Pydantic schemas for live AI-assistant sessions.

A session has three associated entities the API exposes:

* :class:`LiveSessionRead`  — lightweight header for the history list
  (also used on the live page's status pill).
* :class:`LiveSessionParticipantRead` — who the camera recognised,
  with the ``greeted_already`` flag.
* :class:`LiveSessionMessageRead` — one transcript line.
* :class:`LiveSessionDetail` — the session header + full participant
  roster + full message log, for the drill-in history view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


LiveSessionMessageRole = Literal["user", "assistant", "system"]
LiveSessionEndReason = Literal["timeout", "manual", "superseded"]
LiveSessionSource = Literal["live", "email"]


# ---- nested components -----------------------------------------------------


class LiveSessionParticipantRead(OrmModel):
    live_session_participant_id: int
    live_session_id: int
    person_id: int
    person_name: Optional[str] = None
    joined_at: datetime
    greeted_already: bool


class LiveSessionMessageRead(OrmModel):
    live_session_message_id: int
    live_session_id: int
    role: LiveSessionMessageRole
    person_id: Optional[int] = None
    person_name: Optional[str] = None
    content: str
    meta: Optional[dict[str, Any]] = None
    created_at: datetime


# ---- headers & detail ------------------------------------------------------


class LiveSessionRead(OrmModel):
    live_session_id: int
    family_id: int
    started_at: datetime
    ended_at: Optional[datetime]
    last_activity_at: datetime
    start_context: Optional[str]
    end_reason: Optional[LiveSessionEndReason]
    source: LiveSessionSource = "live"
    # Gmail thread id for source='email' sessions; NULL for 'live'.
    external_thread_id: Optional[str] = None
    is_active: bool
    participant_count: int
    message_count: int
    # Tiny preview so the history list can show "John: hey avi!" per row
    # without having to load the full transcript.
    participants_preview: List[str] = Field(default_factory=list)
    last_message_preview: Optional[str] = None


class LiveSessionDetail(LiveSessionRead):
    participants: List[LiveSessionParticipantRead] = Field(default_factory=list)
    messages: List[LiveSessionMessageRead] = Field(default_factory=list)


# ---- request payloads ------------------------------------------------------


class EnsureActiveSessionRequest(BaseModel):
    family_id: int
    # Caller-supplied tag used when we have to create a fresh session.
    # Ignored if there's already an active session — we keep the
    # original context so the history view stays truthful.
    start_context: Optional[str] = None


class EndSessionRequest(BaseModel):
    end_reason: LiveSessionEndReason = "manual"
