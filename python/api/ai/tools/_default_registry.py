"""Wire every concrete tool handler into a :class:`ToolRegistry`.

This module is the *only* place that knows about every tool at once
— the rest of ``ai/tools/`` is split into focused handler modules
(see :mod:`api.ai.tools.handlers`). Adding a new tool means defining
its handler in the right module under ``handlers/`` and adding one
``reg.register(Tool(...))`` block here.

Also lives here: :func:`detect_capabilities` (which integrations are
live for this assistant?) and :func:`describe_capabilities` (the
human-friendly bullet list the LLM uses to answer 'what can you
do?').
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from ...integrations import google_oauth, web_search as web_search_integration
from ._registry import Tool, ToolRegistry
from .handlers import calendar as _cal
from .handlers import messaging as _msg
from .handlers import secrets as _sec
from .handlers import sql as _sql
from .handlers import tasks as _tasks
from .handlers import telegram_invite as _tg
from .handlers import web as _web


def build_default_registry() -> ToolRegistry:
    """Construct the registry the chat agent uses by default."""
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="sql_query",
            label="Query the family database",
            description=(
                "Run a single read-only SELECT against the family database. "
                "Useful for ad-hoc lookups (vehicles, residences, insurance, "
                "etc.) when the prebuilt tools below don't fit. Always include "
                "family_id in the WHERE clause."
            ),
            parameters=_sql.SQL_QUERY_SCHEMA,
            handler=_sql.handle_sql_query,
            timeout_seconds=8.0,
            examples=(
                "How many cars do we own?",
                "When does our auto insurance renew?",
                "Who in the family takes blood pressure medication?",
            ),
        )
    )
    reg.register(
        Tool(
            name="lookup_person",
            label="Look up a family member",
            description=(
                "Find a household member by partial name. Returns person_id, "
                "names, email, and gender. Use this BEFORE drafting an email "
                "to a family member so you have their real address."
            ),
            parameters=_sql.LOOKUP_PERSON_SCHEMA,
            handler=_sql.handle_lookup_person,
            timeout_seconds=4.0,
            examples=(
                "What's Sarah's email address?",
                "Tell me about Ben.",
            ),
        )
    )
    reg.register(
        Tool(
            name="reveal_sensitive_identifier",
            label="Reveal a sensitive identifier (SSN, tax ID)",
            description=(
                "Decrypt and return a family member's full sensitive "
                "identifier (typically Social Security Number). The tool "
                "enforces relationship-based privacy: it ONLY returns a "
                "value when the speaker is the subject themselves, the "
                "subject's spouse, or one of the subject's direct "
                "parents. Children, grandparents, siblings, in-laws, "
                "and anonymous speakers are refused. Every call is "
                "audit-logged. Use this only when the user explicitly "
                "asks for the full number; otherwise stick to the "
                "*_last_four helper columns."
            ),
            parameters=_sec.REVEAL_SENSITIVE_SCHEMA,
            handler=_sec.handle_reveal_sensitive,
            timeout_seconds=5.0,
            examples=(
                "What's my SSN?",
                "Read me my daughter's social security number.",
            ),
        )
    )
    reg.register(
        Tool(
            name="reveal_secret",
            label="Reveal an encrypted family identifier (VIN, plate, account #, policy #, ID #)",
            description=(
                "Decrypt and return one Fernet-encrypted family "
                "identifier: a vehicle's full VIN or license plate, "
                "an identity-document number (driver's licence, "
                "passport, state ID), a bank account or routing "
                "number, or an insurance policy number. Enforces the "
                "same household privacy matrix as "
                "reveal_sensitive_identifier — it ONLY returns a "
                "value when the speaker is the subject themselves, "
                "the subject's spouse, or one of the subject's direct "
                "parents (so a child cannot read a parent's data, "
                "and a sibling cannot read a sibling's). For shared "
                "household assets like a vehicle with no primary "
                "driver, any identified family member of the same "
                "household may read it. Every call is audit-logged. "
                "Use this whenever the user explicitly asks for the "
                "FULL value of one of these fields — for everyday "
                "questions stick to the *_last_four helper columns "
                "you can already see via sql_query."
            ),
            parameters=_sec.REVEAL_SECRET_SCHEMA,
            handler=_sec.handle_reveal_secret,
            timeout_seconds=5.0,
            examples=(
                "What's the VIN on my truck?",
                "Read me my driver's license number.",
                "Tell me the policy number on our auto insurance.",
            ),
        )
    )
    reg.register(
        Tool(
            name="gmail_send",
            label="Send an email",
            description=(
                "Send a plain-text email from the assistant's connected "
                "Gmail account. Returns the Gmail message_id on success. "
                "ONLY call this once you have the recipient's real email "
                "address (use lookup_person first if needed) and a fully-"
                "drafted subject and body."
            ),
            parameters=_msg.GMAIL_SEND_SCHEMA,
            handler=_msg.handle_gmail_send,
            timeout_seconds=20.0,
            requires=("google",),
            examples=(
                "Send Mom a note thanking her for dinner.",
                "Email Ben a one-line summary of tomorrow's calendar.",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_upcoming",
            label="Read the calendar",
            description=(
                "List events on the assistant's connected Google calendar "
                "(and any calendars shared with it) for the next N hours. "
                "Use to answer 'what's coming up' or to gather context for "
                "an email about a future event."
            ),
            parameters=_cal.CALENDAR_LIST_SCHEMA,
            handler=_cal.handle_calendar_list,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on the calendar this week?",
                "Are we free Saturday afternoon?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_check_availability",
            label="Check a person's free/busy",
            description=(
                "Check whether one specific household member is free "
                "or busy in a given time window. Hits Google freebusy "
                "against BOTH the person's personal calendar "
                "(email_address) AND every work calendar from their jobs "
                "when both are configured, and merges the results so "
                "a slot only counts as free if the person is free on "
                "both. Returns per_calendar so you can mention if a "
                "specific calendar isn't shared with the assistant. "
                "Free/busy contains NO event detail (titles, "
                "locations) so this tool is safe to call for any "
                "household member regardless of who is asking."
            ),
            parameters=_cal.CALENDAR_CHECK_SCHEMA,
            handler=_cal.handle_calendar_check_availability,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Is Ben free Friday afternoon?",
                "Is Mom busy at 3pm tomorrow?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_find_free_slots",
            label="Find a free time for someone",
            description=(
                "Suggest open time slots for one household member "
                "across a window (up to ~1 month). Considers BOTH "
                "personal and work calendars when configured — a "
                "suggested slot is only free if the person is free "
                "on every shared calendar. Defaults to 30-minute "
                "slots inside 9am-6pm working hours, configurable. "
                "Warns if any calendar exists on the profile but "
                "isn't shared with the assistant."
            ),
            parameters=_cal.CALENDAR_FREE_SLOTS_SCHEMA,
            handler=_cal.handle_calendar_find_free_slots,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Find me a time Ben is free next week.",
                "When can Sarah do a 45-minute call this Thursday?",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_create_event",
            label="Add a calendar event",
            description=(
                "Add a new event (a hold, a reminder, a block of "
                "focus time, an appointment) to the SPEAKER's own "
                "personal Google calendar. Only writes to the "
                "speaker's calendar — never anyone else's. The "
                "speaker must have flipped on 'Allow Avi to add "
                "calendar events' on their Person profile AND "
                "shared their personal calendar with the assistant "
                "with 'Make changes to events' permission. The tool "
                "returns a clear error message walking the user "
                "through whichever consent they're missing. "
                "Defaults to a 60-minute duration when no end time "
                "is supplied. The user almost always speaks in "
                "local time ('next Tues at 2pm') — translate that "
                "into ISO 8601 with the right offset for the "
                "household's timezone before calling."
            ),
            parameters=_cal.CALENDAR_CREATE_EVENT_SCHEMA,
            handler=_cal.handle_calendar_create_event,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "Add a hold on my calendar next Tuesday at 2pm.",
                "Block 90 minutes of focus time tomorrow morning at 9.",
            ),
        )
    )
    reg.register(
        Tool(
            name="calendar_list_for_person",
            label="List events for one person",
            description=(
                "List the actual events on a household member's "
                "personal AND work calendars between two timestamps. "
                "Honours the household's calendar-detail privacy "
                "rule: only the SUBJECT and their SPOUSE see event "
                "titles / locations; everyone else (parents, "
                "children, siblings, in-laws) gets the timing as "
                "free/busy with the title replaced by '[busy — "
                "private]'. Use when the user wants 'what's on "
                "Ben's schedule this week?' style detail. For 'is X "
                "free?' use calendar_check_availability instead — "
                "it's cheaper and doesn't leak detail."
            ),
            parameters=_cal.CALENDAR_LIST_FOR_PERSON_SCHEMA,
            handler=_cal.handle_calendar_list_for_person,
            timeout_seconds=15.0,
            requires=("google",),
            examples=(
                "What's on Ben's calendar this week?",
                "Show me Sarah's meetings tomorrow.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_create",
            label="Create a household task",
            description=(
                "Add a new task to the family workspace. Two shapes: "
                "(1) HUMAN TODOS (default — owner_kind='human', "
                "task_kind='todo') land on the kanban board. The "
                "speaker is the creator and, unless told otherwise, "
                "the assignee. Use for 'add a task to…', 'remind me "
                "to…', 'we should…'. (2) AI MONITORING JOBS "
                "(owner_kind='ai', task_kind='monitoring') are "
                "standing investigations YOU (Avi) own and re-run on "
                "a cron schedule — use for 'monitor for…', 'keep an "
                "eye on…', 'research and update me on…', 'watch for…'. "
                "AI monitoring tasks immediately kick off a first run "
                "in the background; you do NOT need to call any other "
                "tool to start the analysis. Default priority is "
                "'normal'; bump to 'urgent'/'high' only when the user "
                "is explicit, and use 'future_idea' for casual "
                "'someday' mentions. After creating, briefly confirm "
                "what was tracked (title + kind + cron when "
                "monitoring) in 1-2 sentences."
            ),
            parameters=_tasks.TASK_CREATE_SCHEMA,
            handler=_tasks.handle_task_create,
            timeout_seconds=5.0,
            examples=(
                "Add a task to fix the east gate latch this weekend.",
                "Monitor for good Yankees ticket deals in May.",
                "Research college options for Jackson — biology programs, 25k+ students.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_list",
            label="List household tasks",
            description=(
                "List tasks on the family kanban with filters. Use "
                "mine_only=true for 'my tasks' / 'what's on my plate', "
                "priority='urgent'|'high' for 'what's urgent for me?', "
                "and q='passport' for free-text search. Excludes done "
                "tasks by default — pass include_done=true when the "
                "user is asking what they finished. Returns a compact "
                "list (no descriptions) — call task_get for full "
                "detail on a specific row."
            ),
            parameters=_tasks.TASK_LIST_SCHEMA,
            handler=_tasks.handle_task_list,
            timeout_seconds=5.0,
            examples=(
                "What are my high priority tasks?",
                "What's on the family task board right now?",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_get",
            label="Get full task detail",
            description=(
                "Read one task's full detail — description, comments, "
                "follower list, attachment count. Use after task_list "
                "when the user wants the specifics of a particular "
                "task."
            ),
            parameters=_tasks.TASK_GET_SCHEMA,
            handler=_tasks.handle_task_get,
            timeout_seconds=4.0,
            examples=(
                "Tell me more about the gate task.",
                "What's the status of the passport renewal?",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_update",
            label="Update a task",
            description=(
                "Patch one task — change status (kanban column), "
                "priority, owner, dates, title, or description. Only "
                "include the fields you want changed. Setting "
                "status='done' auto-stamps completed_at; setting it "
                "back to anything else clears it."
            ),
            parameters=_tasks.TASK_UPDATE_SCHEMA,
            handler=_tasks.handle_task_update,
            timeout_seconds=5.0,
            examples=(
                "Mark the gate task as done.",
                "Bump the passport task to urgent.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_add_comment",
            label="Comment on a task",
            description=(
                "Append a comment to a task. Defaults to "
                "author_kind='assistant' so Avi-authored notes "
                "(status changes, summaries) are clearly attributed. "
                "Use author_kind='person' when relaying a message "
                "dictated by the speaker."
            ),
            parameters=_tasks.TASK_ADD_COMMENT_SCHEMA,
            handler=_tasks.handle_task_add_comment,
            timeout_seconds=4.0,
            examples=(
                "Add a note that I picked up the parts.",
                "Comment on the passport task: appointment booked for Tuesday.",
            ),
        )
    )
    reg.register(
        Tool(
            name="telegram_invite",
            label="Invite a household member to chat on Telegram",
            description=(
                "Send a household member a one-time deep-link that "
                "opens the assistant's Telegram bot and binds their "
                "Telegram account to their Person row. Use when the "
                "user asks 'invite Sarah to Telegram', 'send Mom the "
                "Telegram link', or similar. The link is delivered "
                "by SMS when the invitee has a mobile phone on file "
                "(preferred) and falls back to email otherwise — "
                "pass channel='sms' or 'email' to force one. "
                "Telegram's rules forbid the bot from initiating a "
                "conversation, so this deep-link flow is the ONLY "
                "way to onboard someone; do not promise the user "
                "you'll 'just message them' on Telegram. Idempotent: "
                "re-asking inside the 30-day window resends the same "
                "outstanding link rather than minting a new one. "
                "After sending, briefly confirm the channel and "
                "destination so the user knows where to look ("
                "'Texted the link to Sarah at +1…' / 'Emailed it to "
                "mom@example.com')."
            ),
            parameters=_tg.TELEGRAM_INVITE_SCHEMA,
            handler=_tg.handle_telegram_invite,
            timeout_seconds=20.0,
            requires=("telegram",),
            examples=(
                "Invite Sarah to chat with you on Telegram.",
                "Send Mom the Telegram link by email.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_add_follower",
            label="Add a follower to a task",
            description=(
                "Loop another household member into a task as a "
                "follower. Idempotent — returns already_following=true "
                "if the person was already attached. Use lookup_person "
                "first if you only have a name."
            ),
            parameters=_tasks.TASK_ADD_FOLLOWER_SCHEMA,
            handler=_tasks.handle_task_add_follower,
            timeout_seconds=4.0,
            examples=(
                "Loop Sarah in on the passport task.",
                "Add Ben as a follower of the gate task.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_set_schedule",
            label="Set a monitoring task's cron schedule",
            description=(
                "Edit the cron schedule and/or pause flag of a "
                "MONITORING task you own. Use when the user says "
                "things like 'check that weekly instead of daily', "
                "'pause the Yankees-tickets monitor', 'resume the "
                "college research', 'run that monitor every six "
                "hours'. The expression is interpreted in the "
                "family's timezone. Pass run_now=true after a "
                "schedule change when the user wants to see fresh "
                "results immediately rather than waiting for the next "
                "tick."
            ),
            parameters=_tasks.TASK_SET_SCHEDULE_SCHEMA,
            handler=_tasks.handle_task_set_schedule,
            timeout_seconds=5.0,
            examples=(
                "Change the Yankees ticket monitor to run every six hours.",
                "Pause the Tesla stock monitor for now.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_attach_link",
            label="Attach a source URL to a task",
            description=(
                "Cite a source on a task — typically called by Avi "
                "during a monitoring run after web_search to record "
                "where a finding came from, but also fine for "
                "manual 'save this link to the college research "
                "task' requests. Idempotent on (task_id, url) — "
                "re-citing the same URL returns the existing row. "
                "Provide a short summary so the user can skim "
                "citations without clicking through."
            ),
            parameters=_tasks.TASK_ATTACH_LINK_SCHEMA,
            handler=_tasks.handle_task_attach_link,
            timeout_seconds=4.0,
            examples=(
                "Save this NYT article on the college task.",
                "Attach the StubHub listing to the Yankees monitor.",
            ),
        )
    )
    reg.register(
        Tool(
            name="task_attach_message_attachment",
            label="Attach the file the user just sent to a task",
            description=(
                "Promote one of the attachments on the CURRENT inbound "
                "message (email / SMS / WhatsApp / Telegram) onto a "
                "kanban task. Use this whenever the user sends a file "
                "AND asks you to track or remember it — e.g. 'make a "
                "task to review this property, details attached', "
                "'save this receipt to the warranty task', 'add this "
                "PDF to the camp signup task'. The attachment shows up "
                "as the same chip a person upload would create, and "
                "the bytes are copied into the task's own storage so "
                "the user can later delete the original message "
                "without losing the file. ``media_index`` matches the "
                "1-based 'Attachment N:' label that appears in the "
                "user message you were shown — pass 0 to attach every "
                "attachment from this message in one call. Only works "
                "for attachments on THIS turn; cannot reach back to "
                "earlier messages."
            ),
            parameters=_tasks.TASK_ATTACH_MESSAGE_ATTACHMENT_SCHEMA,
            handler=_tasks.handle_task_attach_message_attachment,
            timeout_seconds=15.0,
            examples=(
                "Make a task to review this house, details attached as a PDF.",
                "Save the photo I just sent to the broken-fence task.",
                "Attach all of these to the camp registration task.",
            ),
        )
    )
    reg.register(
        Tool(
            name="web_search",
            label="Search the web",
            description=(
                "Run a real-time web search via the configured "
                "provider. By default this is Gemini's google_search "
                "grounding, which returns a `summary` field "
                "(synthesised, citation-backed answer) alongside the "
                "list of source `results`; alternative SERP providers "
                "(Brave / Tavily) return only `results` with snippets. "
                "Reach for this WHENEVER the user asks a current, "
                "factual question whose answer lives on the open web "
                "and isn't in the household database — it is your "
                "everyday 'look it up' tool, not just a monitoring "
                "verb. Use it for one-shot chat asks ('what's the "
                "weather in Asheville this weekend', 'who won the "
                "Knicks game last night', 'what time does Trader "
                "Joe's close today', 'find me a sheet-pan salmon "
                "recipe', 'latest news on X'), AND inside monitoring "
                "runs to gather material for a comment. When "
                "`summary` is present, treat it as the grounded "
                "answer and synthesise — do not parrot raw URLs. "
                "Inside a monitoring run, also pass the source URLs "
                "to `task_attach_link` so the citation lives on the "
                "task; for casual chat answers you do NOT need to "
                "attach links. The provider may be unavailable on "
                "fresh installs — surface the error to the user "
                "verbatim if so (it explains how the admin can "
                "enable it)."
            ),
            parameters=_web.WEB_SEARCH_SCHEMA,
            handler=_web.handle_web_search,
            # Generous because the Gemini google_search backend
            # occasionally needs a couple of retries to ride out a
            # 503 spike (see GeminiSearchProvider._RETRY_*).
            timeout_seconds=30.0,
            requires=("web_search",),
            examples=(
                "What's the weather in Asheville this weekend?",
                "Who won the Knicks game last night?",
                "Search the web for the latest Tesla earnings news.",
            ),
        )
    )
    return reg


def describe_capabilities(
    registry: ToolRegistry, available: set[str]
) -> str:
    """Render the registry as a friendly bullet list for the system prompt.

    The model uses this to answer "what can you do?" / "help" with
    accurate, up-to-date answers instead of making them up. Tools
    whose capabilities aren't satisfied (e.g. Google not connected)
    are silently omitted so the model never offers things it can't
    actually do this turn.
    """
    tools_available = registry.for_capabilities(available)
    if not tools_available:
        return ""
    lines: List[str] = ["You currently have these tools:"]
    for t in tools_available:
        lines.append(f"- {t.display_label()} ({t.name}) — {t.description}")
        for ex in t.examples[:2]:
            lines.append(f'    e.g. "{ex}"')
    lines.append("")
    lines.append(
        "When the user asks 'what can you do?', 'help', or similar, "
        "summarise these capabilities in 2-4 friendly sentences. Quote "
        "ONE concrete example per capability so they know how to ask. "
        "Do not promise capabilities that aren't in the list above."
    )
    return "\n".join(lines)


def detect_capabilities(db: Session, assistant_id: Optional[int]) -> set[str]:
    """Inspect the database to figure out which capabilities are live.

    Capability flags currently advertised:

    * ``google``     — the assistant has connected a Google account
                       with at least one usable Gmail or Calendar
                       scope (covers ``calendar.readonly``,
                       ``calendar.events``, and ``calendar`` so a
                       household that only reconnected to gain the
                       write capability — but didn't re-grant gmail —
                       still sees the Google tools).
    * ``web_search`` — a search provider is configured AND its API
                       key is present. Hidden from the model when
                       missing so Avi never offers a research
                       capability we can't actually execute.
    * ``telegram``   — a Telegram bot token is configured. Gates
                       ``telegram_invite`` so Avi doesn't promise
                       a deep-link flow that would crash the moment
                       the user accepts.
    """
    caps: set[str] = set()
    if assistant_id is not None:
        row = google_oauth.load_credentials_row(db, assistant_id)
        if row is not None:
            scopes = set((row.scopes or "").split())
            if (
                any(s.endswith("/gmail.send") for s in scopes)
                or any(s.endswith("/gmail.modify") for s in scopes)
                or any(s.endswith("/calendar.readonly") for s in scopes)
                or any(s.endswith("/calendar.events") for s in scopes)
                or any(s.endswith("/calendar") for s in scopes)
            ):
                caps.add("google")
    if web_search_integration.get_provider() is not None:
        caps.add("web_search")
    # Lazy import to avoid pulling settings at module load (config
    # touches a lot of optional integrations on first read).
    from ...config import get_settings

    if get_settings().TELEGRAM_BOT_TOKEN:
        caps.add("telegram")
    return caps
