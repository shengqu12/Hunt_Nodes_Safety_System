#!/usr/bin/env bash
# install_server.sh — install the fleet monitor on the lab server / workstation.
# Run WITHOUT sudo from the repo root (uses user-level systemd):
#   ./install_server.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/lidar-node-guardian"

if [[ "$SRC" != "$DEST" ]]; then
    mkdir -p "$DEST"
    cp -r "$SRC/server" "$SRC/config" "$SRC/systemd" "$DEST/"
fi
chmod +x "$DEST"/server/*.sh

for f in server.env nodes.list; do
    if [[ ! -f "$DEST/config/$f" ]]; then
        cp "$DEST/config/$f.example" "$DEST/config/$f"
        echo ">>> Created $DEST/config/$f — EDIT IT (emails / node IPs)"
    fi
done

mkdir -p "$HOME/.config/systemd/user"
cp "$DEST/systemd/guardian-monitor.service" \
   "$DEST/systemd/guardian-monitor.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now guardian-monitor.timer
# Keep user services running when not logged in:
loginctl enable-linger "$USER" 2>/dev/null || true

echo
echo "Installed. Remaining setup:"
echo "  1. Edit $DEST/config/nodes.list  (Tailscale IPs from: tailscale status)"
echo "  2. Edit $DEST/config/server.env  (MAIL_TO for you and Kieran)"
echo "  3. Email: sudo apt install msmtp msmtp-mta; cp config/msmtprc.example ~/.msmtprc; chmod 600 ~/.msmtprc; fill in app password"
echo "  4. Passwordless SSH to each node: ssh-copy-id jetson@<tailscale_ip>"
echo "  5. Test an alert:  $DEST/server/alert.sh 'Test' 'Guardian test alert'"
echo "  6. Test the loop:  $DEST/server/monitor.sh; cat ~/.lidar-guardian-server/monitor.log"
