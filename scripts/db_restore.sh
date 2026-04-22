#!/usr/bin/env bash
#
# scripts/db_restore.sh
# =====================
#
# Restore a family-assistant Postgres database from a ``pg_dump -Fc``
# file produced by ``scripts/db_backup.sh``.
#
# This script IS destructive — it drops every table in the target DB's
# ``public`` schema before restoring. Two layers of protection:
#
#   1. The target DB and the backup file are printed before anything
#      runs and the script PROMPTS for an explicit ``yes`` to proceed
#      unless ``--yes`` is passed.
#   2. The sidecar ``.meta`` file's sha256 is verified against the
#      dump file before any DROP runs (so a corrupted / partial dump
#      won't get half-applied).
#
# Usage::
#
#     ./scripts/db_restore.sh backups/family_assistant_<timestamp>.dump
#     ./scripts/db_restore.sh backups/family_assistant_<timestamp>.dump --yes
#
# Common gotcha: the user reading .env (``family_assistant``) must have
# the privilege to DROP its own schema. On Homebrew Postgres the role
# is the database owner, so it does. If you ever locked the role down,
# run the restore as a superuser instead:
#
#     PGUSER=ben ./scripts/db_restore.sh ...

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <dump-file> [--yes]" >&2
  exit 2
fi

DUMP_PATH="$1"
shift || true

ASSUME_YES=0
TARGET_DB_OVERRIDE=""
while (( "$#" )); do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    --db) TARGET_DB_OVERRIDE="${2:-}"; shift 2 ;;
    --db=*) TARGET_DB_OVERRIDE="${1#--db=}"; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown flag $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$DUMP_PATH" ]]; then
  echo "ERROR: dump file not found: $DUMP_PATH" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

read_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" | head -1 | sed -E "s/^${key}=//; s/^['\"]//; s/['\"]$//"
}

FA_DB_HOST="$(read_env FA_DB_HOST)"
FA_DB_PORT="$(read_env FA_DB_PORT)"
FA_DB_USER="$(read_env FA_DB_USER)"
FA_DB_PWD="$(read_env FA_DB_PWD)"
FA_DB_NAME="$(read_env FA_DB_NAME)"

: "${FA_DB_HOST:=localhost}"
: "${FA_DB_PORT:=5432}"
: "${FA_DB_USER:=family_assistant}"
: "${FA_DB_NAME:=family_assistant}"

if [[ -n "$TARGET_DB_OVERRIDE" ]]; then
  FA_DB_NAME="$TARGET_DB_OVERRIDE"
fi

# ---------------------------------------------------------------------------
# Verify dump integrity via the sidecar .meta if present.
# ---------------------------------------------------------------------------

META_PATH="$DUMP_PATH.meta"
if [[ -f "$META_PATH" ]]; then
  EXPECTED_SHA=$(grep -E '^sha256: ' "$META_PATH" | awk '{print $2}')
  ACTUAL_SHA=$(shasum -a 256 "$DUMP_PATH" | awk '{print $1}')
  if [[ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]]; then
    echo "ERROR: sha256 mismatch — dump file appears corrupted." >&2
    echo "  expected: $EXPECTED_SHA"
    echo "  actual  : $ACTUAL_SHA"
    exit 3
  fi
  echo "Dump integrity verified (sha256 OK)."
else
  echo "WARNING: $META_PATH not found; skipping integrity check." >&2
fi

# ---------------------------------------------------------------------------
# Show what's about to happen + confirm.
# ---------------------------------------------------------------------------

echo
echo "================================================================"
echo "family-assistant DB RESTORE  (DESTRUCTIVE)"
echo "================================================================"
echo "  dump file     : $DUMP_PATH"
echo "  target DB     : $FA_DB_NAME"
echo "  host:port     : $FA_DB_HOST:$FA_DB_PORT"
echo "  user          : $FA_DB_USER"
echo
echo "  This will DROP every table in '$FA_DB_NAME.public'"
echo "  and recreate them from the dump. ALL CURRENT DATA WILL BE LOST."
echo "================================================================"

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Type the target DB name to confirm: " confirm
  if [[ "$confirm" != "$FA_DB_NAME" ]]; then
    echo "Mismatch ('$confirm' != '$FA_DB_NAME'). Aborting." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Drop + recreate schema, then pg_restore.
# ---------------------------------------------------------------------------

PGPASSWORD="$FA_DB_PWD" psql \
  --host="$FA_DB_HOST" --port="$FA_DB_PORT" \
  --username="$FA_DB_USER" --dbname="$FA_DB_NAME" \
  --quiet --no-psqlrc \
  -v ON_ERROR_STOP=1 \
  -c "DROP SCHEMA IF EXISTS public CASCADE;" \
  -c "CREATE SCHEMA public;"

PGPASSWORD="$FA_DB_PWD" pg_restore \
  --host="$FA_DB_HOST" --port="$FA_DB_PORT" \
  --username="$FA_DB_USER" --dbname="$FA_DB_NAME" \
  --no-owner --no-privileges \
  --exit-on-error \
  "$DUMP_PATH"

echo
echo "Restore complete. '$FA_DB_NAME' now reflects the dump."
