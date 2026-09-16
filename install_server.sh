#!/usr/bin/env bash
# install_server.sh — install the fleet monitor on the lab server / workstation.
# Run WITHOUT sudo from the repo root (uses user-level systemd):
#   ./install_server.sh
#   ./install_server.sh --uninstall
#
# User timers, not cron and not system units: the lab account has no sudo, and
# `loginctl show-user` reports Linger=yes, so user units keep running while
# nobody is logged in.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/lidar-node-guardian"
UNIT_DIR="$HOME/.config/systemd/user"
BOT_UNIT=guardian-askbot.service
TIMERS=(guardian-monitor.timer guardian-report.timer)

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$BOT_UNIT" 2>/dev/null || true
    for timer in "${TIMERS[@]}"; do
        systemctl --user disable --now "$timer" 2>/dev/null || true
    done
    rm -f "$UNIT_DIR/guardian-monitor."{service,timer} \
          "$UNIT_DIR/guardian-report."{service,timer} \
          "$UNIT_DIR/$BOT_UNIT"
    systemctl --user daemon-reload
    echo "Uninstalled. State in ~/.lidar-guardian-server was left alone."
    exit 0
fi

# When the repo IS the deployment (a git checkout at $DEST, which is how this
# is deployed on puget) there is nothing to copy and `git pull` is the update
# path. The copy below is for running the installer from somewhere else.
if [[ "$SRC" != "$DEST" ]]; then
    mkdir -p "$DEST"
    cp -r "$SRC/server" "$SRC/config" "$SRC/systemd" "$DEST/"
    [[ -d "$SRC/bin/lib" ]] && { mkdir -p "$DEST/bin"; cp -r "$SRC/bin/askbot.py" "$SRC/bin/lib" "$DEST/bin/"; }
fi
chmod +x "$DEST"/server/*.sh
[[ -f "$DEST/bin/askbot.py" ]] && chmod +x "$DEST/bin/askbot.py"

for f in server.env nodes.list; do
    if [[ ! -f "$DEST/config/$f" ]]; then
        cp "$DEST/config/$f.example" "$DEST/config/$f"
        echo ">>> Created $DEST/config/$f — EDIT IT (emails / node IPs)"
    fi
done
# server.env holds the Slack webhook and, once the bot is set up, two Slack
# tokens. Re-applied on every run, because a mode that drifts open is a mode
# nobody notices.
chmod 600 "$DEST/config/server.env"

mkdir -p "$UNIT_DIR"
cp "$DEST/systemd/guardian-monitor.service" \
   "$DEST/systemd/guardian-monitor.timer" \
   "$DEST/systemd/guardian-report.service" \
   "$DEST/systemd/guardian-report.timer" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now guardian-monitor.timer
systemctl --user enable --now guardian-report.timer
# Keep user services running when not logged in:
loginctl enable-linger "$USER" 2>/dev/null || true

# ---- the ask-bot (optional) ------------------------------------------------
# Skipped rather than installed-and-broken when there is no app token: a unit
# that cannot connect would just crash-loop, and the point of Restart=always is
# to survive a dropped WebSocket, not to retry a missing configuration.
if [[ -f "$DEST/bin/askbot.py" ]] \
   && grep -qE '^[[:space:]]*SLACK_APP_TOKEN[[:space:]]*=[[:space:]]*"?xapp-' "$DEST/config/server.env"; then
    echo "==> Slack app token found — setting up the ask-bot"
    command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
    if [[ ! -x "$DEST/.venv/bin/python" ]]; then
        python3 -m venv "$DEST/.venv"
    fi
    # slack_sdk is the only dependency outside the stdlib, and only the bot
    # needs it — monitor.sh and daily_report.sh still run on bare bash+python3.
    "$DEST/.venv/bin/pip" install -q --upgrade pip slack_sdk
    sed "s|__GUARDIAN_HOME__|$DEST|g" "$DEST/systemd/$BOT_UNIT" \
        > "$UNIT_DIR/$BOT_UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now "$BOT_UNIT"
    sleep 3
    systemctl --user --no-pager status "$BOT_UNIT" | head -5
else
    echo "==> No SLACK_APP_TOKEN set; skipping the ask-bot."
    echo "    Paste config/slack-app-manifest.yaml at https://api.slack.com/apps,"
    echo "    put the xapp-/xoxb- tokens in config/server.env, check them with"
    echo "      python3 $DEST/bin/askbot.py --check-slack"
    echo "    and re-run this script."
    systemctl --user disable --now "$BOT_UNIT" 2>/dev/null || true
fi

echo
systemctl --user list-timers 'guardian-*' --all --no-pager || true
echo
echo "Installed. Remaining setup / useful commands:"
echo "  1. Edit $DEST/config/nodes.list  (Tailscale IPs from: tailscale status)"
echo "  2. Edit $DEST/config/server.env  (MAIL_TO for you and Kieran)"
echo "  3. Email: sudo apt install msmtp msmtp-mta; cp config/msmtprc.example ~/.msmtprc; chmod 600 ~/.msmtprc"
echo "  4. Passwordless SSH to each node: ssh-copy-id kelrod@<tailscale_ip>"
echo "  5. Test an alert:   $DEST/server/alert.sh 'Test' 'Guardian test alert'"
echo "  6. Preview a brief: $DEST/server/daily_report.sh --print"
echo "  7. Recording probe: $DEST/server/recording_status.sh"
echo "  8. Ask the bot:     python3 $DEST/bin/askbot.py --ask 'why'"
echo "  9. Raw evidence:    python3 $DEST/bin/askbot.py --probe recording"
echo " 10. Watch the bot:   journalctl --user -u $BOT_UNIT -f"
