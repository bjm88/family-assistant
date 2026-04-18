"""The ``google_oauth_credentials`` table — OAuth tokens for Avi's Gmail / Calendar.

One row per :class:`Assistant`. Stores a Fernet-encrypted JSON blob
containing the full ``google.oauth2.credentials.Credentials`` payload
(refresh_token, access_token, token_uri, client_id, client_secret,
scopes, expiry). Keeping it as a single ciphertext column means we can
rotate the encryption key by re-reading + re-writing every row without
any schema change, and the LLM's read-only SQL surface never sees any
plain-text tokens.

The ``granted_email``, ``scopes``, and ``token_expires_at`` columns are
intentionally stored in plaintext so the admin UI (and a SQL-aware
local LLM) can answer "is Avi connected?" / "what scopes did the user
grant?" / "do we need to refresh?" without having to decrypt anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class GoogleOAuthCredential(Base, TimestampMixin):
    __tablename__ = "google_oauth_credentials"
    __table_args__ = (
        UniqueConstraint(
            "assistant_id", name="uq_google_oauth_credentials_per_assistant"
        ),
        {
            "comment": (
                "OAuth credentials linking the family assistant (Avi) to a "
                "Google account. One row per assistant. The token blob is "
                "Fernet-encrypted at rest; only the granted email, scopes, "
                "and expiry are kept in plaintext so the admin UI can "
                "render a status badge without decryption."
            )
        },
    )

    google_oauth_credential_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    assistant_id: Mapped[int] = mapped_column(
        ForeignKey("assistants.assistant_id", ondelete="CASCADE"),
        nullable=False,
        comment="Which assistant owns this Google account.",
    )

    granted_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment=(
            "Email address Google authenticated. Compare with "
            "assistants.email_address — they should match. Stored in "
            "plain so the UI / LLM can answer 'who is connected?' "
            "without touching ciphertext."
        ),
    )
    scopes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "Space-separated OAuth scopes the user actually granted. "
            "Drives capability checks ('can Avi send mail?', 'can Avi "
            "read the calendar?') without forcing a refresh."
        ),
    )
    token_payload_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment=(
            "Fernet-encrypted JSON of the google.oauth2.Credentials "
            "object (refresh_token, access_token, token_uri, client_id, "
            "client_secret, scopes, expiry). NEVER returned to the API "
            "client and NEVER queried in SQL; loaded only by the "
            "google_oauth integration adapter inside the API process."
        ),
    )
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Plaintext copy of the access-token expiry so the UI can "
            "warn 'expires in X minutes' and a SQL view can spot stale "
            "credentials. The refresh_token (in the encrypted blob) is "
            "what lets us mint a fresh access_token after this passes."
        ),
    )

    assistant: Mapped["Assistant"] = relationship()  # noqa: F821
