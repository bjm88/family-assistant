# Integration tests

Adhoc, refactor-confidence integration tests for the FastAPI backend.

## Philosophy

These tests exist to give us confidence when we **refactor** or land a
**major new feature** — they're not unit tests, not run automatically,
and not exhaustive. The goal: prove the load-bearing parts of the
system still work end-to-end, against a real Postgres, with as little
mocking as possible.

Specifically, every test:

1. Drives the API through FastAPI's in-process `TestClient` (no
   separate `uvicorn` to start) **or** calls a service entry point
   directly with the real test database.
2. Verifies the response body **and** cross-checks side effects with
   a direct SQLAlchemy `SELECT` on the same DB.
3. Runs against an isolated `family_assistant_test` Postgres database
   so the live `family_assistant` DB is never touched.

The suite is intentionally **ad-hoc** — there is no CI step, no git
pre-commit hook, no scheduled run. Tests fire only when a developer
asks for them, via either:

```sh
./scripts/run_tests.sh                 # canonical entry point — safest
make test                              # same thing, via Make
make test-integration                  # verbose flavour
uv run pytest tests/integration        # works but bypasses run_tests.sh's safety locks
```

**Always prefer `./scripts/run_tests.sh`.** It hard-codes
`FA_DB_NAME=family_assistant_test`, scrubs any colliding value out of
your shell, points `FA_STORAGE_ROOT` at a scratch directory, and
refuses to run if the test DB name would resolve to the live DB. The
conftest does its own "name must contain 'test'" guard as a final
belt-and-braces, so even bare `pytest` can't reach the live DB —
but the script gives you a clear pre-flight summary and additional
defenses you don't get from invoking pytest directly. See the
script's header for the full safety rationale.

Pytest's auto-discovery does pick up the `tests/` directory if you
just run `pytest` from the repo root — that's fine and intentional.
The "ad-hoc" guarantee comes from the absence of any automated runner,
not from making the tests hard to invoke.

## One-time setup

The test database needs to exist and be owned by your `family_assistant`
Postgres user. The conftest tries to auto-create it on first run; if
your dev role doesn't have `CREATEDB` (the common case), do this once
as a Postgres superuser:

```sh
psql postgres -c "CREATE DATABASE family_assistant_test OWNER family_assistant;"
```

Then install the test deps:

```sh
make install-test
# or: uv sync --group test
```

## Running

```sh
./scripts/run_tests.sh                  # full suite (recommended)
./scripts/run_tests.sh -v               # verbose
make test-integration                   # same as -v, via Make
make test-integration-fast              # summary only
```

You can scope to one file or one test by passing extra args through
to pytest — the script forwards everything after the script name:

```sh
./scripts/run_tests.sh tests/integration/test_inbound_channels.py -v
./scripts/run_tests.sh tests/integration/test_inbound_channels.py::test_whatsapp_inbound_routes_to_agent -v
./scripts/run_tests.sh -k whatsapp
```

## What's covered

| File | What it proves |
|------|----------------|
| `test_smoke.py` | The harness works: health probe + families list round-trip. |
| `test_admin_crud.py` | Light CRUD happy paths for `/api/admin/people` and `/api/admin/jobs` (the resource we just refactored). |
| `test_inbound_channels.py` | Every inbound surface — SMS, WhatsApp, Telegram, Email, plus the live-web chat status probe — routes a message into the shared agent layer and produces the expected audit row + `LiveSession` + outbound send. |
| `test_agent_tools.py` | The agent's key tool handlers (`task_create` for todos, `task_create` for AI monitoring with cron, `web_search`) actually mutate the DB / return the expected payload shape. |

## What's NOT covered (by design)

- The actual LLM (Ollama, Gemini). Every test mocks the brain at the
  `_run_agent_to_completion` boundary or one layer above. Adding real
  LLM coverage would make the suite non-deterministic and slow — out
  of scope for refactor confidence.
- Outbound network calls (Twilio, Telegram Bot API, Gmail). Mocked at
  the integration boundary so tests stay fully offline.
- Streaming SSE on `POST /api/aiassistant/chat`. We hit the cheap
  `/status` endpoint as a smoke probe instead — exercising the full
  SSE pipeline would require mocking the async agent generator inside
  the streaming response, which is fragile. Add it when we have a
  reason to.
- Migration replay. Tests build the schema directly from
  `Base.metadata.create_all()` for one reason: migration `0001` uses
  `Base.metadata.create_all` itself, which makes the chain
  non-replayable from scratch (migration `0002` then tries to rename a
  column that already has its post-rename name). The live dev DB only
  works because it was created when the ORM matched the historical
  shape. Repairing that chain is a separate task; for now, the test
  schema exactly mirrors the live ORM.

## Cleanup philosophy

Per the user's call: the dedicated `__integration_test__` family + its
`IntegrationTest User` person are **find-or-create** and persist
between runs (no churn). Things tests intentionally create/delete
(jobs, extra people, individual tasks) clean themselves up in their
own teardown. Audit rows in `sms_inbox_messages` /
`email_inbox_messages` / `live_sessions` / `agent_tasks` are allowed
to accumulate — that mirrors what the live system looks like and
keeps assertions on "did the audit row land?" honest.

## Adding a new test

1. Pick the right file (or add a new `test_*.py` in
   `tests/integration/`).
2. Use the existing fixtures from `conftest.py`:
   - `client` — FastAPI TestClient (no lifespan; safe to make
     repeated requests in one test).
   - `db` — request-scoped SQLAlchemy session for direct
     verification queries.
   - `test_family` — dict with `family_id`, `person_id`,
     `person_phone`, `person_email`, `assistant_id` for the
     long-lived test family.
3. If your test creates rows the suite doesn't otherwise need
   (e.g. additional people, jobs), delete them in a `try/finally`
   so re-runs stay quiet.
4. If you need to mock an external boundary (Twilio, Telegram, Gmail,
   Ollama, Gemini), patch at the integration module the service
   actually imports (e.g. `api.services.email_inbox.gmail.send_reply`,
   not `api.integrations.gmail.send_reply`) so the mock catches the
   real call site.
