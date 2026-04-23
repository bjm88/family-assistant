"""Serve the React SPA bundle from ``ui/react/dist/``.

Without this router FastAPI returns 404 for ``/admin/*``,
``/aiassistant/*``, ``/login``, and the legacy ``/families/*`` paths.
That's fine in local dev because Vite serves them on its own port
(``localhost:5173``) and reverse-proxies ``/api/*`` over to this
process — but the public ngrok tunnel terminates here at FastAPI, so
the production bundle has to be served by us. Otherwise a family
member who logs in from their phone gets redirected to
``/admin/families/{id}`` and lands on a backend 404.

Build the bundle once with::

    cd ui/react && npm run build      # or scripts/deploy.sh --build

then start the backend; this router will pick up every change to
``dist/`` automatically (no restart required, the files are read on
each request).

Routing strategy
----------------
* ``/assets/*``     → hashed JS/CSS bundles emitted by Vite.
* ``/mediapipe/*``  → BlazeFace WASM + tflite weights mirrored by the
  ``copy-mediapipe-assets.mjs`` postinstall hook.
* ``/admin/*``, ``/aiassistant/*``, ``/login``, ``/families/*`` →
  serve ``index.html`` so React Router can take over.
* Any other GET that hits an unmatched path also gets ``index.html``
  (catch-all). Order matters — this router is registered LAST so the
  more-specific ``/api/*``, ``/legal/*``, ``/landing/*``, and ``/``
  routes still match first.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse


logger = logging.getLogger(__name__)

router = APIRouter(tags=["spa"], include_in_schema=False)


# ``python/api/routers/spa.py`` → repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DIST_DIR = _REPO_ROOT / "ui" / "react" / "dist"
_INDEX_HTML = _DIST_DIR / "index.html"


def _serve_index() -> HTMLResponse:
    """Return ``dist/index.html`` with caching disabled.

    No-cache because Vite hashes the asset filenames already; the
    only thing that changes between builds is which hashed bundle
    ``index.html`` references. Caching the HTML would strand users
    on a stale shell that points at a deleted bundle.
    """
    if not _INDEX_HTML.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "React SPA bundle is missing. Build it once with "
                "`cd ui/react && npm run build` (or "
                "`scripts/deploy.sh --build`) so this FastAPI process "
                "can serve /admin, /aiassistant, /login, etc. through "
                f"the ngrok tunnel. Looked for: {_INDEX_HTML}"
            ),
        )
    return HTMLResponse(
        content=_INDEX_HTML.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


def _serve_static(subdir: str, path: str) -> FileResponse:
    """Serve a file from ``dist/<subdir>/<path>`` with directory-traversal guard."""
    base = (_DIST_DIR / subdir).resolve()
    target = (base / path).resolve()
    # Reject ``..`` segments that try to escape the dist subtree.
    if not str(target).startswith(str(base) + "/") and target != base:
        raise HTTPException(status_code=404)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target)


# ---------------------------------------------------------------------------
# Static asset routes (Vite's hashed bundles + mediapipe weights)
# ---------------------------------------------------------------------------


@router.get("/assets/{path:path}", include_in_schema=False)
def assets(path: str) -> FileResponse:
    return _serve_static("assets", path)


@router.get("/mediapipe/{path:path}", include_in_schema=False)
def mediapipe(path: str) -> FileResponse:
    return _serve_static("mediapipe", path)


# ---------------------------------------------------------------------------
# SPA shell routes — every client-side path lands on index.html so the
# React Router takes over. We list the prefixes explicitly (rather than
# a single catch-all) so a typo'd /api/whatever still 404s loudly
# instead of silently rendering the SPA shell.
# ---------------------------------------------------------------------------


@router.get("/login", include_in_schema=False)
def login_shell() -> HTMLResponse:
    return _serve_index()


@router.get("/admin", include_in_schema=False)
@router.get("/admin/{path:path}", include_in_schema=False)
def admin_shell(path: str = "") -> HTMLResponse:
    return _serve_index()


@router.get("/aiassistant", include_in_schema=False)
@router.get("/aiassistant/{path:path}", include_in_schema=False)
def ai_shell(path: str = "") -> HTMLResponse:
    return _serve_index()


# Legacy ``/families`` paths predate the ``/admin`` rename. The SPA
# itself rewrites them on mount (see ``LegacyFamilyRedirect`` in
# App.tsx), so we just need to serve the shell.
@router.get("/families", include_in_schema=False)
@router.get("/families/{path:path}", include_in_schema=False)
def families_shell(path: str = "") -> HTMLResponse:
    return _serve_index()
