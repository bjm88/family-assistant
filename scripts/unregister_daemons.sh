#!/usr/bin/env bash
# ============================================================================
# scripts/unregister_daemons.sh
# ----------------------------------------------------------------------------
# Removes everything ``register_daemons.sh`` installed.
#
# * Tears down the three LaunchAgents we own
#     (com.familyassistant.{ngrok,backend,frontend}.plist).
# * Optionally stops the brew-managed Postgres + Ollama services.
#   By default we LEAVE THEM RUNNING because other tools on this Mac
#   may rely on them; pass --include-brew to also disable those.
#
# Usage:
#   scripts/unregister_daemons.sh                   # remove our agents only
#   scripts/unregister_daemons.sh --include-brew    # also brew services stop pg + ollama
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

INCLUDE_BREW=0
for arg in "$@"; do
    case "${arg}" in
        --include-brew) INCLUDE_BREW=1 ;;
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

log_step "family-assistant · unregister LaunchAgents"

for short in ngrok backend frontend; do
    log_step "Removing ${short}"
    launchagent_unload "${short}"
done

if [[ "${INCLUDE_BREW}" -eq 1 ]] && command -v brew >/dev/null 2>&1; then
    log_step "Stopping brew services (--include-brew)"
    formula="$(postgres_brew_formula)"
    if [[ -n "${formula}" ]]; then
        log_info "brew services stop ${formula}"
        brew services stop "${formula}" >/dev/null || true
    fi
    if brew services list 2>/dev/null | grep -q '^ollama'; then
        log_info "brew services stop ollama"
        brew services stop ollama >/dev/null || true
    fi
fi

echo ""
log_success "Done."
log_info "Verify:  scripts/register_daemons.sh --status"
