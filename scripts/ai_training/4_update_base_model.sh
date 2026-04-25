#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Retrain when the upstream base model changes.
#
# Flow:
#   1. Query the HuggingFace API for the main-branch commit SHA of
#      config.base_model (e.g. google/gemma-3-4b-it).
#   2. Compare against artifacts/base_model.state (the SHA we last
#      trained on). If identical -> exit 0 with no-op.
#   3. Otherwise: rerun 1_dump_corpus.py -> 2_build_sft_dataset.py ->
#      3_fine_tune.sh, then record the new SHA.
#
# Flags:
#   --force             Retrain even if the SHA is unchanged (e.g. after
#                       you expand templates or add new tables).
#   --install-cron      Install a weekly launchd agent (macOS) that runs
#                       this script at 03:00 every Sunday. Linux gets
#                       systemd --user timer instructions printed.
#   --uninstall-cron    Remove the launchd agent.
#
# Exit codes:
#   0   up-to-date OR retrain completed
#   2   bad CLI args
#   3   HF query failed (network)
#   4   retrain pipeline failed
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$HERE"

FORCE=0
INSTALL_CRON=0
UNINSTALL_CRON=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --install-cron) INSTALL_CRON=1 ;;
        --uninstall-cron) UNINSTALL_CRON=1 ;;
        -h|--help)
            sed -n '1,/^set -euo/p' "$0" | sed -E '/^(#!|$|set )/d' | sed 's/^# ?//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

cfg() {
    uv run python - <<PY
import yaml
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)
keys = "$1".split(".")
v = cfg
for k in keys:
    v = v[k]
print(v)
PY
}

BASE_MODEL="$(cfg base_model)"
STATE_FILE_REL="$(cfg update_tracker.state_file)"
case "$STATE_FILE_REL" in
    /*) STATE_FILE="$STATE_FILE_REL" ;;
    *)  STATE_FILE="$HERE/$STATE_FILE_REL" ;;
esac
mkdir -p "$(dirname "$STATE_FILE")"

# ---------------------------------------------------------------------------
# Cron install / uninstall paths
# ---------------------------------------------------------------------------

LAUNCHD_LABEL="family-assistant.ai-training.update-base"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

if [[ "$INSTALL_CRON" -eq 1 ]]; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
        echo "Installing launchd agent at $LAUNCHD_PLIST (Sunday 03:00)"
        cat > "$LAUNCHD_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>cd "$HERE" && ./4_update_base_model.sh &>> "$HERE/artifacts/update_base.log"</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>0</integer>
        <key>Hour</key><integer>3</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>$HERE/artifacts/update_base.stdout.log</string>
    <key>StandardErrorPath</key><string>$HERE/artifacts/update_base.stderr.log</string>
</dict>
</plist>
PLIST
        launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
        launchctl load "$LAUNCHD_PLIST"
        echo "Loaded. Logs: $HERE/artifacts/update_base.*log"
        exit 0
    fi
    cat <<EOF
Linux detected. Recommended systemd-user timer — paste this into
~/.config/systemd/user/family-assistant-retrain.{service,timer}:

  # family-assistant-retrain.service
  [Unit]
  Description=Retrain family-assistant fast model against latest base
  [Service]
  Type=oneshot
  WorkingDirectory=$HERE
  ExecStart=/bin/bash $HERE/4_update_base_model.sh

  # family-assistant-retrain.timer
  [Unit]
  Description=Weekly family-assistant retrain check
  [Timer]
  OnCalendar=Sun *-*-* 03:00:00
  Persistent=true
  [Install]
  WantedBy=timers.target

Then:
  systemctl --user daemon-reload
  systemctl --user enable --now family-assistant-retrain.timer
EOF
    exit 0
fi

if [[ "$UNINSTALL_CRON" -eq 1 ]]; then
    if [[ "$(uname -s)" == "Darwin" && -f "$LAUNCHD_PLIST" ]]; then
        launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
        rm -f "$LAUNCHD_PLIST"
        echo "Removed $LAUNCHD_PLIST"
    else
        echo "No launchd agent found at $LAUNCHD_PLIST"
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Main retrain path
# ---------------------------------------------------------------------------

echo "Checking HuggingFace for a newer revision of $BASE_MODEL..."
REMOTE_SHA="$(
    curl -fsSL "https://huggingface.co/api/models/${BASE_MODEL}" \
        | uv run python -c "import json, sys; print(json.load(sys.stdin).get('sha', ''))"
)" || {
    echo "Failed to query HuggingFace API for $BASE_MODEL" >&2
    exit 3
}
if [[ -z "$REMOTE_SHA" ]]; then
    echo "HuggingFace returned no SHA for $BASE_MODEL — bailing" >&2
    exit 3
fi

LOCAL_SHA=""
if [[ -f "$STATE_FILE" ]]; then
    LOCAL_SHA="$(cat "$STATE_FILE")"
fi

echo "  local SHA:  ${LOCAL_SHA:-<never trained>}"
echo "  remote SHA: $REMOTE_SHA"

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" && "$FORCE" -ne 1 ]]; then
    echo "Up to date — nothing to do. Use --force to retrain anyway."
    exit 0
fi

echo
echo "Starting retrain pipeline..."
(
    set -e
    uv run python 1_dump_corpus.py
    uv run python 2_build_sft_dataset.py
    ./3_fine_tune.sh
) || {
    echo "Retrain pipeline failed — base_model.state NOT updated" >&2
    exit 4
}

echo "$REMOTE_SHA" > "$STATE_FILE"
echo
echo "Retrain complete. Recorded SHA $REMOTE_SHA in $STATE_FILE"
