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

# Service configuration.
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

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
