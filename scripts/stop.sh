#!/usr/bin/env bash
# ============================================================================
# scripts/stop.sh
# ----------------------------------------------------------------------------
# Stops the backend and/or frontend started by scripts/start.sh. Reads PIDs
# from .run/*.pid, sends SIGTERM (then SIGKILL if needed), and cleans up PID
# files. With --force, also sweeps anything still listening on our ports,
# which is useful when PID files are stale (e.g. after a reboot).
#
# Usage:
#   scripts/stop.sh               # stop both backend and frontend
#   scripts/stop.sh backend       # stop just the backend
#   scripts/stop.sh frontend      # stop just the frontend
#   scripts/stop.sh --force       # also kill any orphans on the service ports
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

FORCE=0
TARGETS=()
for arg in "$@"; do
    case "${arg}" in
        --force|-f) FORCE=1 ;;
        backend|frontend|all) TARGETS+=("${arg}") ;;
        -h|--help)
            sed -n '4,16p' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            exit 2
            ;;
    esac
done
[[ "${#TARGETS[@]}" -eq 0 ]] && TARGETS=("all")
should_stop() {
    local t="$1"
    for x in "${TARGETS[@]}"; do
        [[ "${x}" == "all" || "${x}" == "${t}" ]] && return 0
    done
    return 1
}

stop_service() {
    local label="$1"
    local pidfile="$2"
    local port="$3"

    log_step "Stopping ${label}"

    local pid
    pid="$(read_pid "${pidfile}")"
    if [[ -n "${pid}" ]]; then
        stop_pid "${pid}" "${label}" || true
        rm -f "${pidfile}"
    else
        log_info "No tracked PID for ${label} (pidfile missing or stale)."
    fi

    if [[ "${FORCE}" -eq 1 ]]; then
        local orphans
        orphans="$(pids_on_port "${port}")"
        if [[ -n "${orphans}" ]]; then
            log_warn "Still listening on :${port} — cleaning up orphaned processes."
            stop_by_port "${port}" "orphaned ${label}"
        fi
    elif port_in_use "${port}"; then
        log_warn "Something is still listening on :${port}. Re-run with --force to sweep it."
        log_info "   lsof -nP -iTCP:${port} -sTCP:LISTEN"
    fi

    log_success "${label} is stopped"
}

log_step "family-assistant · stop"
log_info "Project root: ${PROJECT_ROOT}"

if should_stop backend;  then stop_service "backend"  "${BACKEND_PID_FILE}"  "${BACKEND_PORT}";  fi
if should_stop frontend; then stop_service "frontend" "${FRONTEND_PID_FILE}" "${FRONTEND_PORT}"; fi

echo ""
log_success "Done."
