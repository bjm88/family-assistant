#!/usr/bin/env bash
# ============================================================================
# scripts/register_daemons.sh
# ----------------------------------------------------------------------------
# Installs macOS LaunchAgents so the family-assistant stack restarts itself
# automatically whenever this user logs in (i.e. after every reboot).
#
# What gets registered:
#
#   * Postgres        — via ``brew services start <formula>``. Brew already
#                        publishes a working LaunchAgent for Postgres; we just
#                        toggle it on, which is the supported path on macOS.
#   * Ollama          — via ``brew services start ollama`` if installed via
#                        Homebrew, otherwise we point you at Ollama.app's
#                        "Launch on login" toggle (the GUI app handles its
#                        own launch agent and we shouldn't fight it).
#   * ngrok           — our own LaunchAgent at
#                        ~/Library/LaunchAgents/com.familyassistant.ngrok.plist
#   * FastAPI backend — our own LaunchAgent (com.familyassistant.backend.plist)
#   * Vite frontend   — our own LaunchAgent (com.familyassistant.frontend.plist)
#   * DB backup       — our own scheduled LaunchAgent
#                        (com.familyassistant.dbbackup.plist) that runs
#                        scripts/db_backup_to_gdrive.py every Sunday at 23:55
#                        local time. Unlike the daemons above this one is
#                        single-shot (StartCalendarInterval, no KeepAlive).
#
# The always-on daemons above use ``RunAtLoad=true`` + ``KeepAlive=true`` so a
# crash is resurrected by launchd within a few seconds. The DB-backup agent
# uses ``RunAtLoad=false`` + ``StartCalendarInterval`` instead so it fires
# exactly once on its weekly schedule and then exits.
#
# Re-running this script is safe: existing agents are torn down and rebuilt
# with the latest paths/env vars.
#
# Usage:
#   scripts/register_daemons.sh
#   scripts/register_daemons.sh --status    # just show what's installed
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

STATUS_ONLY=0
for arg in "$@"; do
    case "${arg}" in
        --status|-s) STATUS_ONLY=1 ;;
        -h|--help)
            sed -n '4,30p' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            exit 2
            ;;
    esac
done

# launchd executes plists with a near-empty environment, so we must bake
# absolute binary paths and the user's home dir into each plist. Resolve
# everything once at install time.
USER_HOME="${HOME}"
USER_NAME="$(id -un)"
UV_BIN="$(resolve_cmd uv)"
NGROK_BIN="$(resolve_cmd ngrok)"
NPM_BIN="$(resolve_cmd npm)"

# launchd inherits PATH=/usr/bin:/bin:/usr/sbin:/sbin by default, which
# is too small for a Homebrew install. Build a sane PATH that covers
# both Apple Silicon (/opt/homebrew) and Intel (/usr/local) Homebrew,
# plus uv's user-local bin dir.
DAEMON_PATH="/opt/homebrew/bin:/usr/local/bin:${USER_HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

require_paths() {
    local missing=0
    [[ -z "${UV_BIN}" ]]    && { log_error "uv not found in PATH (install: curl -LsSf https://astral.sh/uv/install.sh | sh)"; missing=1; }
    [[ -z "${NGROK_BIN}" ]] && { log_error "ngrok not found in PATH (install: brew install ngrok)"; missing=1; }
    [[ -z "${NPM_BIN}" ]]   && { log_error "npm not found in PATH (install: brew install node)"; missing=1; }
    [[ "${missing}" -eq 1 ]] && return 1
    return 0
}

# ----------------------------------------------------------------------------
# Plist generation
# ----------------------------------------------------------------------------

# Common preamble + closing for our plists. We write them with printf
# so heredocs don't get caught by --enable-history-expansion shells.
plist_open() {
    local label="$1"
    cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
XML
}

plist_close() {
    local stdout_log="$1"
    cat <<XML
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
    <key>Crashed</key>
    <true/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${stdout_log}</string>
  <key>StandardErrorPath</key>
  <string>${stdout_log}</string>
</dict>
</plist>
XML
}

# plist_close_scheduled — variant of plist_close for cron-like jobs.
#
# Long-running daemons (backend, ngrok, frontend) want RunAtLoad=true +
# KeepAlive so launchd resurrects them on crash. A weekly backup is the
# opposite: it should fire ONCE on a fixed schedule and exit cleanly.
# RunAtLoad=true would re-fire every login; KeepAlive would relaunch
# the script in a tight loop after each successful exit. So we emit
# StartCalendarInterval (Weekday/Hour/Minute) + RunAtLoad=false and
# omit KeepAlive entirely.
#
# Args:
#   1: stdout/stderr log path
#   2: weekday (0 = Sunday, 1 = Monday, …, 7 = Sunday — launchd
#      historically accepted both 0 and 7 for Sunday; we use 0)
#   3: hour (0-23, local time)
#   4: minute (0-59)
plist_close_scheduled() {
    local stdout_log="$1"
    local weekday="$2"
    local hour="$3"
    local minute="$4"
    cat <<XML
  <key>RunAtLoad</key>
  <false/>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>${weekday}</integer>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>${minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${stdout_log}</string>
  <key>StandardErrorPath</key>
  <string>${stdout_log}</string>
</dict>
</plist>
XML
}

build_ngrok_plist() {
    local label="$1"
    local domain="$2"
    {
        plist_open "${label}"
        cat <<XML
  <key>ProgramArguments</key>
  <array>
    <string>${NGROK_BIN}</string>
    <string>http</string>
    <string>--log=stdout</string>
    <string>--url=${domain}</string>
    <string>${BACKEND_PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${USER_HOME}</string>
    <key>USER</key>
    <string>${USER_NAME}</string>
    <key>PATH</key>
    <string>${DAEMON_PATH}</string>
  </dict>
XML
        plist_close "${NGROK_LOG}"
    }
}

build_backend_plist() {
    local label="$1"
    {
        plist_open "${label}"
        cat <<XML
  <key>ProgramArguments</key>
  <array>
    <string>${UV_BIN}</string>
    <string>run</string>
    <string>uvicorn</string>
    <string>api.main:app</string>
    <string>--app-dir</string>
    <string>python</string>
    <string>--port</string>
    <string>${BACKEND_PORT}</string>
    <string>--host</string>
    <string>0.0.0.0</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${USER_HOME}</string>
    <key>USER</key>
    <string>${USER_NAME}</string>
    <key>PATH</key>
    <string>${DAEMON_PATH}</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
XML
        plist_close "${BACKEND_LOG}"
    }
}

build_dbbackup_plist() {
    # Weekly DB backup → Google Drive uploader. Fires every Sunday at
    # 23:55 local time (the user's family is east coast — adjust the
    # Hour/Minute args here if you ever need a different cadence).
    # The Python script handles its own success/failure logging; the
    # LaunchAgent just supplies a working dir + PATH so ``uv`` and
    # ``pg_dump`` resolve.
    local label="$1"
    {
        plist_open "${label}"
        cat <<XML
  <key>ProgramArguments</key>
  <array>
    <string>${UV_BIN}</string>
    <string>run</string>
    <string>python</string>
    <string>scripts/db_backup_to_gdrive.py</string>
    <string>--keep</string>
    <string>8</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${USER_HOME}</string>
    <key>USER</key>
    <string>${USER_NAME}</string>
    <key>PATH</key>
    <string>${DAEMON_PATH}</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
XML
        # Sunday (0) at 23:55 — the latest reasonable slot before the
        # week rolls over so the dump captures everything that
        # happened during the week.
        plist_close_scheduled "${DB_BACKUP_LOG}" 0 23 55
    }
}

build_frontend_plist() {
    local label="$1"
    {
        plist_open "${label}"
        cat <<XML
  <key>ProgramArguments</key>
  <array>
    <string>${NPM_BIN}</string>
    <string>run</string>
    <string>dev</string>
    <string>--</string>
    <string>--port</string>
    <string>${FRONTEND_PORT}</string>
    <string>--host</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_ROOT}/ui/react</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${USER_HOME}</string>
    <key>USER</key>
    <string>${USER_NAME}</string>
    <key>PATH</key>
    <string>${DAEMON_PATH}</string>
  </dict>
XML
        plist_close "${FRONTEND_LOG}"
    }
}

# ----------------------------------------------------------------------------
# --status mode: just show what's installed and exit.
# ----------------------------------------------------------------------------
print_status() {
    log_step "LaunchAgent registration status"
    printf "  %-12s %-10s %s\n" "service" "agent" "plist"
    for short in ngrok backend frontend dbbackup; do
        local state path
        state="$(launchagent_status "${short}")"
        path="$(launchagent_path "${short}")"
        printf "  %-12s %-10s %s\n" "${short}" "${state}" "${path}"
    done
    # Postgres + Ollama are managed by Homebrew, not us — surface that.
    if command -v brew >/dev/null 2>&1; then
        log_info "Brew services (managed externally):"
        brew services list 2>/dev/null \
            | awk 'NR==1 || $1 ~ /^(postgresql|ollama)/'
    fi
}

if [[ "${STATUS_ONLY}" -eq 1 ]]; then
    print_status
    exit 0
fi

# ----------------------------------------------------------------------------
# Main install path
# ----------------------------------------------------------------------------
log_step "family-assistant · register macOS LaunchAgents"
log_info "Project root: ${PROJECT_ROOT}"
log_info "Plists go to: ${LAUNCHAGENTS_DIR}"

require_paths
log_info "  uv    → ${UV_BIN}"
log_info "  ngrok → ${NGROK_BIN}"
log_info "  npm   → ${NPM_BIN}"

# ---- Postgres (via brew services) -----------------------------------------
log_step "Postgres (Homebrew service)"
if command -v brew >/dev/null 2>&1; then
    formula="$(postgres_brew_formula)"
    if [[ -z "${formula}" ]]; then
        # Try a sensible default — the user can override via PG_FORMULA env
        # if they keep multiple major versions installed.
        formula="${PG_FORMULA:-postgresql@16}"
        log_warn "No active postgresql brew service detected; trying ${formula}"
    fi
    if brew services start "${formula}" >/dev/null; then
        log_success "Brew service '${formula}' is now registered for autostart"
    else
        log_warn "brew services start ${formula} failed — install Postgres first ('brew install ${formula}')"
    fi
else
    log_warn "Homebrew not installed — skipping Postgres autostart."
    log_warn "  Install Homebrew (https://brew.sh) and re-run, or configure Postgres manually."
fi

# ---- Ollama --------------------------------------------------------------
# Homebrew writes its default plist for ``ollama`` without the env vars
# that matter for our concurrent-request workload. We patch them into
# the plist post-install so a ``brew upgrade ollama`` that regenerates
# the file also gets re-tuned the next time this script runs. Keys
# tuned:
#   * OLLAMA_NUM_PARALLEL=2 — lets two inbound inboxes (Telegram +
#     WhatsApp, say) run the heavy model concurrently instead of
#     serialising through a single slot. Without this the second
#     request queues behind the first for its entire prefill, and the
#     KV cache thrashes between the two.
#   * OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0 — already
#     set on this machine's plist; ensure they survive a reinstall.
# Uses /usr/libexec/PlistBuddy so we edit the XML correctly. The Set-
# or-Add idiom is idempotent: a key that already matches is a no-op.
tune_ollama_plist_env() {
    local plist="$1"
    local key="$2"
    local value="$3"
    if /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables" "$plist" \
        >/dev/null 2>&1; then
        :
    else
        /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$plist" \
            >/dev/null 2>&1 || true
    fi
    if /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:${key}" \
        "$plist" >/dev/null 2>&1; then
        /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:${key} ${value}" \
            "$plist" >/dev/null 2>&1
    else
        /usr/libexec/PlistBuddy -c \
            "Add :EnvironmentVariables:${key} string ${value}" \
            "$plist" >/dev/null 2>&1
    fi
}

log_step "Ollama"
case "$(ollama_install_kind)" in
    brew)
        if brew services start ollama >/dev/null; then
            log_success "Brew service 'ollama' is now registered for autostart"
        else
            log_warn "'brew services start ollama' failed — try 'brew install ollama' first"
        fi
        OLLAMA_PLIST="${HOME}/Library/LaunchAgents/homebrew.mxcl.ollama.plist"
        if [[ -f "${OLLAMA_PLIST}" ]]; then
            tune_ollama_plist_env "${OLLAMA_PLIST}" OLLAMA_NUM_PARALLEL 2
            tune_ollama_plist_env "${OLLAMA_PLIST}" OLLAMA_FLASH_ATTENTION 1
            tune_ollama_plist_env "${OLLAMA_PLIST}" OLLAMA_KV_CACHE_TYPE q8_0
            log_info "  Env     : OLLAMA_NUM_PARALLEL=2, OLLAMA_FLASH_ATTENTION=1, OLLAMA_KV_CACHE_TYPE=q8_0"
            # Restart so the new env takes effect immediately. Without
            # this the next inbound message would still hit the old
            # single-slot daemon until the user reboots.
            if brew services restart ollama >/dev/null; then
                log_success "Restarted Ollama with the tuned environment"
            else
                log_warn "Could not restart Ollama — run 'brew services restart ollama' manually to apply the new env"
            fi
        else
            log_warn "Ollama plist ${OLLAMA_PLIST} missing — skipping env tune"
        fi
        ;;
    app)
        log_info "Ollama.app is installed under /Applications."
        log_info "  Open Ollama → Settings and enable 'Launch Ollama on login'."
        log_info "  (The app manages its own launch agent; we don't override it.)"
        log_info "  For concurrent requests set OLLAMA_NUM_PARALLEL=2 in the app's Advanced settings."
        ;;
    "")
        log_warn "Ollama isn't installed via Homebrew or Ollama.app."
        log_warn "  Install with 'brew install ollama' and re-run this script."
        ;;
esac

# ---- ngrok (our LaunchAgent) ---------------------------------------------
log_step "ngrok (custom LaunchAgent)"
NGROK_DOMAIN_RESOLVED="$(ngrok_domain)"
if [[ -z "${NGROK_DOMAIN_RESOLVED}" ]]; then
    log_warn "NGROK_DOMAIN isn't set in .env — skipping ngrok agent."
    log_warn "  Add NGROK_DOMAIN=your-subdomain.ngrok.app to .env then re-run."
else
    launchagent_install "ngrok" \
        "$(build_ngrok_plist "$(launchagent_label ngrok)" "${NGROK_DOMAIN_RESOLVED}")"
    log_info "  Tunnel : https://${NGROK_DOMAIN_RESOLVED} → :${BACKEND_PORT}"
    log_info "  Logs   : ${NGROK_LOG}"
fi

# ---- Backend (our LaunchAgent) -------------------------------------------
log_step "FastAPI backend (custom LaunchAgent)"
launchagent_install "backend" \
    "$(build_backend_plist "$(launchagent_label backend)")"
log_info "  Listens : http://localhost:${BACKEND_PORT}"
log_info "  Logs    : ${BACKEND_LOG}"

# ---- Frontend (our LaunchAgent) ------------------------------------------
log_step "Vite frontend (custom LaunchAgent)"
if [[ ! -d "${PROJECT_ROOT}/ui/react/node_modules" ]]; then
    log_warn "ui/react/node_modules missing — run 'cd ui/react && npm install' first."
    log_warn "  (Skipping frontend agent so it doesn't crash-loop.)"
else
    launchagent_install "frontend" \
        "$(build_frontend_plist "$(launchagent_label frontend)")"
    log_info "  Listens : http://localhost:${FRONTEND_PORT}"
    log_info "  Logs    : ${FRONTEND_LOG}"
fi

# ---- Weekly DB backup → Google Drive (scheduled LaunchAgent) -------------
# Unlike the always-on daemons above, this one is launched once a week
# by launchd's StartCalendarInterval (Sun 23:55) and exits when done.
# We register it unconditionally; the script itself logs and exits 2
# if DB_BACKUP_GDRIVE isn't set in .env, so a missing folder URL is a
# soft fail rather than a registration-time error.
log_step "Weekly DB backup → Drive (scheduled LaunchAgent)"
launchagent_install "dbbackup" \
    "$(build_dbbackup_plist "$(launchagent_label dbbackup)")"
log_info "  Schedule : every Sunday at 23:55 local time"
log_info "  Script   : scripts/db_backup_to_gdrive.py --keep 8"
log_info "  Logs     : ${DB_BACKUP_LOG}"

echo ""
log_success "Done."
log_info "Verify everything came up:"
log_info "  scripts/register_daemons.sh --status"
log_info "  scripts/ensure_services.sh --check"
log_info "  curl -s http://localhost:${BACKEND_PORT}/api/admin/status | jq .overall"
log_info ""
log_info "To remove all of the above:  scripts/unregister_daemons.sh"
