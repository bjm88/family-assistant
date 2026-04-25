#!/usr/bin/env python3
"""Dump the live app's structure into a training-ready corpus.

Outputs five YAML files under ``artifacts/corpus/``:

* ``schema.yaml``        — every table + column from ``llm_schema_catalog``
  (same view the live LLM reads). Encrypted columns are filtered out.
* ``apis.yaml``          — every FastAPI route: path, method, summary
  (from the route docstring's first line), full-docstring narrative.
* ``tools.yaml``         — Avi's tool registry
  (``python/api/ai/tools.py``): name, description, parameter schema.
* ``integrations.yaml``  — every adapter under
  ``python/api/integrations/`` with its module docstring. Lets the
  fine-tune name real systems (Gmail, Twilio, Telegram, Google
  Calendar, Gemini, …) when describing what the agent will do.
* ``prompts.yaml``       — persona + safety prompts so the fine-tune
  can absorb Avi's voice guidelines.

The output is plain YAML (never embedded in JSON) so you can open it
and audit what the training data will see before committing to a
fine-tune run.

Usage::

    cd scripts/ai_training
    uv run python 1_dump_corpus.py              # live DB
    uv run python 1_dump_corpus.py --dry-run    # no DB, schema from models
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("dump_corpus")


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
PYTHON_API = REPO_ROOT / "python" / "api"

# Add the repo's python/ to sys.path so we can import api.* modules
# (matching how FastAPI runs: `uvicorn --app-dir python api.main:app`).
sys.path.insert(0, str(REPO_ROOT / "python"))


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool
    description: Optional[str] = None


@dataclass
class Table:
    name: str
    description: Optional[str] = None
    columns: List[Column] = field(default_factory=list)


@dataclass
class Route:
    method: str
    path: str
    summary: str
    description: str
    module: str
    operation_id: Optional[str] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    requires: Optional[str] = None


@dataclass
class Integration:
    """One file under ``python/api/integrations/`` with its module docstring.

    The fine-tune uses these to teach the model "what does this app
    actually call when it sends an email / texts someone / opens the
    gate?" so escalation phrasing references real systems by name
    instead of generic "the messaging tool".
    """

    module: str  # filename without extension, e.g. "gmail"
    summary: str  # first sentence of the docstring
    description: str  # full docstring


def load_config() -> Dict[str, Any]:
    cfg_path = HERE / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(cfg: Dict[str, Any], key_path: List[str]) -> Path:
    """Resolve a nested config path (possibly relative) against HERE."""
    value: Any = cfg
    for k in key_path:
        value = value[k]
    p = Path(value)
    if not p.is_absolute():
        p = (HERE / p).resolve()
    return p


# ---------------------------------------------------------------------------
# Schema dump — reuses api.ai.schema_catalog for consistency with runtime.
# ---------------------------------------------------------------------------


def dump_schema_live() -> List[Table]:
    """Pull the schema by talking to Postgres via the live app's code."""
    from api.ai.schema_catalog import fetch_catalog  # type: ignore
    from api.db import SessionLocal  # type: ignore

    db = SessionLocal()
    try:
        rows = fetch_catalog(db)
    finally:
        db.close()

    by_table: Dict[str, Table] = {}
    for r in rows:
        t = by_table.setdefault(
            r.table_name,
            Table(name=r.table_name, description=r.table_description),
        )
        t.columns.append(
            Column(
                name=r.column_name,
                data_type=r.data_type,
                nullable=r.is_nullable,
                description=r.column_description,
            )
        )
    return [by_table[k] for k in sorted(by_table)]


def dump_schema_from_models() -> List[Table]:
    """Fallback: derive the schema from SQLAlchemy models without a DB."""
    import importlib
    import pkgutil

    # Walk api.models/* so every class registers on Base.metadata.
    models_pkg = importlib.import_module("api.models")
    for _, name, _ in pkgutil.walk_packages(
        models_pkg.__path__, prefix="api.models."
    ):
        importlib.import_module(name)

    from api.db import Base  # type: ignore

    tables: List[Table] = []
    for t in sorted(Base.metadata.tables.values(), key=lambda x: x.name):
        cols: List[Column] = []
        for c in t.columns:
            cols.append(
                Column(
                    name=c.name,
                    data_type=str(c.type),
                    nullable=bool(c.nullable),
                    description=c.comment,
                )
            )
        tables.append(
            Table(name=t.name, description=t.comment, columns=cols)
        )
    return tables


# ---------------------------------------------------------------------------
# FastAPI routes — extracted from the running app (no DB needed).
# ---------------------------------------------------------------------------


def dump_routes() -> List[Route]:
    """Introspect the FastAPI app to list every real HTTP route."""
    from fastapi.routing import APIRoute

    from api.main import app  # type: ignore

    out: List[Route] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        # Skip the landing / legal / internal static mounts.
        if route.path.startswith(("/legal", "/static", "/assets")):
            continue
        doc = (route.endpoint.__doc__ or "").strip()
        summary, _, detail = doc.partition("\n\n")
        out.append(
            Route(
                method=",".join(sorted(route.methods or [])),
                path=route.path,
                summary=summary.strip().split("\n")[0] if summary else "",
                description=detail.strip() or summary.strip(),
                module=route.endpoint.__module__,
                operation_id=route.operation_id,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Tool registry — avoids importing the runtime registry (which needs a DB
# session + capability detection). We AST-parse the registry file(s) and
# pull every ``Tool(name=..., description=..., parameters=...)`` invocation.
# Good enough to feed the LLM the tool names + purpose.
#
# Tools used to live in a single ``python/api/ai/tools.py``; they now live
# under ``python/api/ai/tools/`` (registry + per-domain handlers). We try
# both layouts so this script works across refactors.
# ---------------------------------------------------------------------------


def _candidate_tool_files() -> List[Path]:
    """Return every Python file that could contain a ``Tool(...)`` call."""
    candidates: List[Path] = []
    legacy = PYTHON_API / "ai" / "tools.py"
    if legacy.exists():
        candidates.append(legacy)
    pkg = PYTHON_API / "ai" / "tools"
    if pkg.exists():
        candidates.extend(sorted(pkg.rglob("*.py")))
    return candidates


def dump_tools() -> List[ToolSpec]:
    files = _candidate_tool_files()
    if not files:
        logger.warning(
            "no tool registry files found under %s — skipping", PYTHON_API / "ai"
        )
        return []
    specs: List[ToolSpec] = []
    seen_names: set[str] = set()
    for tools_path in files:
        try:
            src = tools_path.read_text()
            tree = ast.parse(src)
        except (OSError, SyntaxError) as exc:
            logger.warning("skip %s — %s", tools_path.name, exc)
            continue
        specs.extend(_collect_tool_specs(tree, seen_names))
    return specs


def _collect_tool_specs(
    tree: ast.AST, seen_names: set[str]
) -> List[ToolSpec]:
    out: List[ToolSpec] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else None
        )
        if fname != "Tool":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        name = _literal(kwargs.get("name"))
        desc = _literal(kwargs.get("description"))
        params = _literal(kwargs.get("parameters")) or {}
        requires = _literal(kwargs.get("requires"))
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        out.append(
            ToolSpec(
                name=name,
                description=(desc or "").strip(),
                parameters=params if isinstance(params, dict) else {},
                requires=requires,
            )
        )
    return out


def _literal(node: Optional[ast.AST]) -> Any:
    """Best-effort eval of an AST literal node. Returns None on failure."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        # Fallback for things like string concatenation in kwargs —
        # stringify the raw source.
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Integration dump — walks ``python/api/integrations/*.py`` and pulls each
# module's docstring. AST-parses (no imports) so missing optional deps
# (twilio, telegram, google libs) don't break the dump.
# ---------------------------------------------------------------------------


def dump_integrations() -> List[Integration]:
    integ_dir = PYTHON_API / "integrations"
    if not integ_dir.exists():
        logger.warning("%s missing — skipping integrations dump", integ_dir)
        return []

    out: List[Integration] = []
    for path in sorted(integ_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            logger.warning("skip %s — syntax error: %s", path.name, exc)
            continue

        doc = ast.get_docstring(tree) or ""
        doc = doc.strip()
        if not doc:
            logger.warning(
                "skip %s — no module docstring (add one to surface "
                "this integration to the fine-tune)",
                path.name,
            )
            continue

        # Summary = first sentence (or first line if no period yet).
        first_para = doc.split("\n\n", 1)[0].strip()
        first_sentence = first_para.split(". ", 1)[0].strip().rstrip(".")
        summary = (first_sentence + ".") if first_sentence else first_para

        out.append(
            Integration(
                module=path.stem,
                summary=summary,
                description=doc,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Prompt / persona dump — loads the live system prompt so the fine-tune
# can absorb Avi's voice rules exactly as the running app presents them.
# ---------------------------------------------------------------------------


def dump_prompts() -> Dict[str, str]:
    from api.ai.ollama import system_prompt_for_avi  # type: ignore

    persona = system_prompt_for_avi("Avi", "Family")

    # ai/prompts.py hosts with_safety() and the safety envelope; read
    # the module docstring + the wrapper text.
    prompts_py = (PYTHON_API / "ai" / "prompts.py").read_text()
    return {
        "persona": persona,
        "prompts_module_source_head": prompts_py[:2000],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
    logger.info("wrote %s (%d bytes)", path, path.stat().st_size)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Derive the schema from SQLAlchemy models instead of hitting Postgres.",
    )
    ap.add_argument(
        "--skip-routes",
        action="store_true",
        help="Skip FastAPI app import (useful when the DB is down).",
    )
    args = ap.parse_args()

    cfg = load_config()
    out_dir = resolve_path(cfg, ["corpus", "output_dir"])

    # 1) Schema
    try:
        tables = dump_schema_from_models() if args.dry_run else dump_schema_live()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "live schema dump failed (%s); falling back to models", exc
        )
        tables = dump_schema_from_models()
    _write_yaml(out_dir / "schema.yaml", [asdict(t) for t in tables])

    # 2) Routes
    routes: List[Route] = []
    if not args.skip_routes:
        try:
            routes = dump_routes()
        except Exception:  # noqa: BLE001
            logger.exception(
                "route dump failed — continuing without routes.yaml"
            )
    _write_yaml(
        out_dir / "apis.yaml",
        [asdict(r) for r in routes],
    )

    # 3) Tools
    tools = dump_tools()
    _write_yaml(out_dir / "tools.yaml", [asdict(t) for t in tools])

    # 4) Integrations
    integrations = dump_integrations()
    _write_yaml(
        out_dir / "integrations.yaml",
        [asdict(i) for i in integrations],
    )

    # 5) Prompts
    _write_yaml(out_dir / "prompts.yaml", dump_prompts())

    # Summary
    summary = {
        "n_tables": len(tables),
        "n_columns": sum(len(t.columns) for t in tables),
        "n_routes": len(routes),
        "n_tools": len(tools),
        "n_integrations": len(integrations),
        "output_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    logger.info("corpus summary: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
