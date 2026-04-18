"""Local text-to-speech for the family assistant (Avi).

Design goals
------------

*   **Generic engine interface** — other engines (ChatTTS, Piper, Bark,
    Apple `say`) can be plugged in via ``_Engine`` without touching the
    endpoint layer. Today we ship a single implementation backed by
    `kokoro-onnx`_: small (~300 MB weights), very fast, and it runs on
    the same ONNX Runtime stack we already use for InsightFace.
*   **Filesystem cache** — synthesis is deterministic for a given
    ``(engine, voice, speed, text)`` triple, so we hash that tuple and
    reuse a cached ``.wav`` when one exists. Greetings like "Hi Ben!"
    end up sub-millisecond after the first pronunciation.
*   **Lazy everything** — neither the Kokoro session nor the phonemizer
    are imported at app boot. First call pays the ~500 ms model load;
    everything after is warm.
*   **Graceful degradation** — if the model files aren't downloaded yet
    or kokoro-onnx isn't available, ``synthesize()`` raises
    :class:`TtsUnavailable` which the router turns into a clean 503.

.. _kokoro-onnx: https://github.com/thewh1teagle/kokoro-onnx
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import get_settings

logger = logging.getLogger(__name__)


# Public URLs for the Kokoro v1.0 release assets. Both files are hosted
# on the project's GitHub release so no HF auth is required.
_KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
_KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)
_KOKORO_MODEL_FILENAME = "kokoro-v1.0.onnx"
_KOKORO_VOICES_FILENAME = "voices-v1.0.bin"


class TtsUnavailable(RuntimeError):
    """Raised when the configured engine can't service a request."""


# Sensible defaults for the few voices we actually care about. The
# Kokoro pack ships many more — pass any of them through the API's
# ``voice`` parameter to override.
_VOICE_BY_GENDER: dict[str, str] = {
    "female": "af_bella",
    "male": "am_adam",
    "nonbinary": "af_nicole",
}
_DEFAULT_VOICE = "af_bella"


# ---------------------------------------------------------------------------
# Kokoro engine
# ---------------------------------------------------------------------------


_kokoro_instance = None
_kokoro_lock = threading.Lock()


def _kokoro_paths() -> tuple[Path, Path]:
    model_dir = get_settings().tts_model_dir
    return (
        model_dir / _KOKORO_MODEL_FILENAME,
        model_dir / _KOKORO_VOICES_FILENAME,
    )


def _download_if_missing(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return
    logger.info("Downloading %s -> %s (this is one-time, ~hundreds of MB)", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        # 1 MB chunks keep memory flat and let us emit progress logs.
        total = int(resp.headers.get("Content-Length", 0) or 0)
        read = 0
        last_log = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            read += len(chunk)
            if total and read - last_log > 50 * (1 << 20):
                logger.info(
                    "  %s: %.0f%% (%.0f / %.0f MB)",
                    dest.name,
                    read * 100.0 / total,
                    read / (1 << 20),
                    total / (1 << 20),
                )
                last_log = read
    tmp.replace(dest)
    logger.info("Downloaded %s (%.0f MB)", dest.name, dest.stat().st_size / (1 << 20))


def _ensure_kokoro_assets() -> tuple[Path, Path]:
    model_path, voices_path = _kokoro_paths()
    _download_if_missing(_KOKORO_MODEL_URL, model_path)
    _download_if_missing(_KOKORO_VOICES_URL, voices_path)
    return model_path, voices_path


def _get_kokoro():
    """Lazy singleton. Downloads weights on first call (~300 MB)."""
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    with _kokoro_lock:
        if _kokoro_instance is not None:
            return _kokoro_instance
        try:
            from kokoro_onnx import Kokoro  # noqa: WPS433 — deliberate lazy import
        except ImportError as e:
            raise TtsUnavailable(
                f"kokoro-onnx is not installed: {e}. Run `uv add kokoro-onnx`."
            )
        try:
            model_path, voices_path = _ensure_kokoro_assets()
        except Exception as e:
            raise TtsUnavailable(
                f"Could not download Kokoro model files: {e}. "
                f"Drop them manually into {_kokoro_paths()[0].parent}."
            )
        try:
            logger.info("Initializing Kokoro TTS (model=%s)", model_path.name)
            _kokoro_instance = Kokoro(str(model_path), str(voices_path))
        except Exception as e:
            raise TtsUnavailable(f"Kokoro failed to initialize: {e}")
        return _kokoro_instance


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TtsResult:
    wav_bytes: bytes
    sample_rate: int
    cached: bool
    engine: str
    voice: str


def pick_voice(*, gender: Optional[str] = None, override: Optional[str] = None) -> str:
    """Resolve a voice pack name from (optional override, optional gender)."""
    if override and override != "auto":
        return override
    settings = get_settings()
    if settings.AI_TTS_VOICE and settings.AI_TTS_VOICE != "auto":
        return settings.AI_TTS_VOICE
    if gender:
        g = gender.lower().strip()
        if g in _VOICE_BY_GENDER:
            return _VOICE_BY_GENDER[g]
    return _DEFAULT_VOICE


def _cache_path(engine: str, voice: str, speed: float, text: str) -> Path:
    key = f"{engine}|{voice}|{speed:.3f}|{text}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:24]
    return get_settings().tts_cache_dir / f"{engine}_{voice}_{digest}.wav"


def _samples_to_wav_bytes(samples, sample_rate: int) -> bytes:
    import soundfile as sf  # noqa: WPS433

    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def synthesize(
    text: str,
    *,
    voice: Optional[str] = None,
    speed: Optional[float] = None,
    gender_hint: Optional[str] = None,
    lang: str = "en-us",
) -> TtsResult:
    """Generate a WAV for ``text``. Result is cached on disk."""
    settings = get_settings()
    if not settings.AI_TTS_ENABLED:
        raise TtsUnavailable("TTS is disabled (set AI_TTS_ENABLED=true).")

    engine = settings.AI_TTS_ENGINE.lower()
    if engine != "kokoro":
        raise TtsUnavailable(
            f"TTS engine '{engine}' is not implemented yet. Use 'kokoro'."
        )

    clean = text.strip()
    if not clean:
        raise TtsUnavailable("Refusing to synthesize empty text.")

    chosen_voice = pick_voice(gender=gender_hint, override=voice)
    chosen_speed = float(speed if speed is not None else settings.AI_TTS_SPEED)

    cache = _cache_path(engine, chosen_voice, chosen_speed, clean)
    if cache.exists():
        return TtsResult(
            wav_bytes=cache.read_bytes(),
            sample_rate=24000,  # Kokoro output is 24 kHz; read-back is metadata-only
            cached=True,
            engine=engine,
            voice=chosen_voice,
        )

    kokoro = _get_kokoro()
    try:
        samples, sample_rate = kokoro.create(
            clean,
            voice=chosen_voice,
            speed=chosen_speed,
            lang=lang,
        )
    except Exception as e:
        raise TtsUnavailable(f"Kokoro synthesis failed: {e}")

    wav_bytes = _samples_to_wav_bytes(samples, sample_rate)
    try:
        cache.write_bytes(wav_bytes)
    except OSError:
        logger.warning("Could not write TTS cache file %s", cache, exc_info=True)

    return TtsResult(
        wav_bytes=wav_bytes,
        sample_rate=sample_rate,
        cached=False,
        engine=engine,
        voice=chosen_voice,
    )


def status() -> dict:
    """Cheap, side-effect-free status probe for the status badge."""
    settings = get_settings()
    model_path, voices_path = _kokoro_paths()
    return {
        "enabled": settings.AI_TTS_ENABLED,
        "engine": settings.AI_TTS_ENGINE,
        "default_voice": settings.AI_TTS_VOICE,
        "model_present": model_path.exists() and model_path.stat().st_size > 1_000_000,
        "voices_present": voices_path.exists() and voices_path.stat().st_size > 100_000,
        "model_path": str(model_path),
        "voices_path": str(voices_path),
        "initialized": _kokoro_instance is not None,
    }
