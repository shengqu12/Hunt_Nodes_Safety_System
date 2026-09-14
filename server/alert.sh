#!/usr/bin/env bash
# alert.sh — send an alert to Sheng & Kieran. Usage: alert.sh SUBJECT BODY
# Channels (enable any/all in config/server.env):
#   - Email via msmtp (MAIL_TO, comma-separated)
#   - Slack incoming webhook (SLACK_WEBHOOK_URL)

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARDIAN_HOME="${GUARDIAN_HOME:-$(dirname "$SCRIPT_DIR")}"
# shellcheck disable=SC1090
source "$GUARDIAN_HOME/config/server.env"

SUBJECT="${1:?subject required}"
BODY="${2:-}"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

echo "$STAMP ALERT: $SUBJECT" >> "${SERVER_STATE_DIR:-$HOME/.lidar-guardian-server}/alerts.log"

if [[ -n "${MAIL_TO:-}" ]] && command -v msmtp >/dev/null; then
    IFS=',' read -ra RCPTS <<< "$MAIL_TO"
    {
        echo "To: $MAIL_TO"
        echo "From: ${MAIL_FROM:-lidar-guardian}"
        echo "Subject: [LiDAR Guardian] $SUBJECT"
        echo
        echo "$BODY"
        echo
        echo "-- sent $STAMP by lidar-node-guardian on $(hostname)"
    } | msmtp "${RCPTS[@]}" || echo "$STAMP msmtp send failed" >> \
        "${SERVER_STATE_DIR:-$HOME/.lidar-guardian-server}/alerts.log"
fi

if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
    curl -m 10 -s -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"*[LiDAR Guardian]* $SUBJECT\n$BODY\"}" \
        "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 || true
fi
