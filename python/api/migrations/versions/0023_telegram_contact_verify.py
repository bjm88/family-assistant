"""Telegram contact-share two-factor verification: ``telegram_contact_verifications``.

Why this exists
---------------
The Telegram Bot API delivers a sender's phone number ONLY when the
user taps a ``request_contact`` keyboard button. The number arrives
inside ``message.contact.phone_number`` — but that field is supplied
by the user's Telegram client, not cryptographically signed by
Telegram's servers. Official clients refuse to share anything other
than the user's own SMS-verified phone, but a custom MTProto client
can forge any string in there. So binding ``Person.telegram_user_id``
straight off a contact share trusts a user-controlled value, and an
attacker who knows a household member's mobile number could
impersonate them with a hand-rolled client.

The fix is a second factor over a channel we already own end-to-end:
Twilio SMS. When a contact share names a phone that matches a
``Person`` row, we

1. mint a 6-digit numeric code,
2. store its SHA-256 hash + a short expiry in this table, keyed on
   the requesting Telegram chat,
3. text the cleartext code to the matched ``Person.mobile_phone_number``
   via Twilio,
4. ask the Telegram user to paste it back into the bot chat.

Only when they reply with the correct code (within the TTL and the
attempt budget) do we actually bind ``Person.telegram_user_id``.
That proves they control BOTH the Telegram account AND the SMS-
receiving phone — the same property the deep-link invite path
provides without needing the household admin in the loop.

Columns
-------
* ``code_hash`` — ``sha256(code)`` hex digest. Cleartext is never
  persisted; even an operator with read access to the DB can't
  bypass the challenge by reading the row.
* ``attempts`` / ``max_attempts`` — bounded brute-force budget.
  Default is 5 wrong guesses against a 10**6 keyspace, i.e. a
  5e-6 success rate per challenge — far below "interesting".
* ``expires_at`` — hard wall-clock deadline. Default 10 min, plenty
  for "open SMS app, copy code, switch to Telegram".
* ``claimed_at`` — set when the correct code arrives; flips the row
  permanently spent.
* ``revoked_at`` — set if the user re-shares contact (we kill the
  old challenge so there's only ever one outstanding) or if the
  attempt budget runs out.
* ``twilio_message_sid`` — audit pointer so the operator can
  cross-reference Twilio delivery logs when a user reports
  "I never got the code".

Indexes
-------
* Partial UNIQUE on ``(telegram_chat_id)`` WHERE
  ``claimed_at IS NULL AND revoked_at IS NULL`` so initiating a new
  verification while one is in flight is a deterministic
  "revoke-then-insert" rather than a pile of competing rows.

Revision ID: 0023_telegram_contact_verify
Revises: 0022_telegram_contact_prompt
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023_telegram_contact_verify"
down_revision: Union[str, None] = "0022_telegram_contact_prompt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_contact_verifications",
        sa.Column(
            "telegram_contact_verification_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Family the matched Person belongs to.",
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "Household member the verification will bind on success. "
                "Resolved at challenge time by phone match against "
                "people.{mobile,home,work}_phone_number."
            ),
        ),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=False,
            comment=(
                "Telegram numeric id of the account that requested "
                "verification (message.from.id). Required so a third "
                "party can't slip in mid-flow and consume the code."
            ),
        ),
        sa.Column(
            "telegram_username",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Snapshot of the requester's @username at challenge "
                "time. Audit only — does not gate code consumption."
            ),
        ),
        sa.Column(
            "telegram_chat_id",
            sa.BigInteger(),
            nullable=False,
            comment=(
                "Telegram chat the verification flow is happening in. "
                "Always private (we never initiate verification in "
                "groups). Used as the partial-unique key so only one "
                "challenge can be outstanding per chat."
            ),
        ),
        sa.Column(
            "phone_normalised",
            sa.String(length=40),
            nullable=False,
            comment=(
                "E.164-normalised form of the phone number the contact "
                "share named. Stored verbatim for audit; the actual "
                "binding decision used api.utils.phone.normalize_phone "
                "to compare against Person rows."
            ),
        ),
        sa.Column(
            "code_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 hex digest of the 6-digit verification code. "
                "Cleartext is never persisted; the hash is only useful "
                "to an attacker who can also brute-force 10**6 codes "
                "AND beat the attempt budget."
            ),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Number of wrong codes the user has submitted so far.",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="5",
            comment=(
                "Hard ceiling on wrong guesses. When attempts reaches "
                "this number we set revoked_at and tell the user to "
                "restart by sharing contact again."
            ),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Wall-clock deadline. After this moment the row is "
                "treated as revoked even without revoked_at being set."
            ),
        ),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Timestamp the correct code arrived. Setting this flips "
                "the row permanently spent and triggers the actual "
                "Person.telegram_user_id binding."
            ),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when the user starts a new contact share (we kill "
                "the old in-flight challenge) or the attempt budget "
                "runs out. Either way the row is no longer claimable."
            ),
        ),
        sa.Column(
            "twilio_message_sid",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Twilio MessageSid for the SMS that delivered the code "
                "— kept so an operator can cross-reference delivery "
                "logs if the user complains they never got the text."
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
        comment=(
            "Outstanding + historical SMS verification challenges for "
            "the Telegram contact-share auto-link flow. A row gets "
            "claimed_at iff the user echoed the texted code back into "
            "Telegram inside the TTL/attempt budget; only then does "
            "Person.telegram_user_id get bound."
        ),
    )

    op.create_index(
        "ix_telegram_contact_verify_family",
        "telegram_contact_verifications",
        ["family_id"],
    )
    op.create_index(
        "ix_telegram_contact_verify_person",
        "telegram_contact_verifications",
        ["person_id"],
    )
    # One outstanding challenge per chat. Initiating a new one while
    # the old is in flight is a "revoke-then-insert" handled by the
    # service; this constraint catches programmer mistakes that would
    # otherwise leave duplicate live rows.
    op.create_index(
        "uq_telegram_contact_verify_active_per_chat",
        "telegram_contact_verifications",
        ["telegram_chat_id"],
        unique=True,
        postgresql_where=sa.text(
            "claimed_at IS NULL AND revoked_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_telegram_contact_verify_active_per_chat",
        table_name="telegram_contact_verifications",
    )
    op.drop_index(
        "ix_telegram_contact_verify_person",
        table_name="telegram_contact_verifications",
    )
    op.drop_index(
        "ix_telegram_contact_verify_family",
        table_name="telegram_contact_verifications",
    )
    op.drop_table("telegram_contact_verifications")
