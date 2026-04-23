"""Inbound-attachment understanding adapter.

Single entry point used by every messaging surface (email, SMS,
WhatsApp, Telegram, live web chat) to turn an *attached file* into a
*piece of text the agent can read*. The agent loop itself stays
text-only — we just pre-summarise the attachment so the small local
Gemma model can reason about it.

Three kinds of attachments are supported in v1:

* **Images** (``image/jpeg``, ``image/png``, ``image/webp``,
  ``image/gif``, ``image/heic``) — captioned by Gemini 2.5 Flash.
  Gemini is multimodal-native, fast (~1-2 s / image), and already
  authenticated via ``GEMINI_API_KEY``.
* **PDFs** (``application/pdf``) — text extracted via :mod:`pypdf`.
  Returns the first ``MAX_DOC_TEXT_CHARS`` so we don't blow the
  context window on a 200-page tax return.
* **Word .docx** (``application/vnd.openxmlformats-officedocument.wordprocessingml.document``) —
  text extracted via :mod:`python-docx`. Old binary ``.doc`` is
  intentionally out of scope.

Anything else (video, audio, archive, PDF-with-no-text, unknown
extension) is handled with a structured "no description available"
result; the rendering helper still surfaces it as
``[Attachment: invoice.zip — 3.4 MB]`` so the agent at least *knows*
something arrived.

Failures here NEVER propagate up to the inbound handler — every
public function returns a :class:`AttachmentDescription` with the
error stashed in ``error`` so a busted PDF can't drop the rest of the
message.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import get_settings


logger = logging.getLogger(__name__)


# --- Tunables --------------------------------------------------------------

# Cap on how much text we return per document. Gemma's context is a
# few thousand tokens once you net out the system prompt + RAG block;
# 4000 chars (~1000 tokens) is enough to cover the first page of most
# bills/statements without crowding everything else out.
MAX_DOC_TEXT_CHARS = 4000

# Image content types we route to Gemini Vision. HEIC support depends
# on Pillow being built with libheif; if it isn't we fall back to the
# generic "no description" path.
IMAGE_MIME_PREFIXES = ("image/",)

PDF_MIME_TYPES = frozenset({"application/pdf"})

DOCX_MIME_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
})


# Gemini vision prompt. Kept short and concrete so the resulting
# caption is dense, factual, and useful for downstream reasoning by
# the local Gemma model — not a flowery art-critic blurb.
_VISION_PROMPT = (
    "Describe this image factually in 2-4 short sentences for a "
    "household assistant. If it shows a person, pet, vehicle, document, "
    "screenshot, receipt, or address, call that out specifically (e.g. "
    "'a tabby cat with green eyes', 'a 2019 Honda Civic, blue, MA "
    "plate', 'a screenshot of a credit-card statement showing $42.18'). "
    "If readable text is present, transcribe the most important lines "
    "verbatim. Do not speculate or add commentary."
)


# --- Public API ------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentDescription:
    """Result of analysing one inbound attachment.

    ``text`` is the agent-facing description (caption for images,
    extracted body for documents). ``model_used`` is informational —
    log lines / debug surfaces use it to attribute calls. ``error`` is
    set when something went wrong; the caller may still render the
    attachment-arrived banner with ``[unable to read: <error>]`` so
    the agent at least knows the attachment exists.
    """

    text: str
    kind: str  # "image_caption" | "pdf_text" | "docx_text" | "skipped" | "error"
    model_used: Optional[str] = None
    error: Optional[str] = None
    truncated: bool = False


def describe_attachment(
    *,
    path: Path,
    mime_type: Optional[str] = None,
    filename: Optional[str] = None,
) -> AttachmentDescription:
    """Top-level dispatcher: read the file at ``path`` and produce text.

    The mime type is preferred when supplied (every inbound surface
    already knows it), otherwise we guess from the file extension.
    """
    label = filename or path.name
    if not get_settings().AI_VISION_ENABLED:
        logger.info(
            "[vision] describe skipped name=%s reason=disabled "
            "(AI_VISION_ENABLED=false)",
            label,
        )
        return AttachmentDescription(
            text="",
            kind="skipped",
            error="AI_VISION_ENABLED is off",
        )

    effective_mime = (mime_type or _guess_mime(filename or path.name) or "").lower()

    if not path.exists() or not path.is_file():
        logger.warning(
            "[vision] describe failed name=%s reason=file_missing path=%s",
            label,
            path,
        )
        return AttachmentDescription(
            text="",
            kind="error",
            error=f"file not found: {path}",
        )

    started = time.monotonic()
    try:
        if effective_mime.startswith(IMAGE_MIME_PREFIXES):
            result = _caption_image(path, mime_type=effective_mime)
            logger.info(
                "[vision] describe image done name=%s mime=%s duration_ms=%d "
                "kind=%s model=%s caption_chars=%d error=%s",
                label,
                effective_mime,
                int((time.monotonic() - started) * 1000),
                result.kind,
                result.model_used,
                len(result.text),
                result.error,
            )
            return result
        if effective_mime in PDF_MIME_TYPES or path.suffix.lower() == ".pdf":
            result = _extract_pdf(path)
            logger.info(
                "[vision] describe pdf done name=%s duration_ms=%d "
                "kind=%s text_chars=%d truncated=%s error=%s",
                label,
                int((time.monotonic() - started) * 1000),
                result.kind,
                len(result.text),
                result.truncated,
                result.error,
            )
            return result
        if (
            effective_mime in DOCX_MIME_TYPES
            or path.suffix.lower() == ".docx"
        ):
            result = _extract_docx(path)
            logger.info(
                "[vision] describe docx done name=%s duration_ms=%d "
                "kind=%s text_chars=%d truncated=%s error=%s",
                label,
                int((time.monotonic() - started) * 1000),
                result.kind,
                len(result.text),
                result.truncated,
                result.error,
            )
            return result
    except Exception as e:  # noqa: BLE001 - keep the inbound handler safe
        logger.exception(
            "[vision] describe crashed name=%s mime=%s",
            label,
            effective_mime,
        )
        return AttachmentDescription(
            text="",
            kind="error",
            error=f"{type(e).__name__}: {e}",
        )

    logger.info(
        "[vision] describe skipped name=%s mime=%s reason=unsupported",
        label,
        effective_mime or "unknown",
    )
    return AttachmentDescription(
        text="",
        kind="skipped",
        error=f"unsupported mime type: {effective_mime or 'unknown'}",
    )


# --- Per-kind implementations ---------------------------------------------


def _caption_image(path: Path, *, mime_type: str) -> AttachmentDescription:
    """Send the image to Gemini and return its description.

    We import google.genai lazily so the rest of the app — and unit
    tests that don't talk to the model — don't pay the SDK init cost.
    """
    settings = get_settings()
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        return AttachmentDescription(
            text="",
            kind="error",
            error="GEMINI_API_KEY is not configured",
        )

    try:
        from google import genai
        from google.genai import types as genai_types
    except Exception as e:  # noqa: BLE001
        return AttachmentDescription(
            text="",
            kind="error",
            error=f"google-genai SDK unavailable: {e}",
        )

    image_bytes = path.read_bytes()
    if len(image_bytes) > settings.AI_ATTACHMENT_MAX_BYTES:
        return AttachmentDescription(
            text="",
            kind="error",
            error=(
                f"image too large for vision call: "
                f"{len(image_bytes)} > {settings.AI_ATTACHMENT_MAX_BYTES} bytes"
            ),
        )

    client = genai.Client(api_key=api_key)
    model = settings.AI_VISION_MODEL
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                genai_types.Part.from_bytes(
                    data=image_bytes, mime_type=mime_type
                ),
                _VISION_PROMPT,
            ],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Gemini vision call failed for %s: %s", path.name, e)
        return AttachmentDescription(
            text="",
            kind="error",
            model_used=model,
            error=_short_genai_error(e),
        )

    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        return AttachmentDescription(
            text="",
            kind="error",
            model_used=model,
            error="Gemini returned an empty caption",
        )

    return AttachmentDescription(text=text, kind="image_caption", model_used=model)


def _extract_pdf(path: Path) -> AttachmentDescription:
    """Read a PDF and return its first ~MAX_DOC_TEXT_CHARS of body text.

    pypdf is pure-Python so it never blocks on a system library. Some
    PDFs are pure scans (no text layer) — we report a friendly error
    in that case so the agent can ask "would you like me to look at
    the scanned image inside?" rather than just shrugging.
    """
    try:
        from pypdf import PdfReader
    except Exception as e:  # noqa: BLE001
        return AttachmentDescription(
            text="", kind="error", error=f"pypdf unavailable: {e}"
        )

    try:
        reader = PdfReader(str(path))
    except Exception as e:  # noqa: BLE001
        return AttachmentDescription(
            text="", kind="error", error=f"pypdf failed to open: {e}"
        )

    if reader.is_encrypted:
        # We don't have a passphrase; bail with a useful message rather
        # than a stack trace.
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return AttachmentDescription(
                text="", kind="error", error="PDF is password protected"
            )

    chunks = []
    for page in reader.pages:
        try:
            piece = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a single bad page shouldn't kill the rest
            piece = ""
        if piece:
            chunks.append(piece.strip())
        if sum(len(c) for c in chunks) >= MAX_DOC_TEXT_CHARS:
            break

    text = "\n\n".join(chunks).strip()
    if not text:
        return AttachmentDescription(
            text="",
            kind="error",
            error="PDF has no extractable text (likely a scan)",
        )

    truncated = len(text) > MAX_DOC_TEXT_CHARS
    if truncated:
        text = text[:MAX_DOC_TEXT_CHARS].rstrip() + " …"

    return AttachmentDescription(
        text=text,
        kind="pdf_text",
        model_used="pypdf",
        truncated=truncated,
    )


def _extract_docx(path: Path) -> AttachmentDescription:
    """Read a Word .docx and return its first ~MAX_DOC_TEXT_CHARS of body text."""
    try:
        from docx import Document  # type: ignore[import-untyped]
    except Exception as e:  # noqa: BLE001
        return AttachmentDescription(
            text="", kind="error", error=f"python-docx unavailable: {e}"
        )

    try:
        doc = Document(str(path))
    except Exception as e:  # noqa: BLE001
        return AttachmentDescription(
            text="", kind="error", error=f"python-docx failed to open: {e}"
        )

    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    text = "\n".join(paragraphs).strip()
    if not text:
        return AttachmentDescription(
            text="",
            kind="error",
            error="DOCX has no extractable paragraph text",
        )

    truncated = len(text) > MAX_DOC_TEXT_CHARS
    if truncated:
        text = text[:MAX_DOC_TEXT_CHARS].rstrip() + " …"

    return AttachmentDescription(
        text=text,
        kind="docx_text",
        model_used="python-docx",
        truncated=truncated,
    )


# --- Helpers --------------------------------------------------------------


def _guess_mime(name: str) -> Optional[str]:
    """mimetypes.guess_type with a couple of patches for inbound MMS quirks."""
    if not name:
        return None
    # Twilio sometimes serves .heic with mime ``image/heif`` which
    # mimetypes doesn't know about. Patch in once at module scope.
    mimetypes.add_type("image/heic", ".heic")
    mimetypes.add_type("image/heif", ".heif")
    mt, _ = mimetypes.guess_type(name)
    return mt


def _short_genai_error(exc: Exception) -> str:
    """Short, single-line summary of a google-genai exception for logs / UI."""
    raw = str(exc)
    return raw.splitlines()[0][:240]


def is_describable(mime_type: Optional[str], filename: Optional[str] = None) -> bool:
    """Quick predicate so callers can decide whether to even bother.

    Used by the rendering helper to skip the vision call for obviously
    unsupported types (video, audio, archives) without round-tripping
    through ``describe_attachment`` and getting a "skipped" result.
    """
    mt = (mime_type or _guess_mime(filename or "") or "").lower()
    if not mt:
        return False
    if mt.startswith(IMAGE_MIME_PREFIXES):
        return True
    if mt in PDF_MIME_TYPES:
        return True
    if mt in DOCX_MIME_TYPES:
        return True
    return False


# --- Prompt rendering ------------------------------------------------------


@dataclass(frozen=True)
class RenderableAttachment:
    """Per-attachment view-model the rendering helper consumes.

    Inbound services build a list of these from their channel-specific
    rows (``sms_inbox_attachments``, ``telegram_inbox_attachments``,
    new email/live attachment tables, etc.) without having to share a
    single ORM model.
    """

    index: int  # 1-based, what shows in the prompt
    filename: str
    mime_type: Optional[str]
    size_bytes: Optional[int]
    description: Optional[AttachmentDescription]


def render_attachments_for_prompt(
    attachments: list[RenderableAttachment],
    *,
    extras_omitted: int = 0,
) -> str:
    """Render the agent-facing attachment block for an inbound message.

    Output looks like::

        --- Attachments ---
        [Attachment 1: cat.jpg, image/jpeg, 245 KB]
        Description (gemini-2.5-flash): A close-up of a tabby cat...

        [Attachment 2: bill.pdf, application/pdf, 1.2 MB]
        Extracted text (pypdf, truncated):
        ConEd electricity bill for service period...

        [Attachment 3: clip.mov, video/quicktime, 12 MB]
        (no description available: unsupported mime type: video/quicktime)

    Returns an empty string when ``attachments`` is empty so callers
    can unconditionally concatenate the result.
    """
    if not attachments:
        return ""

    lines: list[str] = ["--- Attachments ---"]
    for att in attachments:
        size_bit = f", {_human_size(att.size_bytes)}" if att.size_bytes else ""
        mime_bit = f", {att.mime_type}" if att.mime_type else ""
        lines.append(
            f"[Attachment {att.index}: {att.filename}{mime_bit}{size_bit}]"
        )
        desc = att.description
        if desc is None or desc.kind == "skipped":
            reason = (desc.error if desc and desc.error else "not analysed")
            lines.append(f"(no description available: {reason})")
        elif desc.kind == "error":
            lines.append(f"(unable to read: {desc.error or 'unknown error'})")
        elif desc.text:
            label = _kind_label(desc)
            lines.append(f"{label}:")
            lines.append(desc.text)
        lines.append("")  # blank line between attachments
    if extras_omitted > 0:
        lines.append(
            f"[+{extras_omitted} more attachment(s) not analysed — "
            "ask the user to resend them one at a time if you need them.]"
        )
    return "\n".join(lines).rstrip()


def _kind_label(desc: AttachmentDescription) -> str:
    base = {
        "image_caption": "Description",
        "pdf_text": "Extracted text",
        "docx_text": "Extracted text",
    }.get(desc.kind, "Description")
    suffix = []
    if desc.model_used:
        suffix.append(desc.model_used)
    if desc.truncated:
        suffix.append("truncated")
    if suffix:
        return f"{base} ({', '.join(suffix)})"
    return base


def _human_size(n: Optional[int]) -> str:
    """Format a byte count for prompt display: ``245 KB``, ``1.2 MB``, etc."""
    if not n or n < 0:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            if size < 10:
                return f"{size:.1f} {unit}"
            return f"{int(size)} {unit}"
        size /= 1024
    return f"{int(size)} GB"


@dataclass(frozen=True)
class AttachmentInput:
    """Per-attachment input for :func:`describe_many`.

    Each entry pairs the on-disk path of a stored inbound attachment
    with the metadata needed to (a) call the vision adapter on it and
    (b) render it back into the agent prompt afterwards.
    """

    index: int
    path: Path
    filename: str
    mime_type: Optional[str]
    size_bytes: Optional[int]


def describe_many(
    inputs: list[AttachmentInput],
    *,
    max_to_describe: Optional[int] = None,
) -> tuple[list[RenderableAttachment], int]:
    """Describe up to ``max_to_describe`` attachments in order.

    Anything past the cap is still returned as a
    :class:`RenderableAttachment` (so it appears in the rendered
    block) but with ``description=None`` and a "skipped over cap"
    error stamped on a synthesised :class:`AttachmentDescription`. The
    second return value is the number of attachments that were
    skipped purely because of the cap, so callers can pass it to
    :func:`render_attachments_for_prompt` as ``extras_omitted`` for a
    cleaner one-line note instead of N noisy "skipped" entries.

    The cap defaults to ``settings.AI_ATTACHMENT_MAX_PER_MESSAGE`` so
    every channel applies the same per-message budget without each
    inbound surface re-reading the setting.
    """
    if not inputs:
        return [], 0
    settings = get_settings()
    cap = (
        max_to_describe
        if max_to_describe is not None
        else settings.AI_ATTACHMENT_MAX_PER_MESSAGE
    )

    logger.info(
        "[vision] describe_many start n=%d cap=%s vision_enabled=%s",
        len(inputs),
        cap,
        settings.AI_VISION_ENABLED,
    )
    started = time.monotonic()

    rendered: list[RenderableAttachment] = []
    over_cap = 0
    for n, item in enumerate(inputs):
        if cap is not None and n >= cap:
            over_cap += 1
            continue
        desc = describe_attachment(
            path=item.path,
            mime_type=item.mime_type,
            filename=item.filename,
        )
        rendered.append(
            RenderableAttachment(
                index=item.index,
                filename=item.filename,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                description=desc,
            )
        )

    logger.info(
        "[vision] describe_many done described=%d over_cap=%d "
        "total_ms=%d kinds=[%s]",
        len(rendered),
        over_cap,
        int((time.monotonic() - started) * 1000),
        ",".join(r.description.kind for r in rendered),
    )
    return rendered, over_cap


__all__ = [
    "AttachmentDescription",
    "AttachmentInput",
    "MAX_DOC_TEXT_CHARS",
    "RenderableAttachment",
    "describe_attachment",
    "describe_many",
    "is_describable",
    "render_attachments_for_prompt",
]
