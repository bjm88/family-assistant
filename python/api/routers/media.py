"""Serves uploaded photos (profile pictures) back to the UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import storage

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("/{relative_path:path}")
def get_media(relative_path: str):
    try:
        path = storage.absolute_path(relative_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)
