#!/usr/bin/env bash
# recording_status.sh — measure whether the day's capture is running.
#
# Sourced by monitor.sh (to alert) and daily_report.sh (to report). Run it
# directly to see the raw measurements:  server/recording_status.sh
#
# EVERYTHING HERE IS MEASURED ON THE LAB SERVER. Recording does not run on a
# Jetson: record_day.sh starts record_supervisor.py here, and that spawns one
# local record_clouds.py per node and subscribes over the network.
#
# The check this replaces looked for `ros2 bag record` with pgrep ON EACH
# JETSON. There has never been such a process there, so the morning brief's
# "Recording:" line could only ever say "none active" — a fact that was not
# wrong so much as meaningless, which is worse, because it looked like an
# answer.
#
# recording_probe() prints KEY=value lines. Every key is either a measurement
# or the literal string `unknown`. Nothing here infers: a value that could not
# be measured must not become a claim, because the reader — a human at 08:30
# or a model in Slack — cannot go back and check it.

# _rec_conf KEY DEFAULT — read one value out of the recording schedule config.
# Sourced in a subshell, exactly as record_day.sh sources it, so the quoting
# rules are its rules and nothing leaks into our environment.
_rec_conf() {
    local key="$1" default="${2:-}" file="${RECORDING_SCHEDULE_ENV:-}"
    [[ -z "$file" || ! -r "$file" ]] && { echo "$default"; return 1; }
    ( set -a; . "$file" >/dev/null 2>&1; echo "${!key:-$default}" )
}

# Which days the start timer is scheduled for, expanded from its OnCalendar.
# Prints `unknown` when it cannot be determined — being unable to read the
# schedule is not the same as there being no schedule, and an alert must never
# be built on the difference.
_rec_days() {
    local unit="${RECORD_START_TIMER:-lidar-record-start.timer}" cal
    cal=$(systemctl --user show -p TimersCalendar --value "$unit" 2>/dev/null)
    [[ -z "$cal" ]] && { echo "unknown"; return; }
    command -v python3 >/dev/null || { echo "unknown"; return; }
    CAL="$cal" python3 <<'PY' 2>/dev/null || echo "unknown"
import os, re
order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
cal = os.environ["CAL"]
days = set()
for a, b in re.findall(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.\.(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", cal):
    i, j = order.index(a), order.index(b)
    days.update(order[i:j + 1] if i <= j else order[i:] + order[:j + 1])
for d in re.findall(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b(?!\.\.)", cal):
    days.add(d)
print(",".join(d for d in order if d in days) if days else "unknown")
PY
}

# Is a recording supervisor running on THIS host?
#
# pgrep -f matches full command lines, so it also matches any shell wrapper
# that happens to contain the pattern — including the one running this script.
# record_day.sh hit exactly that and waited ten minutes on a process that was
# itself. Only real python processes count.
_rec_session() {
    pgrep -af "record_supervisor.py" 2>/dev/null \
        | grep -vE "bash -c|/bin/sh |recording_status\.sh|daily_report\.sh|monitor\.sh|pgrep|grep" \
        | grep -E "python[0-9.]*[[:space:]]" \
        | sed -n 's/.*--base-session \([^ ]*\).*/\1/p' \
        | head -1
}

recording_probe() {
    local unit="${RECORD_START_TIMER:-lidar-record-start.timer}"
    local start stop state_dir free_gb threshold session refused days today hour

    start=$(_rec_conf RECORD_START_HOUR 7)
    stop=$(_rec_conf RECORD_STOP_HOUR 21)
    threshold=$(_rec_conf RECORD_START_THRESHOLD_GB 145)
    state_dir=$(_rec_conf RECORD_STATE_DIR /tmp)

    if [[ -z "${RECORDING_SCHEDULE_ENV:-}" || ! -r "${RECORDING_SCHEDULE_ENV:-}" ]]; then
        echo "CONFIG=unreadable"
        echo "CONFIG_PATH=${RECORDING_SCHEDULE_ENV:-<unset>}"
        echo "IN_WINDOW=unknown"
        return 0
    fi
    echo "CONFIG=ok"
    echo "CONFIG_PATH=$RECORDING_SCHEDULE_ENV"

    hour=$(date +%-H)
    today=$(date +%a)
    days=$(_rec_days)
    echo "WINDOW=${start}:00-${stop}:00"
    echo "DAYS=$days"
    echo "TODAY=$today"
    echo "HOUR=$hour"

    # `unknown` days propagates: we say we do not know rather than assuming
    # every day is a recording day and alerting all weekend.
    if [[ "$days" == "unknown" ]]; then
        echo "IN_WINDOW=unknown"
    elif (( hour >= start && hour < stop )) && [[ ",$days," == *",$today,"* ]]; then
        echo "IN_WINDOW=yes"
    else
        echo "IN_WINDOW=no"
    fi
    echo "SECS_INTO_WINDOW=$(( (hour - start) * 3600 + $(date +%-M) * 60 ))"

    # `enabled` on disk and `active` in systemd are different facts, and the
    # gap between them is how a whole recording day went missing: the timer was
    # enabled, its symlink was in place, and it was inactive (dead), so nothing
    # fired it and no error appeared anywhere.
    echo "START_TIMER=$(systemctl --user is-active "$unit" 2>/dev/null || echo unknown)"
    echo "START_TIMER_ENABLED=$(systemctl --user is-enabled "$unit" 2>/dev/null || echo unknown)"
    echo "START_TIMER_NEXT=$(systemctl --user show -p NextElapseUSecRealtime --value "$unit" 2>/dev/null || echo unknown)"

    session=$(_rec_session)
    echo "SESSION=${session:-none}"

    refused="$state_dir/record_start_REFUSED"
    if [[ -f "$refused" ]]; then
        echo "REFUSED=yes"
        echo "REFUSED_AT=$(date -r "$refused" '+%Y-%m-%d %H:%M' 2>/dev/null)"
        echo "REFUSED_TEXT=$(tr '\n' ' ' < "$refused" | cut -c1-300)"
    else
        echo "REFUSED=no"
    fi

    if [[ -n "${RECORDING_DATA_DIR:-}" && -d "${RECORDING_DATA_DIR:-}" ]]; then
        # Computed the way record_day.sh computes its own gate, so this number
        # and the gate's number are the same number.
        free_gb=$(df -P --block-size=1 "$RECORDING_DATA_DIR" 2>/dev/null \
                  | awk 'NR==2{printf "%d", $4/1000000000}')
        echo "FREE_GB=${free_gb:-unknown}"
    else
        echo "FREE_GB=unknown"
    fi
    echo "START_THRESHOLD_GB=$threshold"
    echo "EXCLUDED=$(_rec_conf RECORD_EXCLUDE_NODES none)"
}

# Run directly: print the measurements.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    GUARDIAN_HOME="${GUARDIAN_HOME:-$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")}"
    # shellcheck disable=SC1090
    [[ -r "$GUARDIAN_HOME/config/server.env" ]] && . "$GUARDIAN_HOME/config/server.env"
    recording_probe
fi
