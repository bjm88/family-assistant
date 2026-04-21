"""Public marketing landing page served at the site root.

The ngrok tunnel ``https://avi-maisano.ngrok.app`` already forwards to
this FastAPI process (see ``scripts/lib/common.sh::ngrok_start``), so
serving ``GET /`` from here is enough to publish a real homepage at
that URL with no extra tunnels or hosting.

Single source of truth on disk:
    ``ui/react/public/landing/index.html``  — the marketing HTML
    ``ui/react/public/landing/avi.png``     — Avi's portrait

Vite already publishes everything in ``public/`` when the React app
is served on its own dev port, so the same files work both inside
the dev UI and through this FastAPI router (which is what the public
ngrok tunnel hits). Mirrors the pattern in ``routers/legal.py``.

Routes
------
* ``GET /``                 → marketing landing page
* ``GET /landing/avi.png``  → Avi's portrait
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse


router = APIRouter(tags=["landing"])


# ``python/api/routers/landing.py`` → repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LANDING_DIR = _REPO_ROOT / "ui" / "react" / "public" / "landing"


def _read_html(filename: str) -> str:
    path = _LANDING_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - misconfiguration
        raise HTTPException(
            status_code=500,
            detail=f"Landing page asset missing on disk: {path}",
        ) from exc


@router.get(
    "/",
    response_class=HTMLResponse,
    summary="Marketing landing page",
    include_in_schema=False,
)
def landing_page() -> HTMLResponse:
    return HTMLResponse(content=_read_html("index.html"))


@router.get(
    "/landing/avi.png",
    response_class=FileResponse,
    include_in_schema=False,
)
def landing_avi_portrait() -> FileResponse:
    path = _LANDING_DIR / "avi.png"
    if not path.exists():  # pragma: no cover - misconfiguration
        raise HTTPException(status_code=404, detail="avi.png missing")
    return FileResponse(path, media_type="image/png")
