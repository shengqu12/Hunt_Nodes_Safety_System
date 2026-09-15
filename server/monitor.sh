#!/usr/bin/env bash
# monitor.sh — runs on the lab server (or workstation) every minute via
# guardian-monitor.timer. This is the authoritative "is the Jetson alive"
# check: the node's own watchdog cannot report its own death.
#
# For every node in config/nodes.list:
#   1. ping its Tailscale IP           -> node down alerts
#   2. ssh: read status.json           -> stale heartbeat / lidar failed /
#                                          disk warnings, as reported by node
# Alerts are deduplicated with a cooldown and paired with recovery notices.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARDIAN_HOME="${GUARDIAN_HOME:-$(dirname "$SCRIPT_DIR")}"
CONFIG="$GUARDIAN_HOME/config/server.env"
NODES="$GUARDIAN_HOME/config/nodes.list"
STATE_DIR="${SERVER_STATE_DIR:-$HOME/.lidar-guardian-server}"
mkdir -p "$STATE_DIR"

# shellcheck disable=SC1090
source "$CONFIG"

DOWN_THRESHOLD="${DOWN_THRESHOLD:-3}"          # consecutive failed pings
HEARTBEAT_MAX_AGE="${HEARTBEAT_MAX_AGE:-300}"  # seconds before status.json is stale
COOLDOWN="${ALERT_COOLDOWN_SECS:-3600}"

log() { echo "$(date '+%F %T') $*" >> "$STATE_DIR/monitor.log"; }

sget() { local f="$STATE_DIR/$1"; [[ -f "$f" ]] && cat "$f" || echo "${2:-}"; }
sset() { echo "$2" > "$STATE_DIR/$1"; }

# alert_once KEY SUBJECT BODY  — dedup by KEY with cooldown
alert_once() {
    local key="$1" subject="$2" body="$3" now
    now=$(date +%s)
    if (( now - $(sget "cool_$key" 0) > COOLDOWN )); then
        "$SCRIPT_DIR/alert.sh" "$subject" "$body"
        sset "cool_$key" "$now"
    fi
    sset "active_$key" 1
}

# recover_if_active KEY SUBJECT BODY — send recovery notice if alert was active
recover_if_active() {
    local key="$1" subject="$2" body="$3"
    if [[ "$(sget "active_$key" 0)" == "1" ]]; then
        "$SCRIPT_DIR/alert.sh" "$subject" "$body"
        sset "active_$key" 0
        sset "cool_$key" 0
    fi
}

while read -r name ip user; do
    [[ -z "$name" || "$name" =~ ^# ]] && continue

    # ---- 1. reachability over Tailscale ------------------------------------
    if ping -c2 -W3 "$ip" >/dev/null 2>&1; then
        sset "pingfail_$name" 0
        recover_if_active "down_$name" \
            "RECOVERED: $name reachable again" \
            "$name ($ip) is answering pings again."
    else
        f=$(( $(sget "pingfail_$name" 0) + 1 ))
        sset "pingfail_$name" "$f"
        log "$name ping fail $f/$DOWN_THRESHOLD"
        if (( f >= DOWN_THRESHOLD )); then
            alert_once "down_$name" \
                "NODE DOWN: $name unreachable" \
                "$name ($ip) has failed $f consecutive pings over Tailscale. Jetson may have lost power, WiFi, or crashed."
        fi
        continue   # can't read status if unreachable
    fi

    # ---- 2. read the node's status.json over SSH ----------------------------
    # -n is REQUIRED: without it ssh inherits this loop's stdin (nodes.list)
    # and swallows every remaining node, so only the first node is ever checked.
    status_json=$(timeout 15 ssh -n -o BatchMode=yes -o ConnectTimeout=5 \
        "$user@$ip" "cat ${NODE_STATE_DIR:-/var/lib/lidar-guardian}/status.json" 2>/dev/null)

    if [[ -z "$status_json" ]]; then
        alert_once "nostatus_$name" \
            "WARNING: $name status unreadable" \
            "$name is pingable but status.json could not be read over SSH. Guardian may not be installed/running, or SSH keys are missing."
        continue
    fi
    recover_if_active "nostatus_$name" \
        "RECOVERED: $name status readable" "$name status.json is readable again."

    jget() { echo "$status_json" | grep -o "\"$1\":[^,}]*" | head -1 | cut -d: -f2- | tr -d ' "'; }

    ts=$(jget timestamp); lidar=$(jget lidar_status); disk=$(jget disk_used_pct)
    now=$(date +%s)

    # Stale heartbeat: watchdog/timer died even though the Jetson is up.
    if [[ -n "$ts" ]] && (( now - ts > HEARTBEAT_MAX_AGE )); then
        alert_once "stale_$name" \
            "WARNING: $name watchdog heartbeat stale" \
            "$name last updated status.json $(( (now - ts) / 60 )) min ago. The guardian watchdog timer may have stopped."
    else
        recover_if_active "stale_$name" \
            "RECOVERED: $name heartbeat fresh" "$name watchdog is reporting again."
    fi

    # LiDAR failed and node-side auto-recovery is exhausted.
    if [[ "$lidar" == "failed" ]]; then
        alert_once "lidar_$name" \
            "LIDAR DOWN on $name — needs manual attention" \
            "$name reports lidar_status=failed: automatic power cycles were exhausted (or POWER_BACKEND=none). Check the node directly."
    elif [[ "$lidar" == "ok" ]]; then
        recover_if_active "lidar_$name" \
            "RECOVERED: LiDAR on $name healthy" "$name reports lidar_status=ok."
    fi

    log "$name ok: lidar=$lidar disk=${disk}%"
done < "$NODES"
