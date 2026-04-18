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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import (
    addresses,
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
    vehicles,
)


ADMIN_PREFIX = "/api/admin"
AI_PREFIX = "/api/aiassistant"


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
        google.router,
    ]
    for r in admin_routers:
        app.include_router(r, prefix=ADMIN_PREFIX)

    app.include_router(media.router, prefix="/api")

    app.include_router(ai_face.router, prefix=AI_PREFIX)
    app.include_router(ai_chat.router, prefix=AI_PREFIX)
    app.include_router(ai_tts.router, prefix=AI_PREFIX)
    app.include_router(live_sessions.router, prefix=AI_PREFIX)

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
