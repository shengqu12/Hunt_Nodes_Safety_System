#!/usr/bin/env python3
"""Tests for the ask-bot's probes and config handling.

Standard-library unittest, no pytest, so the lab server deploy still needs no
pip for anything but slack_sdk.

    python3 tests/test_guardian.py

Every case here is either a bug that actually happened in this fleet or a rule
the design depends on. Anything that can fail *silently* gets a test — the
failures this system is built against are the ones that look like health.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import askbot                                # noqa: E402
from lib import actions, common, diagnose, probe   # noqa: E402


class TestConfigParsing(unittest.TestCase):
    """server.env is read by bash *and* by us, so we must agree with bash."""

    def test_quoted_value_with_trailing_comment(self):
        # The real server.env ships exactly this line. A parser that strips
        # quotes by testing the first and last character keeps both quotes and
        # the comment, and the value silently becomes garbage. In the sibling
        # NAS guardian that pattern disabled a free-space floor from the day it
        # was deployed, with no error anywhere.
        conf = common.parse_env_text(
            'MAIL_TO="shengq@andrew.cmu.edu"   # TODO: append Kieran\'s address')
        self.assertEqual(conf["MAIL_TO"], "shengq@andrew.cmu.edu")

    def test_unquoted_value_with_quotes_inside_the_comment(self):
        # server.env: DOWN_THRESHOLD=3  # consecutive failed pings => "node down"
        conf = common.parse_env_text(
            'DOWN_THRESHOLD=3            # consecutive pings => "node down" alert')
        self.assertEqual(conf["DOWN_THRESHOLD"], "3")
        self.assertEqual(common.conf_int(conf, "DOWN_THRESHOLD", 99), 3)

    def test_home_is_expanded(self):
        conf = common.parse_env_text('SERVER_STATE_DIR="$HOME/.lidar-guardian-server"')
        self.assertEqual(conf["SERVER_STATE_DIR"],
                         f"{Path.home()}/.lidar-guardian-server")

    def test_comments_exports_and_junk(self):
        conf = common.parse_env_text(
            "# a comment\n\nexport FOO=bar\nnot a config line\n1BAD=x\n")
        self.assertEqual(conf, {"FOO": "bar"})

    def test_bad_number_falls_back_instead_of_raising(self):
        conf = {"HEARTBEAT_MAX_AGE": "not-a-number"}
        self.assertEqual(common.conf_int(conf, "HEARTBEAT_MAX_AGE", 300), 300)


class TestNodeInventory(unittest.TestCase):
    NODES = ("# comment\nnode1 100.117.138.28  kelrod\n"
             "node2 100.68.225.87   kelrod\n\nbroken\n")

    def _load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.list"
            path.write_text(self.NODES)
            return common.load_nodes({}, path)

    def test_parses_and_skips_comments_and_short_lines(self):
        nodes = self._load()
        self.assertEqual([n["name"] for n in nodes], ["node1", "node2"])
        self.assertEqual(nodes[0]["user"], "kelrod")

    def test_node_named_only_matches_a_configured_node(self):
        nodes = self._load()
        self.assertEqual(common.node_named("why is node2 down?", nodes), "node2")
        # node9 does not exist. Answering about it anyway would invent a node.
        self.assertIsNone(common.node_named("what about node9", nodes))
        self.assertIsNone(common.node_named("status", nodes))


class TestSystemdParsing(unittest.TestCase):
    def test_human_timestamp_is_parsed(self):
        # This is the format `systemctl show` actually prints, on systemd 255,
        # even with --timestamp=unix. Reading it as an integer gives None for
        # every timestamp, and the probe then reported lidar-record-stop.timer
        # — which had fired the previous evening — as never having fired.
        when = probe.parse_timestamp("Mon 2026-09-14 21:00:33 EDT")
        self.assertIsNotNone(when)
        self.assertEqual((when.year, when.month, when.day), (2026, 9, 14))

    def test_unset_timestamps_are_none_not_dates(self):
        # systemd writes an unset timestamp as empty, as 0, or as 2**64-1.
        # The last one converts to the year 586524, which formats fine and
        # reads as a real "next firing" — the exact shape of fact this system
        # must not produce.
        for raw in ("", "   ", "n/a", "0", "infinity",
                    "18446744073709551615"):
            self.assertIsNone(probe.parse_timestamp(raw), raw)

    def test_unix_and_usec_forms(self):
        self.assertEqual(probe.parse_timestamp("@1789511091").year, 2026)
        self.assertEqual(probe.parse_timestamp("1789511091000000").year, 2026)

    def test_unparseable_string_is_none_not_a_guess(self):
        self.assertIsNone(probe.parse_timestamp("sometime last Tuesday"))


class TestTailscaleParsing(unittest.TestCase):
    def test_go_zero_time_is_not_a_date(self):
        # Tailscale reports LastSeen "0001-01-01T00:00:00Z" for a peer that is
        # currently ONLINE — Go's zero time, meaning "not applicable".
        # astimezone() on it underflows datetime.min west of UTC, and the
        # OverflowError took out the entire fleet bundle: the answer came back
        # with no nodes in it at all, which reads like a quiet fleet.
        self.assertIsNone(probe._parse_rfc3339("0001-01-01T00:00:00Z"))

    def test_a_real_last_seen_parses(self):
        when = probe._parse_rfc3339("2026-09-15T17:44:03Z")
        self.assertIsNotNone(when)
        self.assertEqual(when.year, 2026)


class TestTimerSummary(unittest.TestCase):
    """A monotonic timer has no wall-clock next firing. It is still firing."""

    def setUp(self):
        self._real = probe.unit_state

    def tearDown(self):
        probe.unit_state = self._real

    def _summary(self, **props):
        probe.unit_state = lambda unit: dict(
            {"LoadState": "loaded", "ActiveState": "active",
             "SubState": "waiting", "UnitFileState": "enabled"}, **props)
        return probe.timer_summary("x.timer")

    def test_monotonic_timer_is_not_reported_as_unscheduled(self):
        # guardian-monitor.timer uses OnUnitActiveSec, so NextElapseUSecRealtime
        # is empty while the timer fires every minute. Calling that "NO next
        # firing scheduled" states the opposite of the truth.
        out = self._summary(NextElapseUSecRealtime="",
                            NextElapseUSecMonotonic="1month 2w 5d 22h 36min")
        self.assertIsNone(out["next_elapse"])
        self.assertTrue(out["next_monotonic"])
        self.assertTrue(out["will_fire"])

    def test_inactive_timer_really_has_no_next_firing(self):
        # lidar-record-start.timer, found enabled on disk and inactive in
        # systemd: nothing fires it, and that is the fact worth alerting on.
        out = self._summary(ActiveState="inactive", SubState="dead",
                            UnitFileState="linked",
                            NextElapseUSecRealtime="",
                            NextElapseUSecMonotonic="infinity")
        self.assertIsNone(out["next_elapse"])
        self.assertEqual(out["next_monotonic"], "")
        self.assertFalse(out["will_fire"])

    def test_calendar_timer_has_a_wall_clock_next_firing(self):
        out = self._summary(
            NextElapseUSecRealtime="Wed 2026-09-16 08:30:00 EDT",
            NextElapseUSecMonotonic="0")
        self.assertIsNotNone(out["next_elapse"])
        self.assertTrue(out["will_fire"])


class TestCalendarDays(unittest.TestCase):
    def test_range_expands(self):
        days = probe._calendar_days("{ OnCalendar=Mon..Thu *-*-* 07:00:00 }")
        self.assertEqual(days, {"Mon", "Tue", "Wed", "Thu"})

    def test_wrapping_range(self):
        self.assertEqual(probe._calendar_days("Fri..Mon *-*-* 07:00:00"),
                         {"Fri", "Sat", "Sun", "Mon"})

    def test_unreadable_calendar_is_none_not_everyday(self):
        # None means "could not narrow it down". If this returned all seven
        # days instead, an unreadable timer would become "recording is
        # scheduled today" and the monitor would alert every weekend.
        self.assertIsNone(probe._calendar_days(""))
        self.assertIsNone(probe._calendar_days("*-*-* 07:00:00"))


class TestRecordingWindow(unittest.TestCase):
    def setUp(self):
        self._real = probe.timer_summary

    def tearDown(self):
        probe.timer_summary = self._real

    def _window(self, calendar, when):
        probe.timer_summary = lambda unit: {"unit": unit, "calendar": calendar}
        return probe.recording_window(
            {"RECORD_START_HOUR": "7", "RECORD_STOP_HOUR": "21"}, when)

    def test_inside_on_a_scheduled_day(self):
        tue = dt.datetime(2026, 9, 15, 18, 0).astimezone()
        self.assertTrue(self._window("Mon..Thu *-*-* 07:00:00", tue)["inside"])

    def test_outside_on_an_unscheduled_day(self):
        sat = dt.datetime(2026, 9, 19, 18, 0).astimezone()
        window = self._window("Mon..Thu *-*-* 07:00:00", sat)
        self.assertFalse(window["inside"])
        self.assertFalse(window["day_ok"])

    def test_outside_the_hours(self):
        tue_night = dt.datetime(2026, 9, 15, 23, 0).astimezone()
        self.assertFalse(self._window("Mon..Thu *-*-* 07:00:00", tue_night)["inside"])

    def test_unknown_days_does_not_become_a_claim(self):
        # day_ok is None, not False: we do not know. `inside` follows the hours
        # alone, and the caller is told the days are unknown so it can say so.
        tue = dt.datetime(2026, 9, 15, 18, 0).astimezone()
        window = self._window("", tue)
        self.assertIsNone(window["day_ok"])
        self.assertIsNone(window["days"])


class TestRecordingSession(unittest.TestCase):
    def setUp(self):
        self._real = probe.common.run

    def tearDown(self):
        probe.common.run = self._real

    def _pgrep(self, output):
        def fake(argv, timeout=15.0):
            if argv[0] == "pgrep":
                return 0, output, ""
            return 1, "", ""             # ps lstart: no start time
        probe.common.run = fake
        return probe.recording_session()

    def test_ignores_its_own_shell_wrapper(self):
        # pgrep -f matches full command lines, so the ssh/bash wrapper that
        # invoked this bot contains the pattern and matches. record_day.sh hit
        # exactly this and waited ten minutes on a process that was itself.
        out = self._pgrep(
            "2574534 bash -c pgrep -af record_supervisor.py; echo done\n")
        self.assertEqual(out["sessions"], [])

    def test_finds_a_real_supervisor_and_its_session(self):
        out = self._pgrep(
            "2318894 /home/shengq/lidar_venv/bin/python3 -u "
            "pipeline/00_start_driver_rosbridge/record_supervisor.py "
            "--base-session geo_20260914_070041 --mode per-node\n")
        self.assertEqual(len(out["sessions"]), 1)
        self.assertEqual(out["sessions"][0]["session"], "geo_20260914_070041")

    def test_no_match_is_empty_not_an_error(self):
        # pgrep exits 1 when nothing matched. That is the normal "not
        # recording" answer, not a broken probe.
        def fake(argv, timeout=15.0):
            return 1, "", ""
        probe.common.run = fake
        self.assertEqual(probe.recording_session(), {"sessions": []})


class TestMonitorLog(unittest.TestCase):
    LOG = (
        "2026-09-15 18:21:10 node1 ping fail 21/3\n"
        "2026-09-15 18:21:11 node2 ok: lidar=ok disk=47%\n"
        "2026-09-15 18:23:10 node1 ping fail 22/3\n"
        "2026-09-15 18:23:11 node2 ok: lidar=ok disk=47%\n"
        "2026-09-15 18:25:11 node2 ok: lidar=ok disk=47%\n"
        "unparseable rubbish\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / "monitor.log").write_text(self.LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_both_line_shapes(self):
        rows = probe.monitor_log(self.state)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0][1:3], ("node1", "pingfail"))
        self.assertEqual(rows[1][1:3], ("node2", "ok"))

    def test_pass_intervals_are_measured_not_assumed(self):
        # The timer asks for 60s. On this host, with a node down, passes were
        # actually ~120s apart. The probe must report what it measured and say
        # which node's lines it measured from.
        cadence = probe.pass_intervals(probe.monitor_log(self.state))
        self.assertEqual(cadence["node"], "node2")
        self.assertEqual(cadence["gaps"], [120.0, 120.0])

    def test_last_seen_per_node(self):
        seen = probe.last_seen_by_monitor(probe.monitor_log(self.state))
        self.assertEqual(set(seen), {"node1", "node2"})
        self.assertEqual(seen["node2"][0].strftime("%H:%M:%S"), "18:25:11")


class TestAlertState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_what_monitor_sh_actually_writes(self):
        (self.state / "active_down_node1").write_text("1\n")
        (self.state / "active_lidar_node5").write_text("0\n")
        (self.state / "cool_down_node1").write_text("1789509910\n")
        (self.state / "pingfail_node1").write_text("22\n")
        flags = probe.alert_flags(self.state)
        self.assertTrue(flags["down_node1"]["active"])
        self.assertFalse(flags["lidar_node5"]["active"])
        self.assertIsNotNone(flags["down_node1"]["last_fired"])
        self.assertEqual(flags["ping_node1"]["consecutive_ping_fails"], 22)

    def test_recent_alerts_window(self):
        now = common.now()
        old = (now - dt.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        new = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        (self.state / "alerts.log").write_text(
            f"{old} ALERT: ancient history\n"
            f"{new} ALERT: NODE DOWN: node1 unreachable\n"
            "garbage line\n")
        alerts = probe.recent_alerts(self.state, hours=24)
        self.assertEqual([subject for _, subject in alerts],
                         ["NODE DOWN: node1 unreachable"])


class TestRecordingDisk(unittest.TestCase):
    def test_matches_record_day_sh_arithmetic(self):
        # record_day.sh gates on int(shutil.disk_usage(...).free / 1e9). If we
        # computed GiB instead, this probe would disagree with the gate it is
        # reporting on — and would say "enough room" on a day that refuses.
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            out = probe.recording_disk(
                {"RECORDING_DATA_DIR": tmp},
                {"RECORD_START_THRESHOLD_GB": "145",
                 "RECORD_MIN_FREE_GB": "35"})
            expected = int(shutil.disk_usage(tmp).free / 1e9)
        self.assertEqual(out["free_gb"], expected)
        self.assertEqual(out["start_threshold_gb"], 145.0)
        self.assertEqual(out["floor_gb"], 35.0)

    def test_unset_path_is_an_error_not_a_zero(self):
        out = probe.recording_disk({}, {})
        self.assertIn("error", out)
        self.assertNotIn("free_gb", out)


class TestNodeProbe(unittest.TestCase):
    """The heartbeat-age rule: whose clock measured it."""

    REMOTE = (
        "NODE_EPOCH=1789511100\n"
        "UPTIME_SECS=365940\n"
        "STATUS=eyJub2RlIjogIm5vZGUyIiwgInRpbWVzdGFtcCI6IDE3ODk1MTEwOTAsICJsaW"
        "Rhcl9zdGF0dXMiOiAib2siLCAiZGlza191c2VkX3BjdCI6IDQ3fQ==\n"
        "UDP56301=bound\n"
        "WATCHDOG_ACTIVE=active\n"
        "WATCHDOG_LAST_EPOCH=1789511085\n"
        "BOOTUNIT=active\n"
        "DF_PCT=47\n"
        "DF_FREE_KB=100000000\n"
        "END=1\n"
    )

    def setUp(self):
        self._real = probe.common.run

    def tearDown(self):
        probe.common.run = self._real

    def _probe(self, ping_rc=0, ssh_out=None, ssh_rc=0):
        self.sent = {}

        def fake(argv, timeout=15.0, stdin_text=None):
            if argv[0] == "ping":
                return ping_rc, "", ""
            if argv[0] == "ssh":
                self.sent["stdin"] = stdin_text
                self.sent["argv"] = argv
                return ssh_rc, (self.REMOTE if ssh_out is None else ssh_out), ""
            return 1, "", ""
        probe.common.run = fake
        return probe.node_probe({"name": "node2", "ip": "10.0.0.2",
                                 "user": "kelrod"}, {})

    def test_the_script_is_actually_fed_to_the_remote_shell(self):
        # `sh -s` reads its script from stdin. With stdin closed, ssh exits 0
        # having run nothing, and all seven nodes report "pings but SSH
        # returned nothing usable" — a fleet-wide outage that exists only in
        # the probe. The first version of this shipped exactly that.
        self._probe()
        self.assertIn("sh -s", self.sent["argv"][-1])
        self.assertIsNotNone(self.sent["stdin"])
        self.assertIn("END=1", self.sent["stdin"])

    def test_heartbeat_age_is_measured_against_the_nodes_own_clock(self):
        # status.json's timestamp was written by the node. Comparing it to the
        # lab server's clock silently folds any clock skew into the staleness
        # verdict; the guardian's bash check does exactly that and cannot say
        # so. Here the age is 10s against the node's clock, and the skew is a
        # separate, labelled fact.
        res = self._probe()
        self.assertEqual(res["heartbeat_age_secs"], 10)
        self.assertEqual(res["heartbeat_age_ref"], "the node's own clock")
        self.assertIn("clock_skew_secs", res)
        # The watchdog's last firing is converted to epoch ON the node, by the
        # date(1) that formatted it, so this age is node-clock to node-clock.
        self.assertEqual(res["watchdog_last_age_secs"], 15)

    def test_ping_failure_never_claims_anything_about_ssh(self):
        res = self._probe(ping_rc=1)
        self.assertFalse(res["ping_ok"])
        self.assertFalse(res["ssh_ok"])
        self.assertNotIn("status", res)

    def test_truncated_ssh_output_is_a_failed_probe_not_empty_facts(self):
        # Without the END=1 sentinel a half-written response would parse as
        # "everything absent", which reads identically to "everything broken".
        res = self._probe(ssh_out="NODE_EPOCH=1789511100\nUDP56301=bound\n")
        self.assertFalse(res["ssh_ok"])
        self.assertIn("error", res)

    def test_missing_status_json_is_distinct_from_a_stale_one(self):
        res = self._probe(ssh_out="NODE_EPOCH=1\nSTATUS_ERR=no such file\n"
                                  "UDP56301=bound\nEND=1\n")
        self.assertTrue(res["ssh_ok"])
        self.assertEqual(res["status_error"], "no such file")
        self.assertNotIn("heartbeat_age_secs", res)


class TestSectionIsolation(unittest.TestCase):
    def setUp(self):
        self._real = diagnose._fleet

    def tearDown(self):
        diagnose._fleet = self._real

    def test_one_broken_section_does_not_empty_the_bundle(self):
        # A stray OverflowError in the Tailscale parse used to take the whole
        # bundle with it, leaving an answer with no nodes in it — which reads
        # like a quiet fleet rather than a broken probe.
        def boom(*a, **k):
            raise OverflowError("date value out of range")
        diagnose._fleet = boom
        bundle = diagnose.collect({"SERVER_STATE_DIR": "/nonexistent"}, "status")
        self.assertTrue(any("fleet probes raised OverflowError" in e
                            for e in bundle.errors))
        # and the failure is visible in the evidence, never as silence
        self.assertIn("[??] probe failed", bundle.as_text())
        self.assertIn("MISSING from this evidence", bundle.as_text())


class TestDuplicateDelivery(unittest.TestCase):
    """One @mention in a channel arrives as TWO events."""

    def test_app_mention_and_message_collapse_to_one_answer(self):
        # Slack delivers app_mention AND message.channels for the same
        # message. Both were answered, 120ms apart, and the pair hitting a
        # cold ollama concurrently is what made the second fail with HTTP 500.
        seen = askbot.SeenEvents()
        mention = {"type": "app_mention", "channel": "C1", "ts": "1789.1",
                   "client_msg_id": "abc-123"}
        message = {"type": "message", "channel": "C1", "ts": "1789.1",
                   "client_msg_id": "abc-123"}
        self.assertTrue(seen.claim(askbot.event_key(mention)))
        self.assertFalse(seen.claim(askbot.event_key(message)))

    def test_slack_redelivery_is_also_suppressed(self):
        seen = askbot.SeenEvents()
        event = {"channel": "C1", "ts": "1789.1"}     # no client_msg_id
        self.assertEqual(askbot.event_key(event), "C1:1789.1")
        self.assertTrue(seen.claim(askbot.event_key(event)))
        self.assertFalse(seen.claim(askbot.event_key(event)))

    def test_distinct_messages_are_both_answered(self):
        seen = askbot.SeenEvents()
        self.assertTrue(seen.claim("a"))
        self.assertTrue(seen.claim("b"))

    def test_an_unidentifiable_event_is_answered_rather_than_dropped(self):
        # Answering twice is a nuisance; never answering is a broken bot.
        seen = askbot.SeenEvents()
        self.assertTrue(seen.claim(""))
        self.assertTrue(seen.claim(""))

    def test_memory_is_bounded(self):
        seen = askbot.SeenEvents(limit=3)
        for i in range(10):
            seen.claim(f"k{i}")
        self.assertEqual(len(seen._seen), 3)
        self.assertTrue(seen.claim("k0"))      # oldest was evicted


class TestEvidenceBudget(unittest.TestCase):
    """Evidence exists so a human can check the answer."""

    def _bundle(self):
        bundle = diagnose.Bundle(topic="status")
        for i in range(40):
            bundle.add(f"node{i} something", "fine", "ok", "x" * 120)
        bundle.add("node3 lidar_status", "failed", "bad", "the thing that matters")
        bundle.add("Recording session", "none", "warn", "also matters")
        return bundle

    def test_no_budget_keeps_everything(self):
        bundle = self._bundle()
        self.assertEqual(len(bundle.as_text().splitlines()), 42)

    def test_unhealthy_facts_survive_the_budget(self):
        # Truncating in order threw away three quarters of a status bundle and
        # kept the healthy start of it. What matters must survive.
        text = self._bundle().as_text(max_chars=1200)
        self.assertIn("node3 lidar_status: failed", text)
        self.assertIn("Recording session: none", text)

    def test_dropped_facts_are_announced_never_silent(self):
        # A silently cut message is indistinguishable from a quiet fleet.
        text = self._bundle().as_text(max_chars=1200)
        self.assertIn("40 further checks were all OK", text)

    def test_a_hard_cut_says_so_and_says_where_the_rest_is(self):
        text = self._bundle().as_text(max_chars=200)
        self.assertIn("TRUNCATED", text)
        self.assertIn("--probe status", text)
        self.assertLessEqual(len(text.split("... TRUNCATED")[0]), 200)


class TestFleetGrouping(unittest.TestCase):
    """A fault on every node is one fact, not seven."""

    def setUp(self):
        self._nodes, self._ts = probe.probe_nodes, probe.tailscale_peers
        probe.tailscale_peers = lambda: {"peers": {}}

    def tearDown(self):
        probe.probe_nodes, probe.tailscale_peers = self._nodes, self._ts

    def _node(self, name, **over):
        base = {"name": name, "ip": f"10.0.0.{name[-1]}", "user": "kelrod",
                "ping_ok": True, "ssh_ok": True, "udp56301": "bound",
                "watchdog_timer": "active", "clock_skew_secs": 0,
                "heartbeat_age_secs": 10,
                "status": {"lidar_status": "ok", "disk_used_pct": 47,
                           "cpu_temp_c": 50, "note": ""}}
        base.update(over)
        return base

    def _collect(self, results):
        probe.probe_nodes = lambda nodes, conf, timeout=20.0: results
        bundle = diagnose.Bundle(topic="status")
        nodes = [{"name": r["name"], "ip": r["ip"], "user": "kelrod"}
                 for r in results]
        diagnose._fleet(bundle, {}, nodes, "/nonexistent", detailed=False)
        return bundle

    def test_one_symptom_on_six_nodes_is_one_fact(self):
        # Six identical 180-character "UDP 56301 unbound" lines cost most of
        # the evidence budget and read as six unrelated problems.
        results = [self._node(f"node{i}", udp56301="unbound")
                   for i in range(1, 7)] + [self._node("node7")]
        bundle = self._collect(results)
        unbound = [f for f in bundle.facts if "UDP 56301 unbound" in f.label]
        self.assertEqual(len(unbound), 1)
        self.assertIn("[6 of 7]", unbound[0].value)
        self.assertIn("node1", unbound[0].value)

    def test_healthy_nodes_are_summarised_not_expanded(self):
        bundle = self._collect([self._node(f"node{i}") for i in range(1, 8)])
        labels = [f.label for f in bundle.facts]
        self.assertIn("Nodes with nothing wrong", labels)
        # No per-node expansion at all when nothing is wrong.
        self.assertFalse([l for l in labels if "heartbeat" in l])

    def test_distinct_symptoms_stay_distinct(self):
        results = [
            self._node("node1", udp56301="unbound"),
            self._node("node2", status={"lidar_status": "failed",
                                        "disk_used_pct": 47, "cpu_temp_c": 50,
                                        "note": ""}),
            self._node("node3", ping_ok=False, ssh_ok=False),
        ]
        bundle = self._collect(results)
        labels = " | ".join(f.label for f in bundle.facts)
        self.assertIn("UDP 56301 unbound", labels)
        self.assertIn("lidar_status=failed", labels)
        self.assertIn("no ping reply", labels)

    def test_a_down_node_reports_nothing_further_about_itself(self):
        # Ping failed, so every other reading is unknown, not ok.
        bundle = self._collect([self._node("node1", ping_ok=False,
                                           ssh_ok=False)])
        labels = " | ".join(f.label for f in bundle.facts)
        self.assertIn("no ping reply", labels)
        self.assertNotIn("UDP 56301", labels)
        self.assertNotIn("nothing wrong", labels)


class TestNoteFolding(unittest.TestCase):
    def test_notes_differing_only_by_address_fold_to_one(self):
        # Five nodes wrote the same sentence with a different IP in it. Printed
        # separately that is 600 characters saying one thing five times, and it
        # reads as five findings.
        folded = diagnose._fold_notes([
            ("node2", "no ping reply from LiDAR (192.168.1.130) - monitor-only"),
            ("node4", "no ping reply from LiDAR (192.168.1.131) - monitor-only"),
        ])
        self.assertEqual(folded.count("no ping reply"), 1)
        self.assertIn("node2 192.168.1.130", folded)
        self.assertIn("node4 192.168.1.131", folded)

    def test_a_genuinely_different_note_stays_separate(self):
        folded = diagnose._fold_notes([
            ("node2", "no ping reply from LiDAR (192.168.1.130)"),
            ("node7", "recovered after 2 power cycles"),
        ])
        self.assertIn("recovered after 2 power cycles", folded)
        self.assertIn("no ping reply", folded)
        self.assertIn("|", folded)


class TestPowerParsing(unittest.TestCase):
    """The only code path that decides whether hardware gets touched."""

    NODES = [{"name": f"node{i}", "ip": f"10.0.0.{i}", "user": "kelrod"}
             for i in range(1, 8)]

    def _parse(self, text):
        return actions.parse(text, self.NODES)

    def test_chinese_and_english_power_on(self):
        for text in ("启动 lidar 4、5", "power on lidar 4 and 5",
                     "lidar 4,5 上电", "turn on lidars 4 5"):
            req = self._parse(text)
            self.assertIsNotNone(req, text)
            self.assertEqual(req.verb, "on", text)
            self.assertEqual([n["name"] for n in req.nodes],
                             ["node4", "node5"], text)

    def test_off_and_cycle(self):
        self.assertEqual(self._parse("关掉 lidar 2 的电源").verb, "off")
        self.assertEqual(self._parse("重启 lidar 4 的雷达").verb, "cycle")
        self.assertEqual(self._parse("power cycle lidar 7").verb, "cycle")

    def test_all(self):
        req = self._parse("power on all lidars")
        self.assertEqual(len(req.nodes), 7)

    def test_a_question_is_never_an_action(self):
        # These fall through to the question path. Parsing one as a power
        # action would switch hardware because somebody asked about it.
        for text in ("why is node5 down?", "how are the lidars now?",
                     "status", "node4 的 lidar 怎么了", "lidar 4 为什么 failed"):
            self.assertIsNone(self._parse(text), text)

    def test_a_number_must_be_attached_to_a_node_or_lidar(self):
        # "restart in 5 minutes" must not become "restart node5".
        self.assertIsNone(self._parse("restart the lidar pipeline in 5 minutes"))
        self.assertIsNone(self._parse("power cycle it in 4 hours"))

    def test_other_subsystems_are_not_hardware(self):
        # Recording, units and the bot are all things people say "restart" about.
        for text in ("restart the recording", "restart the lidar recording",
                     "start the session for lidar 4", "restart the bot",
                     "restart lidar-record-start.timer"):
            self.assertIsNone(self._parse(text), text)

    def test_unconfigured_nodes_are_dropped(self):
        # node9 does not exist. Acting on "whatever was meant" is not an option.
        self.assertIsNone(self._parse("power on lidar 9"))
        req = self._parse("power on lidar 4 and 9")
        self.assertEqual([n["name"] for n in req.nodes], ["node4"])

    def test_no_verb_no_action(self):
        self.assertIsNone(self._parse("lidar 4"))
        self.assertIsNone(self._parse(""))


class TestPowerExecution(unittest.TestCase):
    def setUp(self):
        self._probe = probe.node_probe
        self._run = actions.common.run

    def tearDown(self):
        probe.node_probe = self._probe
        actions.common.run = self._run

    def _node(self, **over):
        base = {"name": "node4", "ip": "10.0.0.4", "user": "kelrod",
                "ssh_ok": True, "ping_ok": True, "relay_tty": "/dev/ttyUSB0",
                "uptime_secs": 50000, "load1": "0.3", "boot_id": "aaa",
                "lidar_carrier": "0"}
        base.update(over)
        return base

    def test_refuses_a_node_with_no_relay(self):
        probe.node_probe = lambda n, c, timeout=20.0: self._node(relay_tty="")
        req = actions.PowerRequest("on", [{"name": "node4", "ip": "10.0.0.4",
                                          "user": "kelrod"}])
        out = actions.execute(req, {}, Path(tempfile.mkdtemp()))
        self.assertEqual(out[0]["outcome"], "refused")
        self.assertIn("no relay serial device", out[0]["why"])

    def test_refuses_a_node_that_has_just_booted(self):
        probe.node_probe = lambda n, c, timeout=20.0: self._node(uptime_secs=10)
        req = actions.PowerRequest("on", [{"name": "node4", "ip": "10.0.0.4",
                                          "user": "kelrod"}])
        out = actions.execute(req, {}, Path(tempfile.mkdtemp()))
        self.assertEqual(out[0]["outcome"], "refused")
        self.assertIn("up 10s", out[0]["why"])

    def test_a_node_reset_is_its_own_outcome_not_a_failure(self):
        # Observed twice on node5: the LiDAR's 18 W inrush resets the Jetson.
        # Reporting that as "failed" would send someone to look at the relay.
        calls = {"n": 0}

        def fake_probe(node, conf, timeout=20.0):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._node()                       # before
            return self._node(boot_id="bbb", uptime_secs=20)   # rebooted
        probe.node_probe = fake_probe
        actions.common.run = lambda argv, timeout=15.0, stdin_text=None: (0, "", "")
        req = actions.PowerRequest("on", [{"name": "node5", "ip": "10.0.0.5",
                                          "user": "kelrod"}])
        out = actions.execute(req, {"POWER_SETTLE_SECS": "4"}, Path(tempfile.mkdtemp()))
        self.assertEqual(out[0]["outcome"], "node_reset")
        self.assertIn("18 W", out[0]["why"])

    def test_success_is_measured_carrier_not_a_successful_write(self):
        calls = {"n": 0}

        def fake_probe(node, conf, timeout=20.0):
            calls["n"] += 1
            return self._node(lidar_carrier="0" if calls["n"] == 1 else "1",
                              lidar_link_ip="192.168.1.5/24")
        probe.node_probe = fake_probe
        actions.common.run = lambda argv, timeout=15.0, stdin_text=None: (0, "", "")
        req = actions.PowerRequest("on", [{"name": "node4", "ip": "10.0.0.4",
                                          "user": "kelrod"}])
        out = actions.execute(req, {"POWER_SETTLE_SECS": "8"}, Path(tempfile.mkdtemp()))
        self.assertEqual(out[0]["outcome"], "ok")
        self.assertEqual(out[0]["carrier_after"], "1")

    def test_a_written_command_with_no_effect_is_not_success(self):
        # The relay has no readback. "The bytes went out" is not the claim.
        probe.node_probe = lambda n, c, timeout=20.0: self._node()
        actions.common.run = lambda argv, timeout=15.0, stdin_text=None: (0, "", "")
        req = actions.PowerRequest("on", [{"name": "node4", "ip": "10.0.0.4",
                                          "user": "kelrod"}])
        out = actions.execute(req, {"POWER_SETTLE_SECS": "4"}, Path(tempfile.mkdtemp()))
        self.assertEqual(out[0]["outcome"], "no_change")


class TestBundle(unittest.TestCase):
    def test_worst_ranks_bad_over_warn_over_unknown(self):
        bundle = diagnose.Bundle(topic="t")
        bundle.add("a", "1", "ok")
        self.assertEqual(bundle.worst, "ok")
        bundle.add("b", "2", "unknown")
        self.assertEqual(bundle.worst, "unknown")
        bundle.add("c", "3", "warn")
        self.assertEqual(bundle.worst, "warn")
        bundle.add("d", "4", "bad")
        self.assertEqual(bundle.worst, "bad")

    def test_evidence_text_marks_and_keeps_details(self):
        bundle = diagnose.Bundle(topic="t")
        bundle.add("Recording session", "none", "bad", "measured on puget")
        bundle.errors.append("tailscale unreadable")
        text = bundle.as_text()
        self.assertIn("[BAD] Recording session: none  (measured on puget)", text)
        # A failed probe appears as unknown, never as a healthy silence.
        self.assertIn("[??] probe failed: tailscale unreadable", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
