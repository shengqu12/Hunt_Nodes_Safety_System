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
    # Build the payload with proper JSON escaping. Alert bodies can contain
    # quotes, braces and newlines (e.g. an embedded status.json); string
    # interpolation produced invalid JSON and Slack silently rejected it.
    payload=$(SLACK_TEXT="*[LiDAR Guardian]* $SUBJECT
$BODY" python3 -c \
        'import json,os; print(json.dumps({"text": os.environ["SLACK_TEXT"]}))' \
        2>/dev/null)

    if [[ -z "$payload" ]]; then
        # python3 unavailable: escape backslash, quote and newline by hand.
        esc=$(printf '%s\n%s' "*[LiDAR Guardian]* $SUBJECT" "$BODY" \
              | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | awk 'BEGIN{ORS=""}{print NR>1?"\\n":""; print}')
        payload="{\"text\":\"$esc\"}"
    fi

    slack_out=$(curl -m 10 -s -w '\n%{http_code}' -X POST \
        -H 'Content-type: application/json' --data "$payload" \
        "$SLACK_WEBHOOK_URL" 2>&1)
    slack_code=$(tail -1 <<< "$slack_out")
    if [[ "$slack_code" != "200" ]]; then
        # Never block on a failed notification, but never fail silently either:
        # a dropped alert that leaves no trace is worse than no alerting.
        echo "$STAMP Slack delivery FAILED (http=$slack_code): $(head -1 <<< "$slack_out")" \
            >> "${SERVER_STATE_DIR:-$HOME/.lidar-guardian-server}/alerts.log"
    fi
fi
