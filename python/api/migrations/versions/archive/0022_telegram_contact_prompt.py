"""Telegram inbox: add ``prompted_for_contact_share`` status verdict.

Why this exists
---------------
The Telegram Bot API never exposes a sender's phone number on its own
— it only arrives if the user explicitly taps a ``KeyboardButton``
with ``request_contact=true`` and confirms the share dialog. To make
Avi able to auto-link an unrecognised sender to their Person row
without forcing the household admin to hand-craft an invite first, we
now reply to first-time strangers with exactly such a button.

Each time we send that prompt we want to write an audit row so the
operator can see "Avi did ask Sarah to share her number, she just
hasn't tapped yet" — and so the per-chat cooldown can be expressed
as a SQL query against the audit trail. The existing CHECK constraint
on ``telegram_inbox_messages.status`` enumerates every legitimate
verdict, so we extend it here with one new value:

* ``prompted_for_contact_share`` — Avi sent a ``request_contact`` reply
  to an otherwise-unknown sender. ``status_reason`` carries the chat
  id and the cooldown horizon for cross-reference.

No data backfill is needed; old rows stay on their existing verdict.

Revision ID: 0022_telegram_contact_prompt
Revises: 0021_telegram_invites
Create Date: 2026-04-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0022_telegram_contact_prompt"
down_revision: Union[str, None] = "0021_telegram_invites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Postgres CHECK constraints aren't directly mutable — we drop and
# re-create. The constraint name and column list MUST stay in lock-
# step with the model in ``api.models.telegram_inbox_message``.
_CONSTRAINT_NAME = "ck_telegram_inbox_messages_status"

_OLD_VALUES = (
    "processed_replied",
    "ignored_unknown_sender",
    "ignored_self",
    "ignored_non_message",
    "ignored_already_seen",
    "failed",
)

_NEW_VALUES = _OLD_VALUES + ("prompted_for_contact_share",)


def _values_clause(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint(
        _CONSTRAINT_NAME,
        "telegram_inbox_messages",
        type_="check",
    )
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "telegram_inbox_messages",
        f"status IN ({_values_clause(_NEW_VALUES)})",
    )


def downgrade() -> None:
    # Roll any rows on the new verdict back to the closest legacy
    # verdict so the tightened constraint won't reject them. We treat
    # an outstanding contact-share prompt as a flavour of
    # ``ignored_unknown_sender`` for the purposes of the old schema —
    # they're both "we did not run the agent for this sender".
    op.execute(
        "UPDATE telegram_inbox_messages "
        "SET status = 'ignored_unknown_sender' "
        "WHERE status = 'prompted_for_contact_share'"
    )
    op.drop_constraint(
        _CONSTRAINT_NAME,
        "telegram_inbox_messages",
        type_="check",
    )
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "telegram_inbox_messages",
        f"status IN ({_values_clause(_OLD_VALUES)})",
    )
