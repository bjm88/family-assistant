#!/usr/bin/env bash
# ============================================================================
# scripts/ensure_services.sh
# ----------------------------------------------------------------------------
# Idempotent "make sure everything Avi needs is up" script.
#
# Walks the dependency stack from the bottom up:
#
#     Postgres → Ollama → ngrok → backend (FastAPI) → frontend (Vite)
#
# For each service it first probes whether the daemon is reachable, and
# only attempts a start when it isn't. Safe to run from a launchd agent,
# from cron, from a login item, or just by hand at the top of the day.
#
# Usage:
#   scripts/ensure_services.sh           # check + start everything
#   scripts/ensure_services.sh --check   # probe only, never start
#   scripts/ensure_services.sh -h        # this message
#
# Exit codes:
#   0  All requested services are healthy at the end of the run.
#   1  At least one service could not be started.
#   2  Bad CLI args.
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

CHECK_ONLY=0
for arg in "$@"; do
    case "${arg}" in
        --check|-c) CHECK_ONLY=1 ;;
        -h|--help)
            sed -n '4,22p' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            exit 2
            ;;
    esac
done

# Track failures without aborting the whole script — we want to attempt
# every service even if one is broken, so the operator sees the full
# picture in a single run.
FAILURES=0

# Run "<name>_is_running" / "<name>_start" with the right log lines.
ensure_daemon() {
    local label="$1"
    local probe="$2"
    local starter="$3"
    log_step "${label}"
    if ${probe}; then
        log_success "${label} is already running"
        return 0
    fi
    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        log_warn "${label} is NOT running (--check, won't start)"
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    if ${starter}; then
        return 0
    else
        FAILURES=$((FAILURES + 1))
        return 1
    fi
}

# Backend / frontend share the existing start.sh logic so we don't duplicate
# port-collision handling, log routing, or PID bookkeeping.
ensure_backend_frontend() {
    log_step "Backend + frontend"
    local backend_pid frontend_pid
    backend_pid="$(read_pid "${BACKEND_PID_FILE}")"
    frontend_pid="$(read_pid "${FRONTEND_PID_FILE}")"

    if [[ -n "${backend_pid}" ]] && [[ -n "${frontend_pid}" ]]; then
        log_success "Backend (PID ${backend_pid}) and frontend (PID ${frontend_pid}) already running"
        return 0
    fi

    if [[ "${CHECK_ONLY}" -eq 1 ]]; then
        [[ -z "${backend_pid}" ]]  && { log_warn "Backend not running";  FAILURES=$((FAILURES + 1)); }
        [[ -z "${frontend_pid}" ]] && { log_warn "Frontend not running"; FAILURES=$((FAILURES + 1)); }
        return 1
    fi

    # Delegate to the existing start.sh. ``--force`` cleans up any orphan
    # listening on our ports (common after a hard reboot when the PID
    # file is stale but the process is gone).
    if "${_SELF_DIR}/start.sh" --force all; then
        return 0
    else
        FAILURES=$((FAILURES + 1))
        return 1
    fi
}

log_step "family-assistant · ensure (mode: $([[ ${CHECK_ONLY} -eq 1 ]] && echo check || echo start))"
log_info "Project root: ${PROJECT_ROOT}"

ensure_daemon "Postgres"      postgres_is_running postgres_start || true
ensure_daemon "Ollama daemon" ollama_is_running   ollama_start   || true
ensure_daemon "ngrok tunnel"  ngrok_is_running    ngrok_start    || true
ensure_backend_frontend                                              || true

echo ""
if [[ "${FAILURES}" -eq 0 ]]; then
    log_success "All services are up."
    log_info "  Status page: http://localhost:${FRONTEND_PORT}/admin/families"
    log_info "  Live probe : curl -s http://localhost:${BACKEND_PORT}/api/admin/status | jq"
    exit 0
fi

log_error "${FAILURES} service(s) had problems — see messages above."
exit 1
