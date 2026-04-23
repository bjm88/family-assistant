"""Browser login (Google OAuth) for the human users of the app.

Mounted at ``/api/auth/*`` and entirely public — these routes are how
an anonymous browser becomes an authenticated one. Per-route
authorization for everything else lives in :mod:`api.auth`.

Endpoints:

* ``GET  /api/auth/google/start``    — kick the browser into Google's
  consent screen with the minimum scopes (``openid email profile``).
* ``GET  /api/auth/google/callback`` — exchange the auth code, verify
  the ``id_token``, look up the email in ``people`` / ``ADMIN_EMAILS``,
  and bake a signed-cookie session.
* ``GET  /api/auth/me``              — return the current user's
  identity (or 401 if not logged in). The React ``AuthProvider``
  polls this on every page mount.
* ``POST /api/auth/logout``          — wipe the cookie.

Why a separate Google OAuth client (``USER_LOGIN_GOOGLE_*``) instead of
re-using Avi's existing ``GOOGLE_OAUTH_CLIENT_*``? So this flow can
request ONLY the non-sensitive ``openid+email+profile`` scopes — no
Gmail, no Calendar — and so revoking either credential set doesn't
disconnect the other.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..auth import (
    CurrentUser,
    UnauthorizedEmail,
    cookie_attrs,
    require_user,
    resolve_user_for_email,
    sign_session,
)
from ..config import get_settings
from ..db import get_db


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/auth", tags=["auth"])


# Google's OIDC discovery doc never moves — hardcoding is fine and saves
# a 100 ms startup HTTP round-trip per worker. If Google ever rotates
# these we'll find out very loudly via login failures.
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# Login is a tiny, non-sensitive scope set. We deliberately do NOT
# request gmail.* or calendar.* here — Avi's OAuth client handles
# those separately so revoking one doesn't blast the other.
LOGIN_SCOPES = ("openid", "email", "profile")


# Local-dev quality-of-life: oauthlib refuses an ``http://`` redirect URI
# unless this env var is explicitly set. We don't import oauthlib here
# (the login flow is plain httpx) but the env flag also unlocks
# Google's tolerant http-localhost handling for the integrations layer
# in the same process. No-op once the redirect is real HTTPS.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


# ---------------------------------------------------------------------------
# OAuth state (CSRF nonce + post-login redirect target)
# ---------------------------------------------------------------------------


@dataclass
class _PendingState:
    next_path: str
    expires_at: float


_pending: dict[str, _PendingState] = {}
_pending_lock = threading.Lock()
_PENDING_TTL_SECONDS = 600  # 10 minutes is plenty for a manual click-through


def _stash_state(next_path: str) -> str:
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _pending_lock:
        # Opportunistic GC of expired tokens.
        for k in [k for k, v in _pending.items() if v.expires_at < now]:
            _pending.pop(k, None)
        _pending[state] = _PendingState(
            next_path=next_path,
            expires_at=now + _PENDING_TTL_SECONDS,
        )
    return state


def _consume_state(state: str) -> Optional[str]:
    """Validate ``state`` and return the stashed ``next`` redirect path."""
    now = time.time()
    with _pending_lock:
        entry = _pending.pop(state, None)
    if entry is None or entry.expires_at < now:
        return None
    return entry.next_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login_oauth_configured() -> bool:
    s = get_settings()
    return bool(
        s.USER_LOGIN_GOOGLE_CLIENT_ID and s.USER_LOGIN_GOOGLE_CLIENT_SECRET
    )


def _safe_next(raw: Optional[str]) -> str:
    """Sanitise the ``?next=`` redirect target so we only bounce within
    the same origin. Strips schemes / hosts / protocol-relative URLs.
    """
    if not raw:
        return "/"
    # Block protocol-relative (``//evil.com``) and absolute URLs.
    if raw.startswith("//") or "://" in raw:
        return "/"
    if not raw.startswith("/"):
        return "/"
    return raw


def _login_redirect(error: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/login?error={error}", status_code=302
    )


# ---------------------------------------------------------------------------
# /me — read current identity
# ---------------------------------------------------------------------------


@router.get("/me")
def me(
    user: CurrentUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the current user's identity.

    Includes a denormalised ``family_name`` so the React ``Layout``
    can render "Welcome, Ben (the Maisanos)" without a follow-up
    fetch — that fetch would itself need auth, but the current user
    might not have direct access to ``/families/{id}`` anyway, and
    we already loaded the row server-side here.
    """
    family_name: Optional[str] = None
    if user.family_id is not None:
        fam = db.get(models.Family, user.family_id)
        family_name = fam.family_name if fam else None
    return {
        "email": user.email,
        "role": user.role,
        "person_id": user.person_id,
        "family_id": user.family_id,
        "family_name": family_name,
    }


# ---------------------------------------------------------------------------
# /logout — clear the cookie
# ---------------------------------------------------------------------------


@router.post("/logout", status_code=204)
def logout(response: Response) -> Response:
    """Wipe the session cookie. Idempotent — safe to call when not logged in."""
    attrs = cookie_attrs()
    # delete_cookie wants slightly different kwargs than set_cookie.
    response.delete_cookie(
        key=attrs["key"],
        path=attrs["path"],
        secure=attrs["secure"],
        httponly=attrs["httponly"],
        samesite=attrs["samesite"],
    )
    response.status_code = 204
    return response


# ---------------------------------------------------------------------------
# /google/start — bounce to Google's consent screen
# ---------------------------------------------------------------------------


@router.get("/google/start")
def google_start(
    request: Request, next: str = Query("/", alias="next")
) -> RedirectResponse:
    if not _login_oauth_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "USER_LOGIN_GOOGLE_CLIENT_ID / USER_LOGIN_GOOGLE_CLIENT_SECRET "
                "are not set. Create a Web-application OAuth client in Google "
                "Cloud Console and put the values in your .env file."
            ),
        )
    settings = get_settings()
    state = _stash_state(_safe_next(next))
    params = {
        "client_id": settings.USER_LOGIN_GOOGLE_CLIENT_ID,
        "redirect_uri": settings.USER_LOGIN_GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "state": state,
        # Login flow only needs an id_token — no refresh token, no
        # offline access. Force the account chooser so a shared
        # browser doesn't silently re-use whichever Google account
        # signed in last.
        "prompt": "select_account",
        "access_type": "online",
        "include_granted_scopes": "true",
    }
    return RedirectResponse(
        url=f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}", status_code=302
    )


# ---------------------------------------------------------------------------
# /google/callback — exchange code, set cookie, redirect home
# ---------------------------------------------------------------------------


def _decode_id_token_email(id_token: str) -> Optional[str]:
    """Pull ``email`` out of an OIDC id_token JWT WITHOUT signature
    verification.

    Why no verify? Because we just received the token directly from
    Google's token endpoint over TLS, in response to our own
    client_secret. There's no third party in the channel that could
    have forged it. The downside of a full verify (fetch JWKs, check
    sig, check iss/aud/exp) is two extra network calls and a
    cryptography dependency for what amounts to a no-op in the
    single-use auth-code-flow path.

    Returns ``None`` if the token is malformed.
    """
    try:
        _, payload_b64, _ = id_token.split(".")
    except ValueError:
        return None
    pad = "=" * (-len(payload_b64) % 4)
    try:
        import base64

        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    # Google sets ``email_verified=true`` for any address on a Google
    # Workspace or Gmail account; refuse anything that comes back
    # marked unverified to avoid a domain-spoofing attack via a
    # consumer Google account that has the address aliased.
    if payload.get("email_verified") is False:
        return None
    email = payload.get("email")
    return email if isinstance(email, str) else None


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Response:
    if error:
        logger.info("Google OAuth callback returned error=%s", error)
        return _login_redirect(error="google_denied")
    if not code or not state:
        return _login_redirect(error="missing_code_or_state")

    next_path = _consume_state(state)
    if next_path is None:
        return _login_redirect(error="state_expired")

    if not _login_oauth_configured():
        return _login_redirect(error="oauth_not_configured")

    settings = get_settings()
    token_payload = {
        "code": code,
        "client_id": settings.USER_LOGIN_GOOGLE_CLIENT_ID,
        "client_secret": settings.USER_LOGIN_GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.USER_LOGIN_GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data=token_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Google token exchange network error: %s", exc)
        return _login_redirect(error="token_exchange_network")
    if resp.status_code != 200:
        logger.warning(
            "Google token exchange non-200: status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return _login_redirect(error="token_exchange_failed")
    try:
        body = resp.json()
    except ValueError:
        return _login_redirect(error="token_exchange_malformed")
    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        return _login_redirect(error="missing_id_token")
    email = _decode_id_token_email(id_token)
    if not email:
        return _login_redirect(error="missing_email")

    try:
        current_user = resolve_user_for_email(db, email)
    except UnauthorizedEmail as exc:
        logger.info("Login denied for unauthorised email %s: %s", email, exc)
        return _login_redirect(error="unauthorised")

    cookie_value = sign_session(
        email=current_user.email,
        role=current_user.role,
        person_id=current_user.person_id,
        family_id=current_user.family_id,
    )
    redirect = RedirectResponse(url=next_path, status_code=302)
    attrs = cookie_attrs()
    redirect.set_cookie(
        key=attrs["key"],
        value=cookie_value,
        httponly=attrs["httponly"],
        secure=attrs["secure"],
        samesite=attrs["samesite"],
        path=attrs["path"],
        max_age=settings.SESSION_LIFETIME_DAYS * 24 * 60 * 60,
    )
    logger.info(
        "[auth] login email=%s role=%s family_id=%s person_id=%s next=%s",
        current_user.email,
        current_user.role,
        current_user.family_id,
        current_user.person_id,
        next_path,
    )
    return redirect
