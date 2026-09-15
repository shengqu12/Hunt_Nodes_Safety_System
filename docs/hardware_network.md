# Hardware and Network

The physical nodes, campus networking, clock synchronisation, and the NAS. Fleet data verified by direct measurement on 2026-09-14 (read-only sweep of every node: discovery responses, configs, bound ports, publish rates). **This file is the lookup reference — check here before touching any node.**

## Quick-reference: fleet table (verified 2026-09-14, rates re-verified 2026-09-15)

Seven Jetsons, all carrying **Livox MID-360S** (dev_type = 35). SSH user on all nodes: kelrod. ROS_DOMAIN_ID = node number.

| Node | Hostname | Tailscale IP | Campus IP\* | ROS domain | Sensor serial | Sensor IP | Rate (Hz) | Guardian | USB power switch |
| :-- | :-- | :-- | :-- | :-: | :-- | :-- | :-: | :-- | :-- |
| node1 | jetson-nano-01 | 100.117.138.28 | 172.26.16.127 | 1 | ARMCN8V0032944 | 192.168.1.144 | 10.00 | ✓ installed | ✓ installed (pilot unit) |
| node2 | jetson-nano-02 | 100.68.225.87 | 172.26.165.1 | 2 | ARMCN920035130 | 192.168.1.130 | 9.98 | ✓ installed | pending (kernel patch + hw) |
| node3 | jetson-nano-03 | 100.123.173.20 | 172.26.105.175 | 3 | ARMCP540030943 | 192.168.1.143 | 10.01 | ✓ installed | pending |
| node4 | jetson-nano-04 | 100.73.100.67 | 172.26.165.38 | 4 | ARMCN910034431 | 192.168.1.131 | 10.00 | ✓ installed | pending |
| node5 | jetson-nano-05 | 100.73.72.30 | 172.26.109.251 | 5 | ARMCN8V0032512 | 192.168.1.112 | 10.02 | ✓ installed | pending |
| node6 | jetson-nano-06 | 100.97.13.90 | 172.26.63.8 | 6 | ARMCP540030538 | 192.168.1.138 | 9.92 | ✓ installed | pending |
| node7 | jetson-nano-07 | 100.117.178.115 | 172.26.96.72 | 7 | ARMCP5M0030259 | **192.168.113.158** ⚠ | 9.97 | ✓ installed | pending |

\* Campus IPs are DHCP snapshots from the sweep date and **will drift** — always address nodes by Tailscale IP.

Sensor-IP rule: MID-360S factory IP is 192.168.1.1XX, XX = last two digits of the serial. node1–6 all match. node7 deliberately does not (see below).

**ROS_DOMAIN_ID pitfall:** any remote ros2 command must export ROS_DOMAIN_ID=\<node number\> first, or it silently sees no topics. Set in ~/.bashrc on node1–6; node7's process runs domain 7 but .bashrc does not set it (origin unconfirmed).

## Measuring publish rate — read this before quoting a number

**`ros2 topic hz` prints a cumulative average, not an instantaneous rate.** Its first window includes DDS discovery latency, so the first line it prints is biased low and then climbs monotonically as the average converges. **Never take the first reading, and in particular never use `grep -m1`.** Sample for at least 20–30 s and take the *last* line, or watch the whole progression.

Worked example (node1, 2026-09-15, both topics sampled concurrently for 60 s):

```
downstream  9.969 → 10.000     never dipped, whole 60 s
upstream    7.574 → 9.950      cumulative average converging, NOT a real slowdown
```

The `7.574` is an artifact of the first window alone. Steady state over the same run was 9.896–9.950 upstream and 9.995–10.000 downstream.

The artifact is **intermittent** — six dedicated first-window re-tests on the same topics all returned 9.98–10.21 — and it is worse when the machine is loaded, because discovery takes longer. Quoting a first-window number has already produced one bogus fleet-wide finding (see node4 below).

Preferred aliveness probe, immune to all of this: **UDP 56301 BOUND on the host** means the driver handshake succeeded (see the constants table).

## Running geo_filter manually — always set OPENBLAS_NUM_THREADS=1

```bash
cd ~/geo_filter_temp && source /opt/ros/humble/setup.bash && \
  OPENBLAS_NUM_THREADS=1 ROS_DOMAIN_ID=<N> setsid nohup \
  python3 geo_filter_node.py --node node<N> --assets geo_filter_assets.npz \
  >> /tmp/geo_filter.log 2>&1 < /dev/null &
```

Without the env var, OpenBLAS oversubscribes the 6-core Jetson: it spawns ~16 threads for numpy work whose matrices are far too small to benefit, so scheduling overhead dominates. Measured on node1, 2026-09-15, changing **only** this variable:

| | without | with `OPENBLAS_NUM_THREADS=1` |
| :-- | :-: | :-: |
| geo_filter CPU | 260–340% (≈3 cores) | **40–67% (≈0.5 core)** |
| process threads | 16 | 11 |
| system load | 5.04 → 3.55 | 2.35 → **1.32** |
| /livox/lidar steady state | 9.896–9.950 | **9.998–10.000** |
| /livox/lidar_geofiltered steady state | 9.995–10.000 | 9.996–9.999 |

Six times the CPU for **no** throughput gain — slightly worse, in fact. The hourly launcher used on node4 already sets it; hand-started instances are the ones that get this wrong.

## Driver / sensor configuration constants

Identical on node1–6; node7 column lists its (apparently deliberate) deviations.

| Item | node1–6 | node7 ⚠ |
| :-- | :-- | :-- |
| Sensor model | MID-360S (dev_type 35) | same |
| Launch file | msg_MID360s_launch.py | same |
| Config file | MID360s_config.json | same file name, different values |
| Sensor subnet | 192.168.1.0/24 | 192.168.113.0/24 |
| Host IP on sensor net | 192.168.1.5 (all six identical) | 192.168.113.203 |
| lidar_net_info.cmd_data_port | 56100 | 56200 |
| lidar_net_info.push_msg_port | 56200 | 56201 |
| host_net_info.cmd_data_port | 56101 | 56200 |
| Host point-data port (BOUND when handshake OK) | 56301 | 56301 |
| pcl_data_type | 1 | 3 (different point format/precision) |
| Sensor MAC OUI | 8c:58:23 (Livox) | 9c:5a:8a (not in discovery whitelist) |
| Mounting | ceiling, roll 180° | same |
| Point rate nominal | ~200k pts/s, 10 Hz, ~19,968 pts/frame | same class |
| Topics | /livox/lidar (PointCloud2, 26 B/pt, frame livox_frame), /livox/imu (~200 Hz), /livox/lidar_geofiltered (~10 Hz where geo_filter runs) | same |

**Health check one-liner:** a driver whose handshake succeeded has UDP 56301 BOUND on the host — this works regardless of DDS/domain issues and is the preferred aliveness probe.

**Do not use** the legacy MID360_config.json / msg_MID360_launch.py still present on some disks — they target the non-S MID-360 and cause the silent discovery-loop failure (see node1 incident below).

## Data-quality status per node (2026-09-14 audits)

From the 1,963-sample track-foot audit against the measured cloud floor (−2.61 m) and the floor-plane review. "Below-floor %" = track feet >5 cm under the floor — a proxy for that node's deployed floor-plane error.

| Node | Below-floor % | Foot p50 (m) | Floor-plane status | Notes |
| :-- | :-: | :-: | :-- | :-- |
| node1 | n/a (no tracks in region during audit) | n/a | unverified after restart | re-check plane + neighbour overlap before re-adding to recording |
| node2 | 46.4% | −2.650 | suspect | needs re-fit |
| node3 | 12.9% | −2.582 | acceptable | best of fleet |
| node4 | 66.5% | −2.700 | **bad — bimodal plane** | worst offender; recalibrate (the 7.24 Hz rate concern was withdrawn — see below) |
| node5 | 35.3% | −2.647 | suspect | needs re-fit |
| node6 | 0.0% | −2.280 | **bad — plane ~0.3 m high** | zero below-floor only because plane is too high |
| node7 | 63.1% | −2.724 | **bad — ~11.8 cm off** | cannot wall-ICP directly vs fused reference; registered pairwise through node4 |

Consequence: recorded tracklet z is unreliable fleet-wide until planes are re-fit ("realign nodes" task). x-y positions are unaffected. Do not use height-dependent analyses (stand/sit, stature) on data recorded before the re-fit.

### node4's "7.24 Hz" — withdrawn, suspected measurement artifact

The 7.24 Hz figure recorded for node4 in the 2026-09-14 sweep is **not believed to be real**. It was produced by `timeout 14 ros2 topic hz … | grep -m1 'average rate'`, i.e. the first-window cumulative average — exactly the reading the section above warns against.

Re-measured on 2026-09-14 and again on 2026-09-15 using steady-state windows: **10.00 Hz every time** (10.014 / 10.016 / 9.952 / 10.025 / 10.000 / 10.000). Three independent diagnostic passes found:

- sensor side delivering the full stream — 2081 point pkt/s and 200.0 IMU pkt/s, matching the node1 control to within 0.02%;
- kernel dropping nothing — `rx_dropped`/`rx_errors`/`rx_missed`/`rx_crc` all zero and static, `/proc/net/udp` drops = 0 with an empty receive queue on both 56301 and 56401;
- the driver not starved — 26–28% CPU with the host 82–86% idle at load 1.2.

Hardware is identical to the rest of the fleet (Orin Nano Super, 6 cores, 7.4 GB, 15 W mode), so the "weaker node" hypothesis is also out. A RELIABLE-QoS back-pressure hypothesis (slow geo_filter dragging the driver down) was tested directly on node1 and **disproven**: the downstream topic held 9.969–10.000 for a full 60 s while the upstream showed the same converging-average pattern.

The original event was never reproduced and left no logs, so this cannot be closed with certainty — but no evidence supports a real rate fault, and there is direct evidence that a ~7.5 reading occurs on a perfectly healthy link. **Do not block node4's floor-plane recalibration on it.**

Minor unrelated observation from the same passes: node4's CPU `scaling_max_freq` is capped at 1.4976 GHz where node1 runs 1.728 GHz, at the same 15 W nvpmodel and only 50–52 °C. Not a rate problem (node4 is 80%+ idle), but a fleet inconsistency worth a look.

## Safety & monitoring layer (Hunt_Nodes_Safety_System)

Repo: <https://github.com/shengqu12/Hunt_Nodes_Safety_System.git>

Per node: guardian-boot.service (staged power-up: Jetson first, LiDAR after stable — solves the boot brownout) + guardian-watchdog.timer (LiDAR health, bounded auto power-cycle, status.json heartbeat at /var/lib/lidar-guardian/). Lab-server side (puget-260273, user shengq): guardian-monitor.timer pings Tailscale IPs, reads heartbeats, and alerts to Slack; guardian-report.timer posts a morning brief at 08:30.

Rollout state: guardian installed on **all seven nodes**. node1 is the only one with power-control hardware (`POWER_BACKEND="cmd"`, LCUS relay on /dev/ttyUSB0); node2–7 run `POWER_BACKEND="none"` — monitor-only, alerting instead of power-cycling — until the kernel patch + relay hardware land (Kieran).

## The node1 incident (Sept 2026) — model/launch mismatch

node1 sat in a silent discovery loop producing no data because it was started with the legacy MID-360 launch/config against its MID-360S sensor: the driver receives discovery replies but silently discards the point stream (observed as tens of thousands of "packets to unknown port"). Fix: launch with msg_MID360s_launch.py. There was no hardware swap; an earlier "sensors were physically swapped" hypothesis was disproven by the fleet serial sweep.

## node7 is deliberately nonstandard — do not "normalise" it casually

The deviations in the constants table are self-consistent (host and sensor on the same nonstandard subnet), which reads as intentional configuration, but the intent is undocumented. **Ask Kieran before changing anything.** Open questions: why the separate subnet/ports; is pcl_data_type: 3 intentional (and does it relate to node7's wall-ICP failure); should OUI 9c:5a:8a be added to discover_livox_sensor.py's whitelist (until then the discovery script fails on node7).

## Open items (as of 2026-09-15)

1. **Floor-plane re-fit** for node4/6/7 first, then node2/5 ("realign nodes"). This is now the top data-quality item.
2. **node7 questions** → Kieran (see above).
3. **Kernel patch + relay hardware** on node2–7 → Kieran; flip each node's `POWER_BACKEND` from `none` to the real backend as hardware lands.
4. **Chrony topology** below predates the 7-node fleet — verify.
5. node1 plane/overlap re-check, then re-add to recording.
6. `RECORDER_PATTERN` in the lab server's `server.env` is still the default `ros2 bag record`; confirm it against a live recorder command line before trusting the morning brief's "Recording" line.
7. node4's CPU clock cap (1.4976 vs 1.728 GHz) — cosmetic/unexplained, low priority.
8. *(downgraded)* node4 @ 7.24 Hz — withdrawn as a suspected measurement artifact; see the section above. Re-open only if a **steady-state** measurement shows it again.

## Campus networking and Tailscale

The Jetsons connect over CMU campus WiFi. Campus DHCP reassigns addresses and static IPs are not possible, so **Tailscale is the canonical address** for every node. Register each Jetson's MAC on CMU-DEVICE rather than relying on CMU-SECURE association. A node going unreachable fails fused operations by design; the guardian layer above is what turns that into an alert instead of a silent gap.

## Clock synchronisation

Fusing LiDARs requires the nodes' clocks to agree with **each other**, not merely each be "synced" to something: nodes independently locked to different NTP sources drift relative to one another while all reporting healthy. The arrangement is chrony with **node1 as the time server and the other nodes chained to it** (sub-millisecond RMS on the original pair). ⚠ Verify this holds for all of node2–7.

## The NAS

Recorded bags, tracklet CSVs, and background models are archived to a Synology DS1525+ at 172.24.72.224, pushed over rsync. Authentication uses sshpass with the password in ~/.nas_password (chmod 600); the archiver is a no-op when that file is absent.

### NAS safety rule — do not violate

A Synology holding millions of files freezes for ~30 minutes if asked to walk a large directory. **Never run `ls`, `find`, `du`, `wc`, or `df` against NAS directories.** Safe operations only:

- `stat` / `test -d` / `test -f` on a **named** path
- `mkdir -p`
- `df` on the /volume1 **mount point only**
- directory listing via **os.scandir pushed over SSH** only
- **rsync push, local → NAS only**

The archiver scripts (nas_archive.py, post_record_hook.py) already restrict themselves to these. Do not add ad-hoc NAS shell commands to any script. Write to a location you have permission for (a personal home under /volume1/homes/… is writable); resolve a shared-folder target with Kieran for the long-term archive.
