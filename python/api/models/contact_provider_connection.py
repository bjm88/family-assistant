"""The ``contact_provider_connections`` table — a person's own address book.

Avi's Gmail / Calendar grant lives on the *assistant* (one Google
account for the whole household). Contacts are different: "add her to
my contacts" means the *speaker's* personal address book, and an
address book can't be shared with another account the way a calendar
can. So each family member connects their own contacts account, and
the grant is stored here per person and per provider.

As with ``google_oauth_credentials`` the credential payload is one
Fernet-encrypted JSON blob; only the account label, scopes and expiry
are plaintext so the admin UI can render a status badge.
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


class ContactProviderConnection(Base, TimestampMixin):
    __tablename__ = "contact_provider_connections"
    __table_args__ = (
        UniqueConstraint(
            "person_id",
            "provider",
            name="uq_contact_provider_connections_person_provider",
        ),
        {
            "comment": (
                "A family member's grant letting Avi read and write their "
                "own personal address book (Google Contacts today; Apple "
                "Contacts is a planned provider). One row per person per "
                "provider. The credential blob is Fernet-encrypted at rest; "
                "only the account label, scopes and expiry are plaintext."
            )
        },
    )

    contact_provider_connection_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Whose address book this grant unlocks.",
    )
    provider: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Contacts backend: 'google' or 'apple'.",
    )
    account_identifier: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment=(
            "The account that granted access (the Google email address), "
            "kept in plaintext so the UI can show which account is linked."
        ),
    )
    scopes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Space-separated permissions the person actually granted.",
    )
    credentials_encrypted: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment=(
            "Fernet-encrypted provider credentials (for Google: the "
            "google.oauth2.Credentials JSON incl. refresh token). NEVER "
            "returned to API clients and NEVER queried in SQL."
        ),
    )
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Plaintext copy of the access-token expiry, if the provider has one.",
    )
    needs_reconnect: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "True once the provider rejected the stored grant (revoked or "
            "expired refresh token). Avi then asks the person to reconnect "
            "instead of retrying."
        ),
    )

    person: Mapped["Person"] = relationship()  # noqa: F821
