"""Per-person SMS opt-out flag + inbox status for opted-out inbound.

Adds ``people.sms_opted_out`` / ``people.sms_opted_out_at`` so a
STOP / OPTOUT / CANCEL / END / QUIT / UNSUBSCRIBE / REVOKE / STOPALL
from a rostered phone is remembered: Avi must not send that person
outbound SMS or reply to later texts. JOIN / SUBSCRIBE clears the
flag.

Also widens ``sms_inbox_messages.status`` with ``ignored_opted_out``
so a later inbound from an opted-out person is auditable separately
from the keyword itself (``ignored_stop``).

Revision ID: 0004_person_sms_opted_out
Revises: 0003_email_attachment_id_text
Create Date: 2026-08-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_person_sms_opted_out"
down_revision: Union[str, None] = "0003_email_attachment_id_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_STATUSES = (
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_stop",
    "ignored_already_seen",
    "failed",
)
_NEW_STATUSES = _OLD_STATUSES + ("ignored_opted_out",)


def _status_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(repr(v) for v in values)
    return f"status IN ({quoted})"


def upgrade() -> None:
    op.add_column(
        "people",
        sa.Column(
            "sms_opted_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "When true, this person has opted out of SMS. Avi must "
                "not send them outbound texts or reply to their inbound "
                "SMS. Cleared by JOIN / SUBSCRIBE."
            ),
        ),
    )
    op.add_column(
        "people",
        sa.Column(
            "sms_opted_out_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When sms_opted_out last flipped to true.",
        ),
    )

    op.drop_constraint(
        "ck_sms_inbox_messages_status",
        "sms_inbox_messages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_sms_inbox_messages_status",
        "sms_inbox_messages",
        _status_sql(_NEW_STATUSES),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_sms_inbox_messages_status",
        "sms_inbox_messages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_sms_inbox_messages_status",
        "sms_inbox_messages",
        _status_sql(_OLD_STATUSES),
    )
    op.drop_column("people", "sms_opted_out_at")
    op.drop_column("people", "sms_opted_out")
