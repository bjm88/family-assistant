"""Text-to-speech endpoints for Avi.

All responses are ``audio/wav``. The UI plays them via a standard
``<audio>`` element fed an object URL — works over any LAN, still comes
out of the Mac Studio speakers when the browser is on the same machine.

``/tts/status`` is the cheap probe for the header badge.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..ai import tts
from ..auth import require_user

logger = logging.getLogger(__name__)
# TTS is a pure text→audio renderer with no per-family scope, but it
# would be a denial-of-service vector left wide open. Require any
# logged-in user.
router = APIRouter(
    prefix="",
    tags=["ai_tts"],
    dependencies=[Depends(require_user)],
)


class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    voice: str | None = None
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    gender_hint: str | None = Field(default=None, max_length=32)


class StatusResponse(BaseModel):
    enabled: bool
    engine: str
    default_voice: str
    model_present: bool
    voices_present: bool
    model_path: str
    voices_path: str
    initialized: bool


@router.get("/tts/status", response_model=StatusResponse)
def tts_status() -> StatusResponse:
    return StatusResponse(**tts.status())


@router.post(
    "/tts",
    responses={
        200: {
            "content": {"audio/wav": {}},
            "description": "A Kokoro-synthesised WAV clip.",
        }
    },
)
def synthesize(payload: SynthesizeRequest) -> Response:
    try:
        result = tts.synthesize(
            payload.text,
            voice=payload.voice,
            speed=payload.speed,
            gender_hint=payload.gender_hint,
        )
    except tts.TtsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))

    headers = {
        "X-Tts-Engine": result.engine,
        "X-Tts-Voice": result.voice,
        "X-Tts-Cached": "1" if result.cached else "0",
        "X-Tts-Sample-Rate": str(result.sample_rate),
        # Let the browser cache these too — WAVs for the same text are
        # byte-identical across requests.
        "Cache-Control": "public, max-age=3600",
    }
    return Response(content=result.wav_bytes, media_type="audio/wav", headers=headers)
