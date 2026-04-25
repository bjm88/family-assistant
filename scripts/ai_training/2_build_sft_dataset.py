#!/usr/bin/env python3
"""Expand the corpus + templates into MLX-LM SFT JSONL.

Produces two files under ``artifacts/dataset/``:

* ``train.jsonl`` — the main supervised fine-tuning set.
* ``valid.jsonl`` — a small held-out split (see ``sft.valid_split``).

Output format follows ``mlx_lm.lora``'s ``chat`` schema::

    {"messages": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."}
    ]}

Each training example falls into one of these buckets:

    schema_qa     — expands templates/schema_questions.yaml over every
                    table + column in artifacts/corpus/schema.yaml.
    sql_patterns  — expands templates/sql_patterns.yaml over the same.
    api_docs      — one example per FastAPI route (route purpose Q&A).
    integrations  — one example per file under python/api/integrations/
                    using each module's docstring + a curated
                    INTEGRATION_PROFILES entry. Teaches the model to
                    name real systems (Gmail, Twilio, Telegram, Google
                    Calendar, Gemini, …) when describing what the
                    agent will do.
    labeled       — templates/labeled_examples.jsonl, the hand-curated
                    gold data. Each row carries an explicit
                    {decision: FAMILY|HEAVY|GEMINI_WEB_API|CALENDAR_API}
                    field that doubles as routing + answer supervision.
    negative      — synthesized "I can't do that, escalating" examples
                    that teach the model to hand off complex asks
                    NOT already covered by labeled HEAVY rows.

Per-bucket caps come from ``config.yaml`` (``sft.max_per_bucket``) so
no single source can dominate the loss surface.

Usage::

    uv run python 2_build_sft_dataset.py            # build
    uv run python 2_build_sft_dataset.py --inspect  # stats + 20 samples
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

logger = logging.getLogger("build_sft")

HERE = Path(__file__).resolve().parent


def load_config() -> Dict[str, Any]:
    with open(HERE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(cfg: Dict[str, Any], key_path: List[str]) -> Path:
    value: Any = cfg
    for k in key_path:
        value = value[k]
    p = Path(value)
    if not p.is_absolute():
        p = (HERE / p).resolve()
    return p


# ---------------------------------------------------------------------------
# Tiny humaniser helpers — used in every bucket.
# ---------------------------------------------------------------------------

_SIMPLE_SINGULAR = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
    "families": "family",
    "identities": "identity",
    "activities": "activity",
    "policies": "policy",
}


def singularize(name: str) -> str:
    """Crude plural→singular for table names. Good enough for English words."""
    if name in _SIMPLE_SINGULAR:
        return _SIMPLE_SINGULAR[name]
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("ses") or name.endswith("xes"):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def humanise(snake: str) -> str:
    """`license_plate_last_four` → `license plate last four`."""
    return snake.replace("_", " ")


def lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


# ---------------------------------------------------------------------------
# Sample columns — pick a representative text column per table so the
# SQL templates can substitute something realistic.
# ---------------------------------------------------------------------------

_COLUMN_BLOCKLIST_SUFFIXES = ("_encrypted", "_id", "_at")
_COLUMN_BLOCKLIST = {"family_id", "created_at", "updated_at"}


def pick_sample_column(columns: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Best-effort pick of a representative human-readable column."""
    # Prefer anything ending in _name or _description.
    for c in columns:
        if c["name"].endswith(("_name", "_description")):
            return c
    # Else first text/varchar column not in block list.
    for c in columns:
        if c["name"] in _COLUMN_BLOCKLIST:
            continue
        if any(c["name"].endswith(s) for s in _COLUMN_BLOCKLIST_SUFFIXES):
            continue
        if "char" in c["data_type"].lower() or "text" in c["data_type"].lower():
            return c
    # Final fallback: any column not in block list.
    for c in columns:
        if c["name"] not in _COLUMN_BLOCKLIST and c["name"] != "family_id":
            return c
    return None


def has_column(table: Dict[str, Any], name: str) -> bool:
    return any(c["name"] == name for c in table["columns"])


# ---------------------------------------------------------------------------
# Bucket builders
# ---------------------------------------------------------------------------


def build_schema_qa(
    schema: List[Dict[str, Any]],
    templates_path: Path,
) -> List[Dict[str, Any]]:
    """Expand per-table + per-column Q&A."""
    with open(templates_path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    out: List[Dict[str, Any]] = []

    for table in schema:
        fmt = {
            "table": table["name"],
            "table_human": singularize(humanise(table["name"])),
            "table_desc": (table.get("description") or "").strip(),
            "table_desc_lower": lower_first(
                (table.get("description") or "").strip()
            ),
        }
        if not fmt["table_desc"]:
            # If we have no COMMENT, skip the per-table expansions that
            # would otherwise render "The X table holds ." — kept intact
            # only for templates that don't use table_desc.
            continue
        for tpl in spec.get("per_table", []):
            out.append(_pair_from_tpl(tpl, fmt, bucket="schema_qa"))

        for col in table["columns"]:
            if not col.get("description"):
                continue
            if col["name"].endswith("_encrypted"):
                continue
            col_fmt = {
                **fmt,
                "column": col["name"],
                "column_human": humanise(col["name"]),
                "data_type": col["data_type"],
                "column_desc": col["description"].strip(),
                "nullable_answer": (
                    "Yes, it can be null."
                    if col["nullable"]
                    else "No, it's required."
                ),
            }
            for tpl in spec.get("per_column", []):
                out.append(_pair_from_tpl(tpl, col_fmt, bucket="schema_qa"))

    return [x for x in out if x is not None]


def build_sql_patterns(
    schema: List[Dict[str, Any]],
    templates_path: Path,
) -> List[Dict[str, Any]]:
    """Expand SQL templates against matching tables."""
    with open(templates_path, "r", encoding="utf-8") as f:
        templates = yaml.safe_load(f)

    # Deterministic pseudo-data — no randomness so re-runs produce the
    # same training set (reproducible fine-tunes).
    family_ids = [1, 2, 3]
    names = ["Ben", "Sara", "Mia", "Theo", "Ellie", "Alex", "Jordan"]
    needles = ["urgent", "travel", "school", "2025", "new"]

    out: List[Dict[str, Any]] = []

    for tpl in templates:
        applies = tpl.get("applies_to", "all")
        for tbl in schema:
            if not _matches(applies, tbl):
                continue

            sample = pick_sample_column(tbl["columns"])
            if sample is None:
                continue

            base_fmt = {
                "table": tbl["name"],
                "table_human": singularize(humanise(tbl["name"])),
                "sample_col": sample["name"],
                "sample_col_human": humanise(sample["name"]),
            }
            for fam in family_ids:
                fmt = {**base_fmt, "fam": fam}
                for name in names[:3]:
                    fmt = {**fmt, "name": name}
                    for needle in needles[:2]:
                        fmt2 = {**fmt, "needle": needle}
                        pair = _pair_from_tpl(
                            tpl, fmt2, bucket="sql_patterns"
                        )
                        if pair is not None:
                            out.append(pair)

    return out


def _matches(applies_to: Any, table: Dict[str, Any]) -> bool:
    if applies_to == "all":
        return True
    if applies_to == "has_family_id":
        return has_column(table, "family_id")
    if isinstance(applies_to, dict):
        if "has_column" in applies_to:
            return has_column(table, applies_to["has_column"])
        if "table_in" in applies_to:
            return table["name"] in applies_to["table_in"]
    # "has_column: person_id" parses as "has_column: person_id" in YAML —
    # handle the string form too.
    if isinstance(applies_to, str) and applies_to.startswith("has_column:"):
        col = applies_to.split(":", 1)[1].strip()
        return has_column(table, col)
    return False


# ---------------------------------------------------------------------------
# Integrations bucket
# ---------------------------------------------------------------------------
#
# The dump (1_dump_corpus.py) writes integrations.yaml — one row per
# file under python/api/integrations/, each with the module docstring.
# The Q→A pairs we generate here let the fine-tuned model name those
# real systems (Gmail, Twilio, etc.) when it describes what the agent
# will do, rather than saying generic things like "the email tool".
#
# INTEGRATION_PROFILES is the curated semantic layer on top of the
# raw docstrings — it gives us nice human display names + capability
# verbs the templates can plug in. Files listed here generate the
# richer "what does the agent use for X?" templates; files NOT listed
# still get the docstring-based "what does the X integration do?"
# pair (so a brand-new integration gets minimal coverage automatically
# until you add a profile entry for it).

INTEGRATION_PROFILES: Dict[str, Dict[str, str]] = {
    "gmail": {
        "display": "Gmail",
        "use_for": "send and read email",
        "capability_q": "How does the agent send email?",
        "capability_a": (
            "Through the Gmail integration — outbound mail goes via the "
            "Gmail API with the household's connected Google account, "
            "and the inbox poller reads incoming mail from registered "
            "family members."
        ),
    },
    "twilio_sms": {
        "display": "Twilio",
        "use_for": "SMS, MMS, and WhatsApp",
        "capability_q": "How does the agent send texts?",
        "capability_a": (
            "Through Twilio. The same adapter handles SMS, MMS, and "
            "WhatsApp — WhatsApp just adds a `whatsapp:` prefix on the "
            "from/to fields. Inbound texts are verified against "
            "Twilio's HMAC signature before they touch the database."
        ),
    },
    "telegram": {
        "display": "Telegram",
        "use_for": "two-way chat over the Telegram Bot API",
        "capability_q": "Does the agent talk to Telegram?",
        "capability_a": (
            "Yes — there's a Telegram bot adapter that long-polls the "
            "Bot API for new messages and replies via sendMessage. No "
            "public URL needed; just a TELEGRAM_BOT_TOKEN."
        ),
    },
    "google_calendar": {
        "display": "Google Calendar",
        "use_for": "calendar reads and writes",
        "capability_q": "How does the agent see calendars?",
        "capability_a": (
            "Through the Google Calendar API. The assistant can see its "
            "own calendar plus any household calendar a member has "
            "shared with it — free/busy lookups, upcoming events, free-"
            "slot search, and (when the person allows it) creating new "
            "events."
        ),
    },
    "google_oauth": {
        "display": "Google OAuth",
        "use_for": "authentication for Gmail and Calendar",
        "capability_q": "How does the agent get into Google?",
        "capability_a": (
            "A small OAuth helper runs the auth-code flow against "
            "/oauth/callback and stores encrypted credentials in "
            "Postgres. Gmail and Calendar both use the same token; "
            "auto-refresh is handled centrally."
        ),
    },
    "gemini": {
        "display": "Gemini",
        "use_for": (
            "image generation, multimodal analysis, and grounded web "
            "search"
        ),
        "capability_q": "What does the agent use Gemini for?",
        "capability_a": (
            "Gemini handles three jobs: avatar/image generation, "
            "multimodal analysis on inbound attachments, and grounded "
            "web search (it returns a synthesised answer with citations "
            "in one round trip)."
        ),
    },
    "web_search": {
        "display": "web search",
        "use_for": "research and monitoring tasks",
        "capability_q": "How does the agent do web research?",
        "capability_a": (
            "Through a pluggable web-search interface. The default "
            "provider is Gemini's grounded `google_search` tool, with "
            "Brave and Tavily as drop-in alternatives selected by the "
            "FA_SEARCH_PROVIDER setting."
        ),
    },
    "doorbird_gate": {
        "display": "DoorBird",
        "use_for": "front-gate intercom and unlock",
        "capability_q": "Can the agent open the gate?",
        "capability_a": (
            "There's a DoorBird adapter wired up, but it's currently "
            "disabled — DoorBird's cloud API isn't open to general "
            "developers, so the integration only works on LAN once the "
            "device firmware exposes it."
        ),
    },
}


def build_integrations(corpus_dir: Path) -> List[Dict[str, Any]]:
    """Expand integrations.yaml + INTEGRATION_PROFILES into Q→A pairs.

    Two flavours per integration:

      1. Docstring-based: "What is the {module} integration?" → first
         sentence of the module docstring.
      2. Profile-based: the curated capability_q/capability_a pair
         from INTEGRATION_PROFILES (only if the module has an entry).

    Plus a single roll-up "What integrations does the agent have?"
    pair listing every module the dump found.
    """
    path = corpus_dir / "integrations.yaml"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        rows = yaml.safe_load(f) or []

    out: List[Dict[str, Any]] = []
    seen_modules: List[str] = []

    for row in rows:
        module = (row.get("module") or "").strip()
        summary = (row.get("summary") or "").strip()
        if not module or not summary:
            continue
        seen_modules.append(module)
        profile = INTEGRATION_PROFILES.get(module, {})
        display = profile.get("display") or module.replace("_", " ")

        out.append(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"What is the {display} integration?"
                        ),
                    },
                    {"role": "assistant", "content": summary},
                ],
                "_bucket": "integrations",
            }
        )
        out.append(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"What does the agent use {display} for?"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": (
                            profile.get("use_for")
                            and (
                                f"The agent uses {display} for "
                                f"{profile['use_for']}."
                            )
                            or summary
                        ),
                    },
                ],
                "_bucket": "integrations",
            }
        )

        capability_q = profile.get("capability_q")
        capability_a = profile.get("capability_a")
        if capability_q and capability_a:
            out.append(
                {
                    "messages": [
                        {"role": "user", "content": capability_q},
                        {"role": "assistant", "content": capability_a},
                    ],
                    "_bucket": "integrations",
                }
            )

    # Roll-up: a single "what integrations do you have" answer that
    # names every discovered module. Helpful both for self-introspection
    # questions and as a sanity check after rebuilding the corpus.
    if seen_modules:
        names = ", ".join(
            INTEGRATION_PROFILES.get(m, {}).get("display")
            or m.replace("_", " ")
            for m in seen_modules
        )
        out.append(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "What integrations does the agent have?",
                    },
                    {
                        "role": "assistant",
                        "content": (
                            f"The agent is wired up to: {names}."
                        ),
                    },
                ],
                "_bucket": "integrations",
            }
        )

    return out


def build_api_docs(corpus_dir: Path) -> List[Dict[str, Any]]:
    apis_path = corpus_dir / "apis.yaml"
    if not apis_path.exists():
        return []
    with open(apis_path, "r", encoding="utf-8") as f:
        routes = yaml.safe_load(f) or []

    out: List[Dict[str, Any]] = []
    for r in routes:
        summary = (r.get("summary") or "").strip()
        path = r["path"]
        method = r.get("method") or "GET"
        if not summary:
            continue
        out.append(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": f"What does {method} {path} do?",
                    },
                    {"role": "assistant", "content": summary},
                ],
                "_bucket": "api_docs",
            }
        )
        # Inverted variant — "which endpoint does X?" given the summary.
        out.append(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Which endpoint should I hit to {lower_first(summary)}"
                            if summary[-1] != "?" else f"Which endpoint {summary}"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": f"{method} {path}.",
                    },
                ],
                "_bucket": "api_docs",
            }
        )
    return out


# Default reply templates per non-FAMILY decision. Used only when a
# row in labeled_examples.jsonl omits an explicit `response`. The
# rotation is deterministic (keyed on the file line number) so re-runs
# produce the same SFT corpus.
_DEFAULT_REPLIES: Dict[str, tuple[str, ...]] = {
    "HEAVY": (
        "Handing this to the full agent.",
        "That needs the full agent — one second.",
        "Passing this to the agent.",
    ),
    "GEMINI_WEB_API": (
        "Let me check the web for that.",
        "Looking that up online — one sec.",
        "Pulling that from a web search.",
    ),
    "CALENDAR_API": (
        "Let me check your calendar.",
        "Pulling that from your calendar.",
        "On it — checking the calendar.",
    ),
}

# All decision values the curation file is allowed to use. Adding a new
# value here is a deliberate vocabulary expansion — the fast model
# only learns the routing categories we list.
_ALLOWED_DECISIONS = ("FAMILY", "HEAVY", "GEMINI_WEB_API", "CALENDAR_API")


def build_labeled_examples(path: Path) -> List[Dict[str, Any]]:
    """Parse the hand-curated labeled_examples.jsonl gold file.

    Schema (per non-comment, non-blank line):

        {
            "user":     str,                       # required
            "decision": <one of _ALLOWED_DECISIONS>,  # required
            "response": str,                       # required for FAMILY
                                                   # and CALENDAR_API;
                                                   # optional otherwise
            "category": str,                       # optional, stats
            "notes":    str                        # optional, ignored
        }

    Decision semantics:

        FAMILY          — fast model answers directly from RAG.
                          `response` is the gold answer.
        HEAVY           — fast model defers to the full agent (tools,
                          attachments, multi-step research, monitoring,
                          anything that needs to coordinate with an
                          external party like booking with a vendor).
        GEMINI_WEB_API  — fast model defers to the web-search/Gemini
                          grounded path (live data: time, weather,
                          math, stocks, news, world facts). In
                          production these are caught upstream by
                          `web_search_shortcut` before the fast model
                          runs — labeling here is a safety-net
                          fallback.
        CALENDAR_API    — fast model handles calendar reads (free/busy,
                          summarise a day) and simple writes (block a
                          hold, move an existing event) directly via
                          the calendar tool. `response` shows the gold
                          shape of the answer/confirmation; at
                          inference time real calendar data flows in.

    Validation is strict — a malformed row halts the build with a
    clear file:line pointer. The point of this file is human review,
    so silently dropping bad rows would defeat the purpose.
    """
    if not path.exists():
        logger.warning("%s missing — labeled bucket will be empty", path)
        return []

    out: List[Dict[str, Any]] = []
    decisions: Counter[str] = Counter()

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"{path}:{lineno}: invalid JSON — {exc.msg}\n"
                f"  line: {line[:120]}"
            )

        user = (obj.get("user") or "").strip()
        decision = (obj.get("decision") or "").strip().upper()
        response = (obj.get("response") or "").strip()

        if not user:
            raise SystemExit(f"{path}:{lineno}: missing 'user' field")
        if decision not in _ALLOWED_DECISIONS:
            raise SystemExit(
                f"{path}:{lineno}: 'decision' must be one of "
                f"{_ALLOWED_DECISIONS} (got {decision!r})"
            )
        if decision in ("FAMILY", "CALENDAR_API") and not response:
            raise SystemExit(
                f"{path}:{lineno}: {decision} rows require 'response' — "
                f"the model needs the gold answer/confirmation shape "
                f"to learn from. Either add a response or change "
                f"decision to HEAVY."
            )
        if not response:
            pool = _DEFAULT_REPLIES[decision]
            response = pool[lineno % len(pool)]

        decisions[decision] += 1
        out.append(
            {
                "messages": [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": response},
                ],
                "_bucket": "labeled",
                "_decision": decision,
                "_category": (obj.get("category") or "").strip() or None,
            }
        )

    logger.info(
        "labeled_examples.jsonl: %d rows (FAMILY=%d, HEAVY=%d)",
        sum(decisions.values()),
        decisions.get("FAMILY", 0),
        decisions.get("HEAVY", 0),
    )
    return out


def build_negatives() -> List[Dict[str, Any]]:
    """Canonical 'I'm the fast tier — escalate' examples.

    Teaches the fine-tuned fast model to defer on flows that genuinely
    need the heavy agent (tools, attachments, multi-step research)
    instead of hallucinating an answer it can't back up.
    """
    pairs = [
        ("send an email to my wife", "That needs the email tool — I'll hand this to the full agent."),
        ("attach the insurance PDF", "Attachments go through the full agent, not me."),
        ("research private schools in Seattle", "That's a web-research task — passing to the agent with search enabled."),
        ("book a dentist appointment", "Scheduling tools sit on the full agent. Handing off."),
        ("draft a long reply to this email", "Long-form drafting belongs to the full agent. One sec."),
        ("analyze the attached photo", "I can't see images directly. The agent handles attachments — one second."),
        ("summarize this 30-page PDF", "That's too large for me — routing to the full agent."),
        ("run a monitoring job for me", "Setting up monitoring tasks goes through the agent tools."),
        ("open the gate", "Gate control is an agent action, not something I handle directly."),
        ("call my mom", "I don't place calls — that's outside my scope."),
    ]
    return [
        {
            "messages": [
                {"role": "user", "content": q},
                {"role": "assistant", "content": a},
            ],
            "_bucket": "negative",
        }
        for q, a in pairs
    ]


# ---------------------------------------------------------------------------
# Template → message pair
# ---------------------------------------------------------------------------


_MISSING_SENTINEL = object()


def _pair_from_tpl(
    tpl: Dict[str, Any],
    fmt: Dict[str, Any],
    *,
    bucket: str,
) -> Optional[Dict[str, Any]]:
    """Render a template (q, a) pair. Drops the row if any {placeholder}
    would format to an empty string — keeps the corpus clean."""
    try:
        q = tpl["q"].format(**fmt).strip()
        a = tpl["a"].format(**fmt).strip()
    except KeyError as e:
        logger.debug("template missing key %s in %s", e, fmt.get("table"))
        return None

    if not q or not a or "{" in q or "{" in a:
        return None
    if "None" in q or "None" in a:
        return None
    # Drop obvious empty answers ("The X table holds .").
    if re.search(r"holds \.$|store $|for \.$", a):
        return None

    return {
        "messages": [
            {"role": "user", "content": q},
            {"role": "assistant", "content": a},
        ],
        "_bucket": bucket,
    }


# ---------------------------------------------------------------------------
# Cap, dedupe, split
# ---------------------------------------------------------------------------


def _hash(messages: List[Dict[str, str]]) -> str:
    h = hashlib.sha1()
    for m in messages:
        h.update((m["role"] + "\x1f" + m["content"] + "\x1e").encode("utf-8"))
    return h.hexdigest()


def cap_and_dedupe(
    examples: Iterable[Dict[str, Any]],
    caps: Dict[str, Optional[int]],
    seed: int = 42,
) -> List[Dict[str, Any]]:
    rnd = random.Random(seed)
    by_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for ex in examples:
        h = _hash(ex["messages"])
        if h in seen:
            continue
        seen.add(h)
        by_bucket[ex["_bucket"]].append(ex)

    out: List[Dict[str, Any]] = []
    for bucket, rows in by_bucket.items():
        cap = caps.get(bucket)
        if cap is not None and len(rows) > cap:
            rnd.shuffle(rows)
            rows = rows[:cap]
        out.extend(rows)
    rnd.shuffle(out)
    return out


def split(
    examples: List[Dict[str, Any]],
    valid_ratio: float,
    seed: int = 42,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rnd = random.Random(seed + 1)
    shuffled = examples[:]
    rnd.shuffle(shuffled)
    n_valid = max(10, int(len(shuffled) * valid_ratio))
    return shuffled[n_valid:], shuffled[:n_valid]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            # Strip our internal marker before writing.
            f.write(json.dumps({"messages": r["messages"]}, ensure_ascii=False))
            f.write("\n")
            n += 1
    return n


def inspect(examples: List[Dict[str, Any]]) -> None:
    counts = Counter(ex["_bucket"] for ex in examples)
    print("\n=== bucket counts ===")
    for k, v in counts.most_common():
        print(f"  {k:16s}  {v:6d}")
    print(f"  {'TOTAL':16s}  {sum(counts.values()):6d}\n")

    # Decision + category breakdown for the labeled bucket (most useful
    # while curating templates/labeled_examples.jsonl).
    labeled = [ex for ex in examples if ex["_bucket"] == "labeled"]
    if labeled:
        d_counts = Counter(ex.get("_decision", "?") for ex in labeled)
        c_counts = Counter(
            ex.get("_category") or "(none)" for ex in labeled
        )
        print("=== labeled bucket: decisions ===")
        for k, v in d_counts.most_common():
            print(f"  {k:16s}  {v:6d}")
        print()
        print("=== labeled bucket: categories ===")
        for k, v in c_counts.most_common():
            print(f"  {k:16s}  {v:6d}")
        print()

    print("=== 20 random samples ===")
    rnd = random.Random(0)
    for ex in rnd.sample(examples, min(20, len(examples))):
        u = ex["messages"][0]["content"]
        a = ex["messages"][1]["content"]
        suffix = ""
        if ex["_bucket"] == "labeled":
            suffix = f" decision={ex.get('_decision')}"
        print(f"[{ex['_bucket']}{suffix}]")
        print(f"  Q: {u[:100]}")
        print(f"  A: {a[:200]}")
        print()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--inspect",
        action="store_true",
        help="After building, print bucket counts + 20 random rows and exit.",
    )
    args = ap.parse_args()

    cfg = load_config()
    corpus_dir = resolve_path(cfg, ["corpus", "output_dir"])
    out_dir = resolve_path(cfg, ["sft", "output_dir"])
    caps = cfg["sft"].get("max_per_bucket", {})
    valid_ratio = float(cfg["sft"]["valid_split"])

    schema_path = corpus_dir / "schema.yaml"
    if not schema_path.exists():
        raise SystemExit(
            f"{schema_path} not found — run 1_dump_corpus.py first."
        )
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = yaml.safe_load(f) or []

    templates_dir = HERE / "templates"

    buckets = (
        build_schema_qa(schema, templates_dir / "schema_questions.yaml")
        + build_sql_patterns(schema, templates_dir / "sql_patterns.yaml")
        + build_api_docs(corpus_dir)
        + build_integrations(corpus_dir)
        + build_labeled_examples(templates_dir / "labeled_examples.jsonl")
        + build_negatives()
    )
    logger.info("generated %d raw examples (pre-cap)", len(buckets))

    capped = cap_and_dedupe(buckets, caps)
    logger.info(
        "after cap+dedupe: %d examples across %d buckets",
        len(capped),
        len({x["_bucket"] for x in capped}),
    )

    if args.inspect:
        inspect(capped)
        return 0

    train, valid = split(capped, valid_ratio)
    n_train = write_jsonl(out_dir / "train.jsonl", train)
    n_valid = write_jsonl(out_dir / "valid.jsonl", valid)
    logger.info(
        "wrote %d train + %d valid examples to %s",
        n_train,
        n_valid,
        out_dir,
    )

    # Manifest so downstream scripts (and operators) can see what was built.
    manifest = {
        "n_train": n_train,
        "n_valid": n_valid,
        "buckets": dict(Counter(ex["_bucket"] for ex in capped)),
        "caps": caps,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
