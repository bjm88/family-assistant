"""Browser-login authentication and per-request authorization.

This module is the single source of truth for "who is the human driving
this request?". It does NOT speak to Google — that's
:mod:`api.routers.auth`. It only handles:

* Stateless **signed-cookie** sessions. Payload is a small JSON blob
  ``{"email", "role", "person_id", "family_id", "exp"}`` HMAC-SHA256
  signed with :setting:`SESSION_SECRET_KEY`. No DB rows, no Redis,
  rotates immediately on logout (the cookie is just deleted).
* The `CurrentUser` dataclass that gets stashed on
  ``request.state.user`` by the cookie middleware in
  :mod:`api.main`. ``None`` means "unauthenticated request".
* FastAPI ``Depends(...)`` factories that route handlers use to
  declare their authorization requirement:

  - :func:`get_current_user` — non-raising, returns ``None`` for guests.
  - :func:`require_user` — 401 if not logged in.
  - :func:`require_admin` — 403 unless the user is in
    :setting:`ADMIN_EMAILS`.
  - :func:`require_family_member` — admin OR a member whose
    ``person.family_id == family_id``. Used by
    ``/api/aiassistant/*`` and the curated read-only Overview GETs.

Why stdlib instead of ``itsdangerous`` / ``pyjwt``? Because the payload
is tiny and the signing scheme is the simplest thing that works:
base64(json) ``.`` base64(HMAC-SHA256(secret, payload)). No new dep
to vet, no JWT footguns (alg=none, RS256 vs HS256, etc.).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .db import get_db


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CurrentUser
# ---------------------------------------------------------------------------


# Three roles, matching the design doc:
#
#   * ``admin``  — email present in ADMIN_EMAILS. Acts across every family,
#     can hit every CRUD endpoint, can override ``recognized_person_id`` on
#     the live chat for testing.
#   * ``member`` — email matches ``people.email_address`` (case-insensitive)
#     for exactly one row. Scoped to that person's ``family_id``.
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


@dataclass(frozen=True)
class CurrentUser:
    """Identity attached to ``request.state.user`` after cookie verify."""

    email: str
    role: str  # 'admin' or 'member'
    person_id: Optional[int]  # None for admins (admins aren't bound to a Person)
    family_id: Optional[int]  # None for admins; admins act across families

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_member(self) -> bool:
        return self.role == ROLE_MEMBER


# ---------------------------------------------------------------------------
# Cookie signing
# ---------------------------------------------------------------------------


class SessionError(RuntimeError):
    """Raised when the signing key isn't configured."""


def _secret_bytes() -> bytes:
    key = get_settings().SESSION_SECRET_KEY
    if not key:
        raise SessionError(
            "SESSION_SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
            "and put it in .env."
        )
    return key.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def sign_session(
    *,
    email: str,
    role: str,
    person_id: Optional[int],
    family_id: Optional[int],
    lifetime_seconds: Optional[int] = None,
) -> str:
    """Return the cookie value for a freshly minted session.

    The output is ``<base64-payload>.<base64-signature>`` so it round-trips
    cleanly through HTTP cookies (URL-safe alphabet, no '=' padding).
    """
    settings = get_settings()
    if lifetime_seconds is None:
        lifetime_seconds = settings.SESSION_LIFETIME_DAYS * 24 * 60 * 60
    payload = {
        "email": email,
        "role": role,
        "person_id": person_id,
        "family_id": family_id,
        "exp": int(time.time()) + int(lifetime_seconds),
    }
    payload_b = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    sig = hmac.new(_secret_bytes(), payload_b, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_b)}.{_b64url_encode(sig)}"


def verify_session(cookie: str) -> Optional[CurrentUser]:
    """Verify a cookie value and return the embedded CurrentUser.

    Returns ``None`` for any failure (bad signature, expired exp,
    malformed payload). Never raises so the cookie middleware can
    treat a bad cookie as "anonymous request" without leaking the
    failure mode.
    """
    if not cookie or "." not in cookie:
        return None
    try:
        payload_b64, sig_b64 = cookie.split(".", 1)
        payload_b = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, base64.binascii.Error):
        return None
    try:
        expected = hmac.new(_secret_bytes(), payload_b, hashlib.sha256).digest()
    except SessionError:
        return None
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(payload_b.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    email = payload.get("email")
    role = payload.get("role")
    if not isinstance(email, str) or role not in (ROLE_ADMIN, ROLE_MEMBER):
        return None
    person_id = payload.get("person_id")
    family_id = payload.get("family_id")
    if person_id is not None and not isinstance(person_id, int):
        return None
    if family_id is not None and not isinstance(family_id, int):
        return None
    return CurrentUser(
        email=email,
        role=role,
        person_id=person_id,
        family_id=family_id,
    )


# ---------------------------------------------------------------------------
# Identity resolution (Google email → CurrentUser)
# ---------------------------------------------------------------------------


class UnauthorizedEmail(RuntimeError):
    """Raised by :func:`resolve_user_for_email` when the email is unknown.

    The OAuth callback maps this to a ``302 /login?error=unauthorised``
    so the user sees a friendly "this account is not authorised" message
    instead of a raw 403 page.
    """


def resolve_user_for_email(db: Session, email: str) -> CurrentUser:
    """Map a verified Google email to a CurrentUser.

    Lookup order:
      1. Lower-cased email in :setting:`ADMIN_EMAILS` → admin role.
      2. Otherwise look for a ``people`` row whose ``email_address``
         matches case-insensitively. If found, the user is a member of
         that person's family. If multiple rows match (shouldn't happen
         in a well-curated household, but the column has no unique
         constraint) we take the first by ``person_id`` for determinism.
      3. Otherwise raise :class:`UnauthorizedEmail`.

    Identity is derived live on every login. Removing a Person from the
    database revokes their access on the next OAuth callback (and on the
    next request once their cookie naturally expires).
    """
    settings = get_settings()
    normalised = email.strip().lower()
    if not normalised:
        raise UnauthorizedEmail("empty email")
    if normalised in settings.admin_emails:
        return CurrentUser(
            email=normalised,
            role=ROLE_ADMIN,
            person_id=None,
            family_id=None,
        )
    # Case-insensitive person lookup. ``email_address`` is free-text
    # (no unique constraint, no DB-level lower-casing) so we do the
    # comparison in Python over the candidate set rather than relying
    # on Postgres CITEXT.
    person = (
        db.query(models.Person)
        .filter(models.Person.email_address.isnot(None))
        .filter(
            models.Person.email_address.ilike(normalised)
        )
        .order_by(models.Person.person_id.asc())
        .first()
    )
    if person is None:
        raise UnauthorizedEmail(
            f"{email} is not on any household roster and is not an admin."
        )
    return CurrentUser(
        email=normalised,
        role=ROLE_MEMBER,
        person_id=person.person_id,
        family_id=person.family_id,
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> Optional[CurrentUser]:
    """Non-raising accessor — returns the cookie middleware's stash.

    Used by routes that need to vary their behaviour based on whether
    a user is logged in (e.g. the landing page) without slamming the
    door on guests.
    """
    return getattr(request.state, "user", None)


def require_user(request: Request) -> CurrentUser:
    """401 unless a valid session cookie is present."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required",
        )
    return user


def require_admin(request: Request) -> CurrentUser:
    """403 unless the logged-in user is an admin (401 if not logged in)."""
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


def require_family_member(family_id: int, request: Request) -> CurrentUser:
    """Admin OR a member whose ``family_id`` matches the requested one.

    Use this on every endpoint a non-admin family member should be
    able to read for their own household — Overview GETs, the live
    AI chat, the live sessions list (further filtered to their own
    participant rows), the kanban (further filtered to tasks they
    own / follow). Members trying to read a different family see the
    same 403 they'd see if they weren't a member at all — there's no
    information leak about which families exist.
    """
    user = require_user(request)
    if user.is_admin:
        return user
    if user.family_id != family_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this family.",
        )
    return user


def require_family_member_from_request(request: Request) -> CurrentUser:
    """Admin OR a member whose family_id matches the request's ``family_id``.

    Reads ``family_id`` from path params first, then query params. Used as
    a router/endpoint-level dependency on member-readable GETs that take
    ``family_id`` in the URL — saves the per-handler boilerplate of
    parsing the int and calling :func:`require_family_member` by hand.

    Endpoints whose ``family_id`` arrives in the JSON body (e.g. the
    chat / greet / followup AI endpoints) should NOT use this — instead
    they must call :func:`require_family_member` explicitly inside the
    handler after parsing the payload, because FastAPI dependencies
    can't see the request body.
    """
    user = require_user(request)
    if user.is_admin:
        return user
    raw = request.path_params.get("family_id") or request.query_params.get(
        "family_id"
    )
    if raw is None:
        # No family_id on the URL; the route can't be safely scoped to
        # a family without one. Refuse rather than silently allow-all.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot determine family scope for this request.",
        )
    try:
        family_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid family_id",
        )
    if user.family_id != family_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this family.",
        )
    return user


def require_family_member_for_person(person_id: int, request: Request,
                                      db: Session = Depends(get_db)) -> CurrentUser:
    """Admin OR a member of the same family as the target Person.

    Helper for routes that take ``person_id`` instead of ``family_id``
    in the path. Resolves the person's family then defers to
    :func:`require_family_member`.
    """
    user = require_user(request)
    if user.is_admin:
        return user
    person = db.get(models.Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if user.family_id != person.family_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this person.",
        )
    return user


# ---------------------------------------------------------------------------
# Cookie write/clear helpers (used by the auth router and the
# sliding-refresh middleware in api.main)
# ---------------------------------------------------------------------------


def cookie_attrs() -> dict:
    """Common kwargs for ``response.set_cookie`` / ``delete_cookie``.

    ``Secure`` is enabled automatically when :setting:`PUBLIC_BASE_URL`
    starts with ``https://`` so local-dev over plain http still works
    while the ngrok-served deployment gets the Secure flag.
    """
    settings = get_settings()
    secure = settings.PUBLIC_BASE_URL.lower().startswith("https://")
    return {
        "key": settings.SESSION_COOKIE_NAME,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }
