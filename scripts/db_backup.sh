#!/usr/bin/env bash
#
# scripts/db_backup.sh
# ====================
#
# Full backup of the family-assistant Postgres database.
#
# - Reads connection details from the project's ``.env`` (FA_DB_HOST,
#   FA_DB_PORT, FA_DB_USER, FA_DB_PWD, FA_DB_NAME) so this script
#   always backs up the same DB the running app reads from.
# - Writes a timestamped ``.dump`` file (PostgreSQL custom format) into
#   ``backups/`` at the repo root. Custom-format dumps are compressed
#   AND restorable with ``pg_restore`` (selective table restore, parallel
#   restore, etc.) — that's why we use ``-Fc`` instead of plain SQL.
# - Also drops a checksum + summary into ``backups/<dump>.meta`` so the
#   matching restore script can verify the file before clobbering live
#   data.
# - Optionally takes a label as the first argument (e.g. ``./scripts/db_backup.sh
#   pre-refactor``) which gets included in the filename.
#
# Usage::
#
#     ./scripts/db_backup.sh                  # backups/family_assistant_2026-04-21T22-30-00.dump
#     ./scripts/db_backup.sh pre-refactor     # backups/family_assistant_pre-refactor_2026-04-21T22-30-00.dump
#     ./scripts/db_backup.sh --keep 7         # also prune dumps older than the newest 7
#
# Restore with: ./scripts/db_restore.sh backups/<file>.dump
#
# IMPORTANT: This script is read-only as far as the LIVE DB is concerned
# (pg_dump only reads). It cannot damage data. The companion
# ``db_restore.sh`` is the destructive one and prompts before clobbering.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root + load .env
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Refusing to guess connection settings." >&2
  exit 2
fi

# Pull DB vars from .env without ``source`` (so we don't accidentally
# execute arbitrary shell sitting in .env). Lines without an ``=`` are
# skipped, comments are ignored, surrounding quotes are stripped.
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

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

LABEL=""
KEEP=""
while (( "$#" )); do
  case "$1" in
    --keep)
      KEEP="${2:-}"
      shift 2
      ;;
    --keep=*)
      KEEP="${1#--keep=}"
      shift
      ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    -*)
      echo "ERROR: unknown flag $1" >&2
      exit 2
      ;;
    *)
      if [[ -z "$LABEL" ]]; then
        LABEL="$1"
      else
        echo "ERROR: too many positional args (got LABEL='$LABEL' and '$1')" >&2
        exit 2
      fi
      shift
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Show what's about to happen (defensive: every backup print the target)
# ---------------------------------------------------------------------------

BACKUP_DIR="$REPO_ROOT/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date -u +'%Y-%m-%dT%H-%M-%SZ')"
if [[ -n "$LABEL" ]]; then
  # Only [A-Za-z0-9._-] in labels — keep the filename safe.
  SAFE_LABEL="$(echo "$LABEL" | tr -c 'A-Za-z0-9._-' '_' | sed 's/_\+/_/g; s/^_//; s/_$//')"
  FILENAME="${FA_DB_NAME}_${SAFE_LABEL}_${TIMESTAMP}.dump"
else
  FILENAME="${FA_DB_NAME}_${TIMESTAMP}.dump"
fi
DUMP_PATH="$BACKUP_DIR/$FILENAME"
META_PATH="$DUMP_PATH.meta"

echo "================================================================"
echo "family-assistant DB BACKUP"
echo "================================================================"
echo "  source DB     : $FA_DB_NAME"
echo "  host:port     : $FA_DB_HOST:$FA_DB_PORT"
echo "  user          : $FA_DB_USER"
echo "  output file   : $DUMP_PATH"
echo "  format        : custom (-Fc) + gzip (level 6)"
echo "================================================================"

# ---------------------------------------------------------------------------
# Run pg_dump
# ---------------------------------------------------------------------------

# PGPASSWORD is the standard non-interactive password channel for libpq;
# scoped to this single command, never written to disk.
PGPASSWORD="$FA_DB_PWD" pg_dump \
  --host="$FA_DB_HOST" \
  --port="$FA_DB_PORT" \
  --username="$FA_DB_USER" \
  --dbname="$FA_DB_NAME" \
  --format=custom \
  --compress=6 \
  --no-owner \
  --no-privileges \
  --file="$DUMP_PATH"

# ---------------------------------------------------------------------------
# Sidecar metadata (size + sha256 + summary) — used by db_restore.sh to
# sanity-check the file before destroying live data with it.
# ---------------------------------------------------------------------------

DUMP_BYTES=$(stat -f%z "$DUMP_PATH" 2>/dev/null || stat -c%s "$DUMP_PATH")
DUMP_SHA=$(shasum -a 256 "$DUMP_PATH" | awk '{print $1}')

# Quick row counts so the meta file is self-describing.
ROW_SUMMARY=$(PGPASSWORD="$FA_DB_PWD" psql \
  --host="$FA_DB_HOST" --port="$FA_DB_PORT" \
  --username="$FA_DB_USER" --dbname="$FA_DB_NAME" \
  --tuples-only --no-align --quiet \
  -c "SELECT
        (SELECT COUNT(*) FROM families)         AS families,
        (SELECT COUNT(*) FROM people)           AS people,
        (SELECT COUNT(*) FROM jobs)             AS jobs,
        (SELECT COUNT(*) FROM tasks)            AS tasks,
        (SELECT COUNT(*) FROM agent_tasks)      AS agent_tasks,
        (SELECT COUNT(*) FROM live_session_messages) AS live_session_messages
      ;" 2>/dev/null || echo "<row-summary unavailable>")

cat > "$META_PATH" <<META
backup_file: $FILENAME
created_at_utc: $TIMESTAMP
source_db_name: $FA_DB_NAME
source_host: $FA_DB_HOST:$FA_DB_PORT
source_user: $FA_DB_USER
size_bytes: $DUMP_BYTES
sha256: $DUMP_SHA
pg_dump_version: $(pg_dump --version)
row_counts (families|people|jobs|tasks|agent_tasks|live_session_messages):
  $ROW_SUMMARY
META

echo
echo "Backup complete."
echo "  size   : $DUMP_BYTES bytes"
echo "  sha256 : $DUMP_SHA"
echo "  meta   : $META_PATH"

# ---------------------------------------------------------------------------
# Optional retention
# ---------------------------------------------------------------------------

if [[ -n "$KEEP" ]]; then
  if ! [[ "$KEEP" =~ ^[0-9]+$ ]] || [[ "$KEEP" -lt 1 ]]; then
    echo "WARNING: --keep value '$KEEP' is not a positive integer; skipping prune." >&2
  else
    # Keep the newest N .dump files and their .meta sidecars; delete the rest.
    # `ls -t` orders by mtime descending; tail strips the keepers.
    OLD_DUMPS=$(ls -t "$BACKUP_DIR"/*.dump 2>/dev/null | tail -n +"$((KEEP + 1))")
    if [[ -n "$OLD_DUMPS" ]]; then
      echo
      echo "Pruning to newest $KEEP backup(s):"
      while IFS= read -r f; do
        echo "  rm $f"
        rm -f "$f" "$f.meta"
      done <<< "$OLD_DUMPS"
    fi
  fi
fi
