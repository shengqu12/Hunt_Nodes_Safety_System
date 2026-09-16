"""The read-only probes. Every fact the bot ever states is measured here.

Two rules govern this file.

**Nothing here decides anything.** A probe measures one thing and returns what
it measured, including "I could not measure it". Verdicts belong to
diagnose.py, and explanations belong to the model.

**A fact must name what was actually measured, and on which host.** This
system spans two kinds of machine — puget (the lab server) and seven Jetsons —
and the single richest source of wrong conclusions here is a fact from one
being read as a fact about the other. The morning brief shipped exactly that
bug: it looked for the recorder with `pgrep "ros2 bag record"` *on each
Jetson*, while recording actually runs on puget, so the line could only ever
say "none active". The model cannot check a fact it is given, so an ambiguous
one is worse than a missing one.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import common

# systemd reports "not set" timestamps as this sentinel rather than 0.
_USEC_INFINITY = 18446744073709551615


# -- systemd (user units on puget) ----------------------------------------

_UNIT_PROPS = ["ActiveState", "SubState", "UnitFileState", "Result",
               "LastTriggerUSec", "NextElapseUSecRealtime",
               "NextElapseUSecMonotonic", "ExecMainStartTimestamp",
               "InactiveEnterTimestamp", "TimersCalendar", "LoadState"]

# NextElapseUSecMonotonic uses these for "no monotonic schedule"; a real one is
# a duration string like "1month 2w 5d 22h 36min 10.433106s".
_NO_MONOTONIC = ("", "0", "infinity", "n/a")


def unit_state(unit: str) -> dict:
    """`systemctl --user show` for one unit, as a plain dict.

    Absent properties are simply missing from the result. `LoadState` tells
    the caller whether the unit exists at all, which is a different answer
    from "it exists and is inactive" — a distinction that matters here,
    because a timer can be `enabled` on disk and still not be loaded.
    """
    code, out, err = common.run(
        ["systemctl", "--user", "show", unit,
         "--property=" + ",".join(_UNIT_PROPS)], timeout=10)
    if code != 0 and not out.strip():
        return {"error": (err or "systemctl failed").strip()[:200]}
    state: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key:
            state[key.strip()] = value.strip()
    return state


def parse_timestamp(raw: str | None) -> dt.datetime | None:
    """A systemd timestamp property -> datetime, or None when it is unset.

    `systemctl show` prints these as human strings — "Mon 2026-09-14 21:00:33
    EDT" — not as the microseconds the property name promises, and
    `--timestamp=unix` does not change it for these properties (checked on
    systemd 255). Reading them as integers returns None for every one, and the
    probe then reports a timer that fired last night as "never fired since
    this systemd user session started". That is a confidently wrong fact, which
    is the single thing these probes exist not to produce.

    Parsing it in Python means parsing "EDT", which strptime cannot do
    portably. So it goes back to date(1) — the tool that printed it — and
    anything date(1) cannot read becomes None rather than a guess.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw.lower() in ("n/a", "infinity", "0", "-"):
        return None
    if raw.startswith("@"):                  # --timestamp=unix, where honoured
        raw = raw[1:].split(".")[0]
    if raw.isdigit():
        value = int(raw)
        if value <= 0 or value == _USEC_INFINITY:
            return None
        # Microseconds if it is far too large to be seconds.
        secs = value / 1e6 if value > 1e12 else value
        try:
            return dt.datetime.fromtimestamp(secs).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    code, out, _ = common.run(["date", "-d", raw, "+%s"], timeout=5)
    if code != 0:
        return None
    try:
        secs = int(out.strip())
    except ValueError:
        return None
    if secs <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(secs).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def timer_summary(unit: str) -> dict:
    """Everything worth knowing about a timer, with each field measured.

    `loaded` is reported separately from `active`. lidar-record-start.timer
    was `enabled` and its symlink was present while being `inactive (dead)`
    and absent from `list-timers` — enabled on disk says nothing about whether
    systemd is currently going to fire it.
    """
    state = unit_state(unit)
    if "error" in state:
        return {"unit": unit, "error": state["error"]}
    last = parse_timestamp(state.get("LastTriggerUSec"))
    nxt = parse_timestamp(state.get("NextElapseUSecRealtime"))
    # A monotonic timer (OnBootSec/OnUnitActiveSec, as guardian-monitor.timer
    # uses) has no realtime next-elapse at all. Reporting that as "NO next
    # firing scheduled" says the opposite of the truth about a timer that is
    # firing every minute, so the two cases are kept apart.
    mono = (state.get("NextElapseUSecMonotonic") or "").strip()
    monotonic = mono if mono.lower() not in _NO_MONOTONIC else ""
    return {
        "unit": unit,
        "load_state": state.get("LoadState", "?"),
        "exists": state.get("LoadState") not in (None, "not-found", "masked"),
        "active": state.get("ActiveState", "?"),
        "sub": state.get("SubState", "?"),
        "enabled": state.get("UnitFileState", "?"),
        "calendar": state.get("TimersCalendar", ""),
        "last_trigger": last,
        "next_elapse": nxt,
        "next_monotonic": monotonic,
        "will_fire": (state.get("ActiveState") == "active"
                      and (nxt is not None or bool(monotonic))),
    }


def service_summary(unit: str) -> dict:
    state = unit_state(unit)
    if "error" in state:
        return {"unit": unit, "error": state["error"]}
    return {
        "unit": unit,
        "load_state": state.get("LoadState", "?"),
        "active": state.get("ActiveState", "?"),
        "sub": state.get("SubState", "?"),
        "result": state.get("Result", "?"),
        "enabled": state.get("UnitFileState", "?"),
    }


# -- Tailscale ------------------------------------------------------------

def tailscale_peers() -> dict:
    """Per-IP peer state from `tailscale status --json`, measured on puget.

    This is the probe the bash guardian has no equivalent of, and it answers a
    question a ping counter cannot: Tailscale distinguishes "the peer has not
    checked in since 17:44" from "packets are not getting through". A node
    that is `Online: false` with a `LastSeen` 45 minutes old is off the
    network, not slow.
    """
    code, out, err = common.run(["tailscale", "status", "--json"], timeout=20)
    if code != 0:
        return {"error": (err or out or "tailscale status failed").strip()[:200]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"tailscale status returned non-JSON: {exc}"}

    peers: dict[str, dict] = {}
    for peer in (data.get("Peer") or {}).values():
        info = {
            "hostname": peer.get("HostName", ""),
            "online": bool(peer.get("Online")),
            "last_seen": _parse_rfc3339(peer.get("LastSeen")),
            "relay": peer.get("Relay") or "",
            "direct_addr": peer.get("CurAddr") or "",
            "os": peer.get("OS", ""),
        }
        for ip in peer.get("TailscaleIPs") or []:
            peers[ip.split("/")[0]] = info
    return {"peers": peers}


def _parse_rfc3339(raw: str | None) -> dt.datetime | None:
    """RFC3339 -> local datetime, or None when there is no such time.

    Tailscale reports `LastSeen: "0001-01-01T00:00:00Z"` for a peer that is
    currently online — Go's zero time, meaning "not applicable", not "last
    seen in the year 1". Converting that to a zone west of UTC underflows
    datetime.min and raises OverflowError, which took out the whole fleet
    bundle the first time this ran against real `tailscale status` output.
    """
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.year <= 1:
        return None
    try:
        return parsed.astimezone()
    except (OverflowError, OSError, ValueError):
        return None


# -- the guardian's own state files ---------------------------------------

def alert_flags(state: Path) -> dict:
    """Open alerts, read from the state files monitor.sh actually writes.

    The morning brief infers "still open" by regexing alert *subjects* out of
    alerts.log and checking whether a later one starts with RECOVERED. These
    files are what the guardian itself keys on, so reading them says what the
    guardian believes rather than what its log text implies.
    """
    flags: dict[str, dict] = {}
    try:
        entries = list(Path(state).iterdir())
    except OSError:
        return flags
    for path in entries:
        if path.name.startswith("active_"):
            key = path.name[len("active_"):]
            flags.setdefault(key, {})["active"] = _read_small(path) == "1"
        elif path.name.startswith("cool_"):
            key = path.name[len("cool_"):]
            raw = _read_small(path)
            try:
                stamp = int(raw)
            except ValueError:
                stamp = 0
            flags.setdefault(key, {})["last_fired"] = (
                dt.datetime.fromtimestamp(stamp).astimezone() if stamp else None)
        elif path.name.startswith("pingfail_"):
            key = "ping_" + path.name[len("pingfail_"):]
            raw = _read_small(path)
            try:
                flags.setdefault(key, {})["consecutive_ping_fails"] = int(raw)
            except ValueError:
                pass
    return flags


def _read_small(path: Path, limit: int = 256) -> str:
    try:
        return path.read_text()[:limit].strip()
    except OSError:
        return ""


ALERT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ALERT: (.+)$")


def recent_alerts(state: Path, hours: float = 24.0, limit: int = 400) -> list:
    """Parse alerts.log. Returns newest-last [(datetime, subject), ...]."""
    path = Path(state) / "alerts.log"
    try:
        lines = path.read_text(errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    cutoff = common.now() - dt.timedelta(hours=hours)
    out = []
    for line in lines:
        match = ALERT_RE.match(line.strip())
        if not match:
            continue
        try:
            when = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        when = when.astimezone()
        if when >= cutoff:
            out.append((when, match.group(2)))
    return out


MONITOR_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+) (ok:|ping fail)(.*)$")


def monitor_log(state: Path, limit: int = 3000) -> list:
    """Parse monitor.log into [(datetime, node, kind, rest), ...]."""
    path = Path(state) / "monitor.log"
    try:
        lines = path.read_text(errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        match = MONITOR_RE.match(line.strip())
        if not match:
            continue
        try:
            when = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append((when.astimezone(), match.group(2),
                    "ok" if match.group(3) == "ok:" else "pingfail",
                    match.group(4).strip()))
    return out


def last_seen_by_monitor(rows: list) -> dict:
    """Newest monitor.log row per node.

    Every server-side fact about a node is only as fresh as this. Note the
    gap it cannot see: monitor.sh `continue`s without logging when a node
    pings but its status.json is unreadable, so a node can go quiet here while
    being reachable. That is why absence is reported as absence, not as down.
    """
    out: dict[str, tuple] = {}
    for row in rows:
        out[row[1]] = row
    return out


def pass_intervals(rows: list, count: int = 6) -> dict:
    """Measured gaps between consecutive monitor passes.

    Derived from the log lines of whichever node appears most often, because
    monitor.sh writes one line per node per pass and a node it cannot classify
    produces none. The node used is reported with the figure: the interval is
    a measurement, not the 60s the timer asks for, and the two have been
    observed to differ by 2x on this host.
    """
    per_node: dict[str, list] = {}
    for when, node, _kind, _rest in rows:
        per_node.setdefault(node, []).append(when)
    if not per_node:
        return {}
    node = max(per_node, key=lambda n: len(per_node[n]))
    stamps = sorted(per_node[node])[-(count + 1):]
    if len(stamps) < 2:
        return {"node": node, "gaps": []}
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    return {"node": node, "gaps": gaps,
            "median": sorted(gaps)[len(gaps) // 2], "last": stamps[-1]}


# -- per-node probe over SSH ----------------------------------------------

# One SSH per node, emitting KEY=value lines. status.json is base64'd rather
# than inlined: the morning brief's field-shifting bug came from trying to fit
# a multi-line JSON document into a line-oriented protocol with a separator
# token, and base64 removes the possibility rather than working around it.
NODE_SCRIPT = r"""
STATE="${NODE_STATE_DIR:-/var/lib/lidar-guardian}"
printf 'NODE_EPOCH=%s\n' "$(date +%s)"
printf 'UPTIME_SECS=%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null)"
if [ -r "$STATE/status.json" ]; then
  printf 'STATUS=%s\n' "$(base64 -w0 < "$STATE/status.json" 2>/dev/null)"
  printf 'STATUS_MTIME=%s\n' "$(stat -c %Y "$STATE/status.json" 2>/dev/null)"
elif [ -e "$STATE/status.json" ]; then
  printf 'STATUS_ERR=%s\n' 'exists but not readable by this ssh user'
else
  printf 'STATUS_ERR=%s\n' 'no such file'
fi
if ss -uln 2>/dev/null | grep -q ':56301 '; then
  printf 'UDP56301=%s\n' 'bound'
else
  printf 'UDP56301=%s\n' 'unbound'
fi
# The LiDAR hangs off a USB-Ethernet adapter, and `carrier` is the physical
# layer: 1 means something is powered at the far end of that cable. It is
# local to the node, needs no network round trip, and separates "the LiDAR has
# no power" from "the LiDAR is powered but not answering at IP level" — which
# a ping cannot do. On the night the relays were fitted, six nodes reported
# lidar_status=failed and carrier=0 was the fact that said why: the LiDARs
# were simply switched off.
LIF=$(ls -d /sys/class/net/enx* 2>/dev/null | head -1)
if [ -n "$LIF" ]; then
  printf 'LIDAR_IF=%s\n'      "$(basename "$LIF")"
  printf 'LIDAR_CARRIER=%s\n' "$(cat "$LIF/carrier" 2>/dev/null)"
  printf 'LIDAR_OPER=%s\n'    "$(cat "$LIF/operstate" 2>/dev/null)"
  printf 'LIDAR_LINK_IP=%s\n' "$(ip -o -4 addr show dev "$(basename "$LIF")" 2>/dev/null | awk '{print $4}' | head -1)"
else
  printf 'LIDAR_IF=%s\n' 'none'
fi
printf 'BOOT_ID=%s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
printf 'LOAD1=%s\n'   "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
printf 'RELAY_TTY=%s\n' "$(for d in /dev/serial/by-path/platform-3610000.usb-usb-0:2.3:1.0-port0 /dev/ttyUSB0; do [ -c "$d" ] && { echo "$d"; break; }; done)"
printf 'WATCHDOG_ACTIVE=%s\n' "$(systemctl is-active guardian-watchdog.timer 2>/dev/null)"
# Converted to epoch seconds HERE, by the same date(1) that formatted it, and
# against the node's own clock. systemctl prints "Tue 2026-09-15 20:44:56 EDT"
# and shipping that string home to be parsed means parsing "EDT".
printf 'WATCHDOG_LAST_EPOCH=%s\n' "$(date -d "$(systemctl show -p LastTriggerUSec --value guardian-watchdog.timer 2>/dev/null)" +%s 2>/dev/null)"
printf 'BOOTUNIT=%s\n' "$(systemctl is-active guardian-boot.service 2>/dev/null)"
printf 'DF_PCT=%s\n' "$(df -P / 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
printf 'DF_FREE_KB=%s\n' "$(df -P / 2>/dev/null | awk 'NR==2{print $4}')"
printf 'END=1\n'
"""


def node_probe(node: dict, conf: dict, timeout: float = 20.0) -> dict:
    """Measure one Jetson. Returns facts plus, always, how it went.

    Reachability and readability are kept apart on purpose. "ping fails",
    "ssh refused", "status.json absent" and "status.json stale" are four
    different faults with four different next steps, and collapsing them into
    "node bad" is how a monitoring system sends you to the wrong rack.
    """
    result = {"name": node["name"], "ip": node["ip"], "user": node["user"],
              "measured_at": common.now()}

    ping_code, _, _ = common.run(
        ["ping", "-c", "2", "-W", "3", node["ip"]], timeout=timeout)
    result["ping_ok"] = ping_code == 0
    if ping_code != 0:
        result["ssh_ok"] = False
        result["error"] = "no ping reply (2 packets, 3s each)"
        return result

    state_dir = conf.get("NODE_STATE_DIR") or "/var/lib/lidar-guardian"
    # `sh -s` reads the script from stdin, so the script must actually be fed
    # in. Left closed, ssh exits 0 having run nothing, and every node in the
    # fleet reports as "pings but SSH returned nothing usable" — a fleet-wide
    # outage that is entirely in the probe. The END=1 sentinel below is what
    # turned that into a visible failure instead of seven nodes of blank facts.
    code, out, err = common.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=accept-new",
         f"{node['user']}@{node['ip']}",
         f"NODE_STATE_DIR={state_dir} sh -s"],
        timeout=timeout, stdin_text=NODE_SCRIPT)
    if code != 0 or "END=1" not in out:
        result["ssh_ok"] = False
        result["error"] = ((err or out).strip()[:200]
                           or f"ssh exited {code} with no output")
        return result
    result["ssh_ok"] = True

    fields: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key:
            fields[key.strip()] = value.strip()

    result["udp56301"] = fields.get("UDP56301", "?")
    result["lidar_if"] = fields.get("LIDAR_IF", "")
    result["lidar_carrier"] = fields.get("LIDAR_CARRIER", "")
    result["lidar_oper"] = fields.get("LIDAR_OPER", "")
    result["lidar_link_ip"] = fields.get("LIDAR_LINK_IP", "")
    result["boot_id"] = fields.get("BOOT_ID", "")
    result["load1"] = fields.get("LOAD1", "")
    result["relay_tty"] = fields.get("RELAY_TTY", "")
    result["watchdog_timer"] = fields.get("WATCHDOG_ACTIVE", "?")
    watchdog_epoch = _as_int(fields.get("WATCHDOG_LAST_EPOCH"))
    result["boot_unit"] = fields.get("BOOTUNIT", "?")
    result["uptime_secs"] = _as_int(fields.get("UPTIME_SECS"))
    result["df_used_pct"] = _as_int(fields.get("DF_PCT"))
    free_kb = _as_int(fields.get("DF_FREE_KB"))
    result["df_free_gb"] = free_kb * 1024 / 1e9 if free_kb is not None else None

    # Clock skew, measured in the same round trip that read the heartbeat.
    # "Heartbeat stale" is computed by comparing a timestamp the node wrote
    # against the server's clock, which silently assumes the two agree. If a
    # Jetson's clock drifts, that verdict is wrong in whichever direction the
    # drift went, and nothing in the current guardian would say so.
    node_epoch = _as_int(fields.get("NODE_EPOCH"))
    if node_epoch is not None:
        result["clock_skew_secs"] = node_epoch - int(common.now().timestamp())
        if watchdog_epoch:
            result["watchdog_last_age_secs"] = node_epoch - watchdog_epoch

    if fields.get("STATUS_ERR"):
        result["status_error"] = fields["STATUS_ERR"]
        return result
    try:
        raw = base64.b64decode(fields.get("STATUS", ""), validate=True)
        result["status"] = json.loads(raw)
    except Exception as exc:
        result["status_error"] = f"status.json unparseable: {type(exc).__name__}"
        return result

    stamp = result["status"].get("timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        # Age against the node's own clock where we have it, so the number is
        # not contaminated by skew; the skew is reported as its own fact.
        reference = node_epoch if node_epoch is not None else int(
            common.now().timestamp())
        result["heartbeat_age_secs"] = reference - int(stamp)
        result["heartbeat_age_ref"] = (
            "the node's own clock" if node_epoch is not None
            else "the lab server's clock")
    return result


def _as_int(raw) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def probe_nodes(nodes: list[dict], conf: dict, timeout: float = 20.0,
                workers: int = 8) -> list[dict]:
    """Probe several Jetsons at once.

    Sequentially this is 7 x (ping + ssh), and an unreachable node spends the
    full ping timeout before the next one starts — the bash monitor's passes
    stretch to about two minutes that way. A person waiting in Slack will not.
    """
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(nodes))) as pool:
        futures = [pool.submit(node_probe, node, conf, timeout)
                   for node in nodes]
        out = []
        for node, future in zip(nodes, futures):
            try:
                out.append(future.result(timeout=timeout + 15))
            except Exception as exc:
                out.append({"name": node["name"], "ip": node["ip"],
                            "user": node["user"], "ping_ok": False,
                            "ssh_ok": False, "measured_at": common.now(),
                            "error": f"probe raised {type(exc).__name__}: {exc}"})
    return out


# -- recording (all of this is measured on puget, never on a node) --------

# record_supervisor.py runs on the lab server and spawns one local
# record_clouds.py child per node target, subscribing over the network. No
# recorder process exists on a Jetson, which is why looking for one there
# could only ever report "none active".
SUPERVISOR_PATTERN = "record_supervisor.py"


def recording_session() -> dict:
    """Is a recording supervisor running on this host right now?

    `pgrep -f` matches full command lines, so it matches any shell wrapper
    that happens to contain the pattern — including the one that invoked this
    bot from an ssh command line. record_day.sh hit exactly this and hung for
    ten minutes waiting on itself. Only real python processes count.
    """
    code, out, _ = common.run(["pgrep", "-af", SUPERVISOR_PATTERN], timeout=10)
    if code not in (0, 1):
        return {"error": f"pgrep exited {code}"}
    sessions = []
    for line in out.splitlines():
        pid, _, cmdline = line.partition(" ")
        if re.search(r"bash -c|/bin/sh |pgrep|record_day\.sh", cmdline):
            continue
        if not re.search(r"python[0-9.]*\s", cmdline):
            continue
        match = re.search(r"--base-session\s+(\S+)", cmdline)
        sessions.append({"pid": _as_int(pid), "session": match.group(1)
                         if match else None, "cmdline": cmdline[:200],
                         "started": _proc_start(pid)})
    return {"sessions": sessions}


def _proc_start(pid: str) -> dt.datetime | None:
    code, out, _ = common.run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=5)
    if code != 0 or not out.strip():
        return None
    try:
        return dt.datetime.strptime(out.strip(), "%a %b %d %H:%M:%S %Y").astimezone()
    except ValueError:
        return None


def recording_window(rec_conf: dict, when: dt.datetime | None = None) -> dict:
    """Is recording *supposed* to be running right now?

    The equivalent probe in the NYC guardian exists so the bot does not call a
    scheduled idle a breakage, and the same trap is here: recording runs
    Mon-Thu inside a daytime window, so silence is the correct state for most
    of the week. The day-of-week restriction lives in the timer's OnCalendar
    and the hours live in recording_schedule.env, so both are read and
    reported separately rather than restated as one number.
    """
    when = when or common.now()
    start = common.conf_int(rec_conf, "RECORD_START_HOUR", 7)
    stop = common.conf_int(rec_conf, "RECORD_STOP_HOUR", 21)
    timer = timer_summary("lidar-record-start.timer")
    calendar = timer.get("calendar", "") if "error" not in timer else ""
    days = _calendar_days(calendar)
    inside_hours = start <= when.hour < stop
    day_ok = None if days is None else when.strftime("%a") in days
    return {
        "start_hour": start, "stop_hour": stop, "hour_now": when.hour,
        "weekday": when.strftime("%a"), "calendar": calendar,
        "days": days, "inside_hours": inside_hours, "day_ok": day_ok,
        "inside": inside_hours and (day_ok is not False),
    }


_DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _calendar_days(calendar: str) -> set | None:
    """Day names out of a systemd OnCalendar spec, or None if not restricted.

    None means "this probe could not narrow the days down", not "every day" —
    the caller must not turn an unread spec into a claim about the schedule.
    """
    if not calendar:
        return None
    found: set[str] = set()
    for match in re.finditer(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.\.(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b",
                             calendar):
        first, last = _DAY_ORDER.index(match.group(1)), _DAY_ORDER.index(match.group(2))
        span = (_DAY_ORDER[first:last + 1] if first <= last
                else _DAY_ORDER[first:] + _DAY_ORDER[:last + 1])
        found.update(span)
    for match in re.finditer(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b(?!\.\.)", calendar):
        found.add(match.group(1))
    return found or None


def start_refusal(rec_conf: dict) -> dict:
    """The marker record_day.sh writes when it refuses to start a day.

    A refusal is otherwise silent: it is a file in /tmp and a journal line in
    a unit nobody watches, so a lost day looks exactly like a quiet one.
    """
    state = rec_conf.get("RECORD_STATE_DIR") or "/tmp"
    path = Path(state) / "record_start_REFUSED"
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "present": False}
    return {"path": str(path), "present": True,
            "mtime": dt.datetime.fromtimestamp(stat.st_mtime).astimezone(),
            "text": _read_small(path, 600)}


def archive_result(rec_conf: dict) -> dict:
    """The last archive run's own report. A file read, never a walk of the NAS."""
    state = rec_conf.get("RECORD_STATE_DIR") or "/tmp"
    path = Path(state) / "record_archive_result.json"
    data = common.read_json(path)
    if data is None:
        return {"path": str(path), "present": False}
    try:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        mtime = None
    results = data.get("results") if isinstance(data, dict) else None
    return {"path": str(path), "present": True, "mtime": mtime,
            "results": results if isinstance(results, list) else []}


def recording_disk(conf: dict, rec_conf: dict) -> dict:
    """Free space where sessions are written, against both recording gates.

    The two thresholds mean different things and are reported separately:
    RECORD_START_THRESHOLD_GB refuses to begin a day, RECORD_MIN_FREE_GB stops
    one already running. Being below the first and above the second is a
    perfectly ordinary state that means "today will not start", and reporting
    a single "disk ok/not ok" would hide it.
    """
    target = conf.get("RECORDING_DATA_DIR", "").strip()
    out = {"path": target,
           "start_threshold_gb": common.conf_float(
               rec_conf, "RECORD_START_THRESHOLD_GB", 145.0),
           "floor_gb": common.conf_float(rec_conf, "RECORD_MIN_FREE_GB", 35.0)}
    if not target:
        out["error"] = "RECORDING_DATA_DIR is not set in server.env"
        return out
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        out["error"] = f"cannot stat {target}: {exc}"
        return out
    # record_day.sh computes its gate as int(free / 1e9); matching that exactly
    # keeps this probe's number and the gate's number the same number.
    out["free_gb"] = int(usage.free / 1e9)
    out["above_start_threshold"] = out["free_gb"] >= out["start_threshold_gb"]
    out["above_floor"] = out["free_gb"] >= out["floor_gb"]
    return out
