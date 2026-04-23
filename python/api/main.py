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

from .ai import ollama as ai_ollama
from .auth import (
    cookie_attrs,
    sign_session,
    verify_session,
)
from .config import get_settings
from .services import email_inbox, monitoring_scheduler, telegram_inbox


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
    agent_tasks,
    ai_chat,
    ai_face,
    ai_tts,
    assistants,
    auth as auth_router,
    documents,
    families,
    financial_accounts,
    goals,
    google,
    identity_documents,
    insurance_policies,
    jobs,
    landing,
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
    spa,
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

    # Avi's standing research jobs (AI-owned monitoring tasks). Always
    # safe to start — the scheduler short-circuits inside its tick if
    # AI_MONITORING_ENABLED is false, so we don't need a parallel
    # gate at the lifespan level.
    background_tasks.append(
        asyncio.create_task(
            monitoring_scheduler.run_monitoring_loop(stop_event),
            name="monitoring_scheduler",
        )
    )

    # Pre-warm both Ollama models so the first chat doesn't pay the
    # 1–10 s cold-load cost. The fast (e2b) ack model is the more
    # critical one — its whole job is to land inside a few seconds,
    # so a cold start blows the AI_FAST_ACK_TIMEOUT_SECONDS budget
    # and the user never sees the contextual placeholder. We pin
    # both models for an hour via the warmup helper's keep_alive.
    #
    # The warmup is fired-and-forgotten on a background task so a
    # missing / unpulled model (or a down Ollama) never blocks
    # FastAPI startup. Both functions log their own outcome.
    async def _ollama_warmup() -> None:
        await asyncio.gather(
            ai_ollama.warmup_model(ai_ollama._model()),
            ai_ollama.warmup_model(ai_ollama.fast_model()),
            return_exceptions=True,
        )

    background_tasks.append(
        asyncio.create_task(_ollama_warmup(), name="ollama_warmup")
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

    # ---- Cookie-based session middleware -----------------------------------
    # Parses the signed-cookie session payload on every request and
    # stashes a CurrentUser (or None for guests) on ``request.state.user``
    # so the FastAPI ``Depends(require_*)`` factories in :mod:`api.auth`
    # can do their work without re-parsing on each call. Also implements
    # *sliding refresh*: every authenticated 2xx response gets a
    # freshly-minted cookie with ``exp = now + SESSION_LIFETIME_DAYS`` so
    # an actively-used session effectively never expires. This middleware
    # never short-circuits — authorization is per-route, so the public
    # allowlist (/, /legal/*, /api/health, /api/auth/*, the Twilio
    # webhook, and Avi's Gmail-OAuth callback) needs no special-casing.
    @app.middleware("http")
    async def session_cookie_middleware(request, call_next):  # type: ignore[no-untyped-def]
        cookie_name = settings.SESSION_COOKIE_NAME
        raw_cookie = request.cookies.get(cookie_name) if cookie_name else None
        user = verify_session(raw_cookie) if raw_cookie else None
        request.state.user = user
        response = await call_next(request)
        # Sliding refresh — only on success and only when the user was
        # already logged in. Skips writes to the OAuth callback (it
        # sets its own cookie) and the logout route (it explicitly
        # clears ours).
        if (
            user is not None
            and 200 <= response.status_code < 400
            and request.url.path != "/api/auth/logout"
            and request.url.path != "/api/auth/google/callback"
        ):
            try:
                fresh = sign_session(
                    email=user.email,
                    role=user.role,
                    person_id=user.person_id,
                    family_id=user.family_id,
                )
                attrs = cookie_attrs()
                response.set_cookie(
                    key=attrs["key"],
                    value=fresh,
                    httponly=attrs["httponly"],
                    secure=attrs["secure"],
                    samesite=attrs["samesite"],
                    path=attrs["path"],
                    max_age=settings.SESSION_LIFETIME_DAYS * 24 * 60 * 60,
                )
            except Exception:  # noqa: BLE001 — sliding refresh must never break a response
                logging.getLogger(__name__).exception(
                    "Sliding session refresh failed"
                )
        return response

    admin_routers = [
        families.router,
        assistants.router,
        people.router,
        person_photos.router,
        person_relationships.router,
        goals.router,
        jobs.router,
        medical_conditions.router,
        medications.router,
        physicians.router,
        pets.router,
        pet_photos.router,
        residences.router,
        residence_photos.router,
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
    # Browser-login OAuth flow — entirely public (anonymous browsers
    # become authenticated browsers here). Per-route guards in
    # :mod:`api.auth` protect everything else.
    app.include_router(auth_router.router, prefix="/api")
    # Twilio inbound webhook — public (no admin auth) so Twilio can hit it.
    # Signature verification (X-Twilio-Signature) is enforced inside the
    # handler whenever TWILIO_AUTH_TOKEN is configured.
    app.include_router(sms_webhook.router, prefix="/api")
    # Public legal pages (Privacy Policy + Terms of Service). Mounted at the
    # site root (no /api prefix) so the URLs you submit to Twilio look like
    # ``https://<host>/legal/privacy-policy`` rather than being buried under
    # /api/*. Twilio's reviewer fetches each URL once during A2P approval.
    app.include_router(legal.router)
    # Public marketing landing page at the site root ("/"). Mounted
    # last among the root-level routes so the explicit /api/*, /admin/*,
    # /aiassistant/*, and /legal/* routes still match first; this just
    # gives the ngrok tunnel a real homepage instead of a 404.
    app.include_router(landing.router)

    app.include_router(ai_face.router, prefix=AI_PREFIX)
    app.include_router(ai_chat.router, prefix=AI_PREFIX)
    app.include_router(ai_tts.router, prefix=AI_PREFIX)
    app.include_router(live_sessions.router, prefix=AI_PREFIX)
    # /api/aiassistant/tasks/* — read-only audit trail for the agent loop.
    app.include_router(agent_tasks.router, prefix="/api")

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok"}

    # Production React SPA — serves /admin/*, /aiassistant/*, /login,
    # /families/* (legacy redirect target), plus /assets/* and
    # /mediapipe/* from ui/react/dist/. Registered LAST so explicit
    # /api/*, /legal/*, and the landing root keep matching first.
    # Without this the public ngrok tunnel returns 404 for every
    # client-side route the SPA owns.
    app.include_router(spa.router)

    return app


app = create_app()
