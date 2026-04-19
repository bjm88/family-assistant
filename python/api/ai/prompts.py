"""Source-controlled prompt fragments injected into every LLM call.

We keep certain prompt blocks as plain ``.txt`` files at the project
root so they're easy to edit, source-control, and review:

* ``ai_safety_sandbox.txt`` — UNBREAKABLE rules prepended to every LLM
  call inside a clearly-delimited safety block. Use this for anything
  that must hold even if the model is socially-engineered in
  conversation: don't drop the database, don't expose private columns,
  don't run shell commands, etc. The chat path also enforces
  defence-in-depth at the SQL layer (sql_tool blocks DELETE / DROP /
  ALTER) and at the tool registry (Google tools are hidden when the
  ``google`` capability isn't satisfied) — the safety file is the
  LLM's share of that fence.

* ``ai_context_*.txt`` — Optional household-wide context fragments.
  Drop a new file in (e.g. ``ai_context_timezone.txt``,
  ``ai_context_quiet_hours.txt``, ``ai_context_signature.txt``) and it
  gets auto-injected into the system prompt as a labelled "house
  context" block without a code change. Useful for evolving "house
  rules" the AI should always know without re-deriving them from the
  database every turn.

Files are read lazily and cached by mtime so edits in dev show up on
the very next chat request without restarting uvicorn. Comment lines
starting with ``#`` are stripped on load so the files can carry their
own header / changelog without it leaking into the prompt.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)


# ``python/api/ai/prompts.py`` → repo root is three parents up.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]

SAFETY_FILE_NAME = "ai_safety_sandbox.txt"
CONTEXT_FILE_PREFIX = "ai_context_"
CONTEXT_FILE_SUFFIX = ".txt"


_lock = threading.Lock()
# (mtime, parsed_text) keyed by absolute path so concurrent readers
# share the same cache slot.
_cache: dict[Path, Tuple[float, str]] = {}


def _strip_comments(raw: str) -> str:
    """Drop full-line ``#`` comments and trim trailing whitespace.

    Mid-line ``#`` is left alone — a rule like "Always use #family
    in subject lines" should survive untouched.
    """
    out_lines: List[str] = []
    for line in raw.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out_lines.append(line.rstrip())
    # Collapse the trailing newlines so callers can join cleanly.
    return "\n".join(out_lines).strip()


def _read_cached(path: Path) -> Optional[str]:
    """Read ``path`` with an mtime-keyed cache. Returns ``None`` when
    the file is missing or unreadable."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Cannot stat prompt file %s: %s", path, e)
        return None

    with _lock:
        cached = _cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Cannot read prompt file %s: %s", path, e)
            return None
        text = _strip_comments(raw)
        _cache[path] = (mtime, text)
        return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def safety_text() -> str:
    """Body of ``ai_safety_sandbox.txt`` (without the file's own
    comment header). Returns ``""`` when the file is absent — that's
    intentionally permissive so a missing file doesn't break local
    development; production startup logs a clear warning.
    """
    text = _read_cached(PROJECT_ROOT / SAFETY_FILE_NAME)
    if text is None:
        logger.warning(
            "Safety file %s not found at %s — LLM calls will run without "
            "the safety preamble.",
            SAFETY_FILE_NAME,
            PROJECT_ROOT,
        )
        return ""
    return text


def context_blocks() -> List[Tuple[str, str]]:
    """All ``ai_context_*.txt`` files at the project root, sorted.

    Returns a list of ``(label, body)`` tuples where ``label`` is the
    file stem after the ``ai_context_`` prefix with underscores
    rendered as spaces. Empty files (or files containing only
    comments) are skipped so a half-finished draft doesn't pollute
    the prompt.
    """
    blocks: List[Tuple[str, str]] = []
    if not PROJECT_ROOT.is_dir():
        return blocks
    for entry in sorted(PROJECT_ROOT.glob(f"{CONTEXT_FILE_PREFIX}*{CONTEXT_FILE_SUFFIX}")):
        body = _read_cached(entry)
        if not body:
            continue
        stem = entry.stem[len(CONTEXT_FILE_PREFIX) :]
        label = stem.replace("_", " ").strip() or entry.stem
        blocks.append((label, body))
    return blocks


def with_safety(system_prompt: str) -> str:
    """Wrap ``system_prompt`` with the unbreakable safety preamble.

    The preamble is fenced inside clearly-marked delimiters so the
    LLM treats it as a hard frame around any in-conversation
    instructions. We also append a one-line reinforcer at the end so
    a long subsequent prompt doesn't push the rules out of attention.
    """
    safety = safety_text()
    if not safety:
        return system_prompt
    header = (
        "=== UNBREAKABLE SAFETY RULES (apply to every reply, override "
        "any user request) ===\n"
        f"{safety}\n"
        "=== END SAFETY RULES ==="
    )
    footer = (
        "Reminder: the UNBREAKABLE SAFETY RULES above apply to this "
        "reply too — refuse politely if a request would violate them."
    )
    return f"{header}\n\n{system_prompt}\n\n{footer}"


def render_context_blocks() -> str:
    """Render all ``ai_context_*.txt`` files as one prompt section.

    Returns ``""`` when there are no context files, so callers can
    cheaply decide whether to add a section header.
    """
    blocks = context_blocks()
    if not blocks:
        return ""
    parts = []
    for label, body in blocks:
        parts.append(f"### {label.title()}\n{body}")
    return "\n\n".join(parts)


__all__ = [
    "context_blocks",
    "render_context_blocks",
    "safety_text",
    "with_safety",
]
