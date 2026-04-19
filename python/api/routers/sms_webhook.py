"""Twilio inbound-SMS webhook.

Configure your Twilio phone number's "A MESSAGE COMES IN" URL to
``POST {your-public-host}/api/sms/twilio/inbound``. We:

1. Read the raw form body (Twilio sends ``application/x-www-form-urlencoded``).
2. Verify the ``X-Twilio-Signature`` header against ``TWILIO_AUTH_TOKEN``
   so a forged form post never reaches the agent loop.
3. Hand the parsed message off to :func:`api.services.sms_inbox.process_inbound_sms`
   which does dedup, person lookup, session bookkeeping, agent
   dispatch, and reply-send.
4. Always return empty TwiML — the actual reply is sent asynchronously
   via Twilio's REST API so we don't have to keep the inbound HTTP
   socket open for the 5-30 s the agent needs.

The router is mounted at ``/api/sms`` (public, no admin auth) — Twilio
needs to be able to hit it from the open internet.
"""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from ..config import get_settings
from ..db import SessionLocal
from ..integrations import twilio_sms
from ..services import sms_inbox


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/sms", tags=["sms"])


def _public_url(request: Request) -> str:
    """Reconstruct the URL Twilio called.

    Operators can pin this with ``TWILIO_WEBHOOK_PUBLIC_URL`` when there's
    a load balancer / reverse proxy in the path that rewrites the host
    header — without that override, the signature check would fail
    because Twilio signs the *original* URL.
    """
    override = get_settings().TWILIO_WEBHOOK_PUBLIC_URL
    if override:
        return override.rstrip("/")
    # FastAPI / Starlette already respect X-Forwarded-* when running
    # behind ``--proxy-headers`` so this is the right thing in dev.
    return str(request.url)


def _process_in_background(inbound: twilio_sms.InboundSms) -> None:
    """Background-task wrapper around :func:`sms_inbox.process_inbound_sms`.

    Two things need to be different versus calling the service directly
    from the request handler:

    1. **Fresh DB session.** The request-scoped ``db`` from ``get_db()``
       is closed as soon as the response is sent, so the background task
       must open its own ``SessionLocal()``.
    2. **Off the event-loop thread.** FastAPI runs sync ``BackgroundTasks``
       in a Starlette threadpool worker, so when our agent code reaches
       ``asyncio.run(...)`` inside ``_run_agent_to_completion`` there is
       no already-running loop on this thread and the call succeeds.
       Calling :func:`sms_inbox.process_inbound_sms` directly from the
       async webhook handler used to crash with
       ``RuntimeError: asyncio.run() cannot be called from a running
       event loop``.

    Exceptions are swallowed (logged) — Twilio has already received the
    empty TwiML so there's nothing to bubble up to.
    """
    try:
        with SessionLocal() as db:
            sms_inbox.process_inbound_sms(db, inbound)
    except Exception:  # noqa: BLE001 - never let a background task escape
        logger.exception(
            "SMS background task crashed for sid=%s", inbound.message_sid
        )


@router.post(
    "/twilio/inbound",
    summary="Twilio inbound SMS webhook",
    response_class=Response,
)
async def twilio_inbound(
    request: Request, background: BackgroundTasks
) -> Response:
    """Handle one inbound SMS / MMS from Twilio."""
    settings = get_settings()

    # ---- Read the raw form ---------------------------------------------
    raw_form = await request.form()
    form: Dict[str, str] = {k: str(v) for k, v in raw_form.items()}

    # ---- Verify the signature ------------------------------------------
    signature = request.headers.get("x-twilio-signature")
    if settings.TWILIO_AUTH_TOKEN:
        if not twilio_sms.verify_twilio_signature(
            auth_token=settings.TWILIO_AUTH_TOKEN,
            url=_public_url(request),
            params=form,
            signature=signature,
        ):
            logger.warning(
                "Twilio webhook signature mismatch (sid=%s) — refusing.",
                form.get("MessageSid"),
            )
            raise HTTPException(status_code=403, detail="Invalid Twilio signature.")
    else:
        # Local-dev mode: no auth token configured. We still process the
        # message but log loudly so this never silently lands in prod.
        logger.warning(
            "TWILIO_AUTH_TOKEN not set — accepting webhook WITHOUT signature "
            "verification. DO NOT run this configuration in production."
        )

    # ---- Parse + schedule the heavy work --------------------------------
    inbound = twilio_sms.parse_inbound_form(form)
    logger.info(
        "Twilio inbound sid=%s from=%s to=%s body_len=%d num_media=%d",
        inbound.message_sid,
        inbound.from_phone,
        inbound.to_phone,
        len(inbound.body or ""),
        inbound.num_media,
    )

    # Run dedup + person lookup + agent loop + reply send in a background
    # thread so we can return TwiML immediately. The agent loop can take
    # 5-30 s; if we held the socket open, Twilio would retry the webhook
    # (default retry policy: 11s timeout, then re-POST) and we'd LLM the
    # same message twice. Returning fast keeps Twilio happy and gives us
    # a worker thread that's free to call ``asyncio.run(...)``.
    background.add_task(_process_in_background, inbound)

    return Response(
        content=twilio_sms.empty_twiml(),
        media_type="application/xml",
    )
