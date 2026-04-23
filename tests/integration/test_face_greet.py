"""Regression tests for the live-page face-recognition → greet flow.

The live AI-assistant page identifies family members by camera and
asks the backend to deliver a short spoken/text greeting via
``POST /api/aiassistant/greet``. Two related bugs have shipped here
in the past:

1. **"silent forever" suppression** — the suppression check used to
   trigger on ANY chat history in the session, so a single chat
   message would silently kill every face-rec greeting for the rest
   of the 30-min idle window. (The user reported this as "live page
   stopped doing face rec and greetings *again*".)

2. **double-greet within session** — without the per-participant
   ``greeted_already`` CAS, a re-acquired MediaPipe track would
   greet the same person twice within seconds.

This module locks the desired behaviour in with end-to-end POSTs
through the in-process FastAPI ``TestClient``. We deliberately do
NOT exercise InsightFace or MediaPipe — those are tested elsewhere
and require optional native deps. The greet endpoint is the actual
user-visible regression surface, so we drive it directly.

Each test goes through the real ``/sessions/ensure-active`` →
``/greet`` → DB-introspection path. We end the session at the end
of every test (``/sessions/{id}/end``) so the next test gets a
fresh session — without that, sessions accumulate and the
"already greeted" assertion would still pass for the wrong reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api import models
from api.ai import session as live_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_session(client, family_id: int) -> int:
    """POST /sessions/ensure-active and return the live_session_id."""
    resp = client.post(
        "/api/aiassistant/sessions/ensure-active",
        json={"family_id": family_id, "start_context": "test_face_greet"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_active"] is True
    return body["live_session_id"]


def _greet(
    client, family_id: int, person_id: int, live_session_id: int | None
) -> dict:
    """POST /greet and return the JSON body."""
    resp = client.post(
        "/api/aiassistant/greet",
        json={
            "family_id": family_id,
            "person_id": person_id,
            "live_session_id": live_session_id,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _end_session(client, live_session_id: int) -> None:
    """POST /sessions/{id}/end so the next test starts fresh."""
    resp = client.post(
        f"/api/aiassistant/sessions/{live_session_id}/end",
        json={"end_reason": "manual"},
    )
    # 200 on success; 404 is fine if the test already ended it.
    assert resp.status_code in (200, 404), resp.text


@pytest.fixture
def fresh_session(client, test_family):
    """Yield a freshly-created live session id and tear it down at the end.

    Other tests in the suite leave sessions lying around (they're
    "find-or-create" per-family per the harness conventions); this
    fixture explicitly closes the session so each face-greet test
    runs against a brand-new session row.
    """
    # Close any pre-existing live session for this family so
    # ensure-active gives us a brand-new one.
    from api.db import SessionLocal

    s = SessionLocal()
    try:
        existing = live_session.get_active_session(
            s, test_family["family_id"]
        )
        if existing is not None:
            live_session.end_session(s, existing, reason="test_setup")
            s.commit()
    finally:
        s.close()

    sid = _ensure_session(client, test_family["family_id"])
    yield sid
    _end_session(client, sid)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_greet_in_fresh_session_returns_template_greeting(
    client, test_family, fresh_session, db
):
    """Fresh session + first face-rec → returns the canonical greeting.

    This is the happy path the user sees after opening the live page
    for the first time of the day. Asserts:

    * ``skipped`` is False (no suppression),
    * ``greeting`` contains the person's first name,
    * ``used_model`` is the template (no LLM call),
    * a ``LiveSessionMessage`` row was logged with
      ``meta.kind == 'greeting'`` so the transcript view shows it,
    * the participant row was created with ``greeted_already=True``.
    """
    body = _greet(
        client,
        test_family["family_id"],
        test_family["person_id"],
        fresh_session,
    )

    assert body["skipped"] is False
    assert body["skipped_reason"] is None
    assert body["used_model"] == "template"
    # Person's first_name is "IntegrationTest" — see TEST_PERSON in
    # conftest.py. Template greeting is "Hi <first_name>, how can I
    # help you?".
    assert "IntegrationTest" in body["greeting"]
    assert "how can i help" in body["greeting"].lower()

    # Transcript row was logged with meta.kind="greeting".
    msg = db.execute(
        select(models.LiveSessionMessage)
        .where(models.LiveSessionMessage.live_session_id == fresh_session)
        .where(models.LiveSessionMessage.role == "assistant")
        .order_by(models.LiveSessionMessage.created_at.desc())
        .limit(1)
    ).scalar_one()
    assert msg.meta is not None
    assert msg.meta.get("kind") == "greeting"

    # Participant row exists and is marked greeted.
    part = db.execute(
        select(models.LiveSessionParticipant)
        .where(models.LiveSessionParticipant.live_session_id == fresh_session)
        .where(
            models.LiveSessionParticipant.person_id == test_family["person_id"]
        )
    ).scalar_one()
    assert part.greeted_already is True


def test_second_greet_for_same_person_in_session_is_silent(
    client, test_family, fresh_session
):
    """Re-detection of the same person in the same session → silent.

    The 90-second client-side suppression catches most re-detects, but
    if the page is reloaded inside that window the server's
    ``greeted_already`` CAS is the second line of defence. Here we
    skip the client suppression entirely (we just call /greet twice
    back-to-back) to prove the server enforces "at most one greeting
    per (session, person)" on its own.
    """
    first = _greet(
        client,
        test_family["family_id"],
        test_family["person_id"],
        fresh_session,
    )
    assert first["skipped"] is False

    second = _greet(
        client,
        test_family["family_id"],
        test_family["person_id"],
        fresh_session,
    )
    assert second["skipped"] is True
    assert second["skipped_reason"] == "already_greeted_in_session"
    assert second["greeting"] == ""


def test_greet_after_recent_chat_is_suppressed(
    client, test_family, fresh_session, db
):
    """Recent chat in the session → camera re-detect should NOT interrupt.

    Models the case where the user typed something to Avi a couple
    of seconds ago and then leaned back into the camera. A sudden
    "Hi <name>!" mid-typing is jarring, so the greet endpoint
    suppresses with ``skipped_reason='session_already_active'``.
    """
    # Insert a fresh "user typed in chat" row.
    sess = db.get(models.LiveSession, fresh_session)
    live_session.log_message(
        db,
        sess,
        role="user",
        content="hey avi",
        person_id=test_family["person_id"],
        meta={"kind": "chat"},
    )
    db.commit()

    body = _greet(
        client,
        test_family["family_id"],
        test_family["person_id"],
        fresh_session,
    )
    assert body["skipped"] is True
    assert body["skipped_reason"] == "session_already_active"
    assert body["greeting"] == ""


def test_greet_after_old_chat_does_not_skip(
    client, test_family, fresh_session, db
):
    """The headline regression test for the 2026-04-20 bug.

    A long-lived live session that had any chat at all was returning
    ``skipped=True`` for every subsequent face-rec greeting until the
    session timed out 30 min later — silently breaking the live page
    every time the user typed once and then walked away.

    Fix: only suppress when the most recent chat is within the
    ``AI_GREET_SUPPRESS_RECENT_CHAT_SECONDS`` window (default 120s).
    Here we backdate a chat row well outside the window and assert
    the greeting fires normally.
    """
    sess = db.get(models.LiveSession, fresh_session)
    msg = live_session.log_message(
        db,
        sess,
        role="user",
        content="hey avi (old)",
        person_id=test_family["person_id"],
        meta={"kind": "chat"},
    )
    db.flush()
    # Backdate well past the 120s window. We can't rely on the test
    # process sleeping that long, so we update created_at directly.
    msg.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.commit()

    body = _greet(
        client,
        test_family["family_id"],
        test_family["person_id"],
        fresh_session,
    )
    assert body["skipped"] is False, (
        "Old chat history should not suppress a new greeting — that "
        "regression is what test_face_greet exists to catch."
    )
    assert "IntegrationTest" in body["greeting"]


def test_greet_without_live_session_id_uses_template_path(
    client, test_family
):
    """``live_session_id=None`` short-circuits to the pre-session
    template path: returns a greeting, never logs anything. This path
    is what the AssistantPage falls back to when ``useLiveSession``
    fails to acquire an id (transient backend hiccup).
    """
    body = _greet(
        client,
        test_family["family_id"],
        test_family["person_id"],
        live_session_id=None,
    )
    assert body["skipped"] is False
    assert body["skipped_reason"] is None
    assert body["used_model"] == "template"
    assert "IntegrationTest" in body["greeting"]


def test_greet_unknown_person_returns_404(client, test_family, fresh_session):
    """An unknown / cross-family person id → 404 (not a 500). The live
    page maps 404s to a console.debug; anything else surfaces as a red
    error bubble in chat. Lock the contract.
    """
    resp = client.post(
        "/api/aiassistant/greet",
        json={
            "family_id": test_family["family_id"],
            "person_id": 9_999_999,
            "live_session_id": fresh_session,
        },
    )
    assert resp.status_code == 404


def test_active_session_ignores_non_live_thread_sessions(
    client, test_family, db
):
    """``/sessions/active`` must not return inbound thread sessions.

    Inbound surfaces (email, sms, whatsapp, telegram) all persist into
    the same ``live_sessions`` table so transcripts share one schema.
    Before this fix, ``/sessions/active`` would return *any* active
    session for the family — and the live page ``LiveSessionRead``
    schema would 500 trying to serialize ``source='whatsapp'``
    against a too-narrow Literal. Two-part regression:

    1. ``get_active_session`` must filter on ``source='live'``,
       skipping the inbound thread sessions completely.
    2. The ``LiveSessionSource`` Literal must list every value that
       ``find_or_create_*_session`` can write, so any non-live row
       that *does* slip through serializes cleanly instead of 500ing.

    Reproducer: seed a fresh whatsapp + telegram thread for the
    family, ensure the live-page session is closed, then GET
    ``/sessions/active``. Expect ``200`` + ``null`` body — not 500.
    """
    # Make sure no live session exists, so the only active rows for
    # this family are the inbound threads we're about to create.
    existing_live = live_session.get_active_session(
        db, test_family["family_id"]
    )
    if existing_live is not None:
        live_session.end_session(db, existing_live, reason="test_setup")
        db.commit()

    # Seed a whatsapp + telegram thread session via the same helpers
    # the inbox surfaces use. Their ``source`` values are exactly
    # what would have crashed the schema before.
    wa, _ = live_session.find_or_create_whatsapp_session(
        db,
        family_id=test_family["family_id"],
        counterparty_phone="+15555550100",
    )
    tg, _ = live_session.find_or_create_telegram_session(
        db,
        family_id=test_family["family_id"],
        chat_id=424242,
    )
    db.commit()

    try:
        resp = client.get(
            f"/api/aiassistant/sessions/active?family_id={test_family['family_id']}"
        )
        assert resp.status_code == 200, resp.text
        # No active LIVE session → endpoint returns JSON null. The
        # whatsapp + telegram threads are present in the table but
        # must be invisible here.
        assert resp.json() is None
    finally:
        # Clean up so we don't leave dangling thread sessions for
        # the next test run.
        live_session.end_session(db, wa, reason="test_setup")
        live_session.end_session(db, tg, reason="test_setup")
        db.commit()


def test_live_session_source_literal_covers_all_creator_helpers():
    """Schema/codepath invariant: every ``source`` value any helper in
    ``api.ai.session`` writes must be listed in ``LiveSessionSource``.

    Without this guard, adding a new inbound surface ("rcs",
    "imessage", etc.) and forgetting to widen the Literal would only
    fail in production the first time a live-page session lookup
    encountered a non-live row — exactly the bug this test was added
    for.
    """
    from typing import get_args

    from api.schemas.live_session import LiveSessionSource

    # Sources the helpers in api/ai/session.py actually emit.
    expected = {"live", "email", "sms", "whatsapp", "telegram"}
    declared = set(get_args(LiveSessionSource))
    missing = expected - declared
    assert not missing, (
        f"LiveSessionSource Literal is missing {missing}; widen the "
        "schema or remove the helper that emits it."
    )
