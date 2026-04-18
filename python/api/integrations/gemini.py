"""Thin adapter around Google's Gemini API.

This module deliberately stays small and provider-specific. Higher-level
features (assistant avatars, RAG, SQL generation, etc.) call in through
the ``GeminiClient`` helpers below and stay decoupled from the wire format.

Environment
-----------
``GEMINI_API_KEY`` must be set. If it is missing, :class:`GeminiClient`
raises :class:`GeminiUnavailable` on construction so callers can fall back
gracefully instead of crashing at import time.

Models
------
The defaults here target the current image- and text-capable Gemini
models. Override ``image_model`` / ``text_model`` if you need a specific
version.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union

from google import genai
from google.genai import types as genai_types

from ..config import get_settings


logger = logging.getLogger(__name__)


# Ordered fallback list for image generation. The first model is preferred;
# if it returns RESOURCE_EXHAUSTED, an empty response, or any other error we
# walk down the list before giving up. All of these have been verified to be
# available on standard Gemini API keys; ``imagen-4.0-fast-generate-001``
# uses the ``predict`` endpoint and is handled via a different SDK call.
DEFAULT_IMAGE_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "imagen-4.0-fast-generate-001",
)
DEFAULT_IMAGE_MODEL = DEFAULT_IMAGE_MODELS[0]
DEFAULT_TEXT_MODEL = "gemini-2.5-flash"


class GeminiUnavailable(RuntimeError):
    """Raised when ``GEMINI_API_KEY`` is missing or the SDK can't be initialised."""


class GeminiError(RuntimeError):
    """Raised when every Gemini call succeeded at the transport level but
    returned no usable data, or every fallback model errored out.
    """


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str
    model: str = ""

    @property
    def extension(self) -> str:
        if "/" in self.mime_type:
            ext = self.mime_type.split("/", 1)[1].split(";", 1)[0].strip().lower()
            if ext == "jpeg":
                return ".jpg"
            if ext:
                return f".{ext}"
        return ".png"


class GeminiClient:
    """Wrapper around ``google.genai.Client`` with a small, opinionated surface."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        image_model: Union[str, Sequence[str], None] = None,
        text_model: str = DEFAULT_TEXT_MODEL,
    ) -> None:
        key = api_key or get_settings().GEMINI_API_KEY
        if not key:
            raise GeminiUnavailable(
                "GEMINI_API_KEY is not configured; set it in .env to enable "
                "Gemini-backed features."
            )
        self._client = genai.Client(api_key=key)
        self.image_models: tuple[str, ...] = _coerce_model_list(image_model)
        self.text_model = text_model

    @property
    def image_model(self) -> str:
        """Primary image model (back-compat for callers reading a single name)."""
        return self.image_models[0]

    # ---- Text generation --------------------------------------------------

    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: int = 1024,
    ) -> str:
        config = genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
        )
        resp = self._client.models.generate_content(
            model=self.text_model, contents=prompt, config=config
        )
        text = getattr(resp, "text", None)
        if not text:
            raise GeminiError("Gemini returned an empty text response.")
        return text

    # ---- Image generation -------------------------------------------------

    def generate_image(self, prompt: str) -> GeneratedImage:
        """Render a single image, trying each configured model in order.

        If a model returns ``RESOURCE_EXHAUSTED``, an empty response, or any
        other error, we move on to the next model in ``image_models``. The
        first model to produce an image wins. If every model fails we raise
        :class:`GeminiError` with a summary of what each attempt returned so
        the caller can surface a meaningful message.
        """
        attempts: List[str] = []
        for model in self.image_models:
            try:
                image = self._generate_one(model, prompt)
                if image is not None:
                    logger.info("Gemini image generated with model=%s", model)
                    return image
                attempts.append(f"{model}: empty response")
                logger.warning("Gemini model %s returned no image data", model)
            except Exception as exc:  # noqa: BLE001 - classify below
                summary = _short_error(exc)
                attempts.append(f"{model}: {summary}")
                logger.warning(
                    "Gemini image model %s failed: %s", model, summary
                )
                continue

        raise GeminiError(
            "All image models failed. Attempts: " + " | ".join(attempts)
        )

    # ------------------------------------------------------------------

    def _generate_one(self, model: str, prompt: str) -> Optional[GeneratedImage]:
        """Dispatch to the right API surface for ``model``.

        ``imagen-*`` models live behind ``generate_images`` (the ``predict``
        endpoint) and return a different response shape; everything else is
        a ``generate_content`` call that may return mixed text + image parts.
        """
        if model.startswith("imagen-"):
            resp = self._client.models.generate_images(
                model=model,
                prompt=prompt,
                config=genai_types.GenerateImagesConfig(
                    number_of_images=1, aspect_ratio="1:1"
                ),
            )
            for generated in getattr(resp, "generated_images", None) or []:
                image_obj = getattr(generated, "image", None)
                data = getattr(image_obj, "image_bytes", None)
                if data:
                    mime = getattr(image_obj, "mime_type", None) or "image/png"
                    return GeneratedImage(data=data, mime_type=mime, model=model)
            return None

        resp = self._client.models.generate_content(model=model, contents=prompt)
        images = _extract_inline_images(resp, model=model)
        return images[0] if images else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_model_list(
    value: Union[str, Sequence[str], None],
) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_IMAGE_MODELS
    if isinstance(value, str):
        return (value,)
    models = tuple(v for v in value if v)
    return models or DEFAULT_IMAGE_MODELS


def _extract_inline_images(resp, *, model: str = "") -> List[GeneratedImage]:  # type: ignore[no-untyped-def]
    out: List[GeneratedImage] = []
    for candidate in getattr(resp, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            mime = getattr(inline, "mime_type", None) or "image/png"
            if data:
                out.append(GeneratedImage(data=data, mime_type=mime, model=model))
    return out


def _short_error(exc: Exception) -> str:
    """Pull a concise ``STATUS: message`` out of a google-genai exception."""
    import re

    raw = str(exc)
    status_match = re.search(r"'status': '([A-Z_]+)'", raw)
    msg_match = re.search(r"'message': '([^'\\]*(?:\\.[^'\\]*)*)'", raw)
    message = msg_match.group(1).split("\\n", 1)[0] if msg_match else raw
    if status_match:
        return f"{status_match.group(1)}: {message}"
    return message if len(message) <= 200 else message[:197] + "..."
