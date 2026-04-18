"""Assistant persona endpoints + Gemini-backed avatar generation.

On create, update (of any prompt-shaping field), and explicit regenerate,
the server asks Gemini for a fresh avatar image. Generation is
best-effort: if Gemini is unavailable or returns an error, the assistant
row is still saved and ``avatar_generation_note`` is populated so the UI
can surface the reason.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..config import get_settings
from ..db import get_db
from ..integrations.gemini import GeminiClient, GeminiError, GeminiUnavailable


router = APIRouter(prefix="/assistants", tags=["assistants"])
logger = logging.getLogger(__name__)


# ---------- avatar landmark detection + cache -------------------------------
# InsightFace is heavy (~300 MB model, ~600 ms cold detect), so we cache
# per-image results in-process keyed by ``profile_image_path`` + mtime.
# The cache is invalidated automatically whenever the avatar file is
# rewritten (new mtime) or explicitly on regenerate.

_landmarks_cache: Dict[Tuple[str, float], Optional[dict]] = {}
_landmarks_lock = threading.Lock()
_landmarks_failed: set[Tuple[str, float]] = set()


def _avatar_landmarks_for(assistant: models.Assistant) -> Optional[dict]:
    """Return cached mouth/eye landmarks for the assistant avatar, or None.

    Detection is best-effort and swallows all exceptions — a missing
    avatar, an image the detector can't parse, or an uninitialised
    InsightFace model should never break the main assistant GET. On
    failure the ``(path, mtime)`` is memoised as "no face" so we don't
    keep re-running the expensive detector for an image we've already
    given up on.
    """
    rel = assistant.profile_image_path
    if not rel:
        return None
    root = Path(get_settings().FA_STORAGE_ROOT)
    abs_path = root / rel
    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        return None
    key = (rel, mtime)
    with _landmarks_lock:
        if key in _landmarks_cache:
            return _landmarks_cache[key]
        if key in _landmarks_failed:
            return None
    try:
        from ..ai import face as face_ai

        data = abs_path.read_bytes()
        result = face_ai.detect_face_landmarks(data)
    except Exception:
        logger.debug("avatar landmark detection failed for %s", rel, exc_info=True)
        with _landmarks_lock:
            _landmarks_failed.add(key)
        return None
    with _landmarks_lock:
        if result is None:
            _landmarks_failed.add(key)
        else:
            _landmarks_cache[key] = result
    return result


def _invalidate_landmarks(rel_path: Optional[str]) -> None:
    if not rel_path:
        return
    with _landmarks_lock:
        for k in list(_landmarks_cache):
            if k[0] == rel_path:
                _landmarks_cache.pop(k, None)
        for k in list(_landmarks_failed):
            if k[0] == rel_path:
                _landmarks_failed.discard(k)


def _to_read_dict(assistant: models.Assistant) -> Dict[str, Any]:
    """Convert an ORM Assistant into the AssistantRead-shaped dict.

    We can't rely on FastAPI's automatic ORM serialization here because
    we want to tack on the derived ``avatar_landmarks`` field, which
    isn't a SQLAlchemy column. Building the dict explicitly keeps the
    shape obvious and avoids the "how do I add a non-column attribute
    to an orm_mode model" dance.
    """
    return {
        "assistant_id": assistant.assistant_id,
        "family_id": assistant.family_id,
        "assistant_name": assistant.assistant_name,
        "gender": assistant.gender,
        "visual_description": assistant.visual_description,
        "personality_description": assistant.personality_description,
        "profile_image_path": assistant.profile_image_path,
        "avatar_generation_note": assistant.avatar_generation_note,
        "avatar_landmarks": _avatar_landmarks_for(assistant),
        "created_at": assistant.created_at,
        "updated_at": assistant.updated_at,
    }


PROMPT_FIELDS = {"assistant_name", "gender", "visual_description"}


def _build_avatar_prompt(a: models.Assistant) -> str:
    parts: List[str] = [
        "Create a warm, friendly digital-illustration avatar for a family AI "
        f"assistant named \"{a.assistant_name}\".",
        "Square 1:1 composition, centered headshot, clean soft gradient "
        "background, no text, no watermarks, no logos.",
    ]
    if a.gender:
        parts.append(f"Gender presentation: {a.gender}.")
    if a.visual_description:
        parts.append(f"Visual description: {a.visual_description.strip()}")
    if a.personality_description:
        parts.append(
            "The avatar should visually reflect this personality: "
            f"{a.personality_description.strip()}"
        )
    return " ".join(parts)


def _generate_avatar_best_effort(assistant: models.Assistant) -> None:
    """Try to (re)generate the avatar. Mutates ``assistant`` in place.

    On success: writes a new file, updates ``profile_image_path``, clears
    ``avatar_generation_note``, and deletes the previous file.

    On failure: leaves ``profile_image_path`` alone and records the error
    in ``avatar_generation_note`` so the UI can surface it.
    """
    try:
        client = GeminiClient()
        image = client.generate_image(_build_avatar_prompt(assistant))
        rel_path, _ = storage.save_assistant_avatar(
            assistant.family_id, image.data, image.extension
        )
        previous = assistant.profile_image_path
        assistant.profile_image_path = rel_path
        assistant.avatar_generation_note = None
        if previous and previous != rel_path:
            storage.delete_if_exists(previous)
            _invalidate_landmarks(previous)
        _invalidate_landmarks(rel_path)
    except GeminiUnavailable as exc:
        assistant.avatar_generation_note = (
            f"Gemini is not configured: {exc}. Add GEMINI_API_KEY to .env "
            "to enable avatar generation."
        )
        logger.warning("Gemini unavailable: %s", exc)
    except GeminiError as exc:
        assistant.avatar_generation_note = f"Gemini returned no image: {exc}"
        logger.warning("Gemini returned no image: %s", exc)
    except Exception as exc:  # transport error, quota, safety block, etc.
        logger.exception("Avatar generation failed")
        assistant.avatar_generation_note = _summarize_exception(exc)


def _summarize_exception(exc: Exception) -> str:
    """Pull a concise user-facing message out of a Gemini/HTTP error.

    The google-genai SDK raises ``ClientError`` with a stringified JSON
    payload. We try to extract the top-level ``error.status`` +
    ``error.message`` so the UI can show something short.
    """
    import re

    raw = str(exc)
    status_match = re.search(r"'status': '([A-Z_]+)'", raw)
    # ``error.message`` may contain embedded newlines; grab up to the first \n.
    msg_match = re.search(r"'message': '([^'\\]*(?:\\.[^'\\]*)*)'", raw)
    message = msg_match.group(1).split("\\n", 1)[0] if msg_match else raw
    if status_match:
        return f"{status_match.group(1)}: {message}"
    # Fallback: trim very long errors.
    return message if len(message) <= 500 else message[:497] + "..."


@router.get("", response_model=List[schemas.AssistantRead])
def list_assistants(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    stmt = select(models.Assistant)
    if family_id is not None:
        stmt = stmt.where(models.Assistant.family_id == family_id)
    rows = list(db.execute(stmt).scalars())
    return [_to_read_dict(a) for a in rows]


@router.get("/{assistant_id}", response_model=schemas.AssistantRead)
def get_assistant(
    assistant_id: int, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return _to_read_dict(assistant)


@router.post(
    "", response_model=schemas.AssistantRead, status_code=status.HTTP_201_CREATED
)
def create_assistant(
    payload: schemas.AssistantCreate,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    family = db.get(models.Family, payload.family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="Family not found")

    assistant = models.Assistant(**payload.model_dump())
    db.add(assistant)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This family already has an assistant.",
        ) from exc

    _generate_avatar_best_effort(assistant)
    db.flush()
    db.refresh(assistant)
    return _to_read_dict(assistant)


@router.patch("/{assistant_id}", response_model=schemas.AssistantRead)
def update_assistant(
    assistant_id: int,
    payload: schemas.AssistantUpdate,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")

    changes = payload.model_dump(exclude_unset=True)
    prompt_fields_changed = bool(PROMPT_FIELDS & set(changes.keys()))
    for field, value in changes.items():
        setattr(assistant, field, value)
    db.flush()

    if prompt_fields_changed:
        _generate_avatar_best_effort(assistant)
        db.flush()

    db.refresh(assistant)
    return _to_read_dict(assistant)


@router.post(
    "/{assistant_id}/regenerate-avatar", response_model=schemas.AssistantRead
)
def regenerate_avatar(
    assistant_id: int, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    # Always invalidate before regenerating — the new portrait may have
    # a mouth in a completely different place.
    _invalidate_landmarks(assistant.profile_image_path)
    _generate_avatar_best_effort(assistant)
    db.flush()
    db.refresh(assistant)
    return _to_read_dict(assistant)


@router.delete("/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assistant(assistant_id: int, db: Session = Depends(get_db)) -> None:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    if assistant.profile_image_path:
        _invalidate_landmarks(assistant.profile_image_path)
        storage.delete_if_exists(assistant.profile_image_path)
    db.delete(assistant)
