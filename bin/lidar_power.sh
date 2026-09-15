#!/usr/bin/env bash
# lidar_power.sh — turn the LiDAR's power on/off through the USB-controlled
# switch Kieran installed. Backend is selected in guardian.env so the same
# repo works on every node regardless of which relay/hub model is attached.
#
# Usage: lidar_power.sh {on|off|cycle}

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

ACTION="${1:-}"
[[ "$ACTION" =~ ^(on|off|cycle)$ ]] || { echo "Usage: $0 {on|off|cycle}"; exit 2; }

do_switch() {  # do_switch on|off
    local want="$1"
    case "${POWER_BACKEND:-cmd}" in
        none)
            # Monitor-only mode: no switch hardware installed yet.
            log INFO "POWER_BACKEND=none — no switch hardware, skipping '$want'"
            return 0
            ;;
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
            if [[ "$want" == "on" ]]; then
                eval "${POWER_ON_CMD:?set POWER_ON_CMD in guardian.env}"
            else
                eval "${POWER_OFF_CMD:?set POWER_OFF_CMD in guardian.env}"
            fi
            ;;
        *)
            log ERROR "Unknown POWER_BACKEND='${POWER_BACKEND:-}'"; return 1 ;;
    esac
}

case "$ACTION" in
    on)
        log INFO "Powering LiDAR ON"
        do_switch on && state_set lidar_power on
        ;;
    off)
        log INFO "Powering LiDAR OFF"
        do_switch off && state_set lidar_power off
        ;;
    cycle)
        log INFO "Power-cycling LiDAR (off ${POWER_CYCLE_OFF_SECS:-10}s, then on)"
        do_switch off
        state_set lidar_power off
        sleep "${POWER_CYCLE_OFF_SECS:-10}"
        do_switch on
        state_set lidar_power on
        ;;
esac
