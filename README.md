# lidar-node-guardian

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
git clone https://github.com/<you>/lidar-node-guardian.git
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
git clone https://github.com/<you>/lidar-node-guardian.git
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
| RECOVERED: … | both | Matching all-clear for any of the above |

## Files

```
bin/            lib.sh, lidar_power.sh, boot_sequence.sh, watchdog.sh   (Jetson)
server/         monitor.sh, alert.sh                                    (lab server)
systemd/        guardian-boot, guardian-watchdog(.timer), guardian-monitor(.timer)
config/         *.example templates — real configs are gitignored
```

Logs: node `/var/lib/lidar-guardian/guardian.log`; server
`~/.lidar-guardian-server/{monitor,alerts}.log`.
