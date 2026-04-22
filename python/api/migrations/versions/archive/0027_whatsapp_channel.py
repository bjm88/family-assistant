"""Add WhatsApp as a sibling channel of SMS in the Twilio inbox.

Twilio's WhatsApp surface is the same Programmable Messaging API as
SMS — same webhook payload (just with a ``whatsapp:`` prefix on
``From`` / ``To``), same ``MessageSid`` namespace, same Basic-auth
credentials, same media URLs. So instead of standing up a parallel
``whatsapp_inbox_messages`` table we widen the existing
``sms_inbox_messages`` to also hold WhatsApp rows by adding a
``channel`` column.

Migration steps
---------------
1. Add ``sms_inbox_messages.channel`` (NOT NULL, default 'sms', CHECK
   in ('sms', 'whatsapp')). Backfilling existing rows with 'sms' is
   trivially correct because every existing row predates the WhatsApp
   sender.
2. Add a partial index on ``(channel, family_id, processed_at)`` so the
   future admin "WhatsApp inbox" tab can list rows fast without
   scanning the full SMS history.
3. Extend ``ck_live_sessions_source`` so a live session can be opened
   with ``source='whatsapp'``. WhatsApp threads accrete their own
   transcript keyed on the counterparty's E.164 phone (same
   ``external_thread_id`` reuse as SMS) — separating by ``source``
   keeps SMS and WhatsApp conversations from accidentally bleeding
   into each other if the same person reaches us on both surfaces.

Downgrade reverses both: shrink the live-session constraint back to
the (live, email, sms, telegram) set, drop the channel column.
NOTE: any 'whatsapp' rows would block the live-session constraint
shrink, so the downgrade explicitly fails closed if WhatsApp data is
present rather than silently corrupting history.

Revision ID: 0027_whatsapp_channel
Revises: 0026_jobs
Create Date: 2026-04-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0027_whatsapp_channel"
down_revision: Union[str, None] = "0026_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- 1. sms_inbox_messages.channel ---------------------------------
    op.add_column(
        "sms_inbox_messages",
        sa.Column(
            "channel",
            sa.String(length=20),
            nullable=False,
            server_default="sms",
            comment=(
                "Twilio Programmable Messaging surface this row arrived "
                "on. 'sms' for plain SMS / MMS via the standard "
                "TWILIO_PRIMARY_PHONE; 'whatsapp' for WhatsApp via the "
                "TWILIO_WHATSAPP_SENDER_NUMBER sender. Drives the choice "
                "of outbound sender, reply length cap, and live-session "
                "source so SMS and WhatsApp threads never collide."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_sms_inbox_messages_channel",
        "sms_inbox_messages",
        "channel IN ('sms', 'whatsapp')",
    )
    op.create_index(
        "ix_sms_inbox_messages_channel_family_processed",
        "sms_inbox_messages",
        ["channel", "family_id", "processed_at"],
    )

    # ---- 2. live_sessions source -- include 'whatsapp' -----------------
    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email', 'sms', 'telegram', 'whatsapp')",
    )


def downgrade() -> None:
    # Refuse to shrink the live-session constraint if WhatsApp threads
    # exist — silently dropping them would corrupt the transcript view.
    bind = op.get_bind()
    whatsapp_session_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM live_sessions WHERE source = 'whatsapp'"
        )
    ).scalar_one()
    if whatsapp_session_count:
        raise RuntimeError(
            f"Refusing to downgrade 0027_whatsapp_channel: "
            f"{whatsapp_session_count} live_sessions row(s) still have "
            "source='whatsapp'. Reassign or delete them first."
        )

    # Same guard for the inbox table: WhatsApp audit rows would violate
    # the about-to-shrink CHECK on `channel`, so refuse if any survive.
    whatsapp_inbox_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM sms_inbox_messages WHERE channel = 'whatsapp'"
        )
    ).scalar_one()
    if whatsapp_inbox_count:
        raise RuntimeError(
            f"Refusing to downgrade 0027_whatsapp_channel: "
            f"{whatsapp_inbox_count} sms_inbox_messages row(s) still "
            "have channel='whatsapp'. Delete them first."
        )

    op.drop_constraint("ck_live_sessions_source", "live_sessions", type_="check")
    op.create_check_constraint(
        "ck_live_sessions_source",
        "live_sessions",
        "source IN ('live', 'email', 'sms', 'telegram')",
    )

    op.drop_index(
        "ix_sms_inbox_messages_channel_family_processed",
        table_name="sms_inbox_messages",
    )
    op.drop_constraint(
        "ck_sms_inbox_messages_channel", "sms_inbox_messages", type_="check"
    )
    op.drop_column("sms_inbox_messages", "channel")
