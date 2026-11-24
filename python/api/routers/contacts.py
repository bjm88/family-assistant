"""Per-person contacts connections — ``/api/contacts/*``.

Each family member links their own address book so Avi can look people
up and save contacts for them:

* ``GET    /status``                  — providers + this person's connections.
* ``GET    /google/oauth/start``      — send the browser to Google's consent screen.
* ``GET    /google/oauth/callback``   — store the grant, return to the UI.
* ``DELETE /connection``              — disconnect (and revoke at Google).
* ``POST   /test``                    — prove the connection works.

Who may act: an admin for anyone, a family member for themselves only.
Connecting someone's address book is that person's decision, so one
member can't do it for another.

Not under ``/api/admin`` because the connect link is also sent to
family members in chat, and they are not admins.
"""

from __future__ import annotations

import html
import logging
from typing import List, Optional
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..auth import CurrentUser, get_current_user, require_user
from ..config import get_settings
from ..db import get_db
from ..integrations import google_oauth
from ..integrations.contacts import connections, messages
from ..integrations.contacts.base import (
    ContactsAuthExpired,
    ContactsError,
    ContactsNotConnected,
    ContactsNotSupported,
)
from ..integrations.contacts.google import CONTACTS_SCOPE


router = APIRouter(prefix="/contacts", tags=["contacts"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


def _authorise(user: CurrentUser, db: Session, person_id: int) -> models.Person:
    """Return the person if ``user`` may manage their contacts connection."""
    person = db.get(models.Person, person_id)
    if not user.is_admin and user.person_id != person_id:
        # Same answer whether or not the person exists.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage your own contacts connection.",
        )
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def _safe_return_to(raw: Optional[str]) -> Optional[str]:
    """Accept only a same-site path, so the callback can't be used to bounce
    a freshly-authorised browser to another origin."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "://" in raw or "\\" in raw or any(ord(ch) < 32 for ch in raw):
        return None
    return raw


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}


def _origin(url: Optional[str]) -> Optional[str]:
    parsed = urlparse(url or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".lower()
    return None


def _return_url(request: Request, return_to: Optional[str]) -> Optional[str]:
    """Where to send the browser once Google is done.

    Normally a plain path, which keeps the browser on the site the
    callback was served from. The page that started the flow can be on
    a different origin, though — the Vite dev server on one port, the
    API on another — so when the click came from this app's own public
    address or from this machine, go back to exactly there. Any other
    origin is ignored.
    """
    path = _safe_return_to(return_to)
    if path is None:
        return None
    came_from = _origin(request.headers.get("referer"))
    if came_from is None:
        return path
    is_this_site = came_from == _origin(get_settings().PUBLIC_BASE_URL)
    is_this_machine = urlparse(came_from).hostname in _LOOPBACK_HOSTS
    return f"{came_from}{path}" if is_this_site or is_this_machine else path


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class ProviderStatus(BaseModel):
    provider: str
    label: str
    # False for placeholder providers (Apple) — shown as "coming soon".
    available: bool
    is_default: bool
    connected: bool = False
    account_identifier: Optional[str] = None
    # The grant stopped working; the person has to connect again.
    needs_reconnect: bool = False
    # False when the account on file isn't the person's own email — a
    # hint that the wrong Google account may have been picked.
    account_matches_person: Optional[bool] = None
    connected_at: Optional[str] = None


class ContactsStatus(BaseModel):
    person_id: int
    # Whether the server has the Google OAuth client configured at all.
    google_oauth_configured: bool
    # The redirect URI that must be registered with Google.
    google_redirect_uri: str
    permission_summary: str
    google_consent_note: str
    providers: List[ProviderStatus] = Field(default_factory=list)


def _status(db: Session, person: models.Person) -> ContactsStatus:
    rows = {r.provider: r for r in connections.list_connections(db, person.person_id)}
    default = connections.default_provider()
    providers: List[ProviderStatus] = []
    for key, cls in connections.PROVIDERS.items():
        entry = ProviderStatus(
            provider=key, label=cls.label, available=cls.available, is_default=key == default
        )
        row = rows.get(key)
        if row is not None:
            entry.connected = True
            entry.account_identifier = row.account_identifier
            entry.needs_reconnect = bool(row.needs_reconnect) or (
                key == "google" and not connections.has_contacts_scope(row)
            )
            if person.email_address:
                entry.account_matches_person = (
                    row.account_identifier.lower() == person.email_address.lower()
                )
            entry.connected_at = row.updated_at.isoformat() if row.updated_at else None
        providers.append(entry)
    return ContactsStatus(
        person_id=person.person_id,
        google_oauth_configured=connections.google_oauth_configured(),
        google_redirect_uri=connections.google_redirect_uri(),
        permission_summary=messages.PERMISSION_SUMMARY,
        google_consent_note=messages.GOOGLE_CONSENT_NOTE,
        providers=providers,
    )


@router.get("/status", response_model=ContactsStatus)
def contacts_status(
    person_id: int = Query(...),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ContactsStatus:
    return _status(db, _authorise(user, db, person_id))


# ---------------------------------------------------------------------------
# Google consent flow
# ---------------------------------------------------------------------------


@router.get("/google/oauth/start")
def google_oauth_start(
    request: Request,
    person_id: int = Query(...),
    return_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Send the browser to Google to grant access to this person's contacts."""
    user = get_current_user(request)
    if user is None:
        # Reached from a link Avi sent in chat: sign in, then come back.
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?{urlencode({'next': here})}", status_code=302)
    person = _authorise(user, db, person_id)

    try:
        flow = connections.build_google_flow()
    except google_oauth.GoogleOAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # No include_granted_scopes: this token must carry contacts access
    # only, even if the same Google account granted this client more
    # elsewhere (see integrations.contacts.connections).
    state = connections.new_state()
    auth_url, _ = flow.authorization_url(
        access_type="offline",  # required to get a refresh token
        prompt="consent",  # always show the screen so a refresh token is issued
        state=state,
        login_hint=person.email_address or None,
    )
    connections.stash_pending(
        state,
        person_id=person_id,
        code_verifier=flow.code_verifier,
        return_to=_return_url(request, return_to),
    )
    return RedirectResponse(auth_url, status_code=302)


# Public on purpose: the browser arrives here from Google, and the
# unguessable single-use ``state`` minted in /start is what ties it to
# the signed-in person who began the flow.
@router.get("/google/oauth/callback")
def google_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Response:
    if not state:
        return _finish(error="Google didn't return to a connection Avi started.")
    try:
        pending = connections.consume_pending(state)
    except ContactsError as exc:
        return _finish(error=str(exc))
    return_to = pending.return_to

    if error or not code:
        declined = error == "access_denied"
        return _finish(
            error=(
                "Access wasn't granted, so nothing was connected."
                if declined
                else f"Google reported a problem: {error or 'no authorisation code'}."
            ),
            return_to=return_to,
        )

    try:
        flow = connections.build_google_flow()
        if pending.code_verifier:
            flow.code_verifier = pending.code_verifier
        flow.fetch_token(code=code)
    except google_oauth.GoogleOAuthUnavailable as exc:
        return _finish(error=str(exc), return_to=return_to)
    except Exception:  # noqa: BLE001 - oauthlib raises many kinds
        logger.exception("Contacts OAuth token exchange failed")
        return _finish(
            error="Google wouldn't complete the connection. Please try again.",
            return_to=return_to,
        )

    scopes = connections.granted_scopes(flow)
    if CONTACTS_SCOPE not in scopes:
        return _finish(error=messages.SCOPE_NOT_GRANTED, return_to=return_to)

    creds = flow.credentials
    if not creds.refresh_token:
        return _finish(
            error=(
                "Google didn't issue long-lived access. Remove Avi under "
                "myaccount.google.com → Security → Third-party access, then "
                "connect again."
            ),
            return_to=return_to,
        )
    account = google_oauth.email_from_id_token(creds)
    if not account:
        return _finish(
            error="Google didn't say which account was connected. Please try again.",
            return_to=return_to,
        )
    if db.get(models.Person, pending.person_id) is None:
        return _finish(error="That person no longer exists.", return_to=return_to)

    connections.save_google_connection(
        db,
        person_id=pending.person_id,
        account_identifier=account,
        creds=creds,
        scopes=scopes,
    )
    return _finish(success=account, return_to=return_to)


def _finish(
    *,
    success: Optional[str] = None,
    error: Optional[str] = None,
    return_to: Optional[str] = None,
) -> Response:
    """End the consent flow: back to the admin page, or a standalone page.

    The admin UI passes ``return_to`` and shows a toast. A family member
    who followed a chat link has no page to return to, so they get a
    small self-contained confirmation instead.
    """
    if return_to:
        # Already vetted by _return_url when the flow started.
        key, value = (
            ("contacts_connected", success) if success else ("contacts_error", error)
        )
        joiner = "&" if "?" in return_to else "?"
        return RedirectResponse(
            f"{return_to}{joiner}{key}={quote(value or '')}", status_code=302
        )
    if success:
        title = "Google Contacts connected"
        body = (
            f"<p>Avi can now look up and save contacts in "
            f"<strong>{html.escape(success)}</strong>.</p>"
            f"<p>{html.escape(messages.PERMISSION_SUMMARY)}</p>"
            "<p>You can close this tab and go back to your conversation — "
            "ask Avi again and he'll save the contact.</p>"
        )
    else:
        title = "Contacts weren't connected"
        body = (
            f"<p>{html.escape(error or 'Something went wrong.')}</p>"
            "<p>Open the link Avi sent you to try again.</p>"
        )
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;"
        "margin:4rem auto;padding:0 1.25rem;line-height:1.5;color:#1f2937}"
        "h1{font-size:1.4rem}</style></head>"
        f"<body><h1>{title}</h1>{body}</body></html>"
    )
    return HTMLResponse(page, status_code=200 if success else 400)


# ---------------------------------------------------------------------------
# Disconnect / test
# ---------------------------------------------------------------------------


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(
    person_id: int = Query(...),
    provider: str = Query("google"),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> None:
    _authorise(user, db, person_id)
    connections.delete_connection(db, person_id, provider)


class ContactsTestResult(BaseModel):
    ok: bool
    provider: str
    account_identifier: Optional[str] = None
    # Only a count — the test never returns anyone's contacts.
    contact_count: int = 0


@router.post("/test", response_model=ContactsTestResult)
def test_connection(
    person_id: int = Query(...),
    provider: Optional[str] = Query(None),
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> ContactsTestResult:
    """Call the provider once to prove the stored grant still works."""
    _authorise(user, db, person_id)
    try:
        adapter = connections.adapter_for_person(db, person_id, provider)
        count = adapter.count()
    except ContactsNotConnected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ContactsAuthExpired as exc:
        connections.mark_needs_reconnect(db, person_id, exc.provider)
        raise HTTPException(
            status_code=409,
            detail="The connection has expired or was revoked. Connect again.",
        ) from exc
    except ContactsNotSupported as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ContactsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    row = connections.get_connection(db, person_id, adapter.provider)
    return ContactsTestResult(
        ok=True,
        provider=adapter.provider,
        account_identifier=row.account_identifier if row else None,
        contact_count=count,
    )
