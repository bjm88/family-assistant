#!/usr/bin/env bash
# ============================================================================
# scripts/restart.sh
# ----------------------------------------------------------------------------
# Restart the family-assistant services. Per-service the script chooses one
# of two strategies based on whether a macOS LaunchAgent is currently
# managing the process:
#
#   1. **LaunchAgent-managed** (the normal case once you've run
#      ``scripts/register_daemons.sh``): use ``launchctl kickstart -k`` so
#      launchd atomically tears down the running instance and respawns it.
#      This avoids the bind race the old "stop --force, then start" pair
#      hit because LaunchAgents have ``KeepAlive=true`` — launchd would
#      resurrect the backend within ~10 s of our SIGTERM, so by the time
#      ``start.sh`` tried to bind :8000 the freshly-spawned launchd child
#      already owned it and uvicorn died with "Address already in use".
#
#   2. **Unmanaged** (you've never run ``register_daemons.sh``, or you
#      explicitly unloaded the agents): fall back to the legacy
#      ``stop.sh --force`` then ``start.sh`` flow.
#
# Before kickstarting any service we run ``scripts/deploy.sh --build``
# so a "restart" is also a *full refresh*: Python deps (uv sync), npm
# deps, Alembic migrations, and a fresh production React bundle in
# ``ui/react/dist/`` (which the FastAPI backend serves to the public
# ngrok tunnel via ``api.routers.spa``). Skip with ``--no-deploy`` when
# you only need to recycle a process and know nothing on disk changed.
#
# Pass-through arguments let you restart a single service:
#
#   scripts/restart.sh                  # deploy --build + restart backend+frontend
#   scripts/restart.sh backend          # deploy --build + restart backend only
#   scripts/restart.sh frontend         # deploy --build + restart frontend only
#   scripts/restart.sh ngrok            # deploy --build + restart ngrok only
#   scripts/restart.sh backend ngrok    # mix and match
#   scripts/restart.sh all              # explicit equivalent of no args
#   scripts/restart.sh --no-deploy      # just kickstart (no install/build/migrate)
#
# Exit non-zero if a kickstarted service fails its health probe within 30 s
# (the agent log is tailed to stderr so you don't have to dig for it).
# ============================================================================

set -euo pipefail

_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
source "${_SELF_DIR}/lib/common.sh"

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------
TARGETS=()
DEPLOY=1
for arg in "$@"; do
    case "${arg}" in
        backend|frontend|ngrok|all) TARGETS+=("${arg}") ;;
        --no-deploy|--skip-deploy) DEPLOY=0 ;;
        -h|--help)
            sed -n '4,42p' "$0"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            log_error "Valid targets: backend, frontend, ngrok, all"
            log_error "Valid flags  : --no-deploy"
            exit 2
            ;;
    esac
done
[[ "${#TARGETS[@]}" -eq 0 ]] && TARGETS=("all")

should_target() {
    local t="$1"
    for x in "${TARGETS[@]}"; do
        [[ "${x}" == "all" || "${x}" == "${t}" ]] && return 0
    done
    return 1
}

# launchd_managed_pid <short>
#   Echo the PID launchd currently believes is running for our agent
#   ("-" if it isn't running at the moment, e.g. mid-throttle window).
#   Used by the sweeper to distinguish the legitimate managed instance
#   from any other process happening to listen on the same port.
launchd_managed_pid() {
    local label
    label="$(launchagent_label "$1")"
    launchctl print "gui/$(id -u)/${label}" 2>/dev/null \
        | awk -F '=' '/^[[:space:]]+pid =/ {gsub(/[[:space:]]/,"",$2); print $2; exit}'
}

# pidfile_for <short>
#   Return the PID file scripts/start.sh would have written for this
#   short name, or an empty string for services start.sh doesn't manage
#   (currently: ngrok, which only has a LaunchAgent path).
pidfile_for() {
    case "$1" in
        backend)  echo "${BACKEND_PID_FILE}";;
        frontend) echo "${FRONTEND_PID_FILE}";;
        *)        echo "";;
    esac
}

# port_for <short>
#   Resolve the listening port a service is expected to bind. Mirrors
#   the wiring in start.sh / register_daemons.sh so the sweeper doesn't
#   have to be told twice.
port_for() {
    case "$1" in
        backend)  echo "${BACKEND_PORT}";;
        frontend) echo "${FRONTEND_PORT}";;
        ngrok)    echo "${NGROK_AGENT_PORT}";;
        *)        echo "";;
    esac
}

# is_descendant_of <pid> <ancestor> [max_depth=6]
#   Return 0 iff <pid>'s parent chain contains <ancestor> within
#   <max_depth> hops. Used by the orphan sweeper so we don't accidentally
#   kill a worker that's a *grandchild* of the launchd-managed process.
#
#   Why this matters: when uvicorn is launched with --reload, the
#   process tree is THREE levels deep, not two:
#
#     launchd-managed (uv shim, ppid=1)         ← launchctl reports this PID
#       └─ uvicorn parent (the reloader)
#            └─ uvicorn worker (multiprocessing fork)   ← also binds :8000
#
#   The naive "ppid == managed_pid" check skipped the reloader but
#   tagged the worker as an orphan and tried to SIGTERM/SIGKILL it,
#   which (a) raced launchd's own restart, (b) triggered scary
#   "still running — something's wrong" errors, and (c) was just
#   plain wrong. Walk up the chain instead.
is_descendant_of() {
    local pid="$1"
    local ancestor="$2"
    local max_depth="${3:-6}"

    [[ -z "${pid}" || -z "${ancestor}" ]] && return 1

    local depth=0
    local cur="${pid}"
    while (( depth < max_depth )); do
        local ppid
        ppid="$(ps -o ppid= -p "${cur}" 2>/dev/null | tr -d ' ' || true)"
        # Reached init (1), kernel (0), or an unreadable PID — stop.
        [[ -z "${ppid}" || "${ppid}" == "0" || "${ppid}" == "1" ]] && return 1
        if [[ "${ppid}" == "${ancestor}" ]]; then
            return 0
        fi
        cur="${ppid}"
        ((depth++))
    done
    return 1
}

# sweep_pre_launchd_orphans <short> <port>
#   Kill anything listening on <port> that isn't the launchd-managed
#   process. Targets the specific failure mode where the user ran
#   scripts/start.sh BEFORE scripts/register_daemons.sh, leaving a
#   "manual" uvicorn (often `uv run uvicorn ... --reload`) holding
#   the port forever and quietly crashing every launchd respawn with
#   EADDRINUSE.
#
#   Side effect: clears the matching .run/<short>.pid file when its
#   PID got swept, so subsequent calls to scripts/start.sh / stop.sh
#   don't keep finding a stale pointer.
sweep_pre_launchd_orphans() {
    local short="$1"
    local port="$2"

    local managed_pid
    managed_pid="$(launchd_managed_pid "${short}")"

    local listeners
    listeners="$(pids_on_port "${port}")"
    if [[ -z "${listeners}" ]]; then
        return 0
    fi

    local swept_any=0
    while IFS= read -r pid; do
        [[ -z "${pid}" ]] && continue
        # Skip launchd's current child — kickstart will handle it.
        if [[ -n "${managed_pid}" && "${pid}" == "${managed_pid}" ]]; then
            continue
        fi
        # Also skip *any* descendant of launchd's managed PID (uvicorn
        # reloader child, its multiprocessing worker grand-child, etc.).
        # They share the inherited socket and will exit when the agent
        # is kickstarted — double-killing them just races launchd.
        if [[ -n "${managed_pid}" ]] \
            && is_descendant_of "${pid}" "${managed_pid}"; then
            continue
        fi
        local cmd
        cmd="$(ps -o command= -p "${pid}" 2>/dev/null || true)"
        log_warn "Sweeping pre-launchd orphan on :${port} (PID ${pid}): ${cmd}"
        stop_pid "${pid}" "orphan ${short} on :${port}" || true
        swept_any=1
    done <<<"${listeners}"

    if [[ "${swept_any}" -eq 1 ]]; then
        # Nuke any matching pidfile so the legacy start/stop scripts
        # don't keep believing in a process we just killed.
        local pf
        pf="$(pidfile_for "${short}")"
        if [[ -n "${pf}" && -f "${pf}" ]]; then
            log_info "Removing stale pidfile ${pf}"
            rm -f "${pf}"
        fi
    fi
}

# ----------------------------------------------------------------------------
# Per-service restart helpers
# ----------------------------------------------------------------------------

# restart_via_launchd <short> <port> <probe_url> <log_file>
#   Trigger ``launchctl kickstart -k`` and wait for the health probe.
#   ``probe_url`` may be empty (e.g. ngrok, where we don't want to depend
#   on the public tunnel being reachable from this host).
restart_via_launchd() {
    local short="$1"
    local port="$2"
    local probe_url="$3"
    local log_file="$4"

    log_step "Restarting ${short} via launchd (LaunchAgent-managed)"

    # Sweep any pre-launchd orphan that's still squatting on the port.
    # Most common cause: the user ran scripts/start.sh manually before
    # they ran scripts/register_daemons.sh, so a long-lived `uv run
    # uvicorn ... --reload` is still holding :8000. launchd's managed
    # instance then crash-loops every ~10s on EADDRINUSE. We refuse to
    # silently leak that situation — clean up first, kickstart second.
    sweep_pre_launchd_orphans "${short}" "${port}"

    if ! launchagent_kickstart "${short}"; then
        log_error "Failed to kickstart ${short} — see launchctl error above."
        return 1
    fi

    if [[ -n "${probe_url}" ]]; then
        log_info "Waiting for ${probe_url} (up to 30s)…"
        if wait_http "${probe_url}" 30; then
            log_success "${short} is healthy at ${probe_url}"
        else
            log_error "${short} did not respond within 30s after kickstart."
            if [[ -f "${log_file}" ]]; then
                log_error "Tail of ${log_file}:"
                tail -n 30 "${log_file}" 1>&2 || true
            fi
            return 1
        fi
    else
        log_success "${short} kickstarted (no in-process health probe)."
        log_info "  Tail logs:  tail -f ${log_file}"
    fi
}

# restart_via_scripts <short>
#   Legacy path for setups that haven't run register_daemons.sh: defer to
#   the existing ``stop.sh --force`` / ``start.sh`` pair, which already
#   know how to clean up orphan PIDs and free a stuck port.
restart_via_scripts() {
    local short="$1"
    log_step "Restarting ${short} via stop+start (no LaunchAgent installed)"
    "${_SELF_DIR}/stop.sh" --force "${short}"
    echo ""
    "${_SELF_DIR}/start.sh" "${short}"
}

# restart_service <short> <port> <probe_url> <log_file>
#   Pick the right strategy and execute it. Centralised so backend /
#   frontend / ngrok all share the same dispatch logic.
restart_service() {
    local short="$1"
    local port="$2"
    local probe_url="$3"
    local log_file="$4"

    if launchagent_is_loaded "${short}"; then
        restart_via_launchd "${short}" "${port}" "${probe_url}" "${log_file}"
    else
        if [[ "${short}" == "ngrok" ]]; then
            # We don't have a non-launchd ngrok manager (start.sh only
            # handles backend/frontend), so make the situation explicit
            # rather than silently no-op.
            log_warn "ngrok LaunchAgent isn't installed — nothing to restart."
            log_warn "  Install via: scripts/register_daemons.sh"
            log_warn "  Or start manually: ngrok http :${BACKEND_PORT}"
            return 0
        fi
        restart_via_scripts "${short}"
    fi
}

# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
log_step "family-assistant · restart (${TARGETS[*]})"
log_info "Project root: ${PROJECT_ROOT}"

# ---- Full-refresh step ------------------------------------------------------
# Re-run the deploy pipeline before kickstarting anything. ``deploy.sh
# --build`` is idempotent and fast on a clean tree (uv sync + npm
# install short-circuit, alembic upgrade is a no-op when at head, and
# vite build takes ~1 s) but it's the only thing that updates the
# production React bundle in ``ui/react/dist/`` — which the FastAPI
# backend now serves to ngrok visitors via ``api.routers.spa``. Without
# this step, public users would see a stale shell after every code
# change while the local Vite dev server (port 5173) silently kept
# pace. Pass --no-deploy to opt out for fast process recycles.
if [[ "${DEPLOY}" -eq 1 ]]; then
    log_step "Refreshing dependencies + production bundle (deploy.sh --build)"
    if ! "${_SELF_DIR}/deploy.sh" --build; then
        log_error "deploy.sh --build failed — see output above."
        log_error "Re-run with --no-deploy to skip this step (e.g. when offline"
        log_error "or your DB isn't reachable for migrations)."
        exit 1
    fi
    echo ""
else
    log_warn "Skipping deploy refresh (--no-deploy)"
    log_warn "  ui/react/dist/ may be stale — public ngrok visitors will see"
    log_warn "  whatever bundle was last built. Run 'cd ui/react && npm run build'"
    log_warn "  manually (or drop --no-deploy) when shipping React changes."
fi

# Heads-up if the user has agents installed but is restarting a target that
# isn't covered. Saves them a round-trip when "restart.sh frontend" doesn't
# fix what they think is a backend bug.
if launchagent_is_loaded backend && ! should_target backend; then
    log_info "(Note: backend LaunchAgent is loaded but not in the target set.)"
fi
if launchagent_is_loaded frontend && ! should_target frontend; then
    log_info "(Note: frontend LaunchAgent is loaded but not in the target set.)"
fi

failed=0

if should_target backend; then
    restart_service backend "${BACKEND_PORT}" \
        "http://localhost:${BACKEND_PORT}/api/health" \
        "${BACKEND_LOG}" \
        || failed=1
fi
if should_target frontend; then
    restart_service frontend "${FRONTEND_PORT}" \
        "http://localhost:${FRONTEND_PORT}/" \
        "${FRONTEND_LOG}" \
        || failed=1
fi
if should_target ngrok; then
    # ngrok's API is the agent dashboard, not the public tunnel — probing
    # the local agent is enough to know launchd brought the binary back.
    restart_service ngrok "${NGROK_AGENT_PORT}" \
        "http://localhost:${NGROK_AGENT_PORT}/api/tunnels" \
        "${NGROK_LOG}" \
        || failed=1
fi

echo ""
if [[ "${failed}" -eq 0 ]]; then
    log_success "Restart complete."
else
    log_error "One or more services failed to come back up cleanly."
    log_error "  Tail backend  : tail -f ${BACKEND_LOG}"
    log_error "  Tail frontend : tail -f ${FRONTEND_LOG}"
    log_error "  Agent status  : scripts/register_daemons.sh --status"
    exit 1
fi
