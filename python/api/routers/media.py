"""Serves uploaded photos (profile pictures) back to the UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import storage
from ..auth import CurrentUser, require_user

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{relative_path:path}")
def get_media(
    relative_path: str,
    user: CurrentUser = Depends(require_user),
):
    try:
        path = storage.absolute_path(relative_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Storage layout starts every family-scoped tree with a
    # ``families/<family_id>/...`` prefix. For non-admins, refuse
    # paths that don't begin with their own family's prefix.
    # Generic non-family-scoped media (e.g. ``shared/...``) stays
    # accessible to any authenticated user.
    if not user.is_admin:
        norm = relative_path.lstrip("/").replace("\\", "/")
        if norm.startswith("families/"):
            try:
                family_id = int(norm.split("/", 2)[1])
            except (ValueError, IndexError):
                raise HTTPException(status_code=404, detail="File not found")
            if user.family_id != family_id:
                raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)
