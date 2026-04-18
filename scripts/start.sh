#!/usr/bin/env bash
# ============================================================================
# scripts/start.sh
# ----------------------------------------------------------------------------
# Starts the family-assistant backend (FastAPI) and frontend (Vite) as
# detached background processes, recording their PIDs under .run/ and their
# stdout/stderr under logs/.
#
# Usage:
#   scripts/start.sh              # start both backend and frontend
#   scripts/start.sh backend      # start just the backend
#   scripts/start.sh frontend     # start just the frontend
#   scripts/start.sh --force      # kill anything on our ports before starting
#
# Exits non-zero if a service fails its health probe within 30 seconds.
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
            sed -n '4,18p' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            exit 2
            ;;
    esac
done
[[ "${#TARGETS[@]}" -eq 0 ]] && TARGETS=("all")
should_start() {
    local t="$1"
    for x in "${TARGETS[@]}"; do
        [[ "${x}" == "all" || "${x}" == "${t}" ]] && return 0
    done
    return 1
}

# ---------- optional third-party daemons ------------------------------------
check_ollama() {
    if curl -fsS -m 1 "http://localhost:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
        log_success "Ollama is reachable at http://localhost:${OLLAMA_PORT}"
    else
        log_warn "Ollama is not responding on :${OLLAMA_PORT}. Start it with 'ollama serve' or the macOS app — chat will fall back to an offline state until it's up."
    fi
}

# ---------- backend ---------------------------------------------------------
start_backend() {
    log_step "Backend (FastAPI / uvicorn)"

    local existing_pid
    existing_pid="$(read_pid "${BACKEND_PID_FILE}")"
    if [[ -n "${existing_pid}" ]]; then
        log_warn "Backend already running (PID ${existing_pid}). Skipping. Use 'scripts/restart.sh' to restart."
        return 0
    fi

    if port_in_use "${BACKEND_PORT}"; then
        if [[ "${FORCE}" -eq 1 ]]; then
            log_warn "Port ${BACKEND_PORT} is in use — --force was passed, cleaning up orphans."
            stop_by_port "${BACKEND_PORT}" "orphaned backend"
        else
            log_error "Port ${BACKEND_PORT} is already in use. Pass --force to kill the orphan, or stop it manually."
            log_info "   lsof -nP -iTCP:${BACKEND_PORT} -sTCP:LISTEN"
            return 1
        fi
    fi

    require_cmd uv "Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"

    cd "${PROJECT_ROOT}"
    log_info "Launching uvicorn on :${BACKEND_PORT}, logging to ${BACKEND_LOG}"
    # Use setsid-equivalent (nohup + &) so the process survives shell exit.
    # `exec` in the subshell ensures the recorded PID is uvicorn itself,
    # not a bash wrapper that would exit after forking.
    nohup bash -c 'exec uv run uvicorn api.main:app --app-dir python --port '"${BACKEND_PORT}"' --host 0.0.0.0 --reload' \
        >"${BACKEND_LOG}" 2>&1 &
    local pid=$!
    echo "${pid}" >"${BACKEND_PID_FILE}"
    log_info "Backend PID: ${pid}"

    log_info "Waiting for /api/health (up to 30s)…"
    if wait_http "http://localhost:${BACKEND_PORT}/api/health" 30; then
        log_success "Backend is healthy at http://localhost:${BACKEND_PORT}"
    else
        log_error "Backend failed to respond on :${BACKEND_PORT} within 30s"
        log_error "Tail of ${BACKEND_LOG}:"
        tail -n 20 "${BACKEND_LOG}" 1>&2 || true
        return 1
    fi
}

# ---------- frontend --------------------------------------------------------
start_frontend() {
    log_step "Frontend (Vite dev server)"

    local existing_pid
    existing_pid="$(read_pid "${FRONTEND_PID_FILE}")"
    if [[ -n "${existing_pid}" ]]; then
        log_warn "Frontend already running (PID ${existing_pid}). Skipping."
        return 0
    fi

    if port_in_use "${FRONTEND_PORT}"; then
        if [[ "${FORCE}" -eq 1 ]]; then
            log_warn "Port ${FRONTEND_PORT} is in use — --force was passed, cleaning up orphans."
            stop_by_port "${FRONTEND_PORT}" "orphaned frontend"
        else
            log_error "Port ${FRONTEND_PORT} is already in use. Pass --force to kill the orphan."
            return 1
        fi
    fi

    if [[ ! -d "${PROJECT_ROOT}/ui/react/node_modules" ]]; then
        log_error "ui/react/node_modules missing — run 'scripts/deploy.sh' first to install dependencies."
        return 1
    fi

    cd "${PROJECT_ROOT}/ui/react"
    log_info "Launching 'npm run dev' on :${FRONTEND_PORT}, logging to ${FRONTEND_LOG}"
    nohup bash -c 'exec npm run dev -- --port '"${FRONTEND_PORT}"' --host' \
        >"${FRONTEND_LOG}" 2>&1 &
    local pid=$!
    echo "${pid}" >"${FRONTEND_PID_FILE}"
    log_info "Frontend PID: ${pid}"

    log_info "Waiting for Vite on :${FRONTEND_PORT} (up to 30s)…"
    if wait_http "http://localhost:${FRONTEND_PORT}/" 30; then
        log_success "Frontend is serving at http://localhost:${FRONTEND_PORT}"
    else
        log_error "Frontend failed to respond on :${FRONTEND_PORT} within 30s"
        log_error "Tail of ${FRONTEND_LOG}:"
        tail -n 20 "${FRONTEND_LOG}" 1>&2 || true
        return 1
    fi
}

# ---------- orchestration ---------------------------------------------------
log_step "family-assistant · start"
log_info "Project root: ${PROJECT_ROOT}"

check_ollama

if should_start backend;  then start_backend;  fi
if should_start frontend; then start_frontend; fi

echo ""
log_success "All requested services are up."
log_info "  Backend:  http://localhost:${BACKEND_PORT}  · logs: ${BACKEND_LOG}"
log_info "  Frontend: http://localhost:${FRONTEND_PORT}  · logs: ${FRONTEND_LOG}"
log_info "  Tail both: tail -f ${BACKEND_LOG} ${FRONTEND_LOG}"
log_info "  Stop:      scripts/stop.sh"
