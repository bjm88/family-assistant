"""Provider registry, per-person connections and the Google consent flow.

This is the only module that knows which providers exist and how a
person's stored grant turns into a live :class:`ContactsAdapter`.
Callers ask :func:`adapter_for_person` and get either an adapter or a
typed error (:class:`ContactsNotConnected`, :class:`ContactsAuthExpired`,
:class:`ContactsNotSupported`) they can turn into a "please connect"
message.

Why a separate consent flow from Avi's own Google connection
------------------------------------------------------------
Avi's Gmail / Calendar grant is for the *assistant's* Google account.
Contacts belong to each family member's own account, so every person
grants ``auth/contacts`` for themselves and the token is stored per
person. The flow reuses the same OAuth client (``GOOGLE_OAUTH_CLIENT_ID``)
with its own redirect URI and — deliberately — without
``include_granted_scopes``: if a person's account had also granted
Gmail to this client, their contacts token must not inherit it.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Type
from urllib.parse import urlencode

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import models
from ...config import get_settings
from ...crypto import decrypt_str, encrypt_str
from .. import google_oauth
from .apple import AppleContactsAdapter
from .base import (
    ContactsAdapter,
    ContactsAuthExpired,
    ContactsError,
    ContactsNotConnected,
    ContactsNotSupported,
)
from .google import CONTACTS_SCOPE, OAUTH_SCOPES, GoogleContactsAdapter


logger = logging.getLogger(__name__)


PROVIDERS: Dict[str, Type[ContactsAdapter]] = {
    GoogleContactsAdapter.provider: GoogleContactsAdapter,
    AppleContactsAdapter.provider: AppleContactsAdapter,
}

_CALLBACK_PATH = "/api/contacts/google/oauth/callback"
_START_PATH = "/api/contacts/google/oauth/start"


def default_provider() -> str:
    """The provider used when a person hasn't connected anything yet."""
    configured = (get_settings().CONTACTS_DEFAULT_PROVIDER or "").strip().lower()
    return configured if configured in PROVIDERS else GoogleContactsAdapter.provider


def provider_class(provider: str) -> Type[ContactsAdapter]:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ContactsError(
            f"Unknown contacts provider {provider!r}. Known: {sorted(PROVIDERS)}."
        ) from None


def provider_label(provider: str) -> str:
    cls = PROVIDERS.get(provider)
    return cls.label if cls else provider


# ---------------------------------------------------------------------------
# Google consent flow
# ---------------------------------------------------------------------------


def google_redirect_uri() -> str:
    """Where Google sends the browser back after the contacts consent.

    Built on ``PUBLIC_BASE_URL`` — the address family members actually
    use, and the same one the connect link Avi sends in chat points at —
    unless ``GOOGLE_CONTACTS_OAUTH_REDIRECT_URI`` overrides it. Whatever
    this returns must be listed, exactly, under the OAuth client's
    Authorized redirect URIs.
    """
    s = get_settings()
    if s.GOOGLE_CONTACTS_OAUTH_REDIRECT_URI:
        return s.GOOGLE_CONTACTS_OAUTH_REDIRECT_URI
    return f"{s.PUBLIC_BASE_URL.rstrip('/')}{_CALLBACK_PATH}"


def google_oauth_configured() -> bool:
    s = get_settings()
    return bool(s.GOOGLE_OAUTH_CLIENT_ID and s.GOOGLE_OAUTH_CLIENT_SECRET)


def build_google_flow() -> Flow:
    s = get_settings()
    if not google_oauth_configured():
        raise google_oauth.GoogleOAuthUnavailable(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set, "
            "so Google Contacts can't be connected yet."
        )
    redirect_uri = google_redirect_uri()
    return Flow.from_client_config(
        {
            "web": {
                "client_id": s.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": s.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=list(OAUTH_SCOPES),
        redirect_uri=redirect_uri,
    )


def connect_url(person_id: int, provider: Optional[str] = None) -> str:
    """Link a person can open to grant access to their address book.

    Opening it requires being signed in to the family site as that
    person (or as an admin), so the link is safe to send in chat.
    """
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{_START_PATH}?{urlencode({'person_id': person_id})}"


@dataclass
class _PendingFlow:
    person_id: int
    code_verifier: Optional[str]
    return_to: Optional[str]
    expires_at: float


_pending: Dict[str, _PendingFlow] = {}
_pending_lock = threading.Lock()
_PENDING_TTL_SECONDS = 600


def new_state() -> str:
    """Mint the unguessable ``state`` value for one consent flow."""
    return secrets.token_urlsafe(32)


def stash_pending(
    state: str,
    *,
    person_id: int,
    code_verifier: Optional[str],
    return_to: Optional[str] = None,
) -> None:
    """Remember an in-flight consent flow under its ``state``.

    ``code_verifier`` is the PKCE secret oauthlib generated while
    building the consent URL; the callback has to replay it or Google
    rejects the code exchange.
    """
    now = time.time()
    with _pending_lock:
        for key in [k for k, v in _pending.items() if v.expires_at < now]:
            _pending.pop(key, None)
        _pending[state] = _PendingFlow(
            person_id=person_id,
            code_verifier=code_verifier,
            return_to=return_to,
            expires_at=now + _PENDING_TTL_SECONDS,
        )


def consume_pending(state: str) -> _PendingFlow:
    """Validate a returned ``state``. Each one is single-use."""
    with _pending_lock:
        entry = _pending.pop(state, None)
    if entry is None or entry.expires_at < time.time():
        raise ContactsError(
            "That connection attempt expired or was already used. "
            "Please start connecting Google Contacts again."
        )
    return entry


# ---------------------------------------------------------------------------
# Stored connections
# ---------------------------------------------------------------------------


def list_connections(
    db: Session, person_id: int
) -> List[models.ContactProviderConnection]:
    return list(
        db.execute(
            select(models.ContactProviderConnection)
            .where(models.ContactProviderConnection.person_id == person_id)
            .order_by(models.ContactProviderConnection.contact_provider_connection_id)
        ).scalars()
    )


def get_connection(
    db: Session, person_id: int, provider: Optional[str] = None
) -> Optional[models.ContactProviderConnection]:
    """The person's connection for ``provider``.

    With no provider given: the default provider's connection if there
    is one, otherwise whichever provider the person did connect.
    """
    rows = list_connections(db, person_id)
    if provider:
        return next((r for r in rows if r.provider == provider), None)
    preferred = default_provider()
    return next((r for r in rows if r.provider == preferred), rows[0] if rows else None)


def has_contacts_scope(row: models.ContactProviderConnection) -> bool:
    return CONTACTS_SCOPE in (row.scopes or "").split()


def granted_scopes(flow: Flow) -> List[str]:
    """Scopes Google actually granted in a finished consent flow.

    ``flow.credentials.scopes`` echoes what was *requested*. Google's
    consent screen lets the user untick individual permissions, so the
    token response is the only truthful source.
    """
    token = flow.oauth2session.token or {}
    scope = token.get("scope") or []
    return scope.split() if isinstance(scope, str) else [str(s) for s in scope]


def save_google_connection(
    db: Session,
    *,
    person_id: int,
    account_identifier: str,
    creds: Credentials,
    scopes: List[str],
) -> models.ContactProviderConnection:
    """Insert or replace a person's Google Contacts grant.

    ``scopes`` is what Google granted (see :func:`granted_scopes`).
    """
    blob = encrypt_str(creds.to_json())
    if blob is None:
        raise ContactsError("Refusing to store empty Google credentials.")
    row = get_connection(db, person_id, GoogleContactsAdapter.provider)
    if row is None:
        row = models.ContactProviderConnection(
            person_id=person_id, provider=GoogleContactsAdapter.provider
        )
        db.add(row)
    row.account_identifier = account_identifier
    row.scopes = " ".join(scopes)
    row.credentials_encrypted = blob
    row.token_expires_at = google_oauth._expiry_to_aware(creds)
    row.needs_reconnect = False
    db.flush()
    return row


def delete_connection(db: Session, person_id: int, provider: str) -> bool:
    """Forget a connection and, for Google, revoke the grant.

    Revoking a Google token withdraws that account's whole grant to the
    OAuth client. The same client also carries Avi's own Gmail /
    Calendar grant, so the revoke is skipped when the person linked the
    very account Avi runs on — otherwise disconnecting contacts would
    cut off Avi's mailbox.
    """
    row = get_connection(db, person_id, provider)
    if row is None:
        return False
    if provider == GoogleContactsAdapter.provider:
        shared_with_assistant = db.execute(
            select(models.GoogleOAuthCredential.google_oauth_credential_id).where(
                func.lower(models.GoogleOAuthCredential.granted_email)
                == row.account_identifier.lower()
            )
        ).first()
        if shared_with_assistant is None:
            _revoke_google(row)
    db.delete(row)
    db.flush()
    return True


def _revoke_google(row: models.ContactProviderConnection) -> None:
    try:
        creds = _google_credentials(row)
        token = creds.refresh_token or creds.token
        if token:
            httpx.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=5.0,
            )
    except Exception as exc:  # noqa: BLE001 - disconnect locally regardless
        logger.warning("Google contacts token revoke failed (non-fatal): %s", exc)


def _google_credentials(row: models.ContactProviderConnection) -> Credentials:
    payload = decrypt_str(row.credentials_encrypted)
    if not payload:
        raise ContactsError("Stored Google Contacts credentials are empty or corrupt.")
    return Credentials.from_authorized_user_info(json.loads(payload))


def _live_google_credentials(
    db: Session, row: models.ContactProviderConnection
) -> Credentials:
    """Decrypt the grant and refresh the access token when it is stale."""
    creds = _google_credentials(row)
    if creds.valid:
        return creds
    if not creds.refresh_token:
        _flag_reconnect(db, row)
        raise ContactsAuthExpired(row.provider)
    try:
        creds.refresh(GoogleAuthRequest())
    except RefreshError as exc:
        # Google refused the refresh token itself: revoked, or expired
        # because the OAuth app is still in "Testing" (7-day tokens).
        logger.info(
            "Google contacts grant for person_id=%s was rejected: %s",
            row.person_id,
            exc,
        )
        _flag_reconnect(db, row)
        raise ContactsAuthExpired(row.provider) from exc
    except Exception as exc:  # noqa: BLE001 - network etc.; worth retrying later
        raise ContactsError(f"Couldn't reach Google to refresh access: {exc}") from exc
    row.credentials_encrypted = encrypt_str(creds.to_json())
    row.token_expires_at = google_oauth._expiry_to_aware(creds)
    db.commit()
    return creds


def _flag_reconnect(db: Session, row: models.ContactProviderConnection) -> None:
    row.needs_reconnect = True
    db.commit()


def mark_needs_reconnect(db: Session, person_id: int, provider: str) -> None:
    """Record that the provider rejected this person's grant mid-call."""
    row = get_connection(db, person_id, provider)
    if row is not None and not row.needs_reconnect:
        _flag_reconnect(db, row)


def adapter_for_person(
    db: Session, person_id: int, provider: Optional[str] = None
) -> ContactsAdapter:
    """Return a ready-to-use adapter for this person's address book.

    Raises :class:`ContactsNotConnected` when they haven't granted
    access, :class:`ContactsAuthExpired` when the grant stopped working
    and :class:`ContactsNotSupported` for placeholder providers.
    """
    row = get_connection(db, person_id, provider)
    wanted = provider or (row.provider if row else default_provider())
    cls = provider_class(wanted)
    if not cls.available:
        raise ContactsNotSupported(wanted)
    if row is None:
        raise ContactsNotConnected(wanted)
    if row.needs_reconnect:
        raise ContactsAuthExpired(wanted)
    if wanted == GoogleContactsAdapter.provider:
        if not has_contacts_scope(row):
            raise ContactsAuthExpired(wanted)
        return GoogleContactsAdapter(_live_google_credentials(db, row))
    raise ContactsNotSupported(wanted)
