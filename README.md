# Hunt_Nodes_Safety_System

> **Fleet reference:** the authoritative hardware/network document is
> [`docs/hardware_network.md`](docs/hardware_network.md) in this repo.
> The copies on Google Drive (`hardware_network.md`, `hardware_network_v2.md`)
> are **archived and out of date** — do not operate from them.

Staged power-up and fleet-wide health monitoring for ceiling-LiDAR Jetson
nodes (Livox MID-360/360S on Jetson Nano, CMU Hunt Library deployment).

Solves two problems:

1. **Brownout at boot.** Jetson + LiDAR starting together can exceed the power
   budget. A USB-controlled switch now sits on the LiDAR's power line; this
   repo boots the Jetson first, waits until it is stable, then powers the
   LiDAR — automatically, on every boot.
2. **Silent failures.** If the LiDAR stops responding, the node first tries to
   fix itself (bounded power cycles); if a node drops off the network entirely,
   the lab server notices and emails the team. Recovery notices are sent too,
   so you know when it healed itself.

## Architecture

```
 ┌────────────── Jetson (each node) ──────────────┐      ┌───── lab server ─────┐
 │ guardian-boot.service   (once per boot)        │      │ guardian-monitor      │
 │   LiDAR OFF → wait stable → LiDAR ON → verify  │      │   (every 60 s)        │
 │                                                │ ssh/ │  ping Tailscale IP    │
 │ guardian-watchdog.timer (every 60 s)           │◄─────│  read status.json     │
 │   ping LiDAR (+ optional ROS topic check)      │ ping │  stale? failed? down? │
 │   fail ×3 → power cycle (max 3/hour) → alert   │      │   → email + Slack     │
 │   writes status.json heartbeat                 │      │   → recovery notices  │
 └────────────────────────────────────────────────┘      └──────────────────────┘
```

Division of labour: the **node** detects and self-heals LiDAR problems; the
**server** is the authority on "the Jetson itself is down" (a dead node cannot
report its own death) and on delivering email.

Safety properties:

- Power cycling is **bounded** (default 3 per hour). After that the node marks
  itself `failed`, alerts once, and waits for a human — no infinite off/on
  loops that could mask or worsen a hardware fault.
- Alerts are **deduplicated** with a cooldown, and every alert has a matching
  recovery notice.
- The boot sequence always starts by forcing the LiDAR **off**, so state is
  deterministic even after a power outage.
- Extras: disk-usage warning (leftover rosbags have filled Jetson disks
  before) and CPU-temperature warning.

## Install on a Jetson

```bash
git clone https://github.com/shengqu12/Hunt_Nodes_Safety_System.git
cd lidar-node-guardian
sudo ./install_node.sh
sudo nano /opt/lidar-guardian/config/guardian.env   # NODE_NAME, LIDAR_IP, POWER_BACKEND
```

Identify the USB switch backend first:

```bash
lsusb                 # 16c0:05df → usbrelay-style HID relay
sudo apt install usbrelay && sudo usbrelay      # prints IDs like HURTM_1
sudo uhubctl          # if it's a power-switchable USB hub instead
```

Then test end-to-end **before** trusting a reboot:

```bash
sudo /opt/lidar-guardian/bin/lidar_power.sh off   # LiDAR should spin down
sudo /opt/lidar-guardian/bin/lidar_power.sh on
sudo systemctl start guardian-boot                # dry-run the full sequence
cat /var/lib/lidar-guardian/status.json
sudo reboot                                       # final verification
```

To auto-start the pipeline after the LiDAR is up, set `AUTO_START_CMD` in
`guardian.env` (e.g. the `launcher.py --start --node node1` command).

## Install on the lab server

```bash
git clone https://github.com/shengqu12/Hunt_Nodes_Safety_System.git
cd lidar-node-guardian
./install_server.sh
nano ~/lidar-node-guardian/config/nodes.list   # tailscale status → IPs
nano ~/lidar-node-guardian/config/server.env   # MAIL_TO=you,kieran
```

Email setup (once): `sudo apt install msmtp msmtp-mta`, copy
`config/msmtprc.example` to `~/.msmtprc` (`chmod 600`), fill in a Gmail app
password (or your SMTP of choice). Passwordless SSH to every node:
`ssh-copy-id jetson@<tailscale_ip>`. Then:

```bash
~/lidar-node-guardian/server/alert.sh "Test" "If you got this, alerts work."
```

## Adding a node

On the new Jetson: clone, `sudo ./install_node.sh`, edit `guardian.env`
(new `NODE_NAME`, its `LIDAR_IP`, its switch backend). On the server: add one
line to `nodes.list`. Done.

## Alert reference

| Alert | Source | Meaning |
|-------|--------|---------|
| LiDAR failed to start at boot | node | No ping after boot power-on; watchdog will retry |
| LiDAR auto-recovered by power cycle | node | Transient failure, fixed itself |
| LiDAR DOWN — auto-recovery exhausted | node+server | 3 cycles didn't help; go check the hardware |
| NODE DOWN: unreachable | server | Jetson lost power/WiFi/crashed |
| Watchdog heartbeat stale | server | Jetson up but guardian timer stopped |
| Disk N% full | node | Check for leftover rosbags |
| RECORDING NOT RUNNING | server | Inside the recording window with no session on the lab server |
| RECOVERED: … | both | Matching all-clear for any of the above |

## Daily morning brief

`guardian-report.timer` posts one Slack message at 08:30 with fleet health,
overnight alerts (and any still unresolved), what is recording, and the worst
disk/temperature figures. A healthy day is two or three lines; problems expand.

The brief is also the monitor's own heartbeat — if puget or the monitor dies,
no brief arrives, so silence stops being indistinguishable from health.

Test it without sending: `server/daily_report.sh --print`.

The `Recording:` line is measured **on the lab server**, by
`server/recording_status.sh`, because that is where recording runs:
`record_day.sh` launches `record_supervisor.py` there and it subscribes to the
nodes over the network. Nothing records on a Jetson. (This line used to
`pgrep` for the recorder on each node, where no such process has ever existed,
so it could only ever say "none active".) Run the probe on its own to see the
raw measurements:

```bash
server/recording_status.sh
```

## Slack ask-bot

`bin/askbot.py` answers questions in Slack — reply `why` under an alert, or
ask about a node, about recording, or about overnight alerts. It runs as
`guardian-askbot.service` in Socket Mode, because the lab server has no public
ingress and Slack cannot POST to it.

The model is never given a shell or any say in what gets looked at. A fixed
bundle of read-only probes collects the facts, the model explains them, and
the raw evidence is posted with every answer. The bot cannot change anything.

```bash
python3 bin/askbot.py --probe recording      # raw evidence, no model at all
python3 bin/askbot.py --probe node --node node3
python3 bin/askbot.py --ask "why is node3 down?"
python3 bin/askbot.py --check-slack          # validate the Slack app setup
python3 tests/test_guardian.py               # stdlib unittest, no pytest
```

Setup is in [`CLAUDE.md`](CLAUDE.md): paste `config/slack-app-manifest.yaml`
into <https://api.slack.com/apps>, collect the `xapp-`/`xoxb-` tokens, put
them in `config/server.env`, `/invite` the bot, and re-run
`./install_server.sh`. Without a token the installer skips the bot rather than
installing a unit that cannot start.

## Gotcha: ssh inside a `while read` loop

`monitor.sh` and `daily_report.sh` call `ssh -n`. Without `-n`, ssh inherits
the loop's stdin, swallows the remaining lines of `nodes.list`, and the loop
exits after the first node — monitoring silently covers one node while looking
completely healthy. This bug shipped once; do not reintroduce it.

`-n` is only needed inside `while read` loops. In a `for` loop it is harmful:
it points stdin at /dev/null and cuts off any script you are piping to the
remote host.

## Files

```
bin/            lib.sh, lidar_power.sh, boot_sequence.sh, watchdog.sh   (Jetson)
bin/askbot.py   the Slack ask-bot                                       (lab server)
bin/lib/        probe.py, diagnose.py, llm.py, common.py — the bot's guts
server/         monitor.sh, daily_report.sh, alert.sh, recording_status.sh
systemd/        guardian-boot, guardian-watchdog(.timer), guardian-monitor(.timer),
                guardian-report(.timer), guardian-askbot.service
config/         *.example templates + slack-app-manifest.yaml
                — real configs are gitignored
tests/          test_guardian.py
```

Design notes and the reasoning behind all of it: [`CLAUDE.md`](CLAUDE.md).

Logs: node `/var/lib/lidar-guardian/guardian.log`; server
`~/.lidar-guardian-server/{monitor,alerts}.log`.
