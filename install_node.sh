#!/usr/bin/env bash
# install_node.sh — install the guardian on a Jetson. Run with sudo from the
# repo root:   sudo ./install_node.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/opt/lidar-guardian"

mkdir -p "$DEST" /var/lib/lidar-guardian
cp -r "$SRC/bin" "$DEST/"
mkdir -p "$DEST/config"
chmod +x "$DEST"/bin/*.sh

if [[ ! -f "$DEST/config/guardian.env" ]]; then
    cp "$SRC/config/guardian.env.example" "$DEST/config/guardian.env"
    echo ">>> Created $DEST/config/guardian.env — EDIT IT before rebooting"
    echo ">>> (NODE_NAME, LIDAR_IP, POWER_BACKEND, JETSON_USER)"
else
    echo "Keeping existing $DEST/config/guardian.env"
fi

# Required: logind RemoveIPC=no. The default (yes) deletes the ROS user's /dev/shm when that user's last session
# ends, which kills Fast DDS shared memory for ROS processes running as that user (node8, 2026-11-11: units active,
# /livox/lidar silent). Idempotent: skip if the drop-in already has this content; refuse to overwrite anything else.
# Takes effect at the next boot (no logind restart here).
IPC_DIR=/etc/systemd/logind.conf.d
IPC_FILE="$IPC_DIR/10-keep-ipc.conf"
IPC_WANT=$'[Login]\nRemoveIPC=no'
if [[ -e "$IPC_FILE" ]]; then
    if [[ "$(cat "$IPC_FILE")" == "$IPC_WANT" ]]; then
        echo "Keeping existing $IPC_FILE (RemoveIPC=no)"
    else
        echo "ERROR: $IPC_FILE exists with different content; fix it by hand (want: [Login] RemoveIPC=no)" >&2
        exit 1
    fi
else
    mkdir -p "$IPC_DIR"
    printf '%s\n' "$IPC_WANT" > "$IPC_FILE"
    chmod 644 "$IPC_FILE"
    echo ">>> Installed $IPC_FILE (RemoveIPC=no; effective at the next boot)"
fi

# ROS units only (nodes that run the Livox driver as lidar-driver.service, e.g. node8/node9; fleet nodes that start
# the pipeline by hand over SSH have no such unit and skip this). Two drop-ins + one helper (node8, 2026-11-12):
#   10-after-guardian.conf  Wants/After=guardian-boot.service: guardian-boot turns the relay OFF at boot, NetworkManager
#                           drops host_ip, and the Livox SDK binds once ("bind failed") and never retries.
#   20-initcheck.conf       ExecStartPost=+/usr/local/sbin/lidar-driver-initcheck: fail the start on
#                           "Init lds lidar fail" so Restart=always acts instead of a live process that never publishes.
# Idempotent: identical files are kept; a different existing file stops the installer before anything is written.
if [[ -f /etc/systemd/system/lidar-driver.service ]]; then
    LD_PAIRS=(
        "$SRC/systemd/lidar-driver.service.d/10-after-guardian.conf:/etc/systemd/system/lidar-driver.service.d/10-after-guardian.conf:644"
        "$SRC/systemd/lidar-driver.service.d/20-initcheck.conf:/etc/systemd/system/lidar-driver.service.d/20-initcheck.conf:644"
        "$SRC/systemd/lidar-driver-initcheck:/usr/local/sbin/lidar-driver-initcheck:755"
    )
    for p in "${LD_PAIRS[@]}"; do
        IFS=: read -r s d m <<<"$p"
        if [[ -e "$d" ]] && ! cmp -s "$s" "$d"; then
            echo "ERROR: $d exists with different content; fix it by hand (want: $s)" >&2
            exit 1
        fi
    done
    mkdir -p /etc/systemd/system/lidar-driver.service.d
    for p in "${LD_PAIRS[@]}"; do
        IFS=: read -r s d m <<<"$p"
        if [[ -e "$d" ]]; then
            echo "Keeping existing $d"
        else
            install -m "$m" -o root -g root "$s" "$d"
            echo ">>> Installed $d"
        fi
    done
else
    echo "No /etc/systemd/system/lidar-driver.service: skipping the lidar-driver drop-ins (pipeline started by hand)"
fi

cp "$SRC/systemd/guardian-boot.service" \
   "$SRC/systemd/guardian-watchdog.service" \
   "$SRC/systemd/guardian-watchdog.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable guardian-boot.service guardian-watchdog.timer
systemctl start guardian-watchdog.timer

echo
echo "Installed. Test before trusting it:"
echo "  sudo $DEST/bin/lidar_power.sh off && sleep 3 && sudo $DEST/bin/lidar_power.sh on"
echo "  sudo $DEST/bin/watchdog.sh            # manual health check"
echo "  sudo systemctl start guardian-boot    # dry-run the boot sequence now"
echo "  cat /var/lib/lidar-guardian/status.json"
echo "  systemd-analyze cat-config systemd/logind.conf | grep RemoveIPC   # last line: RemoveIPC=no"
echo "Then reboot once and confirm the LiDAR comes up staged."
