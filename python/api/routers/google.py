"""Google OAuth + Gmail/Calendar admin endpoints.

Lives at ``/api/admin/google/*``. Drives:

* ``GET  /oauth/start``         — kick the browser into Google's consent flow.
* ``GET  /oauth/callback``      — exchange the auth code, persist tokens,
                                  redirect the browser back to the React UI.
* ``GET  /status``              — quick "is Avi connected?" probe for the UI.
* ``DELETE /credentials``        — revoke + forget a connection.
* ``POST /test/send-email``     — smoke-test the Gmail scope.
* ``GET  /test/upcoming-events`` — smoke-test the Calendar scope.

The OAuth flow uses Google's standard "loopback" pattern (the redirect
URI is ``http://localhost:8000/...``). No public domain is required;
just register that exact URI in your Google Cloud OAuth client.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ..config import get_settings
from ..db import get_db
from ..integrations import google_oauth as oauth_lib
from ..integrations.gmail import GmailSendError, send_email
from ..integrations.google_calendar import (
    CalendarError,
    list_upcoming_events,
    list_visible_calendars,
)


router = APIRouter(prefix="/google", tags=["google"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status / disconnect
# ---------------------------------------------------------------------------


class GoogleStatus(BaseModel):
    connected: bool
    granted_email: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
    token_expires_at: Optional[str] = None
    can_send_email: bool = False
    # Implies inbox read + mark-as-read; the email-inbox poller refuses
    # to start unless this is True for the assistant.
    can_read_inbox: bool = False
    # True iff the OAuth token carries any scope that lets us list
    # calendars + read events. Per-calendar permissions are determined
    # by how each owner shares (see /test/calendars).
    can_read_calendar: bool = False
    # True iff the token can also INSERT events. Set by the
    # ``calendar.events`` and ``calendar`` scopes; legacy
    # ``calendar.readonly`` connections show false here and the
    # ``calendar_create_event`` agent tool refuses to fire.
    can_write_calendar: bool = False
    # True iff the token authorises ``calendarList.list()`` —
    # required for the per-calendar visibility panel. Connects made
    # before this scope was added to DEFAULT_SCOPES will return false
    # and need a disconnect + reconnect.
    can_list_calendars: bool = False
    email_matches_assistant: Optional[bool] = None
    oauth_configured: bool = False


def _status_for(
    row: Optional[models.GoogleOAuthCredential],
    assistant: Optional[models.Assistant],
) -> GoogleStatus:
    s = get_settings()
    oauth_configured = bool(
        s.GOOGLE_OAUTH_CLIENT_ID and s.GOOGLE_OAUTH_CLIENT_SECRET
    )
    if row is None:
        return GoogleStatus(connected=False, oauth_configured=oauth_configured)
    scopes = [s for s in (row.scopes or "").split() if s]
    matches: Optional[bool] = None
    if assistant and assistant.email_address:
        matches = (
            (row.granted_email or "").lower()
            == assistant.email_address.lower()
        )
    return GoogleStatus(
        connected=True,
        granted_email=row.granted_email,
        scopes=scopes,
        token_expires_at=(
            row.token_expires_at.isoformat() if row.token_expires_at else None
        ),
        # gmail.modify is a superset that includes send + read, so we
        # treat either as evidence Avi can send. The inbox poller needs
        # the modify scope specifically.
        can_send_email=any(
            s.endswith("/gmail.send") or s.endswith("/gmail.modify")
            for s in scopes
        ),
        can_read_inbox=any(
            s.endswith("/gmail.modify") or s.endswith("/gmail.readonly")
            for s in scopes
        ),
        # Any of the three calendar scopes grants list + read.
        # ``calendar.events`` is what we request on new connects;
        # ``calendar.readonly`` is the legacy read-only scope; the
        # umbrella ``calendar`` scope (granted to some legacy hookups)
        # also covers reads. Without one of these the calendar tools
        # cannot list events at all.
        can_read_calendar=any(
            s.endswith("/calendar.readonly")
            or s.endswith("/calendar.events")
            or s.endswith("/calendar")
            for s in scopes
        ),
    # Inserts (and edits) require either ``calendar.events`` or the
    # umbrella ``calendar`` scope. The read-only scope is excluded
    # so the UI can correctly nudge legacy connections to reconnect.
    can_write_calendar=any(
        s.endswith("/calendar.events") or s.endswith("/calendar")
        for s in scopes
    ),
    # Required by ``calendarList.list()`` — i.e. the "show me every
    # calendar Avi can see" UI panel. Independent of read/write
    # event scopes because Google chose to gate calendar enumeration
    # behind its own scope. Folded into ``calendar.readonly`` and
    # the umbrella ``calendar`` scope on legacy connections.
    can_list_calendars=any(
        s.endswith("/calendar.calendarlist.readonly")
        or s.endswith("/calendar.calendarlist")
        or s.endswith("/calendar.readonly")
        or s.endswith("/calendar")
        for s in scopes
    ),
        email_matches_assistant=matches,
        oauth_configured=oauth_configured,
    )


@router.get(
    "/status",
    response_model=GoogleStatus,
    dependencies=[Depends(require_admin)],
)
def google_status(
    assistant_id: int = Query(...),
    db: Session = Depends(get_db),
) -> GoogleStatus:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    row = oauth_lib.load_credentials_row(db, assistant_id)
    return _status_for(row, assistant)


@router.delete(
    "/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def disconnect(
    assistant_id: int = Query(...),
    db: Session = Depends(get_db),
) -> None:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    oauth_lib.delete_credentials(db, assistant_id)


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


@router.get("/oauth/start", dependencies=[Depends(require_admin)])
def oauth_start(
    assistant_id: int = Query(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Redirect the browser to Google's consent screen."""
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")

    try:
        flow = oauth_lib.build_flow()
    except oauth_lib.GoogleOAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Mint our own state up front and pass it to authorization_url so we
    # can map the callback back to the right assistant. After the call
    # ``flow.code_verifier`` holds the PKCE secret oauthlib generated to
    # build the ``code_challenge`` Google saw — we MUST replay it in
    # /oauth/callback or the token exchange fails with
    # ``invalid_grant: Missing code verifier``.
    state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(
        access_type="offline",  # required to get a refresh_token
        include_granted_scopes="true",
        prompt="consent",  # force the consent screen so we always get a refresh
        state=state,
        login_hint=assistant.email_address or None,
    )
    oauth_lib.stash_pending(
        assistant_id, code_verifier=flow.code_verifier, state=state
    )
    return RedirectResponse(auth_url, status_code=302)


# NOTE: this endpoint is intentionally NOT admin-gated. Google's
# servers hit it directly with the auth code, so cookie-based auth
# can't apply. The CSRF defence is the unguessable ``state`` token
# we minted in /oauth/start and stashed via ``oauth_lib.consume_pending``.
@router.get("/oauth/callback")
def oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Exchange the auth code for tokens and persist them."""
    if error:
        return _redirect_to_ui(error=error)
    if not code or not state:
        return _redirect_to_ui(error="missing_code_or_state")

    try:
        assistant_id, code_verifier = oauth_lib.consume_pending(state)
    except oauth_lib.GoogleOAuthError as exc:
        return _redirect_to_ui(error=str(exc))

    assistant = db.get(models.Assistant, assistant_id)
    family_id = assistant.family_id if assistant else None

    try:
        flow = oauth_lib.build_flow()
        # Replay the PKCE verifier we stashed in /oauth/start so Google
        # accepts the auth-code exchange.
        if code_verifier:
            flow.code_verifier = code_verifier
        flow.fetch_token(authorization_response=str(request.url))
    except oauth_lib.GoogleOAuthUnavailable as exc:
        return _redirect_to_ui(error=str(exc), family_id=family_id)
    except Exception as exc:  # noqa: BLE001 - oauthlib raises many kinds
        logger.exception("OAuth token exchange failed")
        return _redirect_to_ui(
            error=f"token_exchange_failed: {exc}", family_id=family_id
        )

    creds = flow.credentials
    granted_email = oauth_lib.email_from_id_token(creds) or ""
    if not granted_email:
        return _redirect_to_ui(
            error="Google did not return an email address; check the openid+email scopes.",
            family_id=family_id,
        )

    oauth_lib.save_credentials(
        db,
        assistant_id=assistant_id,
        granted_email=granted_email,
        creds=creds,
    )
    return _redirect_to_ui(success=granted_email, family_id=family_id)


def _redirect_to_ui(
    *,
    success: Optional[str] = None,
    error: Optional[str] = None,
    family_id: Optional[int] = None,
) -> RedirectResponse:
    """Bounce the browser back to the React Assistant page.

    ``GOOGLE_OAUTH_POST_LOGIN_REDIRECT`` provides the origin (and an
    optional fallback path); when we know the assistant's family_id we
    deep-link to ``/admin/families/<family_id>/assistant`` so the
    Assistant page mounts and its useEffect can pop a toast confirming
    the connection. Otherwise we fall back to the configured base URL.
    """
    from urllib.parse import quote, urlparse

    base = get_settings().GOOGLE_OAUTH_POST_LOGIN_REDIRECT
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    target_path = (
        f"/admin/families/{family_id}/assistant"
        if family_id is not None
        else (parsed.path or "/admin/families")
    )

    parts: list[str] = []
    if success:
        parts.append(f"google_connected={quote(success)}")
    if error:
        parts.append(f"google_error={quote(error)}")
    qs = "&".join(parts)
    target = f"{origin}{target_path}" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=302)


# ---------------------------------------------------------------------------
# Smoke-test endpoints — useful from the UI to verify the connection.
# These also serve as the reference call sites for the LLM tool layer
# we'll add next.
# ---------------------------------------------------------------------------


class TestEmailRequest(BaseModel):
    assistant_id: int
    to: EmailStr
    subject: str = "Hello from Avi"
    body: str = (
        "Hi! This is a test message sent from your Family Assistant via "
        "the freshly-connected Google account. If you're reading it, the "
        "Gmail scope is working correctly."
    )


class TestEmailResponse(BaseModel):
    message_id: str
    granted_email: str


@router.post(
    "/test/send-email",
    response_model=TestEmailResponse,
    dependencies=[Depends(require_admin)],
)
def test_send_email(
    payload: TestEmailRequest, db: Session = Depends(get_db)
) -> TestEmailResponse:
    assistant = db.get(models.Assistant, payload.assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    try:
        row, creds = oauth_lib.load_credentials(db, payload.assistant_id)
    except oauth_lib.GoogleNotConnected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except oauth_lib.GoogleOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        msg_id = send_email(
            creds, to=payload.to, subject=payload.subject, body=payload.body
        )
    except GmailSendError as exc:
        logger.warning(
            "Gmail test send failed for assistant_id=%s to=%s: %s",
            payload.assistant_id,
            payload.to,
            exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return TestEmailResponse(message_id=msg_id, granted_email=row.granted_email)


class UpcomingEvent(BaseModel):
    event_id: str
    calendar_id: str
    summary: str
    start: str
    end: str
    location: Optional[str] = None
    organizer_email: Optional[str] = None


class UpcomingEventsResponse(BaseModel):
    granted_email: str
    events: list[UpcomingEvent]


@router.get(
    "/test/upcoming-events",
    response_model=UpcomingEventsResponse,
    dependencies=[Depends(require_admin)],
)
def test_upcoming_events(
    assistant_id: int = Query(...),
    hours: int = Query(72, ge=1, le=720),
    max_results: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> UpcomingEventsResponse:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    try:
        row, creds = oauth_lib.load_credentials(db, assistant_id)
    except oauth_lib.GoogleNotConnected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except oauth_lib.GoogleOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        events = list_upcoming_events(
            creds, hours_ahead=hours, max_results=max_results
        )
    except CalendarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return UpcomingEventsResponse(
        granted_email=row.granted_email,
        events=[UpcomingEvent(**e.__dict__) for e in events],
    )


# ---------------------------------------------------------------------------
# Per-calendar visibility — the Assistant settings page replaces its single
# "Can read calendar" yes/no with the actual list of visible calendars and
# the share level Google grants for each. Lets an admin diagnose the
# "agent logs say 403 but the reply still went out" symptom in one glance:
# any calendar with accessRole == 'freeBusyReader' will produce a 403
# every time the agent tries to enumerate its events (events.list refuses;
# only freebusy.query works for that share level).
# ---------------------------------------------------------------------------


class VisibleCalendarOut(BaseModel):
    calendar_id: str
    summary: str
    summary_override: Optional[str] = None
    description: Optional[str] = None
    primary: bool
    selected: bool
    access_role: str  # owner|writer|reader|freeBusyReader|none|unknown
    background_color: Optional[str] = None
    foreground_color: Optional[str] = None
    time_zone: Optional[str] = None
    can_read_events: bool
    can_write: bool


class VisibleCalendarsResponse(BaseModel):
    granted_email: str
    calendars: list[VisibleCalendarOut]


def _classify_role(role: str) -> tuple[bool, bool]:
    """Return ``(can_read_events, can_write)`` from a Google accessRole.

    ``freeBusyReader`` and ``none`` cannot read individual events
    (events.list returns 403). Only ``owner``/``writer`` can insert.
    """
    can_write = role in ("owner", "writer")
    can_read_events = role in ("owner", "writer", "reader")
    return can_read_events, can_write


@router.get(
    "/test/calendars",
    response_model=VisibleCalendarsResponse,
    dependencies=[Depends(require_admin)],
)
def test_visible_calendars(
    assistant_id: int = Query(...),
    db: Session = Depends(get_db),
) -> VisibleCalendarsResponse:
    """List every calendar Avi can see + its share level.

    Used by the Assistant settings page to render a per-calendar
    permissions table instead of a single yes/no badge.
    """
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    try:
        row, creds = oauth_lib.load_credentials(db, assistant_id)
    except oauth_lib.GoogleNotConnected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except oauth_lib.GoogleOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        cals = list_visible_calendars(creds)
    except CalendarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    out: list[VisibleCalendarOut] = []
    for c in cals:
        can_read, can_write = _classify_role(c.access_role)
        out.append(
            VisibleCalendarOut(
                calendar_id=c.calendar_id,
                summary=c.summary,
                summary_override=c.summary_override,
                description=c.description,
                primary=c.primary,
                selected=c.selected,
                access_role=c.access_role,
                background_color=c.background_color,
                foreground_color=c.foreground_color,
                time_zone=c.time_zone,
                can_read_events=can_read,
                can_write=can_write,
            )
        )
    return VisibleCalendarsResponse(
        granted_email=row.granted_email, calendars=out
    )
