"""Public legal pages — Privacy Policy + Terms of Service.

These are required by Twilio for SMS / A2P 10DLC registration: the brand
and campaign forms ask for publicly-reachable URLs to the operator's
privacy policy and terms of service. Twilio's automated reviewer fetches
each URL once during approval, so the page just has to render with a 200
and a copy of the policy text — no auth, no JavaScript required.

Single source of truth:
    ``ui/react/public/legal/privacy-policy.html``
    ``ui/react/public/legal/terms-of-service.html``

Vite already serves those files directly when you browse the React app
(it auto-publishes everything in ``public/``). This router serves the
same bytes from the FastAPI process so the existing ngrok tunnel that
points at the Twilio webhook (``:8000``) can also serve the legal pages
without a second tunnel.

Routes
------
* ``GET /legal/privacy-policy``      → privacy policy HTML
* ``GET /legal/terms-of-service``    → terms HTML
* ``GET /legal/privacy``             → alias → privacy
* ``GET /legal/terms``               → alias → terms

We also accept the ``.html`` suffix so deep-links from the React build
keep working when Twilio crawls them.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse


router = APIRouter(prefix="/legal", tags=["legal"])


# ``python/api/routers/legal.py`` → repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LEGAL_DIR = _REPO_ROOT / "ui" / "react" / "public" / "legal"


def _read(filename: str) -> str:
    path = _LEGAL_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - misconfiguration
        raise HTTPException(
            status_code=500,
            detail=f"Legal page asset missing on disk: {path}",
        ) from exc


@router.get(
    "/privacy-policy",
    response_class=HTMLResponse,
    summary="Privacy Policy (HTML)",
)
@router.get(
    "/privacy-policy.html",
    response_class=HTMLResponse,
    include_in_schema=False,
)
@router.get(
    "/privacy",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def privacy_policy() -> HTMLResponse:
    return HTMLResponse(content=_read("privacy-policy.html"))


@router.get(
    "/terms-of-service",
    response_class=HTMLResponse,
    summary="Terms of Service (HTML)",
)
@router.get(
    "/terms-of-service.html",
    response_class=HTMLResponse,
    include_in_schema=False,
)
@router.get(
    "/terms",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def terms_of_service() -> HTMLResponse:
    return HTMLResponse(content=_read("terms-of-service.html"))
