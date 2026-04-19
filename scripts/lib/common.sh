#!/usr/bin/env bash
# ============================================================================
# scripts/lib/common.sh
# ----------------------------------------------------------------------------
# Shared helpers sourced by start.sh / stop.sh / restart.sh / deploy.sh.
#
# Design goals:
#   * Single-purpose helpers — each one does exactly one thing, prints a
#     clear log line, and returns a usable exit code.
#   * No surprise mutations — the scripts only touch files under .run/ and
#     logs/ inside the project root.
#   * Cross-invocation friendly — helpers use absolute paths derived from
#     PROJECT_ROOT so they can be called from any cwd.
# ============================================================================

set -euo pipefail

# Resolve the project root as the directory that contains the scripts/ dir.
# Using BASH_SOURCE so this works whether the file is sourced or executed.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${_LIB_DIR}/../.." && pwd)"

# Paths used everywhere.
RUN_DIR="${PROJECT_ROOT}/.run"
LOG_DIR="${PROJECT_ROOT}/logs"
BACKEND_PID_FILE="${RUN_DIR}/backend.pid"
FRONTEND_PID_FILE="${RUN_DIR}/frontend.pid"
BACKEND_LOG="${LOG_DIR}/backend.log"
FRONTEND_LOG="${LOG_DIR}/frontend.log"

# Service configuration. All ports are overridable from the environment so
# the same scripts work in Docker / CI where ports are remapped.
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
NGROK_AGENT_PORT="${NGROK_AGENT_PORT:-4040}"

# Extra log files for the daemons that scripts/start.sh doesn't manage.
NGROK_LOG="${LOG_DIR}/ngrok.log"
OLLAMA_LOG="${LOG_DIR}/ollama.log"

# macOS LaunchAgent layout.
LAUNCHAGENTS_DIR="${HOME}/Library/LaunchAgents"
LAUNCHAGENT_PREFIX="com.familyassistant"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

# ---------- pretty logging --------------------------------------------------
# Most terminals on macOS support ANSI; fall back to plain text if stdout
# isn't a tty (e.g. when piped to a file).
if [[ -t 1 ]]; then
    C_RESET="\033[0m"
    C_BOLD="\033[1m"
    C_DIM="\033[2m"
    C_GREEN="\033[32m"
    C_YELLOW="\033[33m"
    C_RED="\033[31m"
    C_BLUE="\033[34m"
    C_CYAN="\033[36m"
else
    C_RESET=""; C_BOLD=""; C_DIM=""
    C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_CYAN=""
fi

_timestamp() { date +"%H:%M:%S"; }

log_step()    { printf "%b[%s]%b %b%s%b\n"   "${C_DIM}"  "$(_timestamp)" "${C_RESET}" "${C_BOLD}${C_CYAN}"   "$*" "${C_RESET}"; }
log_info()    { printf "%b[%s]%b %s\n"       "${C_DIM}"  "$(_timestamp)" "${C_RESET}" "$*"; }
log_success() { printf "%b[%s]%b %b✔ %s%b\n" "${C_DIM}"  "$(_timestamp)" "${C_RESET}" "${C_GREEN}" "$*" "${C_RESET}"; }
log_warn()    { printf "%b[%s]%b %b! %s%b\n" "${C_DIM}"  "$(_timestamp)" "${C_RESET}" "${C_YELLOW}" "$*" "${C_RESET}"; }
log_error()   { printf "%b[%s]%b %b✖ %s%b\n" "${C_DIM}"  "$(_timestamp)" "${C_RESET}" "${C_RED}"    "$*" "${C_RESET}" 1>&2; }

# ---------- process helpers -------------------------------------------------
# is_pid_alive <pid> — true iff the PID is a running process.
is_pid_alive() {
    local pid="${1:-}"
    [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

# read_pid <pidfile> — echoes the PID from the file if it exists and is
# still alive, otherwise echoes empty string (and cleans up the stale file).
read_pid() {
    local pidfile="$1"
    if [[ -f "${pidfile}" ]]; then
        local pid
        pid="$(cat "${pidfile}" 2>/dev/null || true)"
        if [[ -n "${pid}" ]] && is_pid_alive "${pid}"; then
            echo "${pid}"
            return 0
        fi
        rm -f "${pidfile}"
    fi
    echo ""
}

# pids_on_port <port> — prints the PIDs listening on the given TCP port.
pids_on_port() {
    local port="$1"
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | sort -u || true
}

# port_in_use <port> — exit 0 if something is listening, non-zero otherwise.
port_in_use() {
    [[ -n "$(pids_on_port "$1")" ]]
}

# stop_pid <pid> <label> — send SIGTERM, wait up to 8 s, SIGKILL if needed.
stop_pid() {
    local pid="$1"
    local label="${2:-process}"
    if ! is_pid_alive "${pid}"; then return 0; fi
    log_info "Stopping ${label} (PID ${pid})…"
    kill "${pid}" 2>/dev/null || true
    local i=0
    while is_pid_alive "${pid}" && [[ $i -lt 40 ]]; do
        sleep 0.2
        i=$((i + 1))
    done
    if is_pid_alive "${pid}"; then
        log_warn "${label} didn't exit after SIGTERM, sending SIGKILL…"
        kill -9 "${pid}" 2>/dev/null || true
        sleep 0.3
    fi
    if is_pid_alive "${pid}"; then
        log_error "${label} (PID ${pid}) is still running — something's wrong"
        return 1
    fi
    log_success "${label} stopped"
}

# stop_by_port <port> <label> — kill anything listening on that port.
# Useful for cleaning up orphaned servers from previous sessions.
stop_by_port() {
    local port="$1"
    local label="${2:-process}"
    local pids
    pids="$(pids_on_port "${port}")"
    if [[ -z "${pids}" ]]; then return 0; fi
    while IFS= read -r pid; do
        [[ -n "${pid}" ]] && stop_pid "${pid}" "${label} on :${port}"
    done <<<"${pids}"
}

# ---------- http probes -----------------------------------------------------
# wait_http <url> <timeout-seconds> — poll until HTTP 200 or timeout.
wait_http() {
    local url="$1"
    local timeout="${2:-30}"
    local elapsed=0
    while (( elapsed < timeout )); do
        # -f: fail on 4xx/5xx; -s: silent body; redirect stderr so we don't
        # print "Couldn't connect" on the expected retries before boot.
        if curl -fsS -o /dev/null -m 2 "${url}" 2>/dev/null; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

# ---------- environment checks ----------------------------------------------
require_cmd() {
    local cmd="$1"
    local hint="${2:-}"
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        log_error "Required command '${cmd}' not found in PATH."
        [[ -n "${hint}" ]] && log_error "  Hint: ${hint}"
        return 1
    fi
}

# resolve_cmd <cmd> — echoes the absolute path of <cmd> if available.
# Used to bake real paths into LaunchAgent plists, since launchd runs
# with a minimal PATH and resolving via "command -v" at boot is unsafe.
resolve_cmd() {
    command -v "$1" 2>/dev/null || true
}

# ---------- third-party daemon helpers --------------------------------------
# Each daemon helper exposes two functions:
#   <name>_is_running   → exit 0 iff the daemon is reachable RIGHT NOW.
#   <name>_start        → bring it up, prefer Homebrew services when present.
#
# Helpers MUST be idempotent: calling _start while it's already running
# should be a no-op that prints a friendly note.

# ---- Postgres ---
postgres_is_running() {
    # ``pg_isready`` is the canonical Postgres health probe — it tries a
    # real socket connection but doesn't authenticate, so it works even
    # before our app user/database exist.
    if command -v pg_isready >/dev/null 2>&1; then
        pg_isready -h localhost -p "${POSTGRES_PORT}" -q
    else
        # Fallback: just check that something is listening on the port.
        port_in_use "${POSTGRES_PORT}"
    fi
}

# Returns the brew formula name for the user's installed Postgres
# (postgresql@16, postgresql@15, …). Empty if Postgres isn't a brew formula.
postgres_brew_formula() {
    if ! command -v brew >/dev/null 2>&1; then echo ""; return; fi
    brew services list 2>/dev/null | awk 'NR>1 && $1 ~ /^postgresql/ {print $1; exit}'
}

postgres_start() {
    if postgres_is_running; then
        log_success "Postgres is already running on :${POSTGRES_PORT}"
        return 0
    fi
    local formula
    formula="$(postgres_brew_formula)"
    if [[ -n "${formula}" ]]; then
        log_info "Starting Postgres via 'brew services start ${formula}'…"
        brew services start "${formula}" >/dev/null
    else
        log_warn "Postgres isn't installed via Homebrew."
        log_warn "  Start it manually (e.g. 'pg_ctl -D /usr/local/var/postgres start')"
        log_warn "  or install via 'brew install postgresql@16'."
        return 1
    fi
    # pg_isready can be racy on first boot — retry for a few seconds.
    local i=0
    while ! postgres_is_running && [[ $i -lt 15 ]]; do
        sleep 0.5
        i=$((i + 1))
    done
    if postgres_is_running; then
        log_success "Postgres is up on :${POSTGRES_PORT}"
        return 0
    fi
    log_error "Postgres did not come up within 8s"
    return 1
}

# ---- Ollama ---
ollama_is_running() {
    curl -fsS -m 1 "http://localhost:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1
}

# Echoes "brew" if Ollama is registered as a brew service, "app" if the
# Ollama.app menu-bar daemon is installed under /Applications, or "" if
# neither — we'll fall back to a raw ``ollama serve`` in that case.
ollama_install_kind() {
    if command -v brew >/dev/null 2>&1 && \
        brew services list 2>/dev/null | grep -q '^ollama'; then
        echo "brew"
    elif [[ -d "/Applications/Ollama.app" ]]; then
        echo "app"
    else
        echo ""
    fi
}

ollama_start() {
    if ollama_is_running; then
        log_success "Ollama daemon is already up on :${OLLAMA_PORT}"
        return 0
    fi
    local kind
    kind="$(ollama_install_kind)"
    case "${kind}" in
        brew)
            log_info "Starting Ollama via 'brew services start ollama'…"
            brew services start ollama >/dev/null
            ;;
        app)
            log_info "Launching the Ollama menu-bar app…"
            open -ga "Ollama"
            ;;
        *)
            if ! command -v ollama >/dev/null 2>&1; then
                log_error "ollama is not installed. Get it from https://ollama.com or 'brew install ollama'."
                return 1
            fi
            log_info "Starting 'ollama serve' in the background → ${OLLAMA_LOG}"
            nohup ollama serve >"${OLLAMA_LOG}" 2>&1 &
            ;;
    esac
    local i=0
    while ! ollama_is_running && [[ $i -lt 30 ]]; do
        sleep 0.5
        i=$((i + 1))
    done
    if ollama_is_running; then
        log_success "Ollama daemon is up on :${OLLAMA_PORT}"
        return 0
    fi
    log_error "Ollama did not come up within 15s — see ${OLLAMA_LOG}"
    return 1
}

# ---- ngrok ---
ngrok_is_running() {
    curl -fsS -m 1 "http://localhost:${NGROK_AGENT_PORT}/api/tunnels" >/dev/null 2>&1
}

# Pull the configured public hostname from .env (NGROK_DOMAIN). Strips a
# protocol or trailing slash if the user accidentally pasted a full URL.
ngrok_domain() {
    local raw="${NGROK_DOMAIN:-}"
    if [[ -z "${raw}" && -f "${PROJECT_ROOT}/.env" ]]; then
        raw="$(grep -E '^NGROK_DOMAIN=' "${PROJECT_ROOT}/.env" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
    fi
    raw="${raw#http://}"
    raw="${raw#https://}"
    raw="${raw%/}"
    echo "${raw}"
}

ngrok_start() {
    if ngrok_is_running; then
        log_success "ngrok agent is already up on :${NGROK_AGENT_PORT}"
        return 0
    fi
    if ! command -v ngrok >/dev/null 2>&1; then
        log_error "ngrok is not installed. 'brew install ngrok' (then 'ngrok config add-authtoken …')."
        return 1
    fi
    local domain
    domain="$(ngrok_domain)"
    if [[ -z "${domain}" ]]; then
        log_error "NGROK_DOMAIN isn't set in .env — can't start the tunnel deterministically."
        return 1
    fi
    log_info "Starting ngrok tunnel ${domain} → :${BACKEND_PORT} (log: ${NGROK_LOG})"
    nohup ngrok http "--url=${domain}" "${BACKEND_PORT}" --log=stdout \
        >"${NGROK_LOG}" 2>&1 &
    local i=0
    while ! ngrok_is_running && [[ $i -lt 20 ]]; do
        sleep 0.25
        i=$((i + 1))
    done
    if ngrok_is_running; then
        log_success "ngrok agent is up; public URL: https://${domain}"
        return 0
    fi
    log_error "ngrok did not come up within 5s — see ${NGROK_LOG}"
    return 1
}

# ---------- LaunchAgent helpers ---------------------------------------------
# All plists this repo manages live under ~/Library/LaunchAgents and are
# named "${LAUNCHAGENT_PREFIX}.<short>.plist". Helpers favour the modern
# launchctl bootstrap/bootout API and fall back to load/unload on older
# macOS where bootstrap is rejected.

# launchagent_path <short> — echoes the absolute plist path for the agent.
launchagent_path() {
    echo "${LAUNCHAGENTS_DIR}/${LAUNCHAGENT_PREFIX}.$1.plist"
}

# launchagent_label <short> — echoes the launchd label string.
launchagent_label() {
    echo "${LAUNCHAGENT_PREFIX}.$1"
}

# launchagent_install <short> <plist-content>
#   Writes the plist to ~/Library/LaunchAgents and (re-)bootstraps it.
#   Replaces any existing agent with the same label (so re-running the
#   register script is safe).
launchagent_install() {
    local short="$1"
    local content="$2"
    local path
    path="$(launchagent_path "${short}")"
    local label
    label="$(launchagent_label "${short}")"

    mkdir -p "${LAUNCHAGENTS_DIR}"
    if [[ -f "${path}" ]]; then
        log_info "Replacing existing agent ${label}"
        launchagent_unload "${short}" || true
    fi
    printf "%s" "${content}" >"${path}"
    chmod 0644 "${path}"
    log_info "Wrote ${path}"

    # gui/<UID> is the per-user launchd domain — what `launchctl load`
    # used to target by default. ``bootstrap`` is the supported path on
    # macOS 11+; if that errors (older OS, SIP edge case) we retry with
    # the legacy ``load`` so the script still works.
    if launchctl bootstrap "gui/$(id -u)" "${path}" 2>/dev/null; then
        :
    else
        launchctl load -w "${path}"
    fi
    launchctl enable "gui/$(id -u)/${label}" 2>/dev/null || true
    log_success "Installed LaunchAgent ${label}"
}

# launchagent_unload <short> — best-effort unload + remove of the plist.
launchagent_unload() {
    local short="$1"
    local path
    path="$(launchagent_path "${short}")"
    local label
    label="$(launchagent_label "${short}")"
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null \
        || launchctl unload "${path}" 2>/dev/null \
        || true
    if [[ -f "${path}" ]]; then
        rm -f "${path}"
        log_success "Removed LaunchAgent ${label}"
    fi
}

# launchagent_status <short> — prints "loaded" / "missing" for the agent.
launchagent_status() {
    local label
    label="$(launchagent_label "$1")"
    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
        echo "loaded"
    else
        echo "missing"
    fi
}

# launchagent_is_loaded <short> — exit 0 iff launchd currently knows about
# the agent in the user's gui domain. Used by restart.sh to decide whether
# the right move is `launchctl kickstart -k` (which lets launchd own the
# kill→relaunch) or our manual stop→start pair (when nothing is managing
# the service for us).
launchagent_is_loaded() {
    local label
    label="$(launchagent_label "$1")"
    launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1
}

# launchagent_kickstart <short> — atomic restart of a loaded agent.
#
# launchctl's `kickstart -k <service>` tells launchd to terminate the
# current instance AND start a new one in a single operation it owns
# end to end. That closes the race window the old "kill the PID then
# rerun start.sh" path opened: with KeepAlive=true the agent would be
# resurrected by launchd in the gap, our follow-up bind would fail
# with "Address already in use", and we'd think the restart broke.
launchagent_kickstart() {
    local short="$1"
    local label
    label="$(launchagent_label "${short}")"
    if ! launchagent_is_loaded "${short}"; then
        log_error "LaunchAgent ${label} is not loaded — can't kickstart."
        return 1
    fi
    log_info "Kickstarting ${label} (launchctl kickstart -k)…"
    if ! launchctl kickstart -k "gui/$(id -u)/${label}"; then
        log_error "launchctl kickstart failed for ${label}"
        return 1
    fi
    return 0
}
