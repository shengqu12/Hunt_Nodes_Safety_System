# Hunt LiDAR Node Guardian

Fleet health monitoring, a daily Slack brief, and a Slack ask-bot for the
ceiling-LiDAR Jetson fleet in CMU Hunt Library. Runs on the lab server as
systemd **user** timers.

## The one rule that shaped this design

**Every fact must name what was measured and on which machine.**

This system spans two kinds of host, and once a fact is text they read
identically:

- **puget** (`shengq@100.103.81.11`, `puget-260273`) — the lab server. It
  monitors the fleet, sends the brief, runs the bot, and is also where
  **recording** runs.
- **node1..node7** — Jetson Nanos on the ceiling, each with a Livox MID-360.
  Each runs a watchdog that writes a `status.json` heartbeat.

The morning brief shipped the failure this rule is against. Its `Recording:`
line ran `pgrep -f "ros2 bag record"` **on each Jetson**. No recorder has ever
run on a Jetson — `record_supervisor.py` runs on puget and subscribes over the
network — so the line could only ever print `none active`. It was not wrong so
much as meaningless, which is worse: it looked like an answer, and nobody
checked a green line.

The corollary, which matters most for the bot: **the model cannot check a fact
it is given.** An ambiguous fact is worse than a missing one.

## Fleet facts

Seven Jetsons, in `config/nodes.list` as `<name> <tailscale_ip> <ssh_user>`.
The ssh user is `kelrod`, and puget's key must be on every node or that node
produces no telemetry at all. Reached over Tailscale; the lab has no route to
them otherwise.

`/var/lib/lidar-guardian/status.json` on each node, rewritten by
`guardian-watchdog.timer` every 60s:

```json
{"node": "node2", "timestamp": 1789519378, "time_human": "...",
 "boot_status": "complete", "lidar_status": "ok|degraded|unknown|failed",
 "consecutive_fails": 0, "power_cycles_recent": 0,
 "disk_used_pct": 47, "cpu_temp_c": 50, "note": ""}
```

`lidar_status` is the **node's own verdict from its last watchdog run**, not a
live check, and it moves through `unknown` and `degraded` on the way to
`failed`. Several nodes run `POWER_BACKEND=none` (monitor-only, no relay
hardware): they detect a dead LiDAR and can do nothing about it, so `failed`
there means "waiting for a human", not "gave up after cycling".

### Carrier is the fact that ping cannot give you

Each LiDAR hangs off a USB-Ethernet adapter on its Jetson (`enx*`, RTL8153).
`/sys/class/net/<if>/carrier` is the physical layer: **1 means something is
powered at the far end of that cable.** It is local to the node, needs no
network round trip, and separates "the LiDAR has no power" from "the LiDAR is
powered but not answering at IP level".

On the night the relays were fitted, six nodes reported `lidar_status=failed`
and every node had UDP 56301 unbound. The model's reading of that was that the
LiDAR devices had failed and their power should be checked. The actual state
was `carrier=0`: the LiDARs were simply **switched off**, because the relays
had just been installed and are wired COM+NO, which is open when unpowered.
`lidar_status` and UDP 56301 are both downstream of carrier, so on a node with
carrier 0 they are consequences, not findings.

Powering a LiDAR takes carrier 0 → 1 in about 8 seconds, and the interface
then picks up its address (`192.168.1.5/24` on most nodes).

**node7 is on a different subnet and that is correct.** Its link comes up as
`192.168.113.203/24` and its configured `LIDAR_IP=192.168.113.158` answers
there, 0% loss. It looks like a typo for `192.168.1.158` and is not one;
measured 2026-09-16 after power-on.

### Tailscale knows things ping does not

`tailscale status --json` reports `Online` and `LastSeen` per peer. A node that
is `Online: false, LastSeen: 45m ago` is **off the network** — a different
fault from one whose pings are being dropped, and the bash guardian's
consecutive-ping counter cannot tell them apart.

Two traps in that JSON:

- An **online** peer carries `LastSeen: "0001-01-01T00:00:00Z"` — Go's zero
  time, meaning "not applicable". `astimezone()` on it underflows
  `datetime.min` west of UTC and raises `OverflowError`.
- The `Peer` map is keyed by node key, not by IP; index it by `TailscaleIPs`.

## The LiDAR power switches

Every node has an LCUS-style USB relay on a CH340 bridge (`1a86:7523`),
switching the LiDAR's 12 V line. Reference: `relay-control-reference.md` and
`jetson-ch341-relay-setup.md`.

- 9600 8N1 raw, four bytes: `0xA0`, channel, state, checksum. Channel 1 is the
  LiDAR. ON `A0 01 01 A2`, OFF `A0 01 00 A1`.
- Driven through `/dev/serial/by-path/platform-3610000.usb-usb-0:2.3:1.0-port0`,
  not `/dev/ttyUSB0`, which shifts if another serial device appears.
- **Write-only.** No acknowledgement, no state query, nothing to poll. The
  only feedback is a click and an LED. So "the bytes were written" is never
  reported as "the LiDAR came up" — carrier is.
- **Not persistent.** The relay drops open on reboot, USB disconnect or power
  loss, so a node that resets comes back with its LiDAR off.
- Idempotent: sending ON twice is a no-op at the coil.

### Two things that stop this working

**`brltty` steals the port.** It claims the CH340 about 1.6 seconds after the
device node appears:

```
ch341 1-2.3:1.0: ch341-uart converter detected
usb 1-2.3: ch341-uart converter now attached to ttyUSB0
usb 1-2.3: usbfs: interface 0 claimed by ch341 while 'brltty' sets config #1
ch341-uart ttyUSB0: ch341-uart converter now disconnected from ttyUSB0
```

`sudo apt remove brltty`, then reload `ch341`. This is why node2 had the
module installed and stashed and still had no `/dev/ttyUSB0`.

**JetPack 6 ships no `ch341.ko`.** Every node runs `5.15.148-tegra` and every
node's stashed module has the same vermagic, so installing it on a new node is
a copy from another node plus `depmod -a` — no git clone, no internet on the
Jetson. The vermagic gate against the local `ftdi_sio.ko` still runs per unit.

### A LiDAR answers ping long after its link comes up

On node6, reconnected from repair, the link carrier went 0 → 1 six seconds
after the relay closed — and the sensor did not answer ping at
`192.168.1.138` for a further **eight minutes**, while the node's watchdog
logged six consecutive failures. It then recovered on its own.

So `carrier=1` means the LiDAR has power, not that it is ready. The reference
notes' "allow ~10 s before expecting a point cloud" describes a warm device;
one that has been fully disconnected takes far longer to come up on IP. An
action that reports success on carrier is reporting the thing it measured,
which is correct — but a `lidar_status=failed` in the minutes after a
switch-on is not yet evidence of a fault.

### Powering a LiDAR can reset its Jetson

The MID-360 pulls **18 W for about 8 seconds** at startup. On **node5** that
resets the node — observed twice, at 9 hours uptime and 0.3 load, so it is a
12 V headroom fault and not a timing one. node1, node2, node3, node4 and node7
all survive the same transient.

This is the brownout the whole repo was built around, arriving from the other
direction: the staged boot sequence protects the Jetson at boot, and nothing
protected it against a switch-on later.

Consequences encoded in the code:

- `POWER_EXCLUDE_NODES="node5"` — the bot refuses to switch it, with the
  reason. A block that lives only in a document does not stop anyone typing
  "power on lidar 5".
- **node5 keeps `POWER_BACKEND=none`.** Setting it would make
  `guardian-boot.service` power the LiDAR on every boot, reset the node, and
  boot-loop. node2, node4 and node7 are set to `cmd` with the by-path device,
  matching node1.
- Verification polls **from the lab server**, never over a session held open
  on the node: an action that kills the host must not also kill the record of
  what it did. A changed `boot_id` is the signal, and it is reported as
  `node_reset` with the reason, never as "failed" — "failed" sends someone to
  look at the relay.

## Where recording actually runs

Not in this repo, and not on a node. `lidar_social_recognition_deploy` on
puget drives it:

| | |
|---|---|
| `lidar-record-start.timer` | Mon–Thu 07:00, then hourly 08:00–20:00 |
| `lidar-record-stop.timer` | 21:00 |
| `scripts/record_day.sh start` | pre-flight gates, then launches the supervisor |
| `record_supervisor.py` | on puget; spawns one local `record_clouds.py` per node and subscribes to `/livox/lidar_geofiltered` |
| `config/recording_schedule.env` | every threshold, gitignored, in that repo |

Two independent gates refuse a day, and both are otherwise silent:

- **`RECORD_START_THRESHOLD_GB`** (145). Below it, `record_day.sh` writes
  `/tmp/record_start_REFUSED` and returns. Distinct from
  **`RECORD_MIN_FREE_GB`** (35), which stops a session already running. Being
  below the first and above the second is an ordinary state meaning "today
  will not start", and collapsing them into one "disk ok" hides it.
- **The topic gate**, `check_geofiltered_rate.py`, unless the node is in
  `RECORD_EXCLUDE_NODES` — those nodes are still recorded from and still
  archived, they just cannot veto a start.

We read that repo's config rather than restating its numbers. A copied
threshold is a threshold that goes stale, and the free-space figure is
computed the way the gate computes it (`int(free / 1e9)`, not GiB) so the
probe's number and the gate's number are the same number.

## What is running right now

| Unit | Schedule | Purpose |
|---|---|---|
| `guardian-monitor.timer` | every 60s | probe, alert, record state |
| `guardian-report.timer` | 08:30 daily | the morning brief |
| `guardian-askbot.service` | always on | answers questions in Slack (Socket Mode) |

All are user units under `shengq@puget-260273`, surviving reboot via
`Linger=yes`. **No sudo on this account**, which is why these are user units
and not cron.

`guardian-askbot.service` is installed only once `SLACK_APP_TOKEN` is set; see
below. On the Jetsons, `guardian-boot.service` and `guardian-watchdog.timer`
are system units installed by `install_node.sh`.

**The monitor's real period is not 60s.** Measured gaps between passes are
~120s, and `guardian-monitor.service` is often in `activating` when sampled: a
pass takes longer than the timer's interval, mostly waiting on unreachable
nodes' pings. Every server-side fact is therefore up to ~2 minutes old, which
is why the bot reports the measured cadence and the age of the last pass
before anything else.

## Alerts the guardian sends

From `monitor.sh`, deduplicated by key with `ALERT_COOLDOWN_SECS` (1h), each
with a matching RECOVERED notice:

| Key | Condition |
|---|---|
| `down_<node>` | `DOWN_THRESHOLD` (3) consecutive failed pings |
| `nostatus_<node>` | pings, but `status.json` unreadable over SSH |
| `stale_<node>` | heartbeat older than `HEARTBEAT_MAX_AGE` (300s) |
| `lidar_<node>` | `lidar_status == failed` |
| `recording_down` | inside the window, no session on puget |

Delivery is `alert.sh`: msmtp plus a Slack **incoming webhook**. That webhook
can only send — see the ask-bot section.

`monitor.sh` logs nothing for a node that pings but whose `status.json` it
cannot read, so a node can go absent from `monitor.log` while being reachable.
The bot reports that as "unclassified", never as "down".

## The Slack ask-bot

`bin/askbot.py` answers questions in Slack: reply `why` under an alert, ask
about a node, about recording, or about overnight alerts.

**The model is never given a shell.** `lib/diagnose.py` runs a fixed bundle of
read-only probes for the topic, and the model's entire job is explaining that
evidence, which is posted alongside its answer so you can check it. Deciding
*what to look at* for a given alert was settled when the alert was written;
handing that decision to a small model buys nothing and costs a category of
failure — a fluent, plausible, wrong diagnosis is worse than none, because you
act on it.

The bot is **read-only**. It cannot restart a unit, retune a threshold or
touch a node. (The sibling NAS guardian allows bounded threshold changes; this
one deliberately does not, because its thresholds are read by bash scripts
that are sourced, not parsed.)

**Socket Mode, not the Events API.** The lab server is behind Tailscale with
no public ingress, so Slack cannot POST to it. The bot dials out over a
WebSocket: no port forward, no reverse proxy, no cert. This needs **its own
Slack app** — the alert webhook can only send.

**Backends** are pluggable (`LLM_BACKEND`): `ollama` with `gemma3:27b` keeps
fleet telemetry on the lab network and costs nothing (~3.5s end to end
including the probes, on an idle 4090); `claude` reasons better but sends the
evidence off-site.

**`slack_sdk` needs a venv** (`.venv/`, created by `install_server.sh`). It is
the only dependency outside the stdlib, and only the bot needs it — the bash
guardian still runs on bare `python3`.

**The bot is not load-bearing.** Its unit has no `OnFailure=` hook: a
crash-looping bot must not become a stream of Slack alerts about itself, and
the guardian does not depend on it.

### The probes

`--probe <topic>` prints the evidence without calling a model at all, which is
also how you check the bot when the model is the broken thing.

On puget, no network: guardian timer states, the **measured** pass cadence,
the age of the last pass, per-node last-seen from `monitor.log`, open alerts
read from the `active_*`/`cool_*` files `monitor.sh` itself keys on, and
`alerts.log`.

Per node, one ping plus one SSH, seven in parallel: Tailscale peer state; the
parsed heartbeat with its age; `guardian-watchdog.timer` on the Jetson; disk
measured *now* next to the node's own last report; UDP 56301; uptime; and
clock skew.

Recording, all on puget: inside-window, both record timers, the supervisor
process, the refusal marker, free space against both gates, the start-gate
exclusions, and the last archive result.

Rules these encode, each of which cost something to learn:

- **Heartbeat age is measured against the node's own clock**, and the skew is
  reported separately. The bash check compares the node's timestamp to the
  server's clock, which silently assumes they agree; a drifting Jetson makes
  "stale" wrong in whichever direction it drifted, and nothing says so.
- **`enabled` and `active` are separate facts.**
  `lidar-record-start.timer` was found `enabled`, symlinked, and
  `inactive (dead)` — firing nothing, logging nothing.
- **A monotonic timer is not an unscheduled one.** `OnUnitActiveSec` timers
  have no realtime next-elapse; calling that "no next firing" says the
  opposite of the truth about a timer firing every minute.
- **`Inside recording window`** exists so the bot does not call a Saturday
  breakage. Recording is *scheduled* idle most of the week.
- **Three-valued, not two.** `IN_WINDOW=unknown` means the schedule could not
  be read. It never alerts: turning "I cannot tell" into "recording is broken"
  is the one thing a monitor must not do, and it would fire every weekend.
- **Each bundle section is isolated.** One probe raising used to empty the
  whole bundle, and an answer with no nodes in it reads like a quiet fleet
  rather than a broken probe. A failed section now says its facts are MISSING.
- **Reachability is four outcomes, not one.** Ping fails / SSH refused /
  `status.json` absent / `status.json` stale have four different next steps,
  and collapsing them sends you to the wrong rack.

### Setting up the Slack app

Paste `config/slack-app-manifest.yaml` into **App Manifest** (YAML mode) at
<https://api.slack.com/apps> and save. That sets the scopes, the bot events
and Socket Mode together, in an order that cannot go wrong — the click-through
path makes you add scopes and then remember to reinstall, and forgetting that
leaves the bot silent with no error anywhere.

Slack keeps secrets out of manifests, so two steps stay manual:

| Where | What |
|---|---|
| Basic Information → App-Level Tokens | Generate Token and Scopes, add `connections:write` → the `xapp-` token. Shown once; copy it immediately. |
| OAuth & Permissions → Install to Workspace | → the `xoxb-` Bot User OAuth Token |

Then `/invite @LiDAR Guardian` in the alert channel — the step most often
missed — put both tokens in `config/server.env`, and run:

```bash
python3 bin/askbot.py --check-slack     # verify before installing
./install_server.sh
```

Without `SLACK_APP_TOKEN` the installer skips the bot rather than installing a
unit that cannot start.

Granted scopes are deliberately minimal. Notably **not** `channels:read`: the
bot reads history in channels it was invited to and never browses the
workspace. `--check-slack` therefore cannot list channels, and says so with
`[??]` rather than `[FAIL]` — anything it cannot verify is reported as
unverifiable. A checker that calls a correct configuration broken costs you
the time you would have spent on the real problem.

## Tests

`python3 tests/test_guardian.py` — stdlib unittest, no pytest, so the deploy
still needs no pip beyond `slack_sdk`.

Every case is either a bug that actually happened or a rule the design depends
on. Add a test for anything that can fail **silently** — this system's
failures are the ones that look like health. The ones that already have tests:

- `systemctl show` prints timestamps as human strings (`Mon 2026-09-14
  21:00:33 EDT`), not the microseconds the property name promises, and
  `--timestamp=unix` does not change it (systemd 255). Reading them as
  integers made every timestamp `None`, and the evidence said a timer that
  fired last night had "never fired". They now go back through `date(1)`.
- Tailscale's zero time, above.
- `ssh ... sh -s` reads its script from stdin. With stdin closed, ssh exits 0
  having run nothing, and all seven nodes reported "pings but SSH returned
  nothing usable" — a fleet-wide outage entirely inside the probe. The `END=1`
  sentinel is what makes a truncated response a failed probe rather than a
  page of blank facts.
- `systemctl is-active` exits non-zero for every state that is not active, so
  `systemctl ... || echo unknown` appends "unknown" to a perfectly good
  reading. Branch on the output, not the exit code.
- Config parsing: a quoted value ends at its closing quote and the rest is a
  comment. `server.env` ships `MAIL_TO="..."   # TODO: ...`. The
  first-and-last-character shortcut keeps both quotes, and the number then
  fails `int()` and falls back to a default — which in the sibling NAS
  guardian disabled a free-space floor from the day it was deployed.

## Deploying an update

`~/lidar-node-guardian` on puget **is a checkout of this repo**, on an SSH
remote. The repo is private and the server has no GitHub credential helper,
but its key is registered to the `shengqu12` account.

```bash
# laptop
git push

# lab server
ssh shengq@100.103.81.11
cd ~/lidar-node-guardian && git pull && ./install_server.sh
```

`config/server.env` and `config/nodes.list` are gitignored and never travel
through git. `install_server.sh` re-applies `chmod 600` to `server.env` on
every run and is safe to re-run.

### What the bot can change

Only a LiDAR's power switch, only on nodes named in the message, only through
`lib/actions.py`.

Intent is parsed by **rules**, never by the model — the same split as the rest
of this design. gemma3:27b twice in one evening asserted things its evidence
did not say, once recommending that power be checked on nodes that were
reporting normally; a component that can be confidently wrong must not be the
one that cuts power to hardware. A message that does not parse unambiguously
does nothing and asks. "restart the recording", "restart in 5 minutes" and
"why is lidar 4 failed" all fall through to the question path, and a node
number only counts when it is attached to a word that names a node.

`on` runs directly: idempotent, safe in direction, and what people type in a
hurry. `off` and `cycle` wait for `confirm`, and both are refused while a
recording session is running — the person in Slack cannot see that a capture
is in progress, and it is their day's data.

Every action appends to `actions.log` with the request text, the parse, and
the measured outcome.

## Open problems this does not fix

- **Recording will not start tomorrow.** 106 GB free against a 145 GB start
  threshold; `/tmp/record_start_REFUSED` is already written. The likely cause
  is in the other repo: `RECORD_ARCHIVE_RECLAIM=0` while the comment directly
  above it says "RESTORED TO 1, 2026-08-18" — archived sessions are verified
  and uploaded but never reclaimed, so the disk only grows. Freeing ~40 GB, or
  restoring reclaim, is a decision for that system's owner.
- **`RECORD_EXCLUDE_NODES=node1`** with a comment block describing node3. The
  value is what takes effect; the comment is stale. An excluded node cannot
  veto a start, so a stale exclusion silently accepts a dead node.
- **node5 resets when its LiDAR is switched on.** Reproducible; a 12 V
  headroom problem on that node. It is excluded from switching and left on
  `POWER_BACKEND=none` until the supply is fixed.
- ~~node6 has no LiDAR~~ — **resolved 2026-09-16 10:23.** It was away from
  2026-09-15 for a hardware fault, taking its USB-Ethernet adapter with it, so
  the node reported no `enx*` interface at all. Reconnected, CH341 installed,
  switched on, `lidar_status=ok`.
- **The root filesystem is 95% full.** Keep state small.
- **Monitor passes take ~2 minutes**, not the 60s the timer asks for. Worth
  fixing by probing nodes in parallel, as the bot already does.
