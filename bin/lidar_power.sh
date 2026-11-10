#!/usr/bin/env bash
# lidar_power.sh — turn the LiDAR's power on/off through the USB-controlled
# switch Kieran installed. Backend is selected in guardian.env so the same
# repo works on every node regardless of which relay/hub model is attached.
#
# Usage: lidar_power.sh {on|off|cycle}
#
# Exit status: 0 = the switch command ran and returned 0; 1 = it failed, or a
# device it names does not exist; 2 = usage. The relay is write-only, so 0 is
# "the bytes were written", never "the LiDAR has power" — carrier is that.
#
# "Powering LiDAR ON/OFF" is logged as intent, before anything is sent;
# "relay command OK" only after the command returned 0. On failure it logs
# "[ERROR] relay command failed: <stderr>" and leaves the text in kv_relay_error,
# which watchdog.sh and boot_sequence.sh turn into lidar_status=relay_missing.
#
# Why the device check: guardian.env once named the relay by
# /dev/serial/by-path/...-usb-0:2.3:1.0-port0 — one physical USB jack. With the
# relay in any other jack `stty -F` failed, this script returned 1, the relay got
# nothing, and the log said only "Powering LiDAR ON" (2026-11-10). Name the relay
# by /dev/serial/by-id/ instead.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

ACTION="${1:-}"
[[ "$ACTION" =~ ^(on|off|cycle)$ ]] || { echo "Usage: $0 {on|off|cycle}"; exit 2; }

# Paths a cmd-backend command opens: the `stty -F` argument and redirect targets.
cmd_devices() {
    grep -oE '(-F|>)[[:space:]]*/[^[:space:]"'"'"';&|<>()]+' <<< "$1" \
        | sed -E 's/^(-F|>)[[:space:]]*//' | sort -u
}

relay_error() {  # relay_error MESSAGE — one line, logged and kept for the caller
    local m
    m=$(tr '\n' ' ' <<< "$1" | sed 's/[[:space:]]*$//')
    state_set relay_error "$m"
    log ERROR "relay command failed: $m"
}

switch_backend() {  # switch_backend on|off — the raw command; its stderr is the caller's
    local want="$1"
    case "${POWER_BACKEND:-cmd}" in
        usbrelay)
            # HID relay boards (lsusb shows 16c0:05df "Van Ooijen ... HID device").
            # RELAY_ID looks like "HURTM_1"; usbrelay HURTM_1=1 turns it on.
            local v=0; [[ "$want" == "on" ]] && v=1
            usbrelay "${USBRELAY_ID:?set USBRELAY_ID in guardian.env}=$v"
            ;;
        uhubctl)
            # Per-port power switching on supported USB hubs.
            uhubctl -l "${UHUBCTL_LOCATION:?}" -p "${UHUBCTL_PORT:?}" -a "$want"
            ;;
        gpio)
            # Jetson GPIO driving a relay module. GPIO_CHIP e.g. gpiochip0.
            local v="${GPIO_ON_VALUE:-1}"
            [[ "$want" == "off" ]] && v=$(( 1 - v ))
            gpioset "${GPIO_CHIP:?}" "${GPIO_LINE:?}=$v"
            ;;
        cmd)
            # Fully custom commands from guardian.env.
            if [[ "$want" == "on" ]]; then eval "$POWER_ON_CMD"; else eval "$POWER_OFF_CMD"; fi
            ;;
        *)
            echo "unknown POWER_BACKEND='${POWER_BACKEND:-}'" >&2; return 1 ;;
    esac
}

do_switch() {  # do_switch on|off — 0 only if the command ran and returned 0
    local want="$1" cmd dev err rc
    if [[ "${POWER_BACKEND:-cmd}" == "none" ]]; then
        # Monitor-only mode: no switch hardware installed yet.
        log INFO "POWER_BACKEND=none — no switch hardware, skipping '$want'"
        return 0
    fi
    if [[ "${POWER_BACKEND:-cmd}" == "cmd" ]]; then
        if [[ "$want" == "on" ]]; then cmd="${POWER_ON_CMD:-}"; else cmd="${POWER_OFF_CMD:-}"; fi
        if [[ -z "$cmd" ]]; then
            relay_error "POWER_${want^^}_CMD is not set in guardian.env"; return 1
        fi
        for dev in $(cmd_devices "$cmd"); do
            if [[ ! -e "$dev" ]]; then
                relay_error "device $dev does not exist (relay USB unplugged, or the path names one USB port; use /dev/serial/by-id/)"
                return 1
            fi
        done
    fi
    err=$(switch_backend "$want" 2>&1 >/dev/null); rc=$?
    if (( rc != 0 )); then
        relay_error "${err:-exit status $rc}"; return 1
    fi
    state_set relay_error ""
    log INFO "relay command OK ($want)"
}

case "$ACTION" in
    on)
        log INFO "Powering LiDAR ON"
        do_switch on || exit 1
        state_set lidar_power on
        ;;
    off)
        log INFO "Powering LiDAR OFF"
        do_switch off || exit 1
        state_set lidar_power off
        ;;
    cycle)
        log INFO "Power-cycling LiDAR (off ${POWER_CYCLE_OFF_SECS:-10}s, then on)"
        do_switch off || exit 1
        state_set lidar_power off
        sleep "${POWER_CYCLE_OFF_SECS:-10}"
        do_switch on || exit 1
        state_set lidar_power on
        ;;
esac
exit 0
