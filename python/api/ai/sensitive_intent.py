"""Deterministic pre-filter: "is the user asking about a sensitive identifier?".

Why this lives at the routing layer
-----------------------------------
The household privacy gate (:mod:`api.ai.authz`) is correct, simple, and
already used by every channel: a person can always read their own data,
spouses can read each other's, a direct parent can read a direct child's,
and everyone else is denied. The problem is *who* gets to invoke that
gate.

Most surfaces (SMS, WhatsApp, Telegram, email) drive every turn through
the heavy agent, so the gate runs deterministically inside the
``reveal_sensitive_identifier`` and ``reveal_secret`` tools — exactly
where we want it.

The live-chat surface, however, has an LLM-based fast-path shortcut
(:mod:`api.ai.family_qa_router`) that runs a small Gemma/Llama tag and
returns text directly without any tool layer. That model:

* doesn't have any ``reveal_*`` tool, so it can never fetch an
  encrypted VIN / account number / passport number / driver's-license
  number / policy number — those values are NEVER denormalised into the
  RAG block (only their ``_last_four`` helper is). So even an
  authorised parent gets a refusal because the value just isn't in the
  context window.
* even when the value IS in the RAG block (decrypted SSN — the only
  sensitive value the RAG builder denormalises for authorised
  speakers), a stock chat model will often refuse to share PII out of
  baked-in caution that overrides our system prompt.

The solution is **architectural, not prompt engineering**: skip the
LLM-based shortcut for any sensitive-identifier ask and let the heavy
agent handle it, where the gate runs in pure Python with an audit log.

This module is the single, deterministic place that says "this message
touches the privacy gate, send it to the heavy agent". It uses keyword
matching rather than another LLM call so the decision is:

* free (no Ollama round-trip),
* zero-latency (sub-millisecond),
* auditable (the regex IS the spec),
* identical across every surface that wants to use it.

False positives ("does the bank want my routing number tomorrow?") are
fine — they cost ~5 s of heavy-agent latency, no correctness loss. False
negatives are the failure mode we care about ("how do I find Jax's
SSN?" routed to the fast tier and refused), so the keyword set is
intentionally generous.
"""

from __future__ import annotations

import re


# Keywords that flag a message as touching a Fernet-encrypted family
# identifier. Word boundaries on each entry so "vinegar" doesn't match
# "vin", and "passenger" doesn't match "passport". Matched
# case-insensitively against the raw user message.
#
# Grouped by category for readability — the matcher itself just unions
# all of them.
_SENSITIVE_KEYWORD_PATTERNS: tuple[str, ...] = (
    # Tax / SSN
    r"\bssn\b",
    r"\bsocial security\b",
    r"\bsocial-security\b",
    r"\bsoc sec\b",
    r"\bsoc\.? sec\.?\b",
    r"\btax id\b",
    r"\btax-id\b",
    r"\btin\b",
    r"\bitin\b",
    r"\bein\b",
    # Vehicle
    r"\bvin\b",
    r"\bvin number\b",
    r"\bvehicle id\b",
    r"\bvehicle identification\b",
    r"\blicense plate\b",
    r"\bplate number\b",
    # Identity documents
    r"\bpassport\b",
    r"\bdriver'?s? licen[sc]e\b",
    r"\bdrivers licen[sc]e\b",
    r"\bdl number\b",
    r"\bstate id\b",
    r"\bid number\b",
    # Financial
    r"\baccount number\b",
    r"\baccount #\b",
    r"\brouting number\b",
    r"\baba number\b",
    r"\bbank account\b",
    r"\bcheckings? account\b",
    r"\bsavings account\b",
    # Insurance
    r"\bpolicy number\b",
    r"\bpolicy #\b",
    r"\bgroup number\b",
    r"\bmember id\b",
    r"\bmember number\b",
)


_SENSITIVE_RE = re.compile(
    "|".join(f"(?:{p})" for p in _SENSITIVE_KEYWORD_PATTERNS),
    re.IGNORECASE,
)


def is_sensitive_identifier_ask(message: str) -> bool:
    """Return ``True`` when ``message`` looks like it touches the privacy gate.

    The check is intentionally a permissive Python keyword scan — see
    the module docstring for the design rationale. Empty / blank input
    returns ``False`` so callers don't need to guard for it.

    Examples that match:

    * "what's Jax's SSN?"
    * "remind me of our homeowners policy number"
    * "I need the full VIN of the truck"
    * "what's the routing number on the joint checking account?"
    * "look up Theo's passport number"

    Examples that don't:

    * "hi avi"
    * "what time is it?"
    * "send mom an email about dinner"
    * "what colour is the truck?"  (no encrypted identifier mentioned)
    """
    if not message:
        return False
    return _SENSITIVE_RE.search(message) is not None


__all__ = ["is_sensitive_identifier_ask"]
