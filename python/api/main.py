"""FastAPI application entry point.

Run in development::

    uv run uvicorn api.main:app --reload --app-dir python

OpenAPI docs are served at ``http://localhost:8000/docs``.

URL organization
----------------
* ``/api/admin/*``        — CRUD / admin console endpoints (families, people,
                             vehicles, etc.). These are the "source of truth"
                             routes the admin UI talks to; later they can be
                             guarded by a separate auth layer.
* ``/api/media/*``        — static file proxy; served outside the admin root
                             because the AI assistant page also needs it.
* ``/api/aiassistant/*``  — live AI assistant endpoints (face recognition,
                             chat, greet). Talks to local Ollama + InsightFace.
* ``/api/health``         — health probe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .services import email_inbox, telegram_inbox


# Wire up Python logging the FIRST thing we do, so every ``logger.info``
# / ``logger.warning`` from our ``api.*`` modules shows up in the terminal
# alongside uvicorn's access lines. Without this, the root logger sits at
# WARNING and our diagnostic INFO calls (followup, planner, agent loop)
# vanish silently. The level is controlled by ``FA_LOG_LEVEL`` so prod
# can dial it up to WARNING without a redeploy.
_log_level = os.environ.get("FA_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
# Quiet the noisier libraries so our own messages don't get drowned out.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
from .routers import (
    addresses,
    agent_tasks,
    ai_chat,
    ai_face,
    ai_tts,
    assistants,
    documents,
    families,
    financial_accounts,
    goals,
    google,
    identity_documents,
    insurance_policies,
    legal,
    live_sessions,
    media,
    medical_conditions,
    medications,
    people,
    person_photos,
    person_relationships,
    pet_photos,
    pets,
    physicians,
    residence_photos,
    residences,
    sensitive_identifiers,
    sms_webhook,
    status,
    tasks,
    vehicles,
)


ADMIN_PREFIX = "/api/admin"
AI_PREFIX = "/api/aiassistant"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start + cleanly stop the long-lived background services.

    Currently runs the email-inbox poller (Avi auto-replies to mail
    from registered family members). Add more services here as they
    arrive — every one should accept a stop event so shutdown is
    deterministic instead of hanging on a sleep.
    """
    settings = get_settings()
    stop_event = asyncio.Event()
    background_tasks: list[asyncio.Task] = []

    if settings.AI_EMAIL_INBOX_ENABLED:
        background_tasks.append(
            asyncio.create_task(
                email_inbox.run_email_inbox_loop(stop_event),
                name="email_inbox_poller",
            )
        )
    else:
        logging.getLogger(__name__).info(
            "Email inbox poller disabled via AI_EMAIL_INBOX_ENABLED=false"
        )

    if settings.AI_TELEGRAM_INBOUND_ENABLED:
        background_tasks.append(
            asyncio.create_task(
                telegram_inbox.run_telegram_inbox_loop(stop_event),
                name="telegram_inbox_poller",
            )
        )
    else:
        logging.getLogger(__name__).info(
            "Telegram inbox loop disabled via AI_TELEGRAM_INBOUND_ENABLED=false"
        )

    try:
        yield
    finally:
        stop_event.set()
        for task in background_tasks:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logging.getLogger(__name__).exception(
                    "Background task %s raised on shutdown", task.get_name()
                )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Family Assistant API",
        version="0.2.0",
        description=(
            "Backend API for the Family Assistant. /api/admin/* hosts the "
            "CRUD routes used by the admin console; /api/aiassistant/* hosts "
            "the live AI endpoints (face recognition, chat, greet) backed by "
            "local Ollama + InsightFace."
        ),
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    admin_routers = [
        families.router,
        assistants.router,
        people.router,
        person_photos.router,
        person_relationships.router,
        goals.router,
        medical_conditions.router,
        medications.router,
        physicians.router,
        pets.router,
        pet_photos.router,
        residences.router,
        residence_photos.router,
        addresses.router,
        identity_documents.router,
        sensitive_identifiers.router,
        vehicles.router,
        insurance_policies.router,
        financial_accounts.router,
        documents.router,
        tasks.router,
        google.router,
        status.router,
    ]
    for r in admin_routers:
        app.include_router(r, prefix=ADMIN_PREFIX)

    app.include_router(media.router, prefix="/api")
    # Twilio inbound webhook — public (no admin auth) so Twilio can hit it.
    # Signature verification (X-Twilio-Signature) is enforced inside the
    # handler whenever TWILIO_AUTH_TOKEN is configured.
    app.include_router(sms_webhook.router, prefix="/api")
    # Public legal pages (Privacy Policy + Terms of Service). Mounted at the
    # site root (no /api prefix) so the URLs you submit to Twilio look like
    # ``https://<host>/legal/privacy-policy`` rather than being buried under
    # /api/*. Twilio's reviewer fetches each URL once during A2P approval.
    app.include_router(legal.router)

    app.include_router(ai_face.router, prefix=AI_PREFIX)
    app.include_router(ai_chat.router, prefix=AI_PREFIX)
    app.include_router(ai_tts.router, prefix=AI_PREFIX)
    app.include_router(live_sessions.router, prefix=AI_PREFIX)
    # /api/aiassistant/tasks/* — read-only audit trail for the agent loop.
    app.include_router(agent_tasks.router, prefix="/api")

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
