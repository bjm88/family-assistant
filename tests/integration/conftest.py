"""Integration-test harness for the FastAPI backend.

Design notes
------------
* **Where the API runs.** Tests use FastAPI's in-process ``TestClient``
  rather than hitting a live ``uvicorn``. That keeps the harness fully
  reproducible (no port collisions, no "did I remember to start the
  server?"), while still exercising the real router → service → DB →
  response stack — the same code paths a real HTTP client traverses.

* **Where the data lives.** A dedicated ``family_assistant_test``
  Postgres database, completely separate from the live ``family_assistant``
  DB. The DB name *must* contain the substring ``"test"``; we hard-fail
  otherwise, so nobody can ever wire the suite at the production DB
  by accident. The first run auto-creates the DB and applies all
  alembic migrations; subsequent runs just upgrade to ``head``.

* **Environment overrides.** Every external long-running loop
  (Gmail poller, Telegram poller, monitoring scheduler) is force-disabled
  for the test process so importing :mod:`api.main` doesn't kick off
  background work. Twilio signature verification is disabled by leaving
  ``TWILIO_AUTH_TOKEN`` empty; the SMS webhook router skips signature
  checks in that mode (see ``routers/sms_webhook.py``).

* **Cleanup philosophy.** Per the user's direction: the integration
  test family is *find-or-create*, the test person stays put across
  runs, and chatter (live_sessions, sms_inbox_messages, agent_tasks,
  tasks) is allowed to accumulate. Tests that intentionally exercise
  CREATE/DELETE on admin resources clean up after themselves naturally
  via their own DELETE step.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment overrides — MUST happen before any `from api import ...`
# because get_settings() is @lru_cache'd; the first import freezes the
# settings singleton against whatever env vars are present at that
# moment.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "python"))

# Default test DB name; overridable via env so a developer can point at
# a personal scratch DB if they want. The "test" substring check below
# enforces the safety invariant either way.
os.environ.setdefault("FA_DB_NAME", "family_assistant_test")

# Background long-running loops live in `_lifespan` in api.main. The
# `client` fixture below deliberately does NOT enter the lifespan
# context (no `with TestClient(app):`), so the email / Telegram /
# monitoring pollers never start during tests regardless of these
# flags. We therefore leave the per-surface ENABLED flags TRUE so the
# request-time gates (e.g. ``if not AI_TELEGRAM_INBOUND_ENABLED``
# inside process_inbound_update) don't short-circuit before the agent
# runs. The monitoring loop's *internal* tick gate is force-off so
# nothing bad can happen if a test ever ticked it directly.
os.environ.setdefault("AI_EMAIL_INBOX_ENABLED", "true")
os.environ.setdefault("AI_TELEGRAM_INBOUND_ENABLED", "true")
os.environ.setdefault("AI_MONITORING_ENABLED", "false")
# Fast-ack adds a real-time race + threadpool submit that complicates
# inbound mocking and gives us nothing in tests (the contextual ack
# is a UX nicety on top of the heavy reply, not a correctness signal).
os.environ.setdefault("AI_FAST_ACK_ENABLED", "false")

# Twilio: leave AUTH_TOKEN unset so the SMS webhook accepts unsigned
# POSTs (its handler only enforces X-Twilio-Signature when a token is
# configured). Set sender numbers so channel-config code can resolve
# self-loop checks etc. without crashing on None.
os.environ.setdefault("TWILIO_AUTH_TOKEN", "")
os.environ.setdefault("TWILIO_PRIMARY_PHONE", "+15550001111")
os.environ.setdefault("TWILIO_WHATSAPP_SENDER_NUMBER", "+15550002222")
os.environ.setdefault("AI_WHATSAPP_INBOUND_ENABLED", "true")
os.environ.setdefault("AI_SMS_INBOUND_ENABLED", "true")

# Disable the live-chat web-search shortcut so /chat tests don't try
# to call out to Gemini. The shortcut is a pre-agent fast path that
# would hijack a "what's the weather?" prompt; for tests we want the
# normal agent flow (which we mock).
os.environ.setdefault("AI_WEB_SEARCH_SHORTCUT_ENABLED", "false")

# ---------------------------------------------------------------------------
# Now safe to import the app + DB layer. Everything below runs with the
# test environment baked in.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402
from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from api import models  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.db import SessionLocal, engine  # noqa: E402
from api.main import create_app  # noqa: E402


TEST_FAMILY_NAME = "__integration_test__"
TEST_PERSON = {
    "first_name": "IntegrationTest",
    "last_name": "User",
    "mobile_phone_number": "+15558675309",
    "email_address": "integration.test@example.com",
}


# ---------------------------------------------------------------------------
# DB bootstrap — runs once per pytest session
# ---------------------------------------------------------------------------


def _ensure_test_db_exists(db_name: str) -> None:
    """Confirm the test database is reachable; auto-create when allowed.

    Strategy:
    1. Try a direct connection to ``db_name`` through the app's own
       engine. If that succeeds we're done — nothing to bootstrap.
    2. Otherwise connect to the cluster's ``postgres`` maintenance DB
       and ``CREATE DATABASE``. If the configured user lacks ``CREATEDB``
       (common on lightly-provisioned local Postgres), surface a clear
       error with the exact one-time fix.

    ``CREATE DATABASE`` can't run inside a transaction, so the
    maintenance engine is opened with ``AUTOCOMMIT`` isolation.
    """
    settings = get_settings()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return
    except OperationalError:
        # Falls through to the create attempt below.
        pass

    maintenance_url = (
        f"postgresql+psycopg2://{settings.FA_DB_USER}:{settings.FA_DB_PWD}"
        f"@{settings.FA_DB_HOST}:{settings.FA_DB_PORT}/postgres"
    )
    try:
        with create_engine(
            maintenance_url, isolation_level="AUTOCOMMIT"
        ).connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": db_name},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except Exception as exc:  # noqa: BLE001 — any failure here is fatal
        pytest.exit(
            "Could not bootstrap the integration-test database "
            f"{db_name!r}.\n\n"
            f"Original error: {exc}\n\n"
            "One-time fix (run as a Postgres superuser, e.g. the "
            "postgres role on macOS Homebrew):\n"
            f"  createdb {db_name}\n"
            "  # or:\n"
            f"  psql postgres -c 'CREATE DATABASE {db_name} "
            f"OWNER {settings.FA_DB_USER};'\n",
            returncode=2,
        )


def _build_schema() -> None:
    """Run the alembic chain to materialise the test DB schema.

    The migration chain was squashed to a single baseline (see
    ``python/api/migrations/versions/0001_initial_schema.py``) so this
    is now just ``alembic upgrade head`` — fully equivalent to how a
    new prod DB is provisioned, which means the test harness exercises
    the same migration path real environments use.

    Idempotent: alembic skips revisions already recorded in
    ``alembic_version``.
    """
    alembic_cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    alembic_command.upgrade(alembic_cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_test_database():
    """Create + migrate the test DB once per pytest session.

    ``autouse=True`` so every test inherits the guarantee that the DB
    is up and at ``head`` revision — no individual test has to ask.
    The paranoia check (DB name must contain 'test') runs here too so
    a misconfigured environment fails fast at collection time, not in
    the middle of a test run.
    """
    settings = get_settings()
    if "test" not in settings.FA_DB_NAME.lower():
        pytest.exit(
            f"Refusing to run integration tests against DB "
            f"{settings.FA_DB_NAME!r} — name must contain 'test'. "
            "Set FA_DB_NAME=family_assistant_test (or any name with "
            "'test' in it) before running pytest.",
            returncode=2,
        )

    _ensure_test_db_exists(settings.FA_DB_NAME)
    _build_schema()

    # Sanity-check connectivity through the app's engine (the same one
    # FastAPI requests will use). A trivial SELECT against any of the
    # tables we just created proves the schema is in place.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1 FROM families LIMIT 1"))
    yield


# ---------------------------------------------------------------------------
# Core fixtures — request- (function-) scoped unless otherwise noted
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    """Build the FastAPI app once per session.

    The app reads ``get_settings()`` lazily on each request so it'll
    pick up the test-time env vars set above. We do NOT wrap this in
    ``with TestClient(app):`` here — that's the per-test ``client``
    fixture's job, because TestClient enters/exits the lifespan, and
    we don't want lifespan side-effects (background loops) running
    repeatedly across tests.
    """
    return create_app()


@pytest.fixture
def client(app):
    """Per-test FastAPI TestClient with lifespan disabled.

    The app's lifespan starts the email/telegram pollers + monitoring
    scheduler. Even with their ENABLED flags set to false, the
    ``ollama_warmup`` task fires unconditionally and would hammer a
    non-existent Ollama during tests. We skip lifespan entirely by
    NOT using the ``with TestClient(app):`` form — TestClient still
    works for plain request dispatch without it.
    """
    return TestClient(app)


@pytest.fixture
def db():
    """A request-scoped SQLAlchemy session against the test DB.

    Tests use this for direct ``SELECT``s to verify side effects of
    API calls (audit rows, live_sessions, agent_tasks, tasks). Always
    closed at teardown; ``expire_on_commit=False`` is inherited from
    the global SessionLocal so attributes stay readable after commit.
    """
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Convenience: the long-lived test family + person
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_family():
    """Find-or-create the dedicated integration-test family + a person.

    Idempotent: the row sticks around between runs, which is what the
    user wanted ("we can have check if exists or create for core family
    members and let messages and tasks build up"). Returns a small
    dict of IDs the tests can pin against.
    """
    s = SessionLocal()
    try:
        fam = s.execute(
            select(models.Family).where(
                models.Family.family_name == TEST_FAMILY_NAME
            )
        ).scalar_one_or_none()
        if fam is None:
            fam = models.Family(family_name=TEST_FAMILY_NAME)
            s.add(fam)
            s.flush()

        person = s.execute(
            select(models.Person)
            .where(models.Person.family_id == fam.family_id)
            .where(models.Person.first_name == TEST_PERSON["first_name"])
        ).scalar_one_or_none()
        if person is None:
            person = models.Person(
                family_id=fam.family_id, **TEST_PERSON
            )
            s.add(person)
            s.flush()

        # Email + AI-tool capability detection ask for an Assistant row;
        # most tests don't care which name it has, but the family must
        # have one (it's a 1:1 via uq_assistant_per_family).
        assistant = s.execute(
            select(models.Assistant).where(
                models.Assistant.family_id == fam.family_id
            )
        ).scalar_one_or_none()
        if assistant is None:
            assistant = models.Assistant(family_id=fam.family_id)
            s.add(assistant)
            s.flush()

        s.commit()
        return {
            "family_id": fam.family_id,
            "family_name": fam.family_name,
            "person_id": person.person_id,
            "person_phone": person.mobile_phone_number,
            "person_email": person.email_address,
            "assistant_id": assistant.assistant_id,
        }
    finally:
        s.close()
