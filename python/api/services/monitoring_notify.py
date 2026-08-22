"""Email the household when a monitoring run finds a key event.

A monitoring task's research loop posts findings as ``task_comments``.
Most runs are quiet ("nothing new since yesterday"). When a run
surfaces something the household actually cares about — a 5% Tesla
move, a Cybercab launch date, a new listing — we also send a plain-
text email to the task's creator and followers, with the finding and
a deep-link back to the task.

This is deterministic on purpose. The safety sandbox tells the model
not to send unsolicited mail, and monitoring runs have no live user
to "ask" — so relying on ``gmail_send`` inside the agent loop is how
the Tesla monitor's "email me when something significant happens"
instruction was silently dropped. The scheduler calls
:func:`notify_if_key_event` after every successful run; the model
only has to write a clear comment.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Literal, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..ai import assistants as _assistants
from ..config import get_settings
from ..integrations import gmail, google_oauth


logger = logging.getLogger(__name__)


FindingKind = Literal["key_event", "no_change"]


# First-line tags the monitoring prompt asks the model to emit. We
# strip them before storing / emailing so the household never sees
# the machine label.
_FINDING_PREFIX_RE = re.compile(
    r"^(?:FINDING:\s*(key_event|no_change)|KEY[_\s-]?EVENT:|NO[_\s-]?CHANGE:)\s*",
    re.IGNORECASE,
)

# Quiet-run phrasing the prompt already teaches ("say exactly that in
# one sentence"). A comment that matches these and has no event signal
# is not emailed.
_NO_CHANGE_RE = re.compile(
    r"nothing new|no meaningful|no change since|nothing meaningful|"
    r"no significant|no updates|no noteworthy|unchanged|"
    r"nothing to report|no major (?:change|news|updates?)|"
    r"quiet since|no new (?:news|developments?)",
    re.IGNORECASE,
)

# Signals that the household asked to be woken up for. Tuned against
# the live Tesla monitor (percent moves, launch events, earnings) so
# a generic research dump like TPC1 does not email.
_KEY_EVENT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*%|"
    r"\b(?:jumped|soared|plunged|tumbled|rallied|slumped)\b|"
    r"significant (?:jump|drop|move|gain|loss|swing)|"
    r"launch event|\bunveiled\b|\bunveils\b|"
    r"\bearnings\b|\bannounced\b|"
    r"key event|breaking news",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of a single post-run notify attempt. Never raises to callers."""

    sent: bool
    finding: FindingKind
    detail: str
    recipients: tuple[str, ...] = ()
    gmail_message_ids: tuple[str, ...] = ()


@dataclass
class _Recipient:
    person_id: int
    name: str
    email: str


def classify_monitoring_finding(body: str) -> FindingKind:
    """Decide whether a monitoring comment is a key event or a quiet run.

    Explicit ``FINDING:`` / ``KEY EVENT:`` prefixes win. Otherwise we
    look for quiet-run phrasing vs. event signals (percent moves,
    launches, earnings). Default is ``no_change`` so routine research
    dumps do not spam the inbox.
    """
    text = (body or "").strip()
    if not text:
        return "no_change"

    first = text.splitlines()[0].strip()
    prefix = _FINDING_PREFIX_RE.match(first)
    if prefix:
        tagged = (prefix.group(1) or "").lower()
        if tagged == "no_change" or first.upper().startswith("NO"):
            return "no_change"
        return "key_event"

    has_quiet = bool(_NO_CHANGE_RE.search(text))
    has_event = bool(_KEY_EVENT_RE.search(text))
    if has_event and not has_quiet:
        return "key_event"
    if has_event and has_quiet:
        # "No significant change, though it jumped 0.2%" — the quiet
        # phrasing is the model's own judgement; trust it.
        return "no_change"
    return "no_change"


def strip_finding_prefix(body: str) -> str:
    """Drop a leading ``FINDING:`` / ``KEY EVENT:`` line from ``body``."""
    lines = (body or "").splitlines()
    if not lines:
        return body or ""
    if _FINDING_PREFIX_RE.match(lines[0].strip()):
        return "\n".join(lines[1:]).lstrip("\n")
    return body


def task_page_url(family_id: int, task_id: int) -> str:
    """Deep-link that opens the monitoring tab + this task's detail modal."""
    base = (get_settings().PUBLIC_BASE_URL or "http://localhost:8000").rstrip(
        "/"
    )
    return (
        f"{base}/admin/families/{family_id}/tasks"
        f"?tab=monitoring&task={task_id}"
    )


def notify_if_key_event(
    db: Session,
    *,
    task_id: int,
    run_started_at: datetime,
    fallback_body: str = "",
    record_audit_comment: bool = True,
) -> NotifyResult:
    """Email creator + followers when this run found a key event.

    Picks the longest assistant comment written since
    ``run_started_at`` (the detailed ``task_add_comment`` body, not
    the one-line wrap-up the agent sometimes uses as its final
    message). Failures are logged and returned — they must never
    flip the monitoring run itself to ``error``.
    """
    task = db.get(models.Task, task_id)
    if task is None:
        return NotifyResult(False, "no_change", "task disappeared")

    body, new_links = _best_finding_from_run(
        db, task_id=task_id, since=run_started_at, fallback_body=fallback_body
    )
    finding = classify_monitoring_finding(body)
    if finding != "key_event":
        return NotifyResult(
            False, finding, "run was not a key event; no email sent"
        )

    recipients = _recipients_for_task(db, task)
    if not recipients:
        logger.info(
            "monitoring_notify: task %d has a key event but no "
            "creator/follower email addresses",
            task_id,
        )
        return NotifyResult(
            False, finding, "key event but no recipient email on file"
        )

    assistant_id = _assistants.assistant_id_for_family(db, task.family_id)
    if assistant_id is None:
        return NotifyResult(False, finding, "no assistant for family")
    try:
        _row, creds = google_oauth.load_credentials(db, assistant_id)
    except google_oauth.GoogleNotConnected:
        return NotifyResult(False, finding, "google not connected")
    except (google_oauth.GoogleOAuthError, google_oauth.GoogleOAuthUnavailable) as exc:
        return NotifyResult(False, finding, f"google oauth: {exc}")

    subject, email_body = _format_key_event_email(
        task=task, body=body, links=new_links
    )
    sent_ids: List[str] = []
    sent_to: List[str] = []
    last_error: Optional[str] = None
    for recipient in recipients:
        try:
            gmail_id = gmail.send_email(
                creds, to=recipient.email, subject=subject, body=email_body
            )
        except gmail.GmailSendError as exc:
            last_error = str(exc)
            logger.exception(
                "monitoring_notify: Gmail send failed task=%d to=%s",
                task_id,
                recipient.email,
            )
            continue
        sent_ids.append(gmail_id)
        sent_to.append(recipient.email)
        logger.info(
            "monitoring_notify: emailed task %d ('%s') to %s gmail_id=%s",
            task_id,
            task.title,
            recipient.email,
            gmail_id,
        )

    if not sent_ids:
        return NotifyResult(
            False,
            finding,
            last_error or "gmail send failed",
            recipients=tuple(r.email for r in recipients),
        )

    if record_audit_comment:
        names = ", ".join(r.name for r in recipients if r.email in sent_to)
        db.add(
            models.TaskComment(
                task_id=task.task_id,
                author_person_id=None,
                author_kind="assistant",
                body=f"Emailed {names} a summary of this finding.",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    return NotifyResult(
        True,
        finding,
        f"emailed {', '.join(sent_to)}",
        recipients=tuple(sent_to),
        gmail_message_ids=tuple(sent_ids),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _best_finding_from_run(
    db: Session,
    *,
    task_id: int,
    since: datetime,
    fallback_body: str,
) -> tuple[str, List[models.TaskLink]]:
    """Longest new assistant comment + links attached during this run."""
    comments = (
        db.execute(
            select(models.TaskComment)
            .where(models.TaskComment.task_id == task_id)
            .where(models.TaskComment.author_kind == "assistant")
            .where(models.TaskComment.created_at >= since)
        )
        .scalars()
        .all()
    )
    # Skip the short "Emailed …" audit notes if a retry re-enters.
    candidates = [
        (c.body or "").strip()
        for c in comments
        if (c.body or "").strip()
        and not (c.body or "").startswith("Emailed ")
    ]
    if fallback_body and fallback_body.strip():
        candidates.append(fallback_body.strip())
    body = max(candidates, key=len) if candidates else ""

    links = (
        db.execute(
            select(models.TaskLink)
            .where(models.TaskLink.task_id == task_id)
            .where(models.TaskLink.created_at >= since)
            .order_by(models.TaskLink.created_at.desc())
        )
        .scalars()
        .all()
    )
    return body, list(links)


def _recipients_for_task(db: Session, task: models.Task) -> List[_Recipient]:
    """Creator + followers who have an email, de-duplicated by address."""
    person_ids: List[int] = []
    if task.created_by_person_id:
        person_ids.append(task.created_by_person_id)
    for follower in task.followers:
        person_ids.append(follower.person_id)

    seen_emails: set[str] = set()
    out: List[_Recipient] = []
    for person_id in person_ids:
        person = db.get(models.Person, person_id)
        if person is None or person.family_id != task.family_id:
            continue
        email = (person.email_address or "").strip()
        if not email:
            continue
        key = email.lower()
        if key in seen_emails:
            continue
        seen_emails.add(key)
        name = (
            (person.preferred_name or person.first_name or "").strip()
            or email
        )
        out.append(_Recipient(person.person_id, name, email))
    return out


def _format_key_event_email(
    *,
    task: models.Task,
    body: str,
    links: Sequence[models.TaskLink],
) -> tuple[str, str]:
    clean = strip_finding_prefix(body).strip() or "(No summary was written.)"
    url = task_page_url(task.family_id, task.task_id)
    subject = f"Avi: update on {task.title}"

    parts = [
        f'Avi here — a key update on "{task.title}":',
        "",
        clean,
    ]
    if links:
        parts.append("")
        parts.append("Sources from this run:")
        for link in links[:8]:
            label = (link.title or "").strip() or link.url
            parts.append(f"- {label}")
            parts.append(f"  {link.url}")
            if link.summary:
                summary = link.summary.strip().replace("\n", " ")
                if len(summary) > 200:
                    summary = summary[:197] + "…"
                parts.append(f"  {summary}")
    parts.extend(
        [
            "",
            "Open the task (full history, sources, and comments):",
            url,
            "",
            "— Avi",
        ]
    )
    return subject, "\n".join(parts)


__all__ = [
    "FindingKind",
    "NotifyResult",
    "classify_monitoring_finding",
    "notify_if_key_event",
    "strip_finding_prefix",
    "task_page_url",
]
