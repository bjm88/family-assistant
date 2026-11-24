"""Google keep-alive: refresh outcomes, alert de-duplication, recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.integrations import google_oauth
from api.services import google_keepalive as ka


class _FakeDb:
    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    ka._state.clear()
    sent: list[str] = []
    outcome: dict = {}

    def fake_refresh(_db, _aid):
        if "raise" in outcome:
            raise outcome["raise"]
        return google_oauth.RefreshResult(
            access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            refresh_token_expires_at=outcome.get("rt_expires"),
            email="avi@example.com",
            scopes=(),
        )

    monkeypatch.setattr(google_oauth, "force_refresh", fake_refresh)
    monkeypatch.setattr(ka, "_alert_parents", lambda _db, _aid, text: sent.append(text) or True)
    monkeypatch.setattr(ka, "_reconnect_url", lambda _db, _aid: "http://x/assistant")
    row = SimpleNamespace(assistant_id=7, granted_email="avi@example.com")
    yield SimpleNamespace(
        run=lambda: ka._check_assistant(_FakeDb(), row),
        sent=sent,
        outcome=outcome,
        state=lambda: ka._state[7],
    )
    ka._state.clear()


def test_healthy_grant_records_ok_and_sends_nothing(harness):
    harness.run()
    assert harness.state().last_ok_at is not None
    assert harness.state().last_error is None
    assert harness.sent == []


def test_revoked_grant_alerts_once_then_again_after_recovery(harness):
    harness.outcome["raise"] = google_oauth.GoogleRefreshRevoked("invalid_grant: Token has been expired or revoked.")
    harness.run()
    harness.run()
    assert len(harness.sent) == 1
    assert "Reconnect here" in harness.sent[0]
    assert harness.state().revoked

    harness.outcome.clear()
    harness.run()
    assert not harness.state().revoked

    harness.outcome["raise"] = google_oauth.GoogleRefreshRevoked("invalid_grant")
    harness.run()
    assert len(harness.sent) == 2


def test_transient_failure_is_logged_not_alerted(harness):
    harness.outcome["raise"] = google_oauth.GoogleOAuthError("Token endpoint unreachable")
    harness.run()
    assert harness.sent == []
    assert "unreachable" in harness.state().last_error
    assert not harness.state().revoked


def test_fixed_lifetime_grant_warns_once_when_close(harness):
    harness.outcome["rt_expires"] = datetime.now(timezone.utc) + timedelta(days=6)
    harness.run()
    assert harness.sent == []
    assert harness.state().refresh_token_expires_at is not None

    harness.outcome["rt_expires"] = datetime.now(timezone.utc) + timedelta(hours=20)
    harness.run()
    harness.run()
    assert len(harness.sent) == 1
    assert "Testing mode" in harness.sent[0]
