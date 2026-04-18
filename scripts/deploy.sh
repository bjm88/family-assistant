#!/usr/bin/env bash
# ============================================================================
# scripts/deploy.sh
# ----------------------------------------------------------------------------
# One-shot setup/refresh script. Idempotent — safe to run whenever you pull
# new commits. It:
#
#   1. Verifies required tools (uv, node, npm) and warns about optional ones.
#   2. Installs/updates Python dependencies via `uv sync`.
#   3. Installs/updates npm dependencies for ui/react.
#   4. Runs Alembic migrations against the configured DATABASE_URL.
#   5. Optionally produces a production build of the frontend (--build).
#
# Usage:
#   scripts/deploy.sh              # install + migrate (skip prod build)
#   scripts/deploy.sh --build      # also run `npm run build`
#   scripts/deploy.sh --no-migrate # skip alembic (useful for CI-only setups)
#   scripts/deploy.sh --clean      # wipe node_modules and .venv first
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

BUILD=0
MIGRATE=1
CLEAN=0
for arg in "$@"; do
    case "${arg}" in
        --build)      BUILD=1 ;;
        --no-migrate) MIGRATE=0 ;;
        --clean)      CLEAN=1 ;;
        -h|--help)
            sed -n '4,20p' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            exit 2
            ;;
    esac
done

cd "${PROJECT_ROOT}"

log_step "family-assistant · deploy"
log_info "Project root: ${PROJECT_ROOT}"

# ---------- 1. Tooling sanity check ----------------------------------------
log_step "Checking required tooling"
require_cmd uv   "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
require_cmd node "Install Node.js 20+ (https://nodejs.org or 'brew install node')"
require_cmd npm  "Ships with Node.js — reinstall Node if missing."
log_success "uv $(uv --version | awk '{print $2}')  ·  node $(node --version)  ·  npm $(npm --version)"

# Optional tooling — warn if missing but keep going.
if ! command -v psql >/dev/null 2>&1; then
    log_warn "psql not in PATH — Alembic still works, but you won't have a CLI for ad-hoc DB queries."
fi
if ! command -v ollama >/dev/null 2>&1; then
    log_warn "ollama not in PATH — AI assistant chat will be unavailable until Ollama is installed and 'ollama pull ${AI_OLLAMA_MODEL:-gemma4:26b}' is run."
fi

# ---------- 2. Optional clean slate ----------------------------------------
if [[ "${CLEAN}" -eq 1 ]]; then
    log_step "Cleaning previous environments (--clean)"
    if [[ -d "${PROJECT_ROOT}/.venv" ]]; then
        log_info "Removing .venv/"
        rm -rf "${PROJECT_ROOT}/.venv"
    fi
    if [[ -d "${PROJECT_ROOT}/ui/react/node_modules" ]]; then
        log_info "Removing ui/react/node_modules/"
        rm -rf "${PROJECT_ROOT}/ui/react/node_modules"
    fi
    log_success "Clean complete"
fi

# ---------- 3. Python dependencies ------------------------------------------
log_step "Syncing Python dependencies (uv sync)"
# `uv sync` creates/updates .venv from pyproject.toml + uv.lock in one step.
# Piping to tee lets the user see progress while we still capture exit codes.
if ! uv sync; then
    log_error "uv sync failed — see output above."
    exit 1
fi
log_success "Python dependencies are in sync"

# ---------- 4. Alembic migrations -------------------------------------------
if [[ "${MIGRATE}" -eq 1 ]]; then
    log_step "Applying Alembic migrations"
    if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
        log_warn ".env not found at ${PROJECT_ROOT}/.env — Alembic will fall back to defaults and may fail."
    fi
    if ! uv run alembic upgrade head; then
        log_error "Alembic migration failed — check your DATABASE_URL and Postgres status."
        exit 1
    fi
    log_success "Database is at the latest migration"
else
    log_warn "Skipping migrations (--no-migrate)"
fi

# ---------- 5. Frontend dependencies ----------------------------------------
log_step "Installing frontend dependencies (npm install)"
cd "${PROJECT_ROOT}/ui/react"

# `npm ci` is faster and stricter when package-lock.json is present, but it
# refuses to run without one and wipes node_modules first. We prefer `npm
# install` here because it's safer for mixed dev workflows.
if ! npm install --no-audit --no-fund; then
    log_error "npm install failed — see output above."
    exit 1
fi
log_success "Frontend dependencies installed"

# ---------- 6. Optional production build ------------------------------------
if [[ "${BUILD}" -eq 1 ]]; then
    log_step "Building production frontend (npm run build)"
    if ! npm run build; then
        log_error "Production build failed — see output above."
        exit 1
    fi
    log_success "Production bundle written to ui/react/dist/"
fi

cd "${PROJECT_ROOT}"

echo ""
log_success "Deploy complete."
log_info "  Next:  scripts/start.sh        # boot backend + frontend"
log_info "         scripts/restart.sh      # after pulling new commits"
log_info "         scripts/stop.sh         # shut everything down"
