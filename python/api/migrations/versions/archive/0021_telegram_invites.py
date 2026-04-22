"""Telegram deep-link invites: ``telegram_invites`` audit + claim table.

Why this exists
---------------
Telegram bots **cannot initiate a conversation** with a user — the
recipient must tap "Start" on the bot first. That makes "Hey Avi,
invite my wife to chat with you on Telegram" structurally impossible
to do directly. The work-around (and the standard pattern from the
Bot API docs) is to mint a one-time deep link of the form

    https://t.me/<bot_username>?start=<payload>

and deliver it through a channel we already own (SMS or email). When
the recipient taps the link, Telegram opens the bot pre-loaded with a
``/start <payload>`` message; we look the payload up in this table,
attach the inbound's ``from.id`` / ``@username`` to the matching
``people`` row, and the security gate from then on flows the same way
it does for any other registered family member.

Columns
-------
* ``payload_token`` — URL-safe random secret. UNIQUE so a leaked
  token can only be claimed once.
* ``person_id`` — who the invite is FOR. The token is bound to one
  person; whichever Telegram account first hits ``/start <token>``
  becomes that person's Telegram identity.
* ``sent_via`` / ``sent_to`` — which channel we used to deliver the
  link, plus the literal address/phone for the audit trail.
* ``created_by_person_id`` — the family member who triggered the
  invite (typically the speaker who asked Avi to send it). NULL
  when the invite was created by an automated path or by a not-yet-
  identified user.
* ``expires_at`` — hard deadline. Default is 30 days from creation.
* ``claimed_at`` / ``claimed_telegram_user_id`` /
  ``claimed_telegram_username`` — populated atomically in the
  ``/start`` handler; once set the row is "spent" and a second
  attempt to use the token is silently ignored.
* ``revoked_at`` — manual kill switch for the case where the link
  leaked and we want to disable it before it expires naturally.

Indexes
-------
* UNIQUE on ``payload_token`` enforces single-use.
* Partial-unique on ``(person_id)`` WHERE ``claimed_at IS NULL`` AND
  ``revoked_at IS NULL`` AND ``expires_at > now()`` keeps invite
  generation idempotent — re-asking Avi to invite the same person
  reuses the outstanding link instead of minting a new one.

Revision ID: 0021_telegram_invites
Revises: 0020_telegram_inbox
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0021_telegram_invites"
down_revision: Union[str, None] = "0020_telegram_inbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_invites",
        sa.Column(
            "telegram_invite_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=False,
            comment="Family this invite belongs to.",
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment=(
                "The household member the invite is FOR. Whoever first "
                "hits /start <payload> becomes this person's Telegram "
                "identity."
            ),
        ),
        sa.Column(
            "created_by_person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Family member who triggered the invite (typically the "
                "speaker who asked Avi to send it). NULL for automated "
                "or anonymous-speaker invites."
            ),
        ),
        sa.Column(
            "payload_token",
            sa.String(80),
            nullable=False,
            comment=(
                "URL-safe random secret embedded in the deep link as "
                "the ?start= parameter. The /start handler claims the "
                "row by token in a single conditional UPDATE so a "
                "race between two Telegram accounts can't both bind."
            ),
        ),
        sa.Column(
            "sent_via",
            sa.String(16),
            nullable=False,
            comment=(
                "Channel we delivered the link through — 'sms', "
                "'email', or 'manual' (operator copy/pasted)."
            ),
        ),
        sa.Column(
            "sent_to",
            sa.String(255),
            nullable=True,
            comment=(
                "Literal phone/email we delivered to, kept for the "
                "audit trail. NULL for sent_via='manual'."
            ),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Hard deadline after which the token is no longer "
                "claimable. Default 30 days from creation."
            ),
        ),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the invite was successfully claimed via /start. "
                "NULL while the invite is still outstanding."
            ),
        ),
        sa.Column(
            "claimed_telegram_user_id",
            sa.BigInteger(),
            nullable=True,
            comment=(
                "Telegram numeric user id that consumed the token. "
                "Copied to people.telegram_user_id at the same time "
                "so the security gate works on subsequent messages."
            ),
        ),
        sa.Column(
            "claimed_telegram_username",
            sa.String(64),
            nullable=True,
            comment=(
                "Telegram @username that consumed the token (without "
                "@), if they have one."
            ),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Manual kill switch for the case where a link leaked "
                "and we want to disable it before natural expiry."
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
            "payload_token",
            name="uq_telegram_invites_payload_token",
        ),
        sa.CheckConstraint(
            "sent_via IN ('sms', 'email', 'manual')",
            name="ck_telegram_invites_sent_via",
        ),
        comment=(
            "Outstanding + historical Telegram deep-link invites. The "
            "/start <payload> handler in services.telegram_inbox "
            "claims a row, copies the binding onto the people row, "
            "and from then on the standard person-lookup gate applies."
        ),
    )
    op.create_index(
        "ix_telegram_invites_family",
        "telegram_invites",
        ["family_id"],
    )
    op.create_index(
        "ix_telegram_invites_person",
        "telegram_invites",
        ["person_id"],
    )
    # Partial unique: at most ONE active (unclaimed AND unrevoked) row
    # per person. Lets the invite tool be idempotent — re-asking Avi
    # to invite the same person reuses the existing token rather than
    # minting a fresh one. We deliberately DON'T include
    # ``expires_at > now()`` in the predicate because PostgreSQL
    # forbids non-IMMUTABLE functions in index predicates; expiry is
    # checked at application level (and an expired-but-active row is
    # just refreshed with a new ``expires_at`` rather than replaced).
    op.create_index(
        "uq_telegram_invites_active_per_person",
        "telegram_invites",
        ["person_id"],
        unique=True,
        postgresql_where=sa.text(
            "claimed_at IS NULL AND revoked_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_telegram_invites_active_per_person",
        table_name="telegram_invites",
    )
    op.drop_index("ix_telegram_invites_person", table_name="telegram_invites")
    op.drop_index("ix_telegram_invites_family", table_name="telegram_invites")
    op.drop_table("telegram_invites")
