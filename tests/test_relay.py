#!/usr/bin/env python3
"""Relay-failure tests: the node scripts and the server monitor, run as real bash with stubbed tools.

The bug these pin down (2026-11-10, reproduced on the bench): guardian.env named the relay by
/dev/serial/by-path/...-usb-0:2.3:1.0-port0, which is one physical USB jack. With the relay in any
other jack, `stty -F` failed, lidar_power.sh returned 1, the relay got nothing -- and the log said
only "Powering LiDAR ON". guardian-boot ignored the exit status, the watchdog counted a power
cycle that never happened, and status.json said nothing the server could alert on.

Stubs, first on PATH: `ping` (exit $PING_RC), `sleep` (instant), `stty` (fails like the real
one when the -F device is missing, or with $STTY_FAIL set), `ssh` (prints $SSH_STATUS_FILE),
`curl` (no network). The "relay device" is a file in a temp dir; the commands have the same
shape as the fleet's POWER_ON_CMD / POWER_OFF_CMD.

    python3 tests/test_relay.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
SERVER = REPO / "server"

MISSING_ALERT = ("RELAY USB MISSING on nodeT — relay command failed, "
                 "LiDAR power cannot be controlled")

STUBS = {
    "ping": 'exit "${PING_RC:-1}"\n',
    "sleep": "exit 0\n",
    "curl": "exit 0\n",
    "stty": textwrap.dedent("""\
        dev=""
        while (( $# )); do [[ "$1" == -F ]] && { dev="$2"; shift; }; shift; done
        [[ -e "$dev" ]] || { echo "stty: $dev: No such file or directory" >&2; exit 1; }
        [[ -n "${STTY_FAIL:-}" ]] && { echo "stty: $dev: Input/output error" >&2; exit 1; }
        exit 0
        """),
    "ssh": 'cat "$SSH_STATUS_FILE"\n',
}


def _cmd(dev: Path, payload: str) -> str:
    # The fleet's command, with the device swapped for the temp file.
    return (f"stty -F {dev} 9600 raw -echo && sleep 0.3 && "
            f"printf '{payload}' > {dev}")


class Node:
    """A throwaway node: guardian.env, state dir, stub tools, relay device file."""

    def __init__(self, tmp: Path, **env_over):
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        for name, body in STUBS.items():
            p = self.stubs / name
            p.write_text("#!/usr/bin/env bash\n" + body)
            p.chmod(0o755)
        self.dev = tmp / "serial" / "usb-1a86_USB_Serial-if00-port0"
        self.dev.parent.mkdir()
        conf = {
            "NODE_NAME": "nodeT",
            "LIDAR_IP": "192.0.2.1",
            "POWER_BACKEND": "cmd",
            "POWER_ON_CMD": _cmd(self.dev, r"\\xA0\\x01\\x01\\xA2"),
            "POWER_OFF_CMD": _cmd(self.dev, r"\\xA0\\x01\\x00\\xA1"),
            "FAIL_THRESHOLD": "1",
            "MAX_POWER_CYCLES": "3",
            "POWER_CYCLE_OFF_SECS": "0",
            "POST_CYCLE_WAIT_SECS": "0",
            "MIN_UPTIME_SECS": "0",
            "STABLE_LOAD_MAX": "1000",
            "NETWORK_WAIT_TRIES": "1",
            "PRE_LIDAR_DELAY_SECS": "0",
            "LIDAR_BOOT_TIMEOUT_SECS": "2",
            "ENSURE_TAILSCALE": "0",
            "SLACK_WEBHOOK_URL": "",
            "DISK_WARN_PCT": "101",
            "TEMP_WARN_C": "1000",
        }
        conf.update(env_over)
        self.env_file = tmp / "guardian.env"
        self.env_file.write_text("".join(f'{k}="{v}"\n' for k, v in conf.items()))

    def plug(self):
        self.dev.write_bytes(b"")

    def unplug(self):
        if self.dev.exists():
            self.dev.unlink()

    def run(self, script: str, *args, **extra_env) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(PATH=f"{self.stubs}:{env['PATH']}", CONFIG_FILE=str(self.env_file),
                   STATE_DIR=str(self.state), GUARDIAN_HOME=str(self.tmp))
        env.update({k: str(v) for k, v in extra_env.items()})
        return subprocess.run(["bash", str(BIN / script), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    @property
    def log(self) -> str:
        f = self.state / "guardian.log"
        return f.read_text(errors="replace") if f.exists() else ""

    def kv(self, key: str, default: str = "") -> str:
        f = self.state / f"kv_{key}"
        return f.read_text().strip() if f.exists() else default

    def status(self) -> dict:
        return json.loads((self.state / "status.json").read_text())


class _NodeCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.node = Node(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()


class TestLidarPower(_NodeCase):
    def test_missing_device_fails_loudly(self):
        self.node.unplug()
        r = self.node.run("lidar_power.sh", "on")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Powering LiDAR ON", self.node.log)            # intent is still logged
        self.assertIn("[ERROR] relay command failed:", self.node.log)
        self.assertIn(str(self.node.dev), self.node.log)               # and it names the device
        self.assertNotIn("relay command OK", self.node.log)
        self.assertNotEqual(self.node.kv("lidar_power"), "on")

    def test_command_error_is_logged_with_its_stderr(self):
        self.node.plug()
        r = self.node.run("lidar_power.sh", "off", STTY_FAIL=1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[ERROR] relay command failed: stty: ", self.node.log)
        self.assertIn("Input/output error", self.node.log)
        self.assertNotIn("relay command OK", self.node.log)

    def test_success_logs_ok_only_after_the_bytes_are_written(self):
        self.node.plug()
        r = self.node.run("lidar_power.sh", "on")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.node.log
        self.assertIn("Powering LiDAR ON", log)
        self.assertIn("relay command OK", log)
        self.assertLess(log.index("Powering LiDAR ON"), log.index("relay command OK"))
        self.assertNotIn("[ERROR]", log)
        self.assertEqual(self.node.dev.read_bytes(), b"\xa0\x01\x01\xa2")
        self.assertEqual(self.node.kv("lidar_power"), "on")

    def test_cycle_with_missing_device_exits_non_zero(self):
        self.node.unplug()
        r = self.node.run("lidar_power.sh", "cycle")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[ERROR] relay command failed:", self.node.log)


class TestWatchdogRelay(_NodeCase):
    def setUp(self):
        super().setUp()
        (self.node.state / "kv_boot_complete").write_text("1\n")

    def test_failed_cycle_is_relay_missing_not_a_power_cycle(self):
        self.node.unplug()
        r = self.node.run("watchdog.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.node.status()["lidar_status"], "relay_missing")
        note = self.node.status()["note"]
        self.assertIn("relay command failed: device ", note)
        self.assertEqual(note, note.strip())                         # no dangling space in the alert text
        self.assertIn(MISSING_ALERT, self.node.log)
        self.assertEqual(self.node.kv("cycles_in_window", "0"), "0")

    def test_relay_alert_is_raised_once_not_every_minute(self):
        self.node.unplug()
        for _ in range(3):
            self.node.run("watchdog.sh")
        self.assertEqual(self.node.log.count(MISSING_ALERT), 1)
        self.assertEqual(self.node.kv("cycles_in_window", "0"), "0")

    def test_replugged_relay_recovers_and_then_counts(self):
        self.node.unplug()
        self.node.run("watchdog.sh")
        self.node.plug()
        r = self.node.run("watchdog.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RECOVERED: relay on nodeT", self.node.log)
        self.assertNotEqual(self.node.status()["lidar_status"], "relay_missing")
        self.assertEqual(self.node.kv("cycles_in_window", "0"), "1")
        self.assertEqual(self.node.status()["power_cycles_recent"], 1)   # status.json agrees


class TestBootRelay(_NodeCase):
    def test_boot_with_missing_relay_says_so(self):
        self.node.unplug()
        r = self.node.run("boot_sequence.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.node.status()
        self.assertEqual(st["lidar_status"], "relay_missing")
        self.assertIn(MISSING_ALERT, self.node.log)
        self.assertEqual(self.node.kv("boot_complete"), "1")       # the watchdog still takes over

    def test_boot_with_relay_present_powers_on(self):
        self.node.plug()
        r = self.node.run("boot_sequence.sh", PING_RC=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.node.status()["lidar_status"], "ok")
        self.assertIn("relay command OK", self.node.log)
        self.assertNotIn("RELAY USB MISSING", self.node.log)


class TestMonitorRelay(unittest.TestCase):
    """server/monitor.sh against one stub node whose status.json says relay_missing."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        tmp = Path(self._td.name)
        self.node = Node(tmp)                       # for the stubs only
        (tmp / "config").mkdir()
        (tmp / "config" / "server.env").write_text('RECORDING_ALERTS="0"\nMAIL_TO=""\nSLACK_WEBHOOK_URL=""\n')
        (tmp / "config" / "nodes.list").write_text("nodeT 192.0.2.10 kelrod\n")
        self.sstate = tmp / "server_state"
        self.status_file = tmp / "status.json"
        self.tmp = tmp

    def tearDown(self):
        self._td.cleanup()

    def _pass(self, lidar_status: str, note: str = ""):
        self.status_file.write_text(json.dumps({
            "node": "nodeT", "timestamp": int(time.time()), "lidar_status": lidar_status,
            "consecutive_fails": 4, "disk_used_pct": 40, "note": note}, indent=2))
        env = dict(os.environ)
        env.update(PATH=f"{self.node.stubs}:{env['PATH']}", GUARDIAN_HOME=str(self.tmp),
                   SERVER_STATE_DIR=str(self.sstate), SSH_STATUS_FILE=str(self.status_file),
                   PING_RC="0")
        r = subprocess.run(["bash", str(SERVER / "monitor.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _alerts(self) -> str:
        f = self.sstate / "alerts.log"
        return f.read_text() if f.exists() else ""

    def test_relay_missing_alerts_with_cooldown_and_recovers(self):
        self._pass("relay_missing", "relay command failed: device /dev/serial/x does not exist")
        self.assertEqual(self._alerts().count("RELAY USB MISSING on nodeT"), 1)
        self._pass("relay_missing")                                  # inside the cooldown
        self.assertEqual(self._alerts().count("RELAY USB MISSING on nodeT"), 1)
        self._pass("ok")
        self.assertIn("RECOVERED: relay on nodeT", self._alerts())
        self.assertEqual((self.sstate / "active_relay_nodeT").read_text().strip(), "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
