"""Add ``google_oauth_credentials`` table for Avi's Gmail + Calendar.

One row per assistant. Stores a Fernet-encrypted JSON blob of the full
``google.oauth2.Credentials`` payload, plus three plaintext columns
(``granted_email``, ``scopes``, ``token_expires_at``) so the admin UI
and a SQL-aware LLM can answer "is Avi connected?" without touching
ciphertext.

Revision ID: 0014_google_oauth_credentials
Revises: 0013_medical_records
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014_google_oauth_credentials"
down_revision: Union[str, None] = "0013_medical_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "google_oauth_credentials",
        sa.Column(
            "google_oauth_credential_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.assistant_id", ondelete="CASCADE"),
            nullable=False,
            comment="Which assistant owns this Google account.",
        ),
        sa.Column(
            "granted_email",
            sa.String(length=255),
            nullable=False,
            comment=(
                "Email address Google authenticated. Compare with "
                "assistants.email_address — they should match."
            ),
        ),
        sa.Column(
            "scopes",
            sa.Text(),
            nullable=False,
            comment=(
                "Space-separated OAuth scopes the user actually granted."
            ),
        ),
        sa.Column(
            "token_payload_encrypted",
            sa.LargeBinary(),
            nullable=False,
            comment=(
                "Fernet-encrypted JSON of the google.oauth2.Credentials "
                "object. NEVER returned to the API client and NEVER "
                "queried in SQL."
            ),
        ),
        sa.Column(
            "token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Plaintext copy of the access-token expiry so the UI "
                "can warn about staleness without decryption."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "assistant_id", name="uq_google_oauth_credentials_per_assistant"
        ),
        comment=(
            "OAuth credentials linking the family assistant (Avi) to a "
            "Google account. One row per assistant. The token blob is "
            "Fernet-encrypted at rest."
        ),
    )


def downgrade() -> None:
    op.drop_table("google_oauth_credentials")
