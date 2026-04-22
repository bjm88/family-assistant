#!/usr/bin/env bash
#
# scripts/run_tests.sh
# ====================
#
# Safe entry point for the integration test suite.
#
# Why this script exists
# ----------------------
# The conftest already refuses to run against a DB whose name doesn't
# contain "test" (see ``tests/integration/conftest.py``), but a single
# stray ``FA_DB_NAME=family_assistant`` left over in your shell could
# still mask that check at *bootstrap* time on a future change. After
# the schema-drop incident (April 2026) we agreed to never run any
# pytest invocation that could possibly resolve to the live DB. This
# script enforces that with multiple independent layers so a typo,
# stale env var, or sourced ``.env`` cannot route the suite at the
# wrong place.
#
# Triple-lock safety
# ------------------
# 1. We hard-code ``family_assistant_test`` here and EXPORT it,
#    overriding anything in your shell or ``.env``.
# 2. We diff that name against the live ``FA_DB_NAME`` parsed out of
#    ``.env`` and abort if they collide (defensive — "test" must always
#    be in the test DB name AND must not equal the live name).
# 3. We assert the resolved name contains the substring "test" before
#    handing off to pytest. Pytest itself does the same check inside
#    conftest.py, so a future maintainer can't accidentally remove
#    this script's guard without the suite still failing fast.
# 4. We point ``FA_STORAGE_ROOT`` at a scratch directory so any test
#    that ever tries to write a file can't clobber live photos.
# 5. We never invoke ``alembic downgrade`` from anywhere — the only
#    destructive Alembic op in the codebase lives in ``downgrade()``
#    of the baseline migration. Bootstrap inside conftest only runs
#    ``alembic upgrade head``, which is purely additive.
#
# Usage
# -----
#   ./scripts/run_tests.sh                    # full integration suite
#   ./scripts/run_tests.sh -v                 # verbose pytest output
#   ./scripts/run_tests.sh -k whatsapp        # filter by name
#   ./scripts/run_tests.sh tests/integration/test_smoke.py
#
# Exit codes mirror pytest (0 = pass, non-zero = at least one failure
# or a safety check tripped).

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root + load .env (read-only — we don't ``source`` it)
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

# Mirrored from db_backup.sh — read a single key out of .env without
# evaluating it as shell. Strips surrounding quotes, ignores comments.
read_env() {
  local key="$1"
  if [[ ! -f "$ENV_FILE" ]]; then
    return 0
  fi
  grep -E "^${key}=" "$ENV_FILE" | head -1 \
    | sed -E "s/^${key}=//; s/^['\"]//; s/['\"]$//" || true
}

LIVE_DB_NAME="$(read_env FA_DB_NAME || true)"
LIVE_DB_NAME="${LIVE_DB_NAME:-family_assistant}"

# ---------------------------------------------------------------------------
# Force test-only environment
# ---------------------------------------------------------------------------

# Hard-coded — NEVER read from env so a poisoned shell can't override.
TEST_DB_NAME="family_assistant_test"
TEST_STORAGE_ROOT="$REPO_ROOT/resources/_test_scratch"

# Lock 1: the test DB name we're about to use must contain "test".
if [[ "$TEST_DB_NAME" != *test* ]]; then
  echo "FATAL: TEST_DB_NAME=$TEST_DB_NAME does not contain 'test'." >&2
  echo "Refusing to run — this script has been edited unsafely." >&2
  exit 2
fi

# Lock 2: the test DB name must differ from the live DB in .env.
if [[ "$TEST_DB_NAME" == "$LIVE_DB_NAME" ]]; then
  echo "FATAL: TEST_DB_NAME ($TEST_DB_NAME) collides with the live DB" >&2
  echo "name from $ENV_FILE ($LIVE_DB_NAME)." >&2
  echo "Refusing to run — change .env or this script." >&2
  exit 2
fi

# Lock 3: scrub any pre-existing FA_DB_NAME from the parent env, then
# export the test value. ``unset`` first guarantees the export wins
# even on shells that treat readonly vars specially.
unset FA_DB_NAME || true
export FA_DB_NAME="$TEST_DB_NAME"

# Lock 4: scratch storage root so file-writing tests can't touch
# resources/family. Created lazily on first run.
mkdir -p "$TEST_STORAGE_ROOT"
unset FA_STORAGE_ROOT || true
export FA_STORAGE_ROOT="$TEST_STORAGE_ROOT"

# ---------------------------------------------------------------------------
# Show the user exactly what's about to happen
# ---------------------------------------------------------------------------

cat <<EOF
================================================================
Family Assistant — integration test runner
----------------------------------------------------------------
  Test DB:      $FA_DB_NAME      (live DB in .env: $LIVE_DB_NAME)
  Storage root: $FA_STORAGE_ROOT
  Suite:        tests/integration
  Pytest args:  ${*:-(none)}
================================================================
EOF

# ---------------------------------------------------------------------------
# Hand off to pytest. conftest.py performs its own "name contains test"
# guard at session start as a final belt-and-braces; if any of the
# above locks were bypassed, the suite still refuses to start.
# ---------------------------------------------------------------------------

cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  exec uv run pytest tests/integration "$@"
else
  exec pytest tests/integration "$@"
fi
