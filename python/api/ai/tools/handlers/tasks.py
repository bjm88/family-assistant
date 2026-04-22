"""All ``task_*`` tools — Avi's secretary view of the family kanban.

Avi is the household's "secretary" for the kanban — these handlers
let her *create*, *list*, *read*, *update*, *comment on*, and
*assign followers to* tasks without the LLM having to write SQL.
Tasks are family-shared (no per-person privacy gate beyond the
normal family scope) so the parameter shape is intentionally flat.

When the LLM creates a task on behalf of a recognised speaker we
pass ``ctx.person_id`` as ``created_by`` automatically — the model
only needs to supply the title / description / priority. Same for
comments: ``author_kind`` defaults to ``'assistant'`` when no
``author_person_id`` is given so Avi-authored notes are properly
attributed in the audit trail.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .... import models
from ....config import get_settings
from ....services import cron_helpers
from .._registry import ToolContext, ToolError


logger = logging.getLogger(__name__)


_TASK_STATUSES_LIST = list(models.TASK_STATUSES)
_TASK_PRIORITIES_LIST = list(models.TASK_PRIORITIES)
_TASK_OWNER_KINDS_LIST = list(models.TASK_OWNER_KINDS)
_TASK_KINDS_LIST = list(models.TASK_KINDS)


def _resolve_family_timezone_for_tool(db: Session, family_id: int) -> str:
    """Look up the family's IANA timezone for cron parsing.

    Mirrors the router-side helper but lives here so the tool layer
    doesn't import from ``api.routers`` (which would create a cycle).
    Falls back to ``America/New_York`` to match the column default.
    """
    fam = db.get(models.Family, family_id)
    if fam is not None and getattr(fam, "timezone", None):
        return fam.timezone
    return "America/New_York"


def _serialize_task_for_model(t: models.Task) -> Dict[str, Any]:
    """Compact JSON shape returned to the LLM for a single task.

    Trimmed of the row's full description on list endpoints to keep
    the context window healthy — the model can call ``task_get`` for
    full detail when the user asks for it.
    """
    out: Dict[str, Any] = {
        "task_id": t.task_id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "owner_kind": t.owner_kind,
        "task_kind": t.task_kind,
        "assigned_to_person_id": t.assigned_to_person_id,
        "created_by_person_id": t.created_by_person_id,
        "start_date": t.start_date.isoformat() if t.start_date else None,
        "desired_end_date": (
            t.desired_end_date.isoformat() if t.desired_end_date else None
        ),
        "end_date": t.end_date.isoformat() if t.end_date else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }
    # Monitoring tasks carry their schedule + last-run state. We only
    # include the fields when relevant so the kanban-task payloads
    # stay small in the model's context window.
    if t.task_kind == "monitoring":
        out["cron_schedule"] = t.cron_schedule
        if t.cron_schedule:
            try:
                out["cron_description"] = cron_helpers.describe(
                    t.cron_schedule
                )
            except Exception:  # noqa: BLE001 - never crash a serialise
                out["cron_description"] = None
        out["monitoring_paused"] = bool(t.monitoring_paused)
        out["next_run_at"] = (
            t.next_run_at.isoformat() if t.next_run_at else None
        )
        out["last_run_at"] = (
            t.last_run_at.isoformat() if t.last_run_at else None
        )
        out["last_run_status"] = t.last_run_status
        out["last_run_error"] = t.last_run_error
    return out


# ---- task_create ------------------------------------------------------


TASK_CREATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "Short headline for the kanban card, e.g. 'Fix the east "
                "gate latch' or 'Renew Maddie's passport'. Required."
            ),
        },
        "description": {
            "type": ["string", "null"],
            "description": (
                "Longer detail / acceptance criteria / context. Capture "
                "anything the user said that explains WHAT done looks "
                "like. Optional."
            ),
        },
        "priority": {
            "type": "string",
            "enum": _TASK_PRIORITIES_LIST,
            "description": (
                "One of urgent / high / normal / low / future_idea. "
                "Default to 'normal' unless the speaker tells you "
                "otherwise. Use 'future_idea' for casual 'someday' "
                "mentions so they don't pollute the active board."
            ),
        },
        "status": {
            "type": "string",
            "enum": _TASK_STATUSES_LIST,
            "description": (
                "Initial kanban column. Defaults to 'new'. Set to "
                "'in_progress' if the user is already mid-task."
            ),
        },
        "assigned_to_person_id": {
            "type": ["integer", "null"],
            "description": (
                "person_id of the owner. Defaults to the SPEAKER's "
                "person_id when omitted (the asker becomes the owner)."
            ),
        },
        "desired_end_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": (
                "Soft target the speaker wants the task done by, "
                "ISO YYYY-MM-DD. Use this when the user says 'by "
                "Friday', 'next week', etc."
            ),
        },
        "start_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": "When work is intended to begin (ISO date).",
        },
        "follower_person_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Other household members to loop in as followers. The "
                "creator + assignee are followers implicitly — only "
                "include EXTRAS here."
            ),
        },
        "owner_kind": {
            "type": "string",
            "enum": _TASK_OWNER_KINDS_LIST,
            "description": (
                "Who is accountable for the work. 'human' (default) "
                "means a person on the kanban board owns it. 'ai' "
                "means YOU (Avi) own it as a standing agentic job — "
                "use this when the user says 'monitor', 'keep an eye "
                "on', 'research and update me on', 'watch for', or "
                "asks for ongoing investigation Avi should run "
                "herself. AI-owned tasks ignore assigned_to_person_id."
            ),
        },
        "task_kind": {
            "type": "string",
            "enum": _TASK_KINDS_LIST,
            "description": (
                "Shape of the task. 'todo' (default) is a one-shot "
                "kanban card. 'monitoring' is an ongoing job with a "
                "cron schedule that Avi re-runs on a cadence — use "
                "for 'monitor for X', 'check weekly for Y', "
                "'research and keep updated' style asks. Pair with "
                "owner_kind='ai' for the standing-job pattern."
            ),
        },
        "cron_schedule": {
            "type": ["string", "null"],
            "description": (
                "Standard 5-field cron expression "
                "(minute hour day-of-month month day-of-week) "
                "interpreted in the family's timezone. ONLY meaningful "
                "for monitoring tasks. Omit to use the household "
                "default (typically once a day mid-morning). Examples: "
                "'0 9 * * *' = daily 9am, '0 9 * * 1' = Mondays 9am, "
                "'0 */6 * * *' = every 6 hours."
            ),
        },
        "monitoring_paused": {
            "type": "boolean",
            "description": (
                "If true, create the monitoring task in a PAUSED "
                "state — no immediate first run, no cron firing — "
                "until the user unpauses it. Defaults to false. "
                "Useful when the user wants to set something up "
                "but isn't ready for Avi to start working yet."
            ),
        },
    },
    "required": ["title"],
}


def _parse_task_date(label: str, raw: Optional[str]) -> Optional[date]:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError as exc:
        raise ToolError(f"{label} must be ISO YYYY-MM-DD, got {raw!r}") from exc


async def handle_task_create(
    ctx: ToolContext,
    title: str,
    description: Optional[str] = None,
    priority: str = "normal",
    status: str = "new",
    assigned_to_person_id: Optional[int] = None,
    desired_end_date: Optional[str] = None,
    start_date: Optional[str] = None,
    follower_person_ids: Optional[List[int]] = None,
    owner_kind: str = "human",
    task_kind: str = "todo",
    cron_schedule: Optional[str] = None,
    monitoring_paused: bool = False,
) -> Dict[str, Any]:
    cleaned_title = (title or "").strip()
    if not cleaned_title:
        raise ToolError("Cannot create a task with an empty title.")
    if priority not in models.TASK_PRIORITIES:
        raise ToolError(
            f"priority must be one of {list(models.TASK_PRIORITIES)}, got {priority!r}"
        )
    if status not in models.TASK_STATUSES:
        raise ToolError(
            f"status must be one of {list(models.TASK_STATUSES)}, got {status!r}"
        )
    if owner_kind not in models.TASK_OWNER_KINDS:
        raise ToolError(
            f"owner_kind must be one of {list(models.TASK_OWNER_KINDS)}, got {owner_kind!r}"
        )
    if task_kind not in models.TASK_KINDS:
        raise ToolError(
            f"task_kind must be one of {list(models.TASK_KINDS)}, got {task_kind!r}"
        )

    is_ai_monitoring = owner_kind == "ai" and task_kind == "monitoring"

    # AI-owned monitoring tasks have no human assignee — Avi owns
    # them. Force the assignee to NULL even if the model passed one
    # so the kanban-style UI doesn't try to render an owner badge on
    # a row that Avi is responsible for.
    if owner_kind == "ai":
        owner = None
    else:
        owner = assigned_to_person_id
        if owner is None and ctx.person_id is not None:
            owner = ctx.person_id

    if owner is not None:
        if (
            ctx.db.get(models.Person, owner) is None
            or ctx.db.query(models.Person)
            .filter(
                models.Person.person_id == owner,
                models.Person.family_id == ctx.family_id,
            )
            .first()
            is None
        ):
            raise ToolError(
                f"Assignee person_id={owner} is not a member of this family."
            )

    # Validate cron up-front (only meaningful for monitoring tasks)
    # so a bad expression surfaces as a tool error instead of a
    # silent NULL on the row. For AI monitoring tasks with no cron
    # supplied we fall back to the household default.
    settings = get_settings()
    next_run_utc: Optional[datetime] = None
    cron_to_apply: Optional[str] = cron_schedule
    if task_kind == "monitoring":
        if is_ai_monitoring and not (cron_to_apply and cron_to_apply.strip()):
            cron_to_apply = settings.AI_MONITORING_DEFAULT_CRON
        if cron_to_apply and cron_to_apply.strip():
            tz_name = _resolve_family_timezone_for_tool(
                ctx.db, ctx.family_id
            )
            try:
                info = cron_helpers.parse(cron_to_apply, tz_name)
            except cron_helpers.CronError as exc:
                raise ToolError(
                    f"Invalid cron_schedule {cron_to_apply!r}: {exc}"
                ) from exc
            cron_to_apply = info.expression
            if not monitoring_paused:
                next_run_utc = info.next_run_utc
        else:
            cron_to_apply = None
    else:
        # Non-monitoring tasks must not carry a cron expression — it'd
        # confuse the scheduler if the type were later flipped.
        cron_to_apply = None
        monitoring_paused = False

    task = models.Task(
        family_id=ctx.family_id,
        created_by_person_id=ctx.person_id,
        assigned_to_person_id=owner,
        title=cleaned_title,
        description=description,
        status=status,
        priority=priority,
        owner_kind=owner_kind,
        task_kind=task_kind,
        cron_schedule=cron_to_apply,
        monitoring_paused=bool(monitoring_paused),
        next_run_at=next_run_utc,
        start_date=_parse_task_date("start_date", start_date),
        desired_end_date=_parse_task_date("desired_end_date", desired_end_date),
        completed_at=datetime.now(timezone.utc) if status == "done" else None,
    )
    ctx.db.add(task)
    ctx.db.flush()

    implicit = {p for p in (ctx.person_id, owner) if p is not None}
    for pid in follower_person_ids or []:
        if pid in implicit:
            continue
        if (
            ctx.db.query(models.Person)
            .filter(
                models.Person.person_id == pid,
                models.Person.family_id == ctx.family_id,
            )
            .first()
            is None
        ):
            raise ToolError(
                f"Follower person_id={pid} is not a member of this family."
            )
        ctx.db.add(
            models.TaskFollower(
                task_id=task.task_id,
                person_id=pid,
                added_at=datetime.now(timezone.utc),
            )
        )
        implicit.add(pid)

    ctx.db.flush()
    ctx.db.refresh(task)

    # Kick off the immediate first run for AI monitoring tasks so the
    # user sees research start happening as soon as they say "monitor
    # X" — without waiting for the next cron tick. Commit first so the
    # background worker (running in its own session) can read the row.
    if is_ai_monitoring and not task.monitoring_paused:
        try:
            ctx.db.commit()
        except Exception:  # noqa: BLE001 - keep the tool result useful
            logger.exception(
                "task_create: commit before monitoring kickoff failed for task %d",
                task.task_id,
            )
        else:
            try:
                from ....services import monitoring_scheduler

                monitoring_scheduler.run_now_in_background(task.task_id)
            except Exception:  # noqa: BLE001 - never fail the create on this
                logger.exception(
                    "task_create: failed to kick off first run for task %d",
                    task.task_id,
                )

    return {"created": _serialize_task_for_model(task)}


# ---- task_list --------------------------------------------------------


TASK_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "assigned_to_person_id": {
            "type": ["integer", "null"],
            "description": (
                "Filter to tasks owned by this person_id. Use 0 for "
                "explicitly UNASSIGNED tasks. Omit to see everyone's."
            ),
        },
        "mine_only": {
            "type": "boolean",
            "description": (
                "Shortcut equivalent to assigned_to_person_id=<speaker>. "
                "Defaults to false. Set true when the user says 'my "
                "tasks' / 'what's on my plate'."
            ),
        },
        "priority": {
            "type": ["string", "null"],
            "enum": _TASK_PRIORITIES_LIST + [None],
            "description": (
                "Filter to a single priority bucket. Use repeated calls "
                "for 'urgent and high' answers."
            ),
        },
        "status": {
            "type": ["string", "null"],
            "enum": _TASK_STATUSES_LIST + [None],
            "description": "Filter to one kanban column.",
        },
        "include_done": {
            "type": "boolean",
            "description": (
                "Include status='done' tasks. Defaults to FALSE here so "
                "list calls focus on active work — set true when "
                "answering 'what did I close this week?'."
            ),
        },
        "q": {
            "type": ["string", "null"],
            "description": "Substring match against title or description.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Max rows to return. Defaults to 15.",
        },
    },
    "required": [],
}


async def handle_task_list(
    ctx: ToolContext,
    assigned_to_person_id: Optional[int] = None,
    mine_only: bool = False,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    include_done: bool = False,
    q: Optional[str] = None,
    limit: int = 15,
) -> Dict[str, Any]:
    from sqlalchemy import case, or_, select

    qry = select(models.Task).where(models.Task.family_id == ctx.family_id)
    if mine_only:
        if ctx.person_id is None:
            raise ToolError(
                "I don't know who's asking yet, so I can't filter to "
                "'your' tasks. Greet me on camera or email me from a "
                "registered address and try again."
            )
        qry = qry.where(models.Task.assigned_to_person_id == ctx.person_id)
    elif assigned_to_person_id is not None:
        if int(assigned_to_person_id) == 0:
            qry = qry.where(models.Task.assigned_to_person_id.is_(None))
        else:
            qry = qry.where(
                models.Task.assigned_to_person_id == int(assigned_to_person_id)
            )

    if status is not None:
        if status not in models.TASK_STATUSES:
            raise ToolError(
                f"status must be one of {list(models.TASK_STATUSES)}"
            )
        qry = qry.where(models.Task.status == status)
    elif not include_done:
        qry = qry.where(models.Task.status != "done")

    if priority is not None:
        if priority not in models.TASK_PRIORITIES:
            raise ToolError(
                f"priority must be one of {list(models.TASK_PRIORITIES)}"
            )
        qry = qry.where(models.Task.priority == priority)

    if q:
        like = f"%{q}%"
        qry = qry.where(
            or_(
                models.Task.title.ilike(like),
                models.Task.description.ilike(like),
            )
        )

    priority_rank = case(
        (models.Task.priority == "urgent", 0),
        (models.Task.priority == "high", 1),
        (models.Task.priority == "normal", 2),
        (models.Task.priority == "low", 3),
        (models.Task.priority == "future_idea", 4),
        else_=5,
    )
    status_rank = case(
        (models.Task.status == "in_progress", 0),
        (models.Task.status == "finalizing", 1),
        (models.Task.status == "new", 2),
        (models.Task.status == "done", 9),
        else_=5,
    )
    qry = qry.order_by(
        status_rank.asc(),
        priority_rank.asc(),
        models.Task.created_at.desc(),
    ).limit(min(max(int(limit), 1), 50))

    rows = list(ctx.db.execute(qry).scalars())
    return {
        "count": len(rows),
        "tasks": [_serialize_task_for_model(r) for r in rows],
    }


# ---- task_get ---------------------------------------------------------


TASK_GET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer", "description": "tasks.task_id"},
    },
    "required": ["task_id"],
}


async def handle_task_get(ctx: ToolContext, task_id: int) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    return {
        **_serialize_task_for_model(task),
        "description": task.description,
        "follower_person_ids": [f.person_id for f in task.followers],
        "comments": [
            {
                "task_comment_id": c.task_comment_id,
                "author_kind": c.author_kind,
                "author_person_id": c.author_person_id,
                "body": c.body,
                "created_at": c.created_at.isoformat(),
            }
            for c in task.comments
        ],
        "attachment_count": len(task.attachments),
    }


# ---- task_update ------------------------------------------------------


TASK_UPDATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "status": {
            "type": ["string", "null"],
            "enum": _TASK_STATUSES_LIST + [None],
        },
        "priority": {
            "type": ["string", "null"],
            "enum": _TASK_PRIORITIES_LIST + [None],
        },
        "assigned_to_person_id": {
            "type": ["integer", "null"],
            "description": "Set to null to unassign.",
        },
        "desired_end_date": {
            "type": ["string", "null"],
            "format": "date",
        },
        "start_date": {"type": ["string", "null"], "format": "date"},
        "end_date": {"type": ["string", "null"], "format": "date"},
    },
    "required": ["task_id"],
}


async def handle_task_update(
    ctx: ToolContext,
    task_id: int,
    **fields: Any,
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")

    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in models.TASK_STATUSES:
            raise ToolError(
                f"status must be one of {list(models.TASK_STATUSES)}"
            )
        if fields["status"] == "done" and task.completed_at is None:
            task.completed_at = datetime.now(timezone.utc)
        elif fields["status"] != "done" and task.completed_at is not None:
            task.completed_at = None

    if "priority" in fields and fields["priority"] is not None:
        if fields["priority"] not in models.TASK_PRIORITIES:
            raise ToolError(
                f"priority must be one of {list(models.TASK_PRIORITIES)}"
            )

    if (
        "assigned_to_person_id" in fields
        and fields["assigned_to_person_id"] is not None
        and ctx.db.query(models.Person)
        .filter(
            models.Person.person_id == fields["assigned_to_person_id"],
            models.Person.family_id == ctx.family_id,
        )
        .first()
        is None
    ):
        raise ToolError(
            f"Assignee person_id={fields['assigned_to_person_id']} is not "
            "a member of this family."
        )

    for label in ("start_date", "desired_end_date", "end_date"):
        if label in fields and fields[label] is not None:
            try:
                fields[label] = datetime.fromisoformat(fields[label]).date()
            except ValueError as exc:
                raise ToolError(
                    f"{label} must be ISO YYYY-MM-DD, got {fields[label]!r}"
                ) from exc

    for k, v in fields.items():
        if hasattr(task, k):
            setattr(task, k, v)

    ctx.db.flush()
    ctx.db.refresh(task)
    return {"updated": _serialize_task_for_model(task)}


# ---- task_add_comment -------------------------------------------------


TASK_ADD_COMMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "body": {
            "type": "string",
            "description": (
                "Comment text. Avi typically writes SHORT auto-notes "
                "(one or two sentences) — e.g. 'Marking this as done "
                "per Sarah's request.'"
            ),
        },
        "author_kind": {
            "type": "string",
            "enum": list(models.TASK_COMMENT_AUTHOR_KINDS),
            "description": (
                "'assistant' (default) when Avi is writing the note "
                "herself; 'person' when relaying a comment dictated by "
                "the speaker."
            ),
        },
    },
    "required": ["task_id", "body"],
}


async def handle_task_add_comment(
    ctx: ToolContext,
    task_id: int,
    body: str,
    author_kind: str = "assistant",
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    if author_kind not in models.TASK_COMMENT_AUTHOR_KINDS:
        raise ToolError(
            f"author_kind must be one of {list(models.TASK_COMMENT_AUTHOR_KINDS)}"
        )
    body = (body or "").strip()
    if not body:
        raise ToolError("Comment body cannot be empty.")

    comment = models.TaskComment(
        task_id=task.task_id,
        author_person_id=ctx.person_id if author_kind == "person" else None,
        author_kind=author_kind,
        body=body,
        created_at=datetime.now(timezone.utc),
    )
    ctx.db.add(comment)
    ctx.db.flush()
    ctx.db.refresh(comment)
    return {
        "task_comment_id": comment.task_comment_id,
        "task_id": comment.task_id,
        "author_kind": comment.author_kind,
        "author_person_id": comment.author_person_id,
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
    }


# ---- task_add_follower ------------------------------------------------


TASK_ADD_FOLLOWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "person_id": {
            "type": "integer",
            "description": (
                "person_id of the family member to add as a follower. "
                "Use lookup_person first if you only have a name."
            ),
        },
    },
    "required": ["task_id", "person_id"],
}


async def handle_task_add_follower(
    ctx: ToolContext, task_id: int, person_id: int
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    if (
        ctx.db.query(models.Person)
        .filter(
            models.Person.person_id == int(person_id),
            models.Person.family_id == ctx.family_id,
        )
        .first()
        is None
    ):
        raise ToolError(
            f"person_id={person_id} is not a member of this family."
        )

    existing = (
        ctx.db.query(models.TaskFollower)
        .filter(
            models.TaskFollower.task_id == task.task_id,
            models.TaskFollower.person_id == int(person_id),
        )
        .first()
    )
    if existing is not None:
        return {
            "task_follower_id": existing.task_follower_id,
            "task_id": existing.task_id,
            "person_id": existing.person_id,
            "already_following": True,
        }

    follower = models.TaskFollower(
        task_id=task.task_id,
        person_id=int(person_id),
        added_at=datetime.now(timezone.utc),
    )
    ctx.db.add(follower)
    ctx.db.flush()
    ctx.db.refresh(follower)
    return {
        "task_follower_id": follower.task_follower_id,
        "task_id": follower.task_id,
        "person_id": follower.person_id,
        "already_following": False,
    }


# ---- task_set_schedule ------------------------------------------------


TASK_SET_SCHEDULE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "description": "tasks.task_id of the monitoring task to retune.",
        },
        "cron_schedule": {
            "type": ["string", "null"],
            "description": (
                "Standard 5-field cron expression, interpreted in the "
                "family's timezone. Pass null to clear the schedule "
                "(effectively pauses the auto-runs without flipping "
                "monitoring_paused). Examples: '0 9 * * *' = daily 9am, "
                "'0 9 * * 1' = Mondays 9am, '*/30 * * * *' = every 30 "
                "minutes."
            ),
        },
        "monitoring_paused": {
            "type": ["boolean", "null"],
            "description": (
                "If true, pause the monitoring task — the cron stops "
                "firing until unpaused. If false, resume it. Omit to "
                "leave the pause state unchanged."
            ),
        },
        "run_now": {
            "type": "boolean",
            "description": (
                "If true AND the task is unpaused after this update, "
                "kick off an immediate monitoring run in the "
                "background so the user sees fresh research right "
                "away. Defaults to false (the next cron tick will pick "
                "it up)."
            ),
        },
    },
    "required": ["task_id"],
}


async def handle_task_set_schedule(
    ctx: ToolContext,
    task_id: int,
    cron_schedule: Optional[str] = None,
    monitoring_paused: Optional[bool] = None,
    run_now: bool = False,
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")
    if task.task_kind != "monitoring":
        raise ToolError(
            "Only monitoring tasks have a cron schedule. Convert "
            "the task to task_kind='monitoring' first (or use "
            "task_update for non-schedule fields)."
        )

    # Detect what the model wanted to change. We accept the JSON
    # shape ``{"cron_schedule": null}`` as "clear it" — distinct from
    # ``{}`` which leaves it alone — by relying on whether the key
    # was passed at all. The dispatcher always calls handlers with
    # explicit kwargs, so unset keys arrive as the function's
    # defaults; we treat ``None`` for cron the same as an explicit
    # null because the schema says null = clear.
    if cron_schedule is not None:
        cleaned = cron_schedule.strip()
        if not cleaned:
            task.cron_schedule = None
            task.next_run_at = None
        else:
            tz_name = _resolve_family_timezone_for_tool(
                ctx.db, ctx.family_id
            )
            try:
                info = cron_helpers.parse(cleaned, tz_name)
            except cron_helpers.CronError as exc:
                raise ToolError(
                    f"Invalid cron_schedule {cleaned!r}: {exc}"
                ) from exc
            task.cron_schedule = info.expression
            # Defer next_run_at decision to the pause logic below so
            # we don't accidentally schedule a paused task.
            task.next_run_at = (
                None if task.monitoring_paused else info.next_run_utc
            )

    if monitoring_paused is not None:
        task.monitoring_paused = bool(monitoring_paused)
        if task.monitoring_paused:
            task.next_run_at = None
        elif task.cron_schedule and task.next_run_at is None:
            tz_name = _resolve_family_timezone_for_tool(
                ctx.db, ctx.family_id
            )
            try:
                task.next_run_at = cron_helpers.next_run(
                    task.cron_schedule, tz_name
                )
            except cron_helpers.CronError:
                task.next_run_at = None

    ctx.db.flush()
    ctx.db.refresh(task)

    fired = False
    if run_now and task.owner_kind == "ai" and not task.monitoring_paused:
        try:
            ctx.db.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "task_set_schedule: commit before run_now failed for task %d",
                task.task_id,
            )
        else:
            try:
                from ....services import monitoring_scheduler

                monitoring_scheduler.run_now_in_background(task.task_id)
                fired = True
            except Exception:  # noqa: BLE001
                logger.exception(
                    "task_set_schedule: failed to fire run_now for task %d",
                    task.task_id,
                )

    return {
        "updated": _serialize_task_for_model(task),
        "run_now_fired": fired,
    }


# ---- task_attach_link -------------------------------------------------


TASK_ATTACH_LINK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "description": "tasks.task_id to attach the citation to.",
        },
        "url": {
            "type": "string",
            "description": (
                "Full URL of the source. Must include the scheme "
                "(https://…). Re-attaching the same URL is idempotent "
                "— the existing row is returned instead of erroring."
            ),
        },
        "title": {
            "type": ["string", "null"],
            "description": (
                "Display label for the link, typically the page "
                "<title> or the article headline. Falls back to the "
                "URL host when omitted."
            ),
        },
        "summary": {
            "type": ["string", "null"],
            "description": (
                "One-paragraph note explaining why this source is "
                "relevant to the task — what claim or data point it "
                "supports. Lets the user skim citations without "
                "clicking every link."
            ),
        },
        "added_by_kind": {
            "type": "string",
            "enum": ["assistant", "person"],
            "description": (
                "'assistant' (default) when Avi cited this herself "
                "during research; 'person' when relaying a link the "
                "speaker shared verbally."
            ),
        },
    },
    "required": ["task_id", "url"],
}


async def handle_task_attach_link(
    ctx: ToolContext,
    task_id: int,
    url: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    added_by_kind: str = "assistant",
) -> Dict[str, Any]:
    task = ctx.db.get(models.Task, int(task_id))
    if task is None or task.family_id != ctx.family_id:
        raise ToolError(f"No task with id={task_id} in this family.")

    cleaned_url = (url or "").strip()
    if not cleaned_url:
        raise ToolError("Cannot attach an empty URL.")
    if not (cleaned_url.startswith("http://") or cleaned_url.startswith("https://")):
        raise ToolError(
            f"URL must include http:// or https:// scheme, got {cleaned_url!r}"
        )
    if added_by_kind not in ("assistant", "person"):
        raise ToolError(
            f"added_by_kind must be 'assistant' or 'person', got {added_by_kind!r}"
        )

    # Idempotent on (task_id, url) so re-running a monitoring job
    # that re-cites the same source doesn't duplicate the chip in
    # the UI.
    existing = (
        ctx.db.query(models.TaskLink)
        .filter(
            models.TaskLink.task_id == task.task_id,
            models.TaskLink.url == cleaned_url,
        )
        .first()
    )
    if existing is not None:
        return {
            "task_link_id": existing.task_link_id,
            "task_id": existing.task_id,
            "url": existing.url,
            "title": existing.title,
            "summary": existing.summary,
            "added_by_kind": existing.added_by_kind,
            "already_attached": True,
        }

    cleaned_title = (title or "").strip() or None
    link = models.TaskLink(
        task_id=task.task_id,
        url=cleaned_url,
        title=cleaned_title,
        summary=summary,
        added_by_kind=added_by_kind,
        added_by_person_id=(
            ctx.person_id if added_by_kind == "person" else None
        ),
        created_at=datetime.now(timezone.utc),
    )
    ctx.db.add(link)
    ctx.db.flush()
    ctx.db.refresh(link)
    return {
        "task_link_id": link.task_link_id,
        "task_id": link.task_id,
        "url": link.url,
        "title": link.title,
        "summary": link.summary,
        "added_by_kind": link.added_by_kind,
        "already_attached": False,
    }
