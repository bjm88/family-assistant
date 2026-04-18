"""Assistant persona endpoints + Gemini-backed avatar generation.

On create, update (of any prompt-shaping field), and explicit regenerate,
the server asks Gemini for a fresh avatar image. Generation is
best-effort: if Gemini is unavailable or returns an error, the assistant
row is still saved and ``avatar_generation_note`` is populated so the UI
can surface the reason.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas, storage
from ..db import get_db
from ..integrations.gemini import GeminiClient, GeminiError, GeminiUnavailable


router = APIRouter(prefix="/api/assistants", tags=["assistants"])
logger = logging.getLogger(__name__)


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
) -> List[models.Assistant]:
    stmt = select(models.Assistant)
    if family_id is not None:
        stmt = stmt.where(models.Assistant.family_id == family_id)
    return list(db.execute(stmt).scalars())


@router.get("/{assistant_id}", response_model=schemas.AssistantRead)
def get_assistant(assistant_id: int, db: Session = Depends(get_db)) -> models.Assistant:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return assistant


@router.post(
    "", response_model=schemas.AssistantRead, status_code=status.HTTP_201_CREATED
)
def create_assistant(
    payload: schemas.AssistantCreate,
    db: Session = Depends(get_db),
) -> models.Assistant:
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
    return assistant


@router.patch("/{assistant_id}", response_model=schemas.AssistantRead)
def update_assistant(
    assistant_id: int,
    payload: schemas.AssistantUpdate,
    db: Session = Depends(get_db),
) -> models.Assistant:
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
    return assistant


@router.post(
    "/{assistant_id}/regenerate-avatar", response_model=schemas.AssistantRead
)
def regenerate_avatar(
    assistant_id: int, db: Session = Depends(get_db)
) -> models.Assistant:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    _generate_avatar_best_effort(assistant)
    db.flush()
    db.refresh(assistant)
    return assistant


@router.delete("/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assistant(assistant_id: int, db: Session = Depends(get_db)) -> None:
    assistant = db.get(models.Assistant, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    if assistant.profile_image_path:
        storage.delete_if_exists(assistant.profile_image_path)
    db.delete(assistant)
