"""``gmail_send`` — outbound email through the assistant's Gmail OAuth.

Telegram invites have their own module (:mod:`telegram_invite`) because
they're a stateful onboarding flow rather than a fire-and-forget send.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from ....integrations import google_oauth
from ....integrations.gmail import GmailSendError, send_email
from .._registry import ToolContext, ToolError, ToolResult


GMAIL_SEND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient email address (one).",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": (
                "Plain-text email body. Sign off naturally as the assistant; "
                "do not include the recipient's name in the signature."
            ),
        },
    },
    "required": ["to", "subject", "body"],
}


async def handle_gmail_send(
    ctx: ToolContext, to: str, subject: str, body: str
) -> ToolResult:
    if ctx.assistant_id is None:
        raise ToolError(
            "No assistant is configured for this family — connect one in the admin UI."
        )
    try:
        _row, creds = google_oauth.load_credentials(ctx.db, ctx.assistant_id)
    except google_oauth.GoogleNotConnected as e:
        raise ToolError(str(e)) from e
    except google_oauth.GoogleOAuthError as e:
        raise ToolError(f"Google auth error: {e}") from e

    try:
        message_id = await asyncio.to_thread(
            send_email, creds, to=to, subject=subject, body=body
        )
    except GmailSendError as e:
        raise ToolError(str(e)) from e

    return ToolResult(
        ok=True,
        output={"message_id": message_id, "to": to, "subject": subject},
        summary=f"Sent “{subject}” to {to}",
    )
