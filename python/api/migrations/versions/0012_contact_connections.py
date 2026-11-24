"""Add ``contact_provider_connections`` — per-person address-book grants.

Avi's Gmail / Calendar grant belongs to the assistant. Contacts are
personal: each family member connects their own Google Contacts (Apple
Contacts later) so "add them to my contacts" lands in the speaker's
address book.

Revision ID: 0012_contact_connections
Revises: 0011_sms_double_opt_in
Create Date: 2026-11-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# alembic_version.version_num is VARCHAR(32) — keep revision ids short.
revision: str = "0012_contact_connections"
down_revision: Union[str, None] = "0011_sms_double_opt_in"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contact_provider_connections",
        sa.Column(
            "contact_provider_connection_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="Whose address book this grant unlocks.",
        ),
        sa.Column(
            "provider",
            sa.String(length=20),
            nullable=False,
            comment="Contacts backend: 'google' or 'apple'.",
        ),
        sa.Column(
            "account_identifier",
            sa.String(length=255),
            nullable=False,
            comment=(
                "The account that granted access (the Google email address), "
                "kept in plaintext so the UI can show which account is linked."
            ),
        ),
        sa.Column(
            "scopes",
            sa.Text(),
            nullable=False,
            comment="Space-separated permissions the person actually granted.",
        ),
        sa.Column(
            "credentials_encrypted",
            sa.LargeBinary(),
            nullable=False,
            comment=(
                "Fernet-encrypted provider credentials (for Google: the "
                "google.oauth2.Credentials JSON incl. refresh token). NEVER "
                "returned to API clients and NEVER queried in SQL."
            ),
        ),
        sa.Column(
            "token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Plaintext copy of the access-token expiry, if the provider has one.",
        ),
        sa.Column(
            "needs_reconnect",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "True once the provider rejected the stored grant (revoked or "
                "expired refresh token). Avi then asks the person to reconnect "
                "instead of retrying."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp when the row was first inserted.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp of the most recent update to the row.",
        ),
        sa.UniqueConstraint(
            "person_id",
            "provider",
            name="uq_contact_provider_connections_person_provider",
        ),
        comment=(
            "A family member's grant letting Avi read and write their "
            "own personal address book (Google Contacts today; Apple "
            "Contacts is a planned provider). One row per person per "
            "provider. The credential blob is Fernet-encrypted at rest; "
            "only the account label, scopes and expiry are plaintext."
        ),
    )
    op.create_index(
        "ix_contact_provider_connections_person_id",
        "contact_provider_connections",
        ["person_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_provider_connections_person_id",
        table_name="contact_provider_connections",
    )
    op.drop_table("contact_provider_connections")
