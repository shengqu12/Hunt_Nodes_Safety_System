"""The only things this bot can change: a LiDAR's power switch.

Three rules, each of which cost something to learn on the night the relays
were fitted.

**The model never decides to act.** Intent is parsed by rules here, exactly as
`askbot.route()` parses questions. The model's job stays what it was —
explaining measurements. Within one evening this fleet's model twice asserted
things its evidence did not say; a component that can be confidently wrong
must not be the one that cuts power to hardware. If the rules cannot parse a
request unambiguously, nothing happens and the bot asks.

**The relay cannot be read back.** It is a write-only serial board: four bytes
in, no acknowledgement, no state query. "The bytes were written" and "the
LiDAR came up" are different claims, and only the second one is worth
reporting. So every action is verified by measuring its effect.

**Switching a LiDAR on can reset its Jetson.** The MID-360 draws 18 W for
about 8 seconds at startup, and on node5 that transient resets the node —
twice, reproducibly, at 9 hours of uptime and 0.3 load. So verification runs
from the lab server, never on the node: an action that kills the host must not
also kill the measurement of what it did. A changed boot_id is how that
outcome is recognised, and it is reported as itself, not as "failed".
"""

from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import common, probe

# LCUS-style USB relay, 9600 8N1 raw. Four bytes: 0xA0, channel, state,
# checksum (sum of the first three, low byte). Channel 1 is the LiDAR.
RELAY_BYTES = {
    "on":  r"\xA0\x01\x01\xA2",
    "off": r"\xA0\x01\x00\xA1",
}

# Where the relay is. by-path pins it to the physical USB jack; /dev/ttyUSB0
# shifts if another serial device appears. The node probe reports which one it
# actually found, and that is what gets used.
FALLBACK_TTY = "/dev/ttyUSB0"


class ActionError(RuntimeError):
    pass


@dataclass
class PowerRequest:
    verb: str                      # on | off | cycle
    nodes: list = field(default_factory=list)
    text: str = ""

    def describe(self) -> str:
        names = ", ".join(n["name"] for n in self.nodes)
        return {"on": f"power the LiDAR ON for {names}",
                "off": f"power the LiDAR OFF for {names}",
                "cycle": f"power-cycle the LiDAR for {names}"}[self.verb]


# -- parsing ---------------------------------------------------------------

_VERBS = [
    ("cycle", r"\b(cycle|restart|reboot|power[-\s]?cycle)\b|重启|重新启动|重上电"),
    ("off",   r"\b(off|shutdown|shut\s?down|stop|kill)\b|关掉|关闭|断电|停掉|关"),
    ("on",    r"\b(on|start|up|enable|boot)\b|启动|打开|开启|上电|开"),
]

# "lidar 4,5" / "power on lidar 4 and 5" / "启动 lidar 4、5".
#
# The run must START with a digit immediately after the word, so "restart the
# lidar pipeline in 5 minutes" matches nothing: a number only counts when it
# is attached to something that names a node or a lidar. Getting this wrong
# means switching hardware because a number appeared in a sentence.
_SCOPE = re.compile(
    r"(?:lidars?|nodes?|节点|雷达)\s*"
    r"(\d+(?:(?:[\s,，、和与]|and\b)+\d+)*)", re.IGNORECASE)
_ALL = re.compile(r"\ball\b|全部|所有|整个|每(?:一)?(?:台|个)")


def parse(text: str, nodes: list) -> PowerRequest | None:
    """Turn a message into a bounded power request, or None.

    Returns None whenever the request is not unambiguously a power action on
    named, configured nodes. None means "ask", never "guess".
    """
    if not text:
        return None
    lowered = text.lower().strip()

    verb = None
    for name, pattern in _VERBS:
        if re.search(pattern, lowered, re.IGNORECASE):
            verb = name
            break
    if verb is None:
        return None

    # A power request has to be about the lidar/relay, not about a unit, a
    # recording or the bot itself. Without this, "restart the recording" and
    # "start the bot" both parse as hardware actions.
    if not re.search(r"\blidar|\brelay|\bpower\b|雷达|继电器|电源", lowered):
        return None
    if re.search(r"\brecord|\bsession|\bservice|\bunit|\btimer|\bbot\b|"
                 r"录制|会话|服务", lowered):
        return None

    wanted: list = []
    if _ALL.search(lowered):
        wanted = list(nodes)
    else:
        by_name = {n["name"].lower(): n for n in nodes}
        for match in _SCOPE.finditer(text):
            for number in re.findall(r"\d+", match.group(1)):
                node = by_name.get(f"node{int(number)}")
                if node and node not in wanted:
                    wanted.append(node)
    if not wanted:
        return None
    return PowerRequest(verb=verb, nodes=wanted, text=text.strip()[:200])


# -- execution -------------------------------------------------------------

def execute(request: PowerRequest, conf: dict, state: Path) -> list:
    """Run the request node by node. Never raises; a refusal is a result."""
    results = []
    for node in request.nodes:
        result = _one_node(node, request.verb, conf)
        result["node"] = node["name"]
        results.append(result)
        _audit(state, request, result)
    return results


def _one_node(node: dict, verb: str, conf: dict) -> dict:
    min_uptime = common.conf_int(conf, "POWER_MIN_UPTIME_SECS", 120)
    max_load = common.conf_float(conf, "POWER_MAX_LOAD", 2.0)
    settle = common.conf_int(conf, "POWER_SETTLE_SECS", 30)

    before = probe.node_probe(node, conf)
    if not before.get("ssh_ok"):
        return {"outcome": "refused",
                "why": f"could not reach the node to check it first: "
                       f"{before.get('error', 'no telemetry')}"}

    tty = before.get("relay_tty") or ""
    if not tty:
        return {"outcome": "refused",
                "why": "no relay serial device on this node, so its LiDAR "
                       "cannot be switched (ch341 missing, or brltty has "
                       "claimed the adapter)"}

    # The same gates guardian-boot.service uses before its staged power-up.
    # They did not save node5 — its reset is a power-budget fault, not a
    # timing one — but they are what stops a node being loaded while it is
    # still coming up.
    uptime = before.get("uptime_secs")
    if uptime is not None and uptime < min_uptime:
        return {"outcome": "refused",
                "why": f"node has only been up {uptime}s; the staged power-up "
                       f"waits for {min_uptime}s"}
    try:
        load = float(before.get("load1") or 0)
    except ValueError:
        load = 0.0
    if load > max_load:
        return {"outcome": "refused",
                "why": f"1-minute load is {load}, above the {max_load} the "
                       f"staged power-up waits for"}

    steps = [("off", 0)] if verb == "off" else (
        [("on", 0)] if verb == "on" else [("off", 5), ("on", 0)])

    for state_name, pause in steps:
        code, out, err = _write_relay(node, tty, state_name)
        if code != 0:
            return {"outcome": "write_failed",
                    "why": (err or out or f"ssh exited {code}").strip()[:200],
                    "tty": tty}
        if pause:
            time.sleep(pause)

    return _verify(node, conf, before, verb, tty, settle)


def _write_relay(node: dict, tty: str, state_name: str) -> tuple:
    """Four bytes, then leave. The ssh session must not linger: if the node
    resets under the load, a connection held open here dies with it and takes
    the evidence along."""
    payload = RELAY_BYTES[state_name]
    script = (f"D={tty}; [ -c \"$D\" ] || D={FALLBACK_TTY}; "
              f"[ -c \"$D\" ] || {{ echo NO-TTY >&2; exit 1; }}; "
              f"stty -F \"$D\" 9600 raw -echo && sleep 0.3 && "
              f"printf '{payload}' > \"$D\"")
    return common.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
         "-o", "StrictHostKeyChecking=accept-new",
         f"{node['user']}@{node['ip']}", "bash", "-c", script], timeout=25)


def _verify(node: dict, conf: dict, before: dict, verb: str, tty: str,
            settle: int) -> dict:
    """Measure what the write actually did, from the lab server.

    Polling from here rather than from the node is the whole point: the
    outcome we most need to detect is the node going away.
    """
    want_carrier = "0" if verb == "off" else "1"
    deadline = time.monotonic() + settle
    last = before
    gone = False

    while time.monotonic() < deadline:
        time.sleep(3)
        now = probe.node_probe(node, conf)
        if not now.get("ssh_ok"):
            gone = True                       # keep polling; it may come back
            last = now
            continue
        if before.get("boot_id") and now.get("boot_id") and \
                now["boot_id"] != before["boot_id"]:
            return {
                "outcome": "node_reset", "tty": tty,
                "why": "the node rebooted while its LiDAR was being switched. "
                       "The MID-360 draws 18 W for about 8 s at startup and "
                       "this node's supply cannot take it. The relay is not "
                       "persistent, so the LiDAR is off again now.",
                "carrier_before": before.get("lidar_carrier"),
                "carrier_after": now.get("lidar_carrier"),
            }
        last = now
        if now.get("lidar_carrier") == want_carrier:
            return {"outcome": "ok", "tty": tty,
                    "carrier_before": before.get("lidar_carrier"),
                    "carrier_after": now.get("lidar_carrier"),
                    "link_ip": now.get("lidar_link_ip"),
                    "lidar_status": (now.get("status") or {}).get("lidar_status")}

    if gone and not last.get("ssh_ok"):
        return {"outcome": "node_unreachable", "tty": tty,
                "why": "the node stopped answering after the write and had "
                       "not come back within the settle window. It may be "
                       "rebooting; the next monitor pass will say."}
    return {"outcome": "no_change", "tty": tty,
            "carrier_before": before.get("lidar_carrier"),
            "carrier_after": last.get("lidar_carrier"),
            "why": f"the bytes were written but the link carrier is still "
                   f"{last.get('lidar_carrier', '?')} after {settle}s. The "
                   f"relay gives no readback, so this is as far as measurement "
                   f"goes: check the 12 V side and the wiring."}


# -- audit -----------------------------------------------------------------

def _audit(state: Path, request: PowerRequest, result: dict) -> None:
    line = (f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} "
            f"{request.verb} {result.get('node', '?')} "
            f"outcome={result.get('outcome')} "
            f"carrier={result.get('carrier_before', '?')}->"
            f"{result.get('carrier_after', '?')} "
            f"request={request.text!r}")
    try:
        Path(state).mkdir(parents=True, exist_ok=True)
        with (Path(state) / "actions.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def render(request: PowerRequest, results: list) -> str:
    """One Slack message describing what was measured, per node."""
    icon = {"ok": ":white_check_mark:", "node_reset": ":rotating_light:",
            "refused": ":no_entry:", "write_failed": ":no_entry:",
            "no_change": ":warning:", "node_unreachable": ":warning:"}
    lines = [f"*{request.describe()}*"]
    for res in results:
        mark = icon.get(res["outcome"], ":grey_question:")
        if res["outcome"] == "ok":
            lines.append(
                f"{mark} *{res['node']}* — link carrier "
                f"{res.get('carrier_before', '?')} → {res.get('carrier_after', '?')}"
                + (f", link ip {res['link_ip']}" if res.get("link_ip") else ""))
        else:
            lines.append(f"{mark} *{res['node']}* — {res['outcome']}: "
                         f"{res.get('why', '')}")
    lines.append("_carrier is the physical layer on the node's LiDAR adapter. "
                 "The relay has no readback, so this is the measured effect, "
                 "not a report that the command was sent._")
    return "\n".join(lines)
