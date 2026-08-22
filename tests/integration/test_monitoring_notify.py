"""Key-event email after a monitoring run.

The classifier is pure string logic (no DB). The send path is exercised
against the test family with Gmail / OAuth mocked at the same boundary
the SMS→email fallback tests use.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from api import models
from api.config import get_settings
from api.services import monitoring_notify
from api.services.monitoring_notify import (
    classify_monitoring_finding,
    strip_finding_prefix,
    task_page_url,
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_classify_explicit_finding_prefix():
    assert (
        classify_monitoring_finding("FINDING: key_event\nTesla jumped 5%.")
        == "key_event"
    )
    assert (
        classify_monitoring_finding("FINDING: no_change\nNothing new today.")
        == "no_change"
    )
    assert classify_monitoring_finding("KEY EVENT:\nCybercab date set.") == "key_event"
    assert classify_monitoring_finding("NO CHANGE:\nQuiet since yesterday.") == "no_change"


def test_classify_tesla_style_percent_move_is_key_event():
    body = (
        "**Tesla (TSLA) Stock Update - August 22, 2026**\n\n"
        "Tesla's stock saw a significant jump of **5.14%** on the last "
        "trading day, closing at **$362.86**. Anticipation is building "
        "for the September 3 launch event in Austin."
    )
    assert classify_monitoring_finding(body) == "key_event"


def test_classify_quiet_run_and_encyclopedia_dump_are_no_change():
    assert (
        classify_monitoring_finding(
            "Nothing new since the last run — price is still around $360."
        )
        == "no_change"
    )
    # TPC1-style research dump: several definitions, no event language.
    tpc1 = (
        "**TPC1 Research Update**\n\n"
        "There are several distinct entities referred to as TPC1, "
        "including historical submarine cable systems and a biological "
        "protein / cell line. See the attached sources."
    )
    assert classify_monitoring_finding(tpc1) == "no_change"


def test_classify_quiet_phrasing_wins_over_weak_percent():
    assert (
        classify_monitoring_finding(
            "No significant change today, though it ticked 0.2%."
        )
        == "no_change"
    )


def test_strip_finding_prefix_drops_machine_tag():
    assert (
        strip_finding_prefix("FINDING: key_event\n\nTesla jumped 5%.")
        == "Tesla jumped 5%."
    )
    assert strip_finding_prefix("Just a normal comment.") == "Just a normal comment."


def test_task_page_url_uses_public_base_and_query():
    settings = get_settings()
    url = task_page_url(2, 13)
    assert url.startswith(settings.PUBLIC_BASE_URL.rstrip("/"))
    assert url.endswith("/admin/families/2/tasks?tab=monitoring&task=13")


# ---------------------------------------------------------------------------
# notify_if_key_event
# ---------------------------------------------------------------------------


def _make_monitoring_task(db, test_family, *, title: str, body: str):
    now = datetime.now(timezone.utc)
    task = models.Task(
        family_id=test_family["family_id"],
        created_by_person_id=test_family["person_id"],
        title=title,
        description="Email me when something significant happens.",
        status="new",
        priority="normal",
        owner_kind="ai",
        task_kind="monitoring",
        cron_schedule="0 9 * * *",
        monitoring_paused=False,
    )
    db.add(task)
    db.flush()
    db.add(
        models.TaskComment(
            task_id=task.task_id,
            author_person_id=None,
            author_kind="assistant",
            body=body,
            created_at=now,
        )
    )
    db.commit()
    return task, now - timedelta(minutes=5)


def test_notify_sends_email_with_task_link_on_key_event(db, test_family):
    body = (
        "FINDING: key_event\n\n"
        "Tesla jumped 5.14% on Friday and the Cybercab launch event "
        "is set for September 3 in Austin."
    )
    task, started = _make_monitoring_task(
        db, test_family, title="Monitor Tesla stock price", body=body
    )
    email_calls: list[dict[str, Any]] = []

    try:
        with patch(
            "api.services.monitoring_notify.google_oauth.load_credentials",
            return_value=(MagicMock(), MagicMock()),
        ), patch(
            "api.services.monitoring_notify.gmail.send_email",
            side_effect=lambda creds, **kw: email_calls.append(kw) or "gmail-mon-1",
        ):
            result = monitoring_notify.notify_if_key_event(
                db, task_id=task.task_id, run_started_at=started
            )

        assert result.sent is True
        assert result.finding == "key_event"
        assert len(email_calls) == 1
        assert email_calls[0]["to"] == test_family["person_email"]
        assert "Monitor Tesla stock price" in email_calls[0]["subject"]
        assert "5.14%" in email_calls[0]["body"]
        assert "FINDING:" not in email_calls[0]["body"]
        expected_link = task_page_url(test_family["family_id"], task.task_id)
        assert expected_link in email_calls[0]["body"]

        db.expire_all()
        audit = (
            db.query(models.TaskComment)
            .filter(models.TaskComment.task_id == task.task_id)
            .filter(models.TaskComment.body.like("Emailed %"))
            .one()
        )
        assert "IntegrationTest" in audit.body
    finally:
        row = db.get(models.Task, task.task_id)
        if row is not None:
            db.delete(row)
            db.commit()


def test_notify_skips_email_on_quiet_run(db, test_family):
    task, started = _make_monitoring_task(
        db,
        test_family,
        title="Monitor Tesla stock price",
        body="FINDING: no_change\nNothing new since yesterday.",
    )
    try:
        with patch(
            "api.services.monitoring_notify.gmail.send_email"
        ) as send_mock:
            result = monitoring_notify.notify_if_key_event(
                db, task_id=task.task_id, run_started_at=started
            )
        assert result.sent is False
        assert result.finding == "no_change"
        send_mock.assert_not_called()
    finally:
        row = db.get(models.Task, task.task_id)
        if row is not None:
            db.delete(row)
            db.commit()
