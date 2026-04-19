"""Telegram inbox plumbing: ``live_sessions.source='telegram'`` + audit tables.

Mirrors what 0019_sms_inbox did for Twilio SMS, but for inbound
Telegram messages routed through a Bot API long-poll loop:

1. Extends the ``ck_live_sessions_source`` check constraint so a live
   session can be opened with ``source='telegram'``. The existing
   ``external_thread_id`` column is reused — for Telegram we store the
   counterparty's chat id (as a stringified int) there so a
   back-and-forth thread accretes into a single session row + transcript
   exactly the way email and SMS threads do.

2. Adds two opt-in lookup columns to ``people``:

   * ``telegram_user_id`` (BigInteger) — the unique numeric Telegram
     user id (``message.from.id``). This is the strongest signal we
     can match on because Telegram usernames are mutable but the
     numeric id never changes.
   * ``telegram_username`` (String, no leading ``@``) — fallback
     identifier for the case where the admin only knows the family
     member's handle and hasn't captured the numeric id yet. Matched
     case-insensitively.

   Both are indexed so the per-update lookup is O(log n) instead of
   a full table scan, and both default to NULL so a Telegram message
   from a stranger silently fails the lookup gate the same way an
   unknown phone number does on the SMS path.

3. Adds the ``telegram_inbox_messages`` audit table — one row per
   inbound Bot API ``update``, with the same explicit security
   verdict pattern as ``email_inbox_messages`` / ``sms_inbox_messages``
   so it's trivial to ask "did Avi reply to that message?" / "why
   was that stranger ignored?" without re-fetching anything from
   Telegram.

4. Adds ``telegram_inbox_attachments`` for photos / documents / voice.
   Telegram file URLs are short-lived (``getFile`` returns a path that
   the Bot API can serve for ~60 minutes), so we copy each attachment
   to ``FA_STORAGE_ROOT`` immediately and store the relative path
   here. Files live under
   ``family_<id>/telegram/<telegram_inbox_message_id>/`` so deleting
   the audit row's directory cleans up its media in one ``rm -rf``.

Revision ID: 0020_telegram_inbox
Revises: 0019_sms_inbox
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0020_telegram_inbox"
down_revision: Union[str, None] = "0019_sms_inbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TELEGRAM_STATUSES = (
    "('processed_replied', 'ignored_unknown_sender', "
    "'ignored_self', 'ignored_non_message', 'ignored_already_seen', "
    "'failed')"
)


def upgrade() -> None:
    # ---- Extend live_sessions.source check ------------------------------
    # Postgres can't ALTER an existing CHECK in place; drop and recreate.
    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email', 'sms', 'telegram')",
    )

    # ---- people.telegram_user_id + people.telegram_username -------------
    op.add_column(
        "people",
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=True,
            comment=(
                "Telegram numeric user id (message.from.id from the Bot "
                "API). Stable for the lifetime of the account — unlike "
                "telegram_username, which the user can change at any "
                "time. The Telegram inbox poller looks this up first "
                "to decide whether to reply."
            ),
        ),
    )
    op.add_column(
        "people",
        sa.Column(
            "telegram_username",
            sa.String(64),
            nullable=True,
            comment=(
                "Telegram @username (without the leading @). Fallback "
                "lookup key when telegram_user_id has not yet been "
                "captured. Matched case-insensitively."
            ),
        ),
    )
    op.create_index(
        "ix_people_telegram_user_id",
        "people",
        ["telegram_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_people_telegram_username_lower",
        "people",
        [sa.text("lower(telegram_username)")],
        unique=False,
    )

    # ---- telegram_inbox_messages ---------------------------------------
    op.create_table(
        "telegram_inbox_messages",
        sa.Column(
            "telegram_inbox_message_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "family_id",
            sa.Integer(),
            sa.ForeignKey("families.family_id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "Family the inbound user resolved to (via the matched "
                "person). NULL for ignored_unknown_sender / failed rows "
                "that arrive before we know whose family it is."
            ),
        ),
        sa.Column(
            "telegram_update_id",
            sa.BigInteger(),
            nullable=False,
            comment=(
                "Bot API update_id for this inbound message. Used as "
                "the dedup key so a re-delivered update never spawns "
                "a second agent run, and as the high-water mark for "
                "the long-poll offset."
            ),
        ),
        sa.Column(
            "telegram_chat_id",
            sa.BigInteger(),
            nullable=False,
            comment=(
                "Telegram chat id the message arrived in (== from.id "
                "for private chats). Used as the reply destination "
                "and as the live_sessions.external_thread_id key."
            ),
        ),
        sa.Column(
            "telegram_message_id",
            sa.BigInteger(),
            nullable=False,
            comment=(
                "Per-chat message id (message.message_id). Combined "
                "with telegram_chat_id, this is what reply_to_message_id "
                "uses to thread our reply under the inbound."
            ),
        ),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=True,
            comment=(
                "Numeric id of the sender (message.from.id). Compared "
                "against people.telegram_user_id to decide whether to "
                "reply. NULL only for channel posts that have no from."
            ),
        ),
        sa.Column(
            "telegram_username",
            sa.String(64),
            nullable=True,
            comment="Sender's @username (without @), if they have one.",
        ),
        sa.Column(
            "sender_display_name",
            sa.String(255),
            nullable=True,
            comment=(
                "Best-effort 'first_name last_name' assembly from the "
                "Bot API for the audit trail."
            ),
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=True,
            comment=(
                "Verbatim message text (or the caption when the message "
                "is a photo / video / document). NULL for messages that "
                "are media-only with no caption."
            ),
        ),
        sa.Column(
            "num_media",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment=(
                "Count of media files attached to this message — every "
                "one gets a telegram_inbox_attachments row."
            ),
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Family member whose Telegram identity matched "
                "telegram_user_id (or telegram_username). NULL for "
                "ignored senders — kept that way on purpose so the "
                "audit row survives the person being deleted later."
            ),
        ),
        sa.Column(
            "status",
            sa.String(40),
            nullable=False,
            comment=(
                "Outcome verdict. 'processed_replied' = sent a reply, "
                "'ignored_unknown_sender' = no person matched, "
                "'ignored_self' = the message came from our own bot "
                "(loopback), 'ignored_non_message' = the update wasn't "
                "a normal text/media message (edits, channel posts, "
                "join events, …), 'ignored_already_seen' = dedup hit, "
                "'failed' = wanted to reply but the agent loop or "
                "sendMessage call blew up (status_reason has detail)."
            ),
        ),
        sa.Column(
            "status_reason",
            sa.Text(),
            nullable=True,
            comment="Human-readable detail for status (error, etc.).",
        ),
        sa.Column(
            "reply_telegram_message_id",
            sa.BigInteger(),
            nullable=True,
            comment=(
                "Telegram message_id of the reply Avi sent, when "
                "status='processed_replied'."
            ),
        ),
        sa.Column(
            "agent_task_id",
            sa.Integer(),
            sa.ForeignKey("agent_tasks.agent_task_id", ondelete="SET NULL"),
            nullable=True,
            comment="Audit link to the agent task that drafted the reply.",
        ),
        sa.Column(
            "live_session_id",
            sa.Integer(),
            sa.ForeignKey("live_sessions.live_session_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Live session row that holds the inbound + reply "
                "transcript for this Telegram thread."
            ),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Wall-clock time the poller pulled this update off the "
                "Bot API."
            ),
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the poller finished writing this row.",
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
            "telegram_update_id",
            name="uq_telegram_inbox_update_id",
        ),
        sa.CheckConstraint(
            f"status IN {_TELEGRAM_STATUSES}",
            name="ck_telegram_inbox_messages_status",
        ),
        comment=(
            "One row per inbound Telegram update Avi inspected. Includes "
            "the security verdict so the family can audit exactly which "
            "messages got a reply, which were ignored, and why."
        ),
    )
    op.create_index(
        "ix_telegram_inbox_messages_family_processed",
        "telegram_inbox_messages",
        ["family_id", "processed_at"],
    )
    op.create_index(
        "ix_telegram_inbox_messages_chat",
        "telegram_inbox_messages",
        ["telegram_chat_id"],
    )

    # ---- telegram_inbox_attachments ------------------------------------
    op.create_table(
        "telegram_inbox_attachments",
        sa.Column(
            "telegram_inbox_attachment_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "telegram_inbox_message_id",
            sa.Integer(),
            sa.ForeignKey(
                "telegram_inbox_messages.telegram_inbox_message_id",
                ondelete="CASCADE",
            ),
            nullable=False,
            comment="The Telegram message this attachment belongs to.",
        ),
        sa.Column(
            "media_index",
            sa.Integer(),
            nullable=False,
            comment=(
                "0-based slot inside the inbound message — kept so the "
                "original ordering is recoverable."
            ),
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment=(
                "Telegram attachment family — one of 'photo', 'document', "
                "'voice', 'audio', 'video', 'sticker', 'animation'."
            ),
        ),
        sa.Column(
            "telegram_file_id",
            sa.String(255),
            nullable=False,
            comment=(
                "Bot API file_id used with getFile to download the "
                "binary. Stored for forensic purposes — the resolved "
                "URL itself stops working ~60 minutes after delivery."
            ),
        ),
        sa.Column(
            "mime_type",
            sa.String(120),
            nullable=False,
            comment="Content-Type Telegram reported (image/jpeg, etc.).",
        ),
        sa.Column(
            "file_size_bytes",
            sa.BigInteger(),
            nullable=False,
            comment="Bytes written to disk.",
        ),
        sa.Column(
            "stored_path",
            sa.Text(),
            nullable=False,
            comment=(
                "Path RELATIVE to FA_STORAGE_ROOT, e.g. "
                "'family_2/telegram/17/<uuid>.jpg'. Goes through "
                "storage.absolute_path() to serve."
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
            "telegram_inbox_message_id",
            "media_index",
            name="uq_telegram_inbox_attachment_slot",
        ),
        comment=(
            "Files attached to an inbound Telegram message, copied off "
            "the Bot API onto local storage so we have a permanent copy."
        ),
    )


def downgrade() -> None:
    op.drop_table("telegram_inbox_attachments")
    op.drop_index(
        "ix_telegram_inbox_messages_chat",
        table_name="telegram_inbox_messages",
    )
    op.drop_index(
        "ix_telegram_inbox_messages_family_processed",
        table_name="telegram_inbox_messages",
    )
    op.drop_table("telegram_inbox_messages")
    op.drop_index(
        "ix_people_telegram_username_lower",
        table_name="people",
    )
    op.drop_index(
        "ix_people_telegram_user_id",
        table_name="people",
    )
    op.drop_column("people", "telegram_username")
    op.drop_column("people", "telegram_user_id")
    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email', 'sms')",
    )
