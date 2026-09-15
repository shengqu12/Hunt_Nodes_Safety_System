#!/usr/bin/env bash
# watchdog.sh — periodic health check, run by guardian-watchdog.timer.
#
# Checks (in order):
#   1. LiDAR reachable on Ethernet (ping)
#   2. Optional: ROS topic actually publishing (ROS_CHECK=1)
#   3. Disk usage and CPU temperature (warnings only)
#
# Failure policy:
#   - FAIL_THRESHOLD consecutive failures  -> power-cycle the LiDAR
#   - at most MAX_POWER_CYCLES cycles per CYCLE_WINDOW_SECS, then give up,
#     mark FAILED, alert once, and wait for a human (prevents an endless
#     off/on loop from cooking the sensor or masking a real hardware fault)
#   - recovery after a failure sends a recovery notice
#
# It also refreshes status.json every run — this is the heartbeat the lab
# server reads over SSH. If the Jetson itself dies, status.json goes stale
# and the *server-side* monitor raises the alarm.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

# Don't monitor mid-boot.
if [[ "$(state_get boot_complete 0)" != "1" ]]; then
    log INFO "Boot sequence not complete yet — skipping check"
    exit 0
fi

FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
MAX_POWER_CYCLES="${MAX_POWER_CYCLES:-3}"
CYCLE_WINDOW_SECS="${CYCLE_WINDOW_SECS:-3600}"

# ---- health checks ----------------------------------------------------------
healthy=1
reason=""

if ! ping -c2 -W2 "${LIDAR_IP:?}" >/dev/null 2>&1; then
    healthy=0; reason="no ping reply from LiDAR ($LIDAR_IP)"
elif [[ "${ROS_CHECK:-0}" == "1" ]]; then
    # Verify data is actually flowing, not just that the sensor answers ping.
    # Requires ROS_SETUP and ROS_TOPIC in guardian.env.
    if ! sudo -u "${JETSON_USER:-root}" bash -lc \
        "source '${ROS_SETUP:?}' && timeout ${ROS_CHECK_TIMEOUT:-10} \
         ros2 topic echo --once '${ROS_TOPIC:-/livox/lidar}' >/dev/null 2>&1"; then
        healthy=0; reason="LiDAR pings but no messages on ${ROS_TOPIC:-/livox/lidar}"
    fi
fi

# ---- soft warnings (don't trigger power cycles) -----------------------------
disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
if [[ -n "$disk" && "$disk" -ge "${DISK_WARN_PCT:-85}" ]]; then
    if (( $(date +%s) - $(state_get last_disk_alert 0) > ${ALERT_COOLDOWN_SECS:-3600} )); then
        node_alert "Disk ${disk}% full" "Jetson root filesystem at ${disk}%. Check for leftover rosbags (e.g. ~/empty_scene_rebuild)."
        state_set last_disk_alert "$(date +%s)"
    fi
fi

temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)
temp=$(( temp / 1000 ))
if (( temp >= ${TEMP_WARN_C:-85} )); then
    if (( $(date +%s) - $(state_get last_temp_alert 0) > ${ALERT_COOLDOWN_SECS:-3600} )); then
        node_alert "CPU temperature ${temp}C" "Jetson thermal zone at ${temp}C."
        state_set last_temp_alert "$(date +%s)"
    fi
fi

# ---- failure / recovery state machine ---------------------------------------
now=$(date +%s)

# Reset the power-cycle window when it expires.
if (( now - $(state_get window_start 0) > CYCLE_WINDOW_SECS )); then
    state_set window_start "$now"
    state_set cycles_in_window 0
fi

if (( healthy )); then
    prev_status="$(state_get lidar_status ok)"
    state_set consecutive_fails 0
    state_set lidar_status ok
    if [[ "$prev_status" == "failed" || "$prev_status" == "cycling" ]]; then
        node_alert "RECOVERED: LiDAR back online" "LiDAR on ${NODE_NAME:-node} is healthy again."
    fi
    write_status "ok" "complete" ""
    exit 0
fi

fails=$(( $(state_get consecutive_fails 0) + 1 ))
state_set consecutive_fails "$fails"
log WARN "Health check failed ($fails/$FAIL_THRESHOLD): $reason"

if (( fails < FAIL_THRESHOLD )); then
    write_status "degraded" "complete" "$reason"
    exit 0
fi

# Monitor-only mode: no switch hardware — alert instead of cycling.
if [[ "${POWER_BACKEND:-cmd}" == "none" ]]; then
    if [[ "$(state_get lidar_status)" != "failed" ]]; then
        state_set lidar_status failed
        node_alert "LiDAR DOWN on ${NODE_NAME:-node} (monitor-only, no auto-recovery)" \
            "$reason. POWER_BACKEND=none: no switch hardware installed, cannot power-cycle. Manual intervention needed."
    fi
    write_status "failed" "complete" "$reason — monitor-only, no power backend"
    exit 0
fi

cycles=$(state_get cycles_in_window 0)
if (( cycles < MAX_POWER_CYCLES )); then
    state_set cycles_in_window $(( cycles + 1 ))
    state_set lidar_status cycling
    log WARN "Attempting power cycle $(( cycles + 1 ))/$MAX_POWER_CYCLES"
    write_status "cycling" "complete" "$reason — power cycle $(( cycles + 1 ))"
    "$SCRIPT_DIR/lidar_power.sh" cycle
    sleep "${POST_CYCLE_WAIT_SECS:-45}"
    if ping -c2 -W2 "$LIDAR_IP" >/dev/null 2>&1; then
        log INFO "LiDAR recovered after power cycle"
        state_set consecutive_fails 0
        state_set lidar_status ok
        node_alert "LiDAR auto-recovered by power cycle" "${NODE_NAME:-node}: $reason. Power cycle $(( cycles + 1 )) fixed it."
        write_status "ok" "complete" "recovered via power cycle"
    fi
else
    # Give up, alert once, wait for a human.
    if [[ "$(state_get lidar_status)" != "failed" ]]; then
        state_set lidar_status failed
        node_alert "LiDAR DOWN — auto-recovery exhausted" \
            "${NODE_NAME:-node}: $reason. $MAX_POWER_CYCLES power cycles in the last window did not help. Manual intervention needed."
    fi
    write_status "failed" "complete" "$reason — recovery exhausted"
fi
