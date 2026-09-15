#!/usr/bin/env bash
# daily_report.sh — one Slack message every morning summarising the fleet.
#
# Design rule: only what tells you, at a glance, whether today needs your
# attention. Detail stays on the nodes.
#
# The brief doubles as the monitor's own heartbeat: if guardian-monitor or
# puget dies, no brief arrives, and silence stops looking like health.
#
# Usage:
#   daily_report.sh            send to Slack/email via alert.sh
#   daily_report.sh --print    print to stdout, send nothing (for testing)

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARDIAN_HOME="${GUARDIAN_HOME:-$(dirname "$SCRIPT_DIR")}"
NODES="$GUARDIAN_HOME/config/nodes.list"
# shellcheck disable=SC1090
source "$GUARDIAN_HOME/config/server.env"
STATE_DIR="${SERVER_STATE_DIR:-$HOME/.lidar-guardian-server}"
ALERTS_LOG="$STATE_DIR/alerts.log"
NODE_STATE="${NODE_STATE_DIR:-/var/lib/lidar-guardian}"

DRY_RUN=0
[[ "${1:-}" == "--print" ]] && DRY_RUN=1

RAW="$(mktemp)"
trap 'rm -f "$RAW"' EXIT

# ---- gather, one line per node: name|reachable|json|rate|recording --------
while read -r name ip user; do
    [[ -z "$name" || "$name" =~ ^# ]] && continue

    if ! ping -c2 -W3 "$ip" >/dev/null 2>&1; then
        echo "$name|down|||" >> "$RAW"
        continue
    fi

    # One SSH per node. The remote emits three fixed lines — no separator
    # tokens: `echo` supplies its own newline, so a literal marker plus a
    # substitution produced two newlines and shifted every field by a line.
    # Line 1 status.json (or empty), line 2 bound/unbound, line 3 recorder.
    # -n so ssh cannot eat this loop's stdin (see README).
    #
    # RECORDER_PATTERN's first character is bracketed before it reaches the
    # remote: the whole script arrives as `bash -c "<text>"`, so the pattern
    # appears in the remote shell's own command line and pgrep -f matches
    # itself. [r]os2 matches the real process but not the literal text.
    pat="${RECORDER_PATTERN:-ros2 bag record}"
    pat_safe="[${pat:0:1}]${pat:1}"

    out=$(timeout 30 ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$user@$ip" "
        cat $NODE_STATE/status.json 2>/dev/null | tr -d '\n' | sed 's/\$/\n/'
        (ss -uln 2>/dev/null | grep -q ':56301 ' && echo bound) || echo unbound
        pgrep -fa \"$pat_safe\" 2>/dev/null | head -1
        echo
    " 2>/dev/null)

    json=$(sed -n '1p' <<< "$out")
    bound=$(sed -n '2p' <<< "$out")
    rec=$(sed -n '3p' <<< "$out")
    # A node that answers ping but returns nothing usable is worth flagging.
    [[ -z "$json" && -z "$bound" ]] && bound="unreachable-shell"
    echo "$name|up|$json|$bound|$rec" >> "$RAW"
done < "$NODES"

# ---- assemble the message -------------------------------------------------
REPORT=$(RAW_FILE="$RAW" ALERTS="$ALERTS_LOG" python3 <<'PY'
import os, json, re, datetime

raw = open(os.environ["RAW_FILE"]).read().strip().splitlines()
now = datetime.datetime.now()

healthy, problems, caps = [], [], []
recording = []
for line in raw:
    parts = line.split("|", 4)
    name, reach = parts[0], parts[1]
    js, bound, rec = (parts[2], parts[3], parts[4]) if len(parts) > 4 else ("", "", "")
    if reach == "down":
        problems.append(f"{name}: UNREACHABLE")
        continue
    try:
        d = json.loads(js) if js.strip() else {}
    except Exception:
        d = {}
    lidar = d.get("lidar_status", "?")
    disk = d.get("disk_used_pct", 0)
    temp = d.get("cpu_temp_c", 0)
    ts = d.get("timestamp", 0)
    age = int(now.timestamp() - ts) if ts else None

    issues = []
    if lidar not in ("ok",):
        issues.append(f"lidar={lidar}")
    if bound != "bound":
        issues.append("driver handshake down (56301 unbound)")
    if age is not None and age > 300:
        issues.append(f"heartbeat {age//60} min stale")
    if isinstance(disk, int) and disk >= 85:
        issues.append(f"disk {disk}%")
    if isinstance(temp, int) and temp >= 85:
        issues.append(f"{temp}C")

    if issues:
        problems.append(f"{name}: " + ", ".join(issues))
    else:
        healthy.append(name)
    caps.append((name, disk if isinstance(disk, int) else 0,
                 temp if isinstance(temp, int) else 0))
    if rec.strip():
        recording.append(name)

# ---- overnight alerts (last 24 h) ----
alerts, unresolved = [], []
try:
    cutoff = now - datetime.timedelta(hours=24)
    for ln in open(os.environ["ALERTS"]):
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ALERT: (.+)", ln.strip())
        if not m:
            continue
        t = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        if t >= cutoff:
            alerts.append((t, m.group(2)))
except FileNotFoundError:
    pass

# An alert whose subject does not start with RECOVERED, and which no later
# RECOVERED for the same node follows, is still open.
def node_of(s):
    m = re.search(r"\b(node\d+)\b", s)
    return m.group(1) if m else None
last = {}
for t, s in alerts:
    n = node_of(s)
    if n:
        last[n] = s
unresolved = [f"{n}: {s}" for n, s in last.items() if not s.startswith("RECOVERED")]

total = len(healthy) + len(problems)
head = "healthy" if not problems else "NEEDS ATTENTION"
L = []
L.append(f"*Morning brief — {now.strftime('%a %d %b, %H:%M')}*")
L.append(f"*Fleet:* {len(healthy)}/{total} {head}")
if healthy:
    L.append("  ok: " + " ".join(healthy))
for p in problems:
    L.append(f"  :warning: {p}")

L.append("")
L.append(f"*Overnight (24 h):* {len(alerts)} alert(s)")
if alerts:
    for t, s in alerts[-6:]:
        L.append(f"  {t.strftime('%H:%M')} {s}")
    if len(alerts) > 6:
        L.append(f"  … and {len(alerts)-6} earlier")
if unresolved:
    L.append("  :rotating_light: still open: " + "; ".join(unresolved))

L.append("")
L.append("*Recording:* " + (" ".join(recording) if recording else "none active"))

if caps:
    worst = sorted(caps, key=lambda c: -c[1])[:3]
    L.append("*Capacity:* disk " + ", ".join(f"{n} {d}%" for n, d, _ in worst)
             + f" · max temp {max(c[2] for c in caps)}C")

L.append("")
L.append("_No brief in the morning means the monitor itself is down._")
print("\n".join(L))
PY
)

if (( DRY_RUN )); then
    echo "$REPORT"
else
    "$SCRIPT_DIR/alert.sh" "Morning brief" "$REPORT"
fi
