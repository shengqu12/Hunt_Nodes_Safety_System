#!/usr/bin/env bash
# lib.sh — shared helpers for lidar-node-guardian (runs on the Jetson).
# Sourced by boot_sequence.sh, watchdog.sh, lidar_power.sh.

set -u

GUARDIAN_HOME="${GUARDIAN_HOME:-/opt/lidar-guardian}"
CONFIG_FILE="${CONFIG_FILE:-$GUARDIAN_HOME/config/guardian.env}"

if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
else
    echo "FATAL: config not found: $CONFIG_FILE" >&2
    exit 1
fi

STATE_DIR="${STATE_DIR:-/var/lib/lidar-guardian}"
LOG_FILE="${LOG_FILE:-$STATE_DIR/guardian.log}"
STATUS_FILE="$STATE_DIR/status.json"
mkdir -p "$STATE_DIR"

log() {
    local level="$1"; shift
    local msg
    msg="$(date '+%Y-%m-%d %H:%M:%S') [$level] $*"
    echo "$msg" | tee -a "$LOG_FILE" >&2
    # Rotate crudely at ~5 MB to avoid filling the Jetson disk
    if [[ -f "$LOG_FILE" && $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 5242880 ]]; then
        mv "$LOG_FILE" "$LOG_FILE.1"
    fi
}

# --- small key=value state store (counters, timestamps) ----------------------
state_get() {  # state_get KEY DEFAULT
    local f="$STATE_DIR/kv_$1"
    [[ -f "$f" ]] && cat "$f" || echo "${2:-}"
}
state_set() {  # state_set KEY VALUE
    echo "$2" > "$STATE_DIR/kv_$1"
}

# --- status.json: what the lab server reads over SSH -------------------------
# write_status LIDAR_STATUS BOOT_STATUS EXTRA_NOTE
write_status() {
    local lidar="$1" boot="$2" note="${3:-}"
    local disk temp
    disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
    temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)
    temp=$(( temp / 1000 ))
    cat > "$STATUS_FILE.tmp" <<EOF
{
  "node": "${NODE_NAME:-unknown}",
  "timestamp": $(date +%s),
  "time_human": "$(date '+%Y-%m-%d %H:%M:%S')",
  "boot_status": "$boot",
  "lidar_status": "$lidar",
  "consecutive_fails": $(state_get consecutive_fails 0),
  "power_cycles_recent": $(state_get cycles_in_window 0),
  "disk_used_pct": ${disk:-0},
  "cpu_temp_c": $temp,
  "note": "$note"
}
EOF
    mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

# --- optional node-side Slack alert (email is handled by the lab server) -----
node_alert() {  # node_alert "SUBJECT" "BODY"
    local subject="$1" body="$2"
    log ALERT "$subject — $body"
    if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
        local payload
        payload=$(SLACK_TEXT="[${NODE_NAME:-node}] $subject
$body" python3 -c \
            'import json,os; print(json.dumps({"text": os.environ["SLACK_TEXT"]}))' \
            2>/dev/null)
        if [[ -n "$payload" ]]; then
            curl -m 10 -s -X POST -H 'Content-type: application/json' \
                --data "$payload" "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 \
                || log WARN "Slack webhook failed"
        else
            log WARN "Slack payload build failed (python3 missing?)"
        fi
    fi
}
