"""Deterministic diagnostic bundles.

Every fact the assistant ever states comes from here. The model is never given
a shell, a command allowlist, or any say in what gets looked at — it receives
this structured evidence and explains it, and the evidence is posted next to
its answer so a human can check both.

That split is the point. Agentic debugging is where small models fail worst:
they produce a fluent, plausible, wrong diagnosis, which for a monitoring
system is worse than none, because you act on it. Deciding *what to check* for
a given alert was already settled when the alert was written.

Two rules specific to this fleet:

**Say which machine a fact came from.** Facts about puget and facts about a
Jetson read identically once they are text. The morning brief looked for the
recorder on the Jetsons when it runs on puget, and reported "none active" for
months without that ever being false in a way anyone could see.

**Never let a stale fact pass as a current one.** Most of what the bash
guardian knows is as old as its last monitor pass. Where a fact is a
recollection rather than a measurement, it says so and gives its age.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import common, probe


@dataclass
class Fact:
    label: str
    value: str
    status: str = "info"        # ok | warn | bad | unknown | info
    detail: str = ""


@dataclass
class Bundle:
    topic: str
    facts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    elapsed: float = 0.0

    def add(self, label, value, status="info", detail="") -> None:
        self.facts.append(Fact(label, str(value), status, detail))

    MARKS = {"ok": "OK", "warn": "WARN", "bad": "BAD",
             "unknown": "??", "info": "--"}

    def _line(self, fact) -> str:
        mark = self.MARKS.get(fact.status, "--")
        return (f"[{mark}] {fact.label}: {fact.value}"
                + (f"  ({fact.detail})" if fact.detail else ""))

    def as_text(self, max_chars: int | None = None) -> str:
        """The exact evidence handed to the model — and shown to the human.

        With a budget, drop the healthy facts before the unhealthy ones and
        **say what was dropped**. Evidence exists so a human can check the
        answer; silently cutting three quarters of it leaves them unable to
        tell a quiet fleet from a cut-off message, which is the same ambiguity
        this whole design is arranged against. A `status` bundle is ~69 facts
        and ~11k characters, and Slack will not show that in one message.
        """
        lines = [self._line(f) for f in self.facts]
        lines += [f"[??] probe failed: {err}" for err in self.errors]
        text = "\n".join(lines)
        if max_chars is None or len(text) <= max_chars:
            return text

        # Keep everything that is not OK, in order; summarise the rest.
        keep = [f for f in self.facts if f.status != "ok"]
        dropped = [f for f in self.facts if f.status == "ok"]
        lines = [self._line(f) for f in keep]
        lines += [f"[??] probe failed: {err}" for err in self.errors]
        if dropped:
            names = ", ".join(f.label for f in dropped)
            lines.append(f"[OK] {len(dropped)} further checks were all OK and "
                         f"are summarised here rather than listed: {names}")
        text = "\n".join(lines)

        if len(text) > max_chars:
            cut = text[:max_chars].rsplit("\n", 1)[0]
            shown = cut.count("\n") + 1
            total = len(lines)
            text = (cut + f"\n... TRUNCATED: {total - shown} of {total} lines "
                          f"are not shown. Full evidence: "
                          f"python3 bin/askbot.py --probe {self.topic}")
        return text

    @property
    def worst(self) -> str:
        for level in ("bad", "warn", "unknown"):
            if any(f.status == level for f in self.facts):
                return level
        return "ok"


def collect(conf: dict, topic: str = "general",
            node_name: str | None = None) -> Bundle:
    """Run the bundle for `topic`. Never raises; a failed probe is a fact."""
    started = time.monotonic()
    bundle = Bundle(topic=topic)
    state = common.state_dir(conf)
    nodes = common.load_nodes(conf)
    rec_conf = common.recording_env(conf)

    # Each section is isolated. A bug in one probe used to take the whole
    # bundle with it: a stray OverflowError in the Tailscale parse left the
    # answer with five facts and no mention of a single node, which reads like
    # a quiet fleet rather than a broken probe.
    sections = [("guardian", lambda: _guardian_health(bundle, conf, state, nodes))]
    if topic in ("node", "fleet", "status", "general"):
        wanted = ([n for n in nodes if n["name"] == node_name]
                  if node_name else nodes)
        sections.append(("fleet",
                         lambda: _fleet(bundle, conf, wanted, state,
                                        detailed=bool(node_name))))
    if topic in ("recording", "status", "general"):
        sections.append(("recording",
                         lambda: _recording(bundle, conf, rec_conf, nodes)))
    if topic in ("alerts", "status", "general", "node"):
        sections.append(("alerts", lambda: _alerts(bundle, state, node_name)))

    for name, run_section in sections:
        try:
            run_section()
        except Exception as exc:
            bundle.errors.append(
                f"the {name} probes raised {type(exc).__name__}: {exc} — "
                f"everything they would have reported is MISSING from this "
                f"evidence, not absent from the system")

    bundle.elapsed = round(time.monotonic() - started, 2)
    return bundle


def _next_firing(summary: dict) -> str:
    """How to describe a timer's next firing without overstating it.

    A monotonic timer (OnBootSec/OnUnitActiveSec, which guardian-monitor.timer
    uses) has no realtime next-elapse. Calling that "NO next firing scheduled"
    says the opposite of the truth about a timer firing every minute.
    """
    nxt = summary.get("next_elapse")
    if nxt:
        return f"; next {nxt:%a %d %b %H:%M}"
    if summary.get("next_monotonic"):
        return (f"; next firing is on a monotonic schedule relative to boot "
                f"({summary['next_monotonic']}), so it has no wall-clock time")
    # A timer whose unit is running right now has no next elapse yet: systemd
    # computes it when the unit finishes. Reporting that as "NO next firing
    # scheduled" says a healthy every-minute timer is dead.
    if summary.get("active") == "active" and summary.get("sub") == "running":
        return ("; the unit it triggers is running right now, so the next "
                "elapse is not computed yet")
    return "; NO next firing scheduled"


# -- is the guardian itself working? --------------------------------------

def _guardian_health(bundle: Bundle, conf: dict, state, nodes: list) -> None:
    """The monitor watches the fleet; this watches the monitor.

    It runs for every topic, and first, because it bounds how much the rest of
    the evidence is worth. If the monitor has not run for an hour, "node3 was
    ok" means "node3 was ok an hour ago".
    """
    for unit in ("guardian-monitor.timer", "guardian-report.timer"):
        summary = probe.timer_summary(unit)
        if "error" in summary:
            bundle.add(f"{unit}", "could not be read", "unknown", summary["error"])
            continue
        status = "ok" if summary["active"] == "active" else "bad"
        detail = f"enabled={summary['enabled']}"
        if summary["last_trigger"]:
            age = (common.now() - summary["last_trigger"]).total_seconds()
            detail += (f"; last fired {summary['last_trigger']:%H:%M:%S} "
                       f"({common.human_duration(age)} ago)")
        else:
            detail += "; no recorded last firing"
        detail += _next_firing(summary)
        bundle.add(unit, f"{summary['active']} ({summary['sub']})",
                   status, detail)

    service = probe.service_summary("guardian-monitor.service")
    if "error" not in service and service["active"] == "activating":
        # A pass that is still running when the next one is due means the
        # timer's period is not the real period. Worth stating plainly,
        # because every "last seen" figure below inherits it.
        bundle.add("guardian-monitor.service", "still running a pass", "warn",
                   "the previous pass had not finished when this was measured; "
                   "passes are taking longer than the timer's interval")

    rows = probe.monitor_log(state)
    if not rows:
        bundle.add("monitor.log", "no parseable lines", "unknown",
                   f"looked in {state}/monitor.log")
        return

    cadence = probe.pass_intervals(rows)
    if cadence.get("gaps"):
        gaps = ", ".join(f"{g:.0f}s" for g in cadence["gaps"])
        bundle.add("Measured gap between monitor passes", gaps, "info",
                   f"consecutive log lines for {cadence['node']}; monitor.sh "
                   f"writes one line per node per pass. "
                   f"guardian-monitor.timer asks for 60s")

    newest = max(row[0] for row in rows)
    age = (common.now() - newest).total_seconds()
    bundle.add("Last monitor pass", f"{newest:%Y-%m-%d %H:%M:%S} "
               f"({common.human_duration(age)} ago)",
               "ok" if age < 300 else ("warn" if age < 1800 else "bad"),
               "everything below that is not marked 'measured now' is at most "
               "this fresh")

    seen = probe.last_seen_by_monitor(rows)
    missing = [n["name"] for n in nodes if n["name"] not in seen]
    if missing:
        # Not the same as "down": monitor.sh logs nothing at all for a node
        # that pings but whose status.json it cannot read.
        bundle.add("Nodes absent from monitor.log", ", ".join(missing), "warn",
                   "monitor.sh writes no line for a node that pings but whose "
                   "status.json is unreadable, so this is 'unclassified', "
                   "not 'down'")


# -- the fleet ------------------------------------------------------------

def _fleet(bundle: Bundle, conf: dict, nodes: list, state, detailed: bool) -> None:
    if not nodes:
        bundle.add("Node inventory", "nodes.list is empty or unreadable",
                   "unknown")
        return

    ts = probe.tailscale_peers()
    if "error" in ts:
        bundle.add("Tailscale status", "could not be read", "unknown", ts["error"])
        peers = {}
    else:
        peers = ts["peers"]

    timeout = common.conf_float(conf, "PROBE_SSH_TIMEOUT_SECS", 20.0)
    results = probe.probe_nodes(nodes, conf, timeout=timeout)
    flags = probe.alert_flags(state)
    stale_max = common.conf_int(conf, "HEARTBEAT_MAX_AGE", 300)
    disk_warn = common.conf_int(conf, "DISK_WARN_PCT", 85)
    temp_warn = common.conf_int(conf, "TEMP_WARN_C", 85)

    healthy = []
    for res in results:
        name = res["name"]
        peer = peers.get(res["ip"])

        # Tailscale first: it distinguishes "the peer has not checked in" from
        # "packets are not getting through", which a ping counter cannot.
        if peer is not None:
            if peer["online"]:
                link = ("direct " + peer["direct_addr"] if peer["direct_addr"]
                        else "relay " + (peer["relay"] or "?"))
                if not detailed and res.get("ssh_ok"):
                    pass                     # keep the healthy case quiet
                else:
                    bundle.add(f"{name} Tailscale", f"online ({link})", "ok",
                               f"peer {peer['hostname']}, measured on puget now")
            else:
                last = peer["last_seen"]
                ago = (common.human_duration(
                    (common.now() - last).total_seconds()) if last else "unknown")
                bundle.add(f"{name} Tailscale", f"OFFLINE, last seen {ago} ago",
                           "bad",
                           f"peer {peer['hostname']}; Tailscale's own view "
                           f"measured on puget now — the node is off the "
                           f"network, not merely slow to answer pings")
        elif not detailed:
            pass
        else:
            bundle.add(f"{name} Tailscale", "no peer with this IP", "unknown",
                       f"{res['ip']} is not in `tailscale status` output")

        ping_fails = flags.get(f"ping_{name}", {}).get("consecutive_ping_fails")

        if not res.get("ping_ok"):
            detail = "measured now, 2 packets with a 3s timeout each"
            if ping_fails:
                detail += (f"; monitor.sh has counted {ping_fails} consecutive "
                           f"failed passes")
            bundle.add(f"{name} reachability", "NO PING REPLY", "bad", detail)
            continue

        if not res.get("ssh_ok"):
            # Pings but will not talk: a genuinely different fault from down.
            bundle.add(f"{name} reachability",
                       "pings, but SSH returned nothing usable", "bad",
                       f"measured now: {res.get('error', '?')}")
            continue

        # Only expand a node that has something wrong, unless the question
        # named one. Seven healthy nodes at seven facts each buried the two
        # broken ones and blew past what Slack will show in a message.
        if detailed or _node_has_issue(res, stale_max, disk_warn, temp_warn):
            _node_facts(bundle, res, stale_max, disk_warn, temp_warn, detailed)
        else:
            healthy.append(name)

    if healthy and not detailed:
        bundle.add("Nodes with nothing wrong", ", ".join(healthy), "ok",
                   f"{len(healthy)} of {len(results)} probed, measured now over "
                   f"ping + one SSH each: heartbeat fresh, lidar_status ok, "
                   f"watchdog timer active, UDP 56301 bound, disk and temp "
                   f"under their thresholds. Ask about one by name for its "
                   f"full readings")


def _node_has_issue(res: dict, stale_max: int, disk_warn: int,
                    temp_warn: int) -> bool:
    status = res.get("status") or {}
    age = res.get("heartbeat_age_secs")
    return bool(
        res.get("status_error")
        or status.get("lidar_status") not in ("ok",)
        or (age is not None and age > stale_max)
        or (isinstance(status.get("disk_used_pct"), int)
            and status["disk_used_pct"] >= disk_warn)
        or (isinstance(status.get("cpu_temp_c"), int)
            and status["cpu_temp_c"] >= temp_warn)
        or res.get("udp56301") != "bound"
        or res.get("watchdog_timer") not in ("active",)
    )


def _node_facts(bundle: Bundle, res: dict, stale_max: int, disk_warn: int,
                temp_warn: int, detailed: bool) -> None:
    name = res["name"]

    if res.get("status_error"):
        bundle.add(f"{name} status.json", res["status_error"], "bad",
                   "read over SSH just now; the node is up but its guardian "
                   "is not writing a heartbeat")
    else:
        status = res.get("status") or {}
        age = res.get("heartbeat_age_secs")
        ref = res.get("heartbeat_age_ref", "?")
        if age is None:
            bundle.add(f"{name} heartbeat", "status.json has no usable timestamp",
                       "unknown")
        else:
            bundle.add(
                f"{name} heartbeat",
                f"written {common.human_duration(age)} ago",
                "ok" if age <= stale_max else "bad",
                f"age measured against {ref}; the guardian calls it stale "
                f"above {stale_max}s")

        lidar = status.get("lidar_status", "?")
        bundle.add(f"{name} lidar_status", lidar,
                   "ok" if lidar == "ok" else "bad",
                   "the node's own verdict as of its last watchdog run, "
                   "not a live check of the LiDAR")

        cycles = status.get("power_cycles_recent")
        if cycles:
            bundle.add(f"{name} power cycles", f"{cycles} recently", "warn",
                       "the node has been power-cycling its LiDAR; the budget "
                       "is bounded and then it gives up and waits for a human")

        disk = status.get("disk_used_pct")
        if isinstance(disk, int):
            bundle.add(f"{name} disk (node's last report)", f"{disk}% used",
                       "bad" if disk >= disk_warn else "ok",
                       f"from status.json, so as old as the heartbeat above; "
                       f"warn threshold {disk_warn}%")
        temp = status.get("cpu_temp_c")
        if isinstance(temp, int):
            bundle.add(f"{name} CPU temp", f"{temp} C",
                       "bad" if temp >= temp_warn else "ok",
                       f"from status.json; warn threshold {temp_warn}C")
        if status.get("note"):
            bundle.add(f"{name} note", str(status["note"])[:200], "info",
                       "free text the node's watchdog wrote")

    # Measured live, and deliberately reported next to the node's own figure:
    # if the watchdog is dead, status.json's disk number is frozen while this
    # one keeps moving.
    if res.get("df_used_pct") is not None:
        bundle.add(f"{name} disk (measured now)", f"{res['df_used_pct']}% used",
                   "bad" if res["df_used_pct"] >= disk_warn else "ok",
                   f"df -P / over SSH just now, "
                   f"{common.human_gb(res.get('df_free_gb'))} free")

    wd = res.get("watchdog_timer", "?")
    wd_age = res.get("watchdog_last_age_secs")
    wd_detail = ("on the Jetson. This is the direct answer to a stale "
                 "heartbeat: a dead timer means nothing is writing "
                 "status.json, as distinct from the whole node being wedged")
    if wd_age is not None:
        wd_detail += (f"; last fired {common.human_duration(wd_age)} ago, "
                      f"measured on the node against its own clock")
    bundle.add(f"{name} guardian-watchdog.timer", wd,
               "ok" if wd == "active" else "bad", wd_detail)

    port = res.get("udp56301", "?")
    bundle.add(f"{name} UDP 56301", port,
               "ok" if port == "bound" else "bad",
               "measured with `ss -uln` on the Jetson: something holds the "
               "Livox driver's port. Bound does not prove the driver is "
               "publishing, only that the socket is open")

    skew = res.get("clock_skew_secs")
    if skew is not None and abs(skew) >= 5:
        bundle.add(f"{name} clock skew", f"{skew:+d}s vs the lab server", "warn",
                   "heartbeat staleness is a comparison of two clocks; this "
                   "much drift moves that verdict by the same amount")
    elif detailed and skew is not None:
        bundle.add(f"{name} clock skew", f"{skew:+d}s vs the lab server", "ok")

    if detailed and res.get("uptime_secs") is not None:
        bundle.add(f"{name} uptime",
                   common.human_duration(res["uptime_secs"]), "info",
                   "distinguishes a node still coming up from one that has "
                   "been up for days with a dead watchdog")


# -- recording ------------------------------------------------------------

def _recording(bundle: Bundle, conf: dict, rec_conf: dict, nodes: list) -> None:
    """Everything here is measured on puget. Recording does not run on a node.

    record_day.sh starts record_supervisor.py on the lab server, which spawns
    one local record_clouds.py per node target and subscribes over the
    network. Every fact below is labelled accordingly, because the previous
    version of this check looked on the Jetsons and could only ever find
    nothing.
    """
    if not rec_conf:
        bundle.add("Recording config", "not readable", "unknown",
                   f"RECORDING_SCHEDULE_ENV="
                   f"{conf.get('RECORDING_SCHEDULE_ENV') or '(unset)'} in "
                   f"server.env; without it nothing below can be checked")
        return

    window = probe.recording_window(rec_conf)
    if window["days"] is None:
        day_note = ("the timer's OnCalendar could not be read, so which days "
                    "recording runs is unknown")
    else:
        day_note = (f"scheduled days {', '.join(sorted(window['days']))}; "
                    f"today is {window['weekday']}")
    bundle.add("Inside recording window",
               "yes" if window["inside"] else
               "NO — recording is scheduled to be idle right now",
               "ok" if window["inside"] else "info",
               f"hours {window['start_hour']:02d}:00-{window['stop_hour']:02d}:00 "
               f"from recording_schedule.env, now {window['hour_now']:02d}h "
               f"lab time; {day_note}")

    for unit in ("lidar-record-start.timer", "lidar-record-stop.timer"):
        summary = probe.timer_summary(unit)
        if "error" in summary:
            bundle.add(unit, "could not be read", "unknown", summary["error"])
            continue
        # `enabled` and `active` are separate facts on purpose. This timer was
        # found enabled, with its symlink in place, and inactive (dead) — so
        # nothing was going to fire it and no error said so anywhere.
        bad = summary["active"] != "active"
        detail = f"unit file state: {summary['enabled']}"
        if summary["last_trigger"]:
            detail += f"; last fired {summary['last_trigger']:%a %d %b %H:%M}"
        else:
            detail += "; no recorded last firing"
        detail += _next_firing(summary)
        if bad:
            detail += (" — being 'enabled' on disk does not mean systemd will "
                       "fire it")
        bundle.add(unit, f"{summary['active']} ({summary['sub']})",
                   "bad" if bad else "ok", detail)

    session = probe.recording_session()
    if "error" in session:
        bundle.add("Recording session", "could not be determined", "unknown",
                   session["error"])
    elif session["sessions"]:
        for entry in session["sessions"][:3]:
            started = entry.get("started")
            ran = (common.human_duration((common.now() - started).total_seconds())
                   if started else "unknown")
            bundle.add("Recording session",
                       f"{entry['session'] or '(no --base-session in cmdline)'} "
                       f"running for {ran}", "ok",
                       f"record_supervisor.py pid {entry['pid']} ON PUGET "
                       f"(the lab server), measured now. No recorder process "
                       f"runs on a Jetson")
    else:
        inside = window["inside"]
        bundle.add("Recording session",
                   "no record_supervisor.py process on puget",
                   "bad" if inside else "info",
                   "measured now on the lab server, which is where recording "
                   "runs" + ("" if inside else
                             " — and the window above says it is not due"))

    refusal = probe.start_refusal(rec_conf)
    if refusal["present"]:
        bundle.add("Start refusal marker", refusal["text"][:300], "bad",
                   f"{refusal['path']}, written "
                   f"{refusal['mtime']:%Y-%m-%d %H:%M}. record_day.sh writes "
                   f"this instead of starting, and nothing else surfaces it")
    else:
        bundle.add("Start refusal marker", "absent", "ok",
                   f"{refusal['path']} does not exist, so the last start "
                   f"attempt did not refuse — it may also never have run")

    disk = probe.recording_disk(conf, rec_conf)
    if "error" in disk:
        bundle.add("Recording disk", "could not be measured", "unknown",
                   disk["error"])
    else:
        bundle.add("Recording disk headroom",
                   f"{disk['free_gb']} GB free at {disk['path']}",
                   "ok" if disk["above_start_threshold"] else "bad",
                   f"a new day needs >= {disk['start_threshold_gb']:.0f} GB "
                   f"(RECORD_START_THRESHOLD_GB) to start; a running session "
                   f"stops below {disk['floor_gb']:.0f} GB "
                   f"(RECORD_MIN_FREE_GB). Currently "
                   f"{'above' if disk['above_start_threshold'] else 'BELOW'} "
                   f"the start threshold and "
                   f"{'above' if disk['above_floor'] else 'BELOW'} the floor")

    excluded = (rec_conf.get("RECORD_EXCLUDE_NODES") or "").strip()
    if excluded:
        bundle.add("Start-gate exclusions", excluded, "warn",
                   "RECORD_EXCLUDE_NODES. These nodes cannot veto the start of "
                   "a recording day; they are still recorded from and still "
                   "archived. A stale exclusion silently accepts a dead node")
    else:
        bundle.add("Start-gate exclusions", "none", "ok",
                   "every node must be publishing for a day to start")

    archive = probe.archive_result(rec_conf)
    if archive["present"] and archive["results"]:
        last = archive["results"][0]
        findings = last.get("quality_findings") or []
        bundle.add("Last archived session",
                   f"{last.get('session', '?')}, "
                   f"{(last.get('bytes') or 0) / 1e9:.1f} GB, "
                   f"sha256_verified={last.get('sha256_verified')}, "
                   f"integrity_ok={last.get('integrity_ok')}",
                   "ok" if last.get("integrity_ok") else "warn",
                   f"read from {archive['path']}, written "
                   f"{archive['mtime']:%Y-%m-%d %H:%M}" if archive["mtime"]
                   else archive["path"])
        if findings:
            bundle.add("Last session quality findings",
                       " | ".join(str(f)[:160] for f in findings[:4]), "warn",
                       "the archive step's own report on gaps in that session")


# -- alerts ---------------------------------------------------------------

def _alerts(bundle: Bundle, state, node_name: str | None) -> None:
    flags = probe.alert_flags(state)
    open_keys = sorted(k for k, v in flags.items() if v.get("active"))
    if node_name:
        open_keys = [k for k in open_keys if k.endswith(f"_{node_name}")]

    if open_keys:
        described = []
        for key in open_keys:
            fired = flags[key].get("last_fired")
            described.append(f"{key}" + (f" (last fired {fired:%d %b %H:%M})"
                                         if fired else ""))
        bundle.add("Alerts the guardian believes are open", "; ".join(described),
                   "bad",
                   f"read from the active_* files in {state}, which is what "
                   f"monitor.sh itself keys on")
    else:
        bundle.add("Alerts the guardian believes are open", "none", "ok",
                   f"no active_* flag is set in {state}")

    alerts = probe.recent_alerts(state, hours=24)
    if node_name:
        alerts = [a for a in alerts if node_name in a[1]]
    if not alerts:
        bundle.add("Alerts in the last 24h",
                   "none" + (f" mentioning {node_name}" if node_name else ""),
                   "ok", f"from {state}/alerts.log")
        return
    shown = alerts[-8:]
    bundle.add("Alerts in the last 24h", f"{len(alerts)}", "warn",
               "; ".join(f"{when:%H:%M} {subject}" for when, subject in shown)
               + (f" (+{len(alerts) - len(shown)} earlier)"
                  if len(alerts) > len(shown) else ""))
