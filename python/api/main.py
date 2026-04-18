"""FastAPI application entry point.

Run in development::

    uv run uvicorn api.main:app --reload --app-dir python

OpenAPI docs are served at ``http://localhost:8000/docs``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import (
    addresses,
    assistants,
    documents,
    families,
    financial_accounts,
    identity_documents,
    insurance_policies,
    media,
    people,
    person_photos,
    person_relationships,
    sensitive_identifiers,
    vehicles,
)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Family Assistant API",
        version="0.1.0",
        description=(
            "Backend API for the Family Assistant admin console. "
            "Manages families, people, identity records, vehicles, "
            "insurance policies, financial accounts, and uploaded documents."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(families.router)
    app.include_router(assistants.router)
    app.include_router(people.router)
    app.include_router(person_photos.router)
    app.include_router(person_relationships.router)
    app.include_router(addresses.router)
    app.include_router(identity_documents.router)
    app.include_router(sensitive_identifiers.router)
    app.include_router(vehicles.router)
    app.include_router(insurance_policies.router)
    app.include_router(financial_accounts.router)
    app.include_router(documents.router)
    app.include_router(media.router)

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
