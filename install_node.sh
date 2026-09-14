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
echo "Then reboot once and confirm the LiDAR comes up staged."
