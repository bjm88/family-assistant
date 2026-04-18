#!/usr/bin/env bash
# ============================================================================
# scripts/restart.sh
# ----------------------------------------------------------------------------
# Convenience wrapper: stops services (with --force so stale orphans don't
# block the restart), then starts them again. Pass-through arguments let you
# restart a single service, e.g. `scripts/restart.sh backend`.
#
# Usage:
#   scripts/restart.sh             # restart both
#   scripts/restart.sh backend     # restart just the backend
#   scripts/restart.sh frontend    # restart just the frontend
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

TARGETS=("$@")
[[ "${#TARGETS[@]}" -eq 0 ]] && TARGETS=("all")

log_step "family-assistant · restart (${TARGETS[*]})"

# `--force` guarantees we don't leave a half-dead process blocking our ports.
"${_SELF_DIR}/stop.sh" --force "${TARGETS[@]}"
echo ""
"${_SELF_DIR}/start.sh" "${TARGETS[@]}"
