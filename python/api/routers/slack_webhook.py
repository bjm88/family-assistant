"""Slack Events API webhook.

In the Slack app's **Event Subscriptions** page, set the Request URL to
``POST {your-public-host}/api/slack/events``. For each request we:

1. Read the raw body — the signature covers the exact bytes Slack sent.
2. Verify ``X-Slack-Signature`` against ``SLACK_APP_SIGNING_SECRET``.
   Unlike the Twilio webhook there is no unsigned dev mode: without a
   signing secret every request is refused, because anything accepted
   here can make Avi act for a family member.
3. Answer Slack's one-off ``url_verification`` handshake.
4. Hand real events to :func:`api.services.slack_inbox.process_event`
   on a worker thread and return 200 straight away. Slack re-sends any
   event it doesn't get an answer to within three seconds; the agent
   takes far longer than that.

The router is mounted at ``/api/slack`` (public, no admin auth) — Slack
needs to reach it from the open internet.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db import SessionLocal
from ..integrations import slack
from ..services import slack_inbox


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/slack", tags=["slack"])


def _process_in_background(payload: Dict[str, Any]) -> None:
    """Run the Slack pipeline with its own DB session, off the event loop.

    Same reasons as the Twilio webhook: the request-scoped session is
    gone once the response is sent, and the agent code calls
    ``asyncio.run`` which needs a thread without a running loop.
    Exceptions are logged, never raised — Slack already has its 200.
    """
    try:
        with SessionLocal() as db:
            slack_inbox.process_event(db, payload)
    except Exception:  # noqa: BLE001 - never let a background task escape
        logger.exception(
            "Slack background task crashed for event=%s", payload.get("event_id")
        )


@router.post("/events", summary="Slack Events API webhook")
async def slack_events(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Handle one Events API delivery from Slack."""
    settings = get_settings()
    body = await request.body()

    # ---- Verify the signature ------------------------------------------
    if not settings.SLACK_APP_SIGNING_SECRET:
        logger.error(
            "Slack webhook called but SLACK_APP_SIGNING_SECRET is not set — refusing."
        )
        raise HTTPException(status_code=503, detail="Slack is not configured.")
    if not slack.verify_signature(
        signing_secret=settings.SLACK_APP_SIGNING_SECRET,
        timestamp=request.headers.get("x-slack-request-timestamp"),
        body=body,
        signature=request.headers.get("x-slack-signature"),
    ):
        logger.warning("Slack webhook signature mismatch — refusing.")
        raise HTTPException(status_code=403, detail="Invalid Slack signature.")

    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body is not a JSON object.")

    # ---- One-off handshake when the Request URL is saved ----------------
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    # ---- Real events ----------------------------------------------------
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    retry = request.headers.get("x-slack-retry-num")
    logger.info(
        "Slack event id=%s type=%s subtype=%s channel_type=%s%s",
        payload.get("event_id"),
        event.get("type"),
        event.get("subtype"),
        event.get("channel_type"),
        (
            f" retry={retry} reason={request.headers.get('x-slack-retry-reason')}"
            if retry
            else ""
        ),
    )
    if payload.get("type") == "event_callback":
        background.add_task(_process_in_background, payload)
    return JSONResponse({"ok": True})
