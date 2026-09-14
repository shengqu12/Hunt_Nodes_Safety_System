#!/usr/bin/env bash
# boot_sequence.sh — staged power-up, run once at boot by guardian-boot.service.
#
#   1. Force LiDAR power OFF so we always start from a known state
#      (also guarantees the Jetson never shares its boot-time power surge
#      with the LiDAR — the original brownout problem).
#   2. Wait for the Jetson to be stable: uptime, load average, and network.
#   3. Power the LiDAR ON and wait until it responds on Ethernet.
#   4. Optionally auto-start the pipeline (AUTO_START_CMD).
#   5. Mark boot complete so the watchdog begins monitoring.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

log INFO "===== boot_sequence start ====="
state_set boot_complete 0
state_set consecutive_fails 0
state_set cycles_in_window 0
state_set window_start "$(date +%s)"
write_status "unknown" "booting" "boot sequence started"

# 1. Deterministic starting state: LiDAR off.
"$SCRIPT_DIR/lidar_power.sh" off || log WARN "initial power-off failed (continuing)"

# 2. Wait for stability.
MIN_UPTIME="${MIN_UPTIME_SECS:-45}"
up=$(cut -d. -f1 /proc/uptime)
if (( up < MIN_UPTIME )); then
    log INFO "Uptime ${up}s < ${MIN_UPTIME}s, sleeping $(( MIN_UPTIME - up ))s"
    sleep $(( MIN_UPTIME - up ))
fi

# Wait for load average to settle (boot storm over)
LOAD_MAX="${STABLE_LOAD_MAX:-2.0}"
for i in $(seq 1 "${STABLE_LOAD_TRIES:-12}"); do
    load=$(cut -d' ' -f1 /proc/loadavg)
    if awk -v l="$load" -v m="$LOAD_MAX" 'BEGIN{exit !(l<m)}'; then
        log INFO "Load average $load < $LOAD_MAX — system stable"
        break
    fi
    log INFO "Load $load >= $LOAD_MAX, waiting (try $i)"
    sleep 10
done

# Wait for network (default route reachable)
for i in $(seq 1 "${NETWORK_WAIT_TRIES:-30}"); do
    gw=$(ip route | awk '/default/ {print $3; exit}')
    if [[ -n "$gw" ]] && ping -c1 -W2 "$gw" >/dev/null 2>&1; then
        log INFO "Network up (gateway $gw reachable)"
        break
    fi
    sleep 5
done

# Optional: make sure Tailscale is up so the lab server can always reach us
if [[ "${ENSURE_TAILSCALE:-1}" == "1" ]] && command -v tailscale >/dev/null; then
    if ! tailscale status >/dev/null 2>&1; then
        log WARN "Tailscale not running, attempting 'tailscale up'"
        tailscale up --timeout 30s >/dev/null 2>&1 || log WARN "tailscale up failed"
    fi
fi

# 3. Extra guard delay, then power on the LiDAR.
sleep "${PRE_LIDAR_DELAY_SECS:-10}"
"$SCRIPT_DIR/lidar_power.sh" on

# Wait for the LiDAR to answer on Ethernet.
ok=0
deadline=$(( $(date +%s) + ${LIDAR_BOOT_TIMEOUT_SECS:-120} ))
while (( $(date +%s) < deadline )); do
    if ping -c1 -W2 "${LIDAR_IP:?set LIDAR_IP in guardian.env}" >/dev/null 2>&1; then
        ok=1; break
    fi
    sleep 3
done

if (( ok )); then
    log INFO "LiDAR reachable at $LIDAR_IP"
    write_status "ok" "complete" "boot sequence finished"
else
    log ERROR "LiDAR did not come up within ${LIDAR_BOOT_TIMEOUT_SECS:-120}s"
    node_alert "LiDAR failed to start at boot" "No ping reply from $LIDAR_IP after power-on. Watchdog will retry."
    write_status "down" "complete" "lidar unreachable after boot power-on"
fi

# 4. Optional pipeline auto-start (runs as JETSON_USER, detached).
if [[ -n "${AUTO_START_CMD:-}" ]]; then
    log INFO "Auto-starting pipeline: $AUTO_START_CMD"
    sudo -u "${JETSON_USER:-$(logname 2>/dev/null || echo root)}" \
        bash -lc "nohup $AUTO_START_CMD >> $STATE_DIR/autostart.log 2>&1 &" \
        || log WARN "AUTO_START_CMD failed"
fi

# 5. Hand over to the watchdog.
state_set boot_complete 1
log INFO "===== boot_sequence done ====="
