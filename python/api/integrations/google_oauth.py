"""Google OAuth glue — auth-code flow, token persistence, auto-refresh.

This module deliberately *does not* know about Gmail or Calendar. Its
job is only to:

1. Build a :class:`google_auth_oauthlib.flow.Flow` that points at our
   local ``/oauth/callback``.
2. Round-trip credentials to and from the Postgres
   ``google_oauth_credentials`` table, with the entire
   :class:`google.oauth2.credentials.Credentials` JSON encrypted at
   rest via :func:`api.crypto.encrypt_str`.
3. Hand back live :class:`Credentials` objects for the higher-level
   adapters (:mod:`api.integrations.gmail`,
   :mod:`api.integrations.google_calendar`).

Local-dev redirect URI
----------------------
Google explicitly allows ``http://localhost`` (any port, any path) as
an OAuth redirect URI for the "Web application" client type, so this
all works without a public domain. See
https://developers.google.com/identity/protocols/oauth2/web-server.

Scope strategy
--------------
We request the *minimum useful* scope set on first connect:

* ``openid``, ``email``, ``profile`` — so we know which Google account
  the user just authorised (returned as ``id_token`` claims).
* ``https://www.googleapis.com/auth/gmail.send`` — outbound mail only,
  no inbox read.
* ``https://www.googleapis.com/auth/calendar.events`` — read AND
  write events on the assistant's own calendar plus any calendars
  shared *with* it (free/busy lookups, event listings, AND inserting
  new events / holds when the household member has flipped on
  ``people.ai_can_write_calendar`` in their profile).

Add more scopes by overriding :data:`DEFAULT_SCOPES`; existing
credentials keep working as long as the new scope set is a superset.

Re-consent note
---------------
Households connected before the ``calendar.readonly →
calendar.events`` upgrade still hold the older read-only token.
Calendar reads keep working unchanged, but write tools (e.g.
``calendar_create_event``) will return a Google 403 with
``insufficient_scope``. The integration layer detects that and
surfaces a "disconnect + reconnect Google in /aiassistant settings
to grant write access" message — there's no automatic upgrade path
because Google's incremental-auth flow still requires the user to
click through the consent screen.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..crypto import decrypt_str, encrypt_str


# Local-dev quality-of-life: oauthlib refuses an ``http://`` redirect URI
# unless this env var is explicitly set, and it errors when Google returns
# a slightly different scope list than we requested (it routinely promotes
# ``openid`` → ``https://www.googleapis.com/auth/openid``, for instance).
# Both are no-ops in production once the redirect is real HTTPS.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


logger = logging.getLogger(__name__)


DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    # gmail.modify is a superset of gmail.send + gmail.readonly:
    # it lets Avi list inbox messages, fetch their bodies, send
    # replies, and mark threads as read so we don't reprocess them.
    # Existing connections still work because Google honours the
    # already-granted scope set; only NEW connects request the
    # extra capability.
    "https://www.googleapis.com/auth/gmail.modify",
    # calendar.events is a superset of calendar.readonly: it lets
    # Avi list / read events across every calendar the user has
    # shared with the assistant AND insert new events on calendars
    # the user has shared with edit permission. Required by the
    # calendar_create_event tool. Existing households with the
    # older read-only token must disconnect + reconnect on the
    # AI Assistant settings page to gain the write capability;
    # see the module docstring for why.
    "https://www.googleapis.com/auth/calendar.events",
    # calendar.events is intentionally narrow: it grants per-event
    # access on calendars whose ids we already know, but it does NOT
    # authorise the ``calendarList.list()`` endpoint Google uses to
    # *enumerate* every calendar the user has access to. Without this
    # extra scope the agent's existing tools still work (they always
    # pass an explicit calendar id from the DB), but the Assistant
    # settings page's "Visible calendars + permission level" panel
    # 403s with "insufficient authentication scopes". This scope is
    # read-only and trivial — Google's consent screen folds it into
    # the same "View your calendars" line so the user doesn't see an
    # extra checkbox.
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    # drive.file is the *least-privilege* Drive scope — it grants
    # access ONLY to files the app itself creates or that the user
    # explicitly opens through a Google Picker. Avi cannot list,
    # read, or modify any of the user's other Drive files. Used by
    # the weekly DB-backup uploader (scripts/db_backup_to_gdrive.py)
    # to drop pg_dump archives into the folder configured via
    # DB_BACKUP_GDRIVE; uploads target a known folder id (the user
    # supplies the folder URL in .env) so we don't need broader
    # ``drive`` access. As with the calendar scope upgrade above:
    # households connected before this scope was added must
    # disconnect + reconnect Google on the AI Assistant settings
    # page before the backup uploader can write to Drive.
    "https://www.googleapis.com/auth/drive.file",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GoogleOAuthUnavailable(RuntimeError):
    """Raised when the OAuth client id/secret env vars aren't configured."""


class GoogleOAuthError(RuntimeError):
    """Raised when an OAuth exchange or refresh fails."""


class GoogleNotConnected(RuntimeError):
    """Raised when an integration call is made for an assistant that
    hasn't gone through the OAuth flow yet.
    """


# ---------------------------------------------------------------------------
# Flow construction
# ---------------------------------------------------------------------------


def _client_config() -> Dict[str, Dict[str, object]]:
    """Build the in-memory ``client_config`` Google's libraries expect.

    Equivalent to a ``client_secrets.json`` download but built from
    env-vars so secrets stay out of the repo.
    """
    s = get_settings()
    if not (s.GOOGLE_OAUTH_CLIENT_ID and s.GOOGLE_OAUTH_CLIENT_SECRET):
        raise GoogleOAuthUnavailable(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not "
            "set. Create a Web-application OAuth client in Google Cloud "
            "Console (Credentials → Create OAuth client ID), add "
            f"{s.GOOGLE_OAUTH_REDIRECT_URI} as an Authorized redirect "
            "URI, and put the values in your .env file."
        )
    return {
        "web": {
            "client_id": s.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": s.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [s.GOOGLE_OAUTH_REDIRECT_URI],
        }
    }


def build_flow(scopes: Optional[List[str]] = None) -> Flow:
    """Return a fresh :class:`Flow` ready to drive ``/oauth/start``."""
    return Flow.from_client_config(
        _client_config(),
        scopes=list(scopes or DEFAULT_SCOPES),
        redirect_uri=get_settings().GOOGLE_OAUTH_REDIRECT_URI,
    )


# ---------------------------------------------------------------------------
# State / CSRF — short-lived in-memory store keyed by random nonce.
# ---------------------------------------------------------------------------


@dataclass
class _PendingFlow:
    assistant_id: int
    code_verifier: Optional[str]
    expires_at: float


_pending: Dict[str, _PendingFlow] = {}
_pending_lock = threading.Lock()
_PENDING_TTL_SECONDS = 600  # 10 minutes is plenty for a manual click-through


def stash_pending(
    assistant_id: int,
    *,
    code_verifier: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    """Remember the in-flight OAuth flow so the callback can find it.

    The PKCE ``code_verifier`` (the secret half of the
    ``code_challenge`` we just sent Google) is stashed alongside the
    state so the callback handler can replay it during ``fetch_token``.
    Without this, ``google_auth_oauthlib`` builds a fresh verifier in
    each handler and Google rejects the exchange with
    ``invalid_grant: Missing code verifier``.

    Pass ``state`` to remember a value the caller already minted (so it
    can embed the same value in the OAuth URL); leave it ``None`` to
    have a random one generated.
    """
    final_state = state or secrets.token_urlsafe(32)
    now = time.time()
    with _pending_lock:
        # Drop expired tokens opportunistically so this dict stays tiny.
        for k in [k for k, v in _pending.items() if v.expires_at < now]:
            _pending.pop(k, None)
        _pending[final_state] = _PendingFlow(
            assistant_id=assistant_id,
            code_verifier=code_verifier,
            expires_at=now + _PENDING_TTL_SECONDS,
        )
    return final_state


def consume_pending(state: str) -> Tuple[int, Optional[str]]:
    """Validate a returned ``state`` and return ``(assistant_id, code_verifier)``.

    Each token is single-use; expired or unknown tokens raise
    :class:`GoogleOAuthError` so the user gets a clean 4xx instead of
    a partial OAuth state.
    """
    now = time.time()
    with _pending_lock:
        entry = _pending.pop(state, None)
    if entry is None:
        raise GoogleOAuthError(
            "OAuth state token is unknown or already used. Please "
            "click 'Connect with Google' again to start a fresh flow."
        )
    if entry.expires_at < now:
        raise GoogleOAuthError(
            "OAuth flow expired (10 min limit). Please click 'Connect "
            "with Google' again."
        )
    return entry.assistant_id, entry.code_verifier


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------


def _credentials_to_blob(creds: Credentials) -> bytes:
    """Serialize and encrypt a Credentials object for at-rest storage."""
    payload = creds.to_json()
    blob = encrypt_str(payload)
    if blob is None:
        # encrypt_str returns None only for empty input; should never happen.
        raise GoogleOAuthError("Refusing to store an empty credentials blob.")
    return blob


def _blob_to_credentials(blob: bytes) -> Credentials:
    payload = decrypt_str(blob)
    if not payload:
        raise GoogleOAuthError("Stored credentials are empty/corrupt.")
    return Credentials.from_authorized_user_info(json.loads(payload))


def _expiry_to_aware(creds: Credentials) -> Optional[datetime]:
    """Return ``creds.expiry`` as a tz-aware UTC datetime (or None)."""
    expiry = creds.expiry
    if expiry is None:
        return None
    # google-auth uses naive UTC; we want tz-aware for Postgres TIMESTAMPTZ.
    if expiry.tzinfo is None:
        return expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc)


def save_credentials(
    db: Session,
    *,
    assistant_id: int,
    granted_email: str,
    creds: Credentials,
) -> models.GoogleOAuthCredential:
    """Insert or update the assistant's encrypted credentials row."""
    row = (
        db.query(models.GoogleOAuthCredential)
        .filter_by(assistant_id=assistant_id)
        .one_or_none()
    )
    blob = _credentials_to_blob(creds)
    scopes = " ".join(creds.scopes or [])
    expiry = _expiry_to_aware(creds)

    if row is None:
        row = models.GoogleOAuthCredential(
            assistant_id=assistant_id,
            granted_email=granted_email,
            scopes=scopes,
            token_payload_encrypted=blob,
            token_expires_at=expiry,
        )
        db.add(row)
    else:
        row.granted_email = granted_email
        row.scopes = scopes
        row.token_payload_encrypted = blob
        row.token_expires_at = expiry
    db.flush()
    db.refresh(row)
    return row


def load_credentials_row(
    db: Session, assistant_id: int
) -> Optional[models.GoogleOAuthCredential]:
    return (
        db.query(models.GoogleOAuthCredential)
        .filter_by(assistant_id=assistant_id)
        .one_or_none()
    )


def load_credentials(
    db: Session, assistant_id: int
) -> Tuple[models.GoogleOAuthCredential, Credentials]:
    """Load + decrypt + auto-refresh credentials for the assistant.

    Persists the rotated access_token + new expiry back to the database
    whenever a refresh fires, so subsequent calls don't pay the refresh
    cost twice. Raises :class:`GoogleNotConnected` when no row exists.
    """
    row = load_credentials_row(db, assistant_id)
    if row is None:
        raise GoogleNotConnected(
            "Assistant has not connected a Google account yet. "
            "Visit /aiassistant settings and click 'Connect with Google'."
        )
    creds = _blob_to_credentials(row.token_payload_encrypted)

    # If the access token is stale (or missing entirely) and we have a
    # refresh token, refresh now so the API call upstream succeeds on
    # the first try. Token rotation is then persisted so we don't repeat
    # the refresh on the next request.
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
        except Exception as exc:  # noqa: BLE001 - bubble up as our type
            raise GoogleOAuthError(
                f"Failed to refresh Google access token: {exc}"
            ) from exc
        row.token_payload_encrypted = _credentials_to_blob(creds)
        row.token_expires_at = _expiry_to_aware(creds)
        db.flush()

    return row, creds


def delete_credentials(db: Session, assistant_id: int) -> bool:
    """Remove the row + best-effort revoke the refresh token at Google.

    Returns ``True`` if a row was deleted. Revocation failures are
    logged but never raised — the user clearly wants Avi disconnected
    locally regardless of whether Google's revoke endpoint is healthy.
    """
    row = load_credentials_row(db, assistant_id)
    if row is None:
        return False
    try:
        creds = _blob_to_credentials(row.token_payload_encrypted)
        if creds.refresh_token:
            import requests  # type: ignore

            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": creds.refresh_token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("Google token revoke failed (non-fatal): %s", exc)
    db.delete(row)
    return True


# ---------------------------------------------------------------------------
# Helpers used by the HTTP router
# ---------------------------------------------------------------------------


def email_from_id_token(creds: Credentials) -> Optional[str]:
    """Pull the verified Gmail address out of the OIDC id_token payload."""
    id_token = getattr(creds, "id_token", None)
    if not id_token:
        return None
    # Lazy import — google.oauth2.id_token pulls in cryptography backends.
    try:
        from google.oauth2 import id_token as id_token_lib

        s = get_settings()
        info = id_token_lib.verify_oauth2_token(
            id_token,
            GoogleAuthRequest(),
            audience=s.GOOGLE_OAUTH_CLIENT_ID,
            clock_skew_in_seconds=10,
        )
        email = info.get("email")
        return str(email) if email else None
    except Exception as exc:  # noqa: BLE001 - tolerate clock skew etc.
        logger.warning("id_token verification failed: %s", exc)
        return None
