"""SMS inbox plumbing: ``live_sessions.source='sms'`` + ``sms_inbox_messages``.

Mirrors what 0016_email_inbox did for Gmail, but for inbound Twilio
SMS / MMS:

1. Extends the ``ck_live_sessions_source`` check constraint so a live
   session can be opened with ``source='sms'``. The existing
   ``external_thread_id`` column is reused — for SMS we store the
   counterparty's E.164 phone number there so a back-and-forth thread
   accretes into a single session row + transcript exactly the way
   email threads do.

2. Adds the ``sms_inbox_messages`` audit table — one row per inbound
   Twilio webhook call, with the same explicit security verdict
   pattern as ``email_inbox_messages`` so it's trivial to ask "did
   Avi reply to that text?" / "why was that stranger ignored?"
   without re-fetching anything from Twilio.

3. Adds ``sms_inbox_attachments`` for MMS media. Twilio inbound media
   URLs only stay live while the message is queued, so we copy each
   attachment to ``FA_STORAGE_ROOT`` immediately and store the
   relative path here. Files live under
   ``family_<id>/sms/<sms_inbox_message_id>/`` so deleting the audit
   row's directory cleans up its media in one ``rm -rf``.

Revision ID: 0019_sms_inbox
Revises: 0018_tasks
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0019_sms_inbox"
down_revision: Union[str, None] = "0018_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SMS_STATUSES = (
    "('processed_replied', 'ignored_unknown_sender', "
    "'ignored_self', 'ignored_stop', 'ignored_already_seen', "
    "'failed')"
)


def upgrade() -> None:
    # ---- Extend live_sessions.source check ------------------------------
    # Postgres can't ALTER an existing CHECK in place; drop and recreate.
    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email', 'sms')",
    )

    # ---- sms_inbox_messages --------------------------------------------
    op.create_table(
        "sms_inbox_messages",
        sa.Column(
            "sms_inbox_message_id",
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
                "Family the inbound number resolved to (via the matched "
                "person). NULL for ignored_unknown_sender / failed rows "
                "that arrive before we know whose family it is."
            ),
        ),
        sa.Column(
            "twilio_message_sid",
            sa.String(64),
            nullable=False,
            comment=(
                "Twilio's stable id (MessageSid) for the inbound message. "
                "Used as the dedup key so a re-delivered webhook never "
                "spawns a second agent run."
            ),
        ),
        sa.Column(
            "twilio_messaging_service_sid",
            sa.String(64),
            nullable=True,
            comment=(
                "MessagingServiceSid (MGxxx…) when the message arrived "
                "through a Messaging Service rather than a single number."
            ),
        ),
        sa.Column(
            "from_phone",
            sa.String(40),
            nullable=False,
            comment=(
                "Sender phone number in E.164 (e.g. +14155551234). "
                "Compared against people.{mobile,home,work}_phone_number "
                "to decide whether to reply."
            ),
        ),
        sa.Column(
            "to_phone",
            sa.String(40),
            nullable=False,
            comment=(
                "Twilio number that received the message (E.164). Lets "
                "an operator answer 'which line did this come in on?' "
                "if multiple Twilio numbers are ever wired up."
            ),
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=True,
            comment=(
                "Verbatim text body. SMS is capped at ~1.6 KB by the "
                "carrier so the whole message is safe to store."
            ),
        ),
        sa.Column(
            "num_media",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment=(
                "Twilio NumMedia field — count of MMS attachments. "
                "Each one gets its own sms_inbox_attachments row."
            ),
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "Family member whose phone number matched from_phone. "
                "NULL for ignored senders — kept that way on purpose so "
                "the audit row survives the person being deleted later."
            ),
        ),
        sa.Column(
            "status",
            sa.String(40),
            nullable=False,
            comment=(
                "Outcome verdict. 'processed_replied' = sent a reply, "
                "'ignored_unknown_sender' = no person matched, "
                "'ignored_self' = the from_phone matched our own Twilio "
                "number (loopback), 'ignored_stop' = STOP/UNSUBSCRIBE "
                "keyword received, 'ignored_already_seen' = dedup hit, "
                "'failed' = wanted to reply but the agent loop or "
                "Twilio send blew up (status_reason has detail)."
            ),
        ),
        sa.Column(
            "status_reason",
            sa.Text(),
            nullable=True,
            comment="Human-readable detail for status (error, etc.).",
        ),
        sa.Column(
            "reply_message_sid",
            sa.String(64),
            nullable=True,
            comment=(
                "Twilio MessageSid of the reply Avi sent, when "
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
                "transcript for this SMS thread."
            ),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "Wall-clock time the webhook hit our server (Twilio "
                "doesn't include a per-message timestamp in the form "
                "post; we record arrival time instead)."
            ),
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="When the webhook handler finished writing this row.",
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
            "twilio_message_sid",
            name="uq_sms_inbox_twilio_message_sid",
        ),
        sa.CheckConstraint(
            f"status IN {_SMS_STATUSES}",
            name="ck_sms_inbox_messages_status",
        ),
        comment=(
            "One row per inbound Twilio SMS/MMS Avi inspected. Includes "
            "the security verdict so the family can audit exactly which "
            "messages got a reply, which were ignored, and why."
        ),
    )
    op.create_index(
        "ix_sms_inbox_messages_family_processed",
        "sms_inbox_messages",
        ["family_id", "processed_at"],
    )
    op.create_index(
        "ix_sms_inbox_messages_from_phone",
        "sms_inbox_messages",
        ["from_phone"],
    )

    # ---- sms_inbox_attachments -----------------------------------------
    op.create_table(
        "sms_inbox_attachments",
        sa.Column(
            "sms_inbox_attachment_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "sms_inbox_message_id",
            sa.Integer(),
            sa.ForeignKey(
                "sms_inbox_messages.sms_inbox_message_id", ondelete="CASCADE"
            ),
            nullable=False,
            comment="The SMS row this MMS attachment belongs to.",
        ),
        sa.Column(
            "media_index",
            sa.Integer(),
            nullable=False,
            comment=(
                "0-based slot from Twilio's MediaUrl0…MediaUrl9 — kept so "
                "the original ordering is recoverable."
            ),
        ),
        sa.Column(
            "twilio_media_url",
            sa.Text(),
            nullable=False,
            comment=(
                "Original Twilio Media URL the file was downloaded from. "
                "Stored for forensic purposes — the URL itself stops "
                "working a few minutes after delivery."
            ),
        ),
        sa.Column(
            "mime_type",
            sa.String(120),
            nullable=False,
            comment="Content-Type Twilio reported (image/jpeg, etc.).",
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
                "'family_2/sms/17/<uuid>.jpg'. Goes through "
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
            "sms_inbox_message_id",
            "media_index",
            name="uq_sms_inbox_attachment_slot",
        ),
        comment=(
            "MMS media files attached to an inbound SMS, copied off "
            "Twilio onto local storage so we have a permanent copy."
        ),
    )


def downgrade() -> None:
    op.drop_table("sms_inbox_attachments")
    op.drop_index(
        "ix_sms_inbox_messages_from_phone",
        table_name="sms_inbox_messages",
    )
    op.drop_index(
        "ix_sms_inbox_messages_family_processed",
        table_name="sms_inbox_messages",
    )
    op.drop_table("sms_inbox_messages")
    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email')",
    )
