#!/usr/bin/env python3
"""data_report.py — one Slack message per recording day for the SenSys analysis
window (2026-10-06 .. 2026-10-26), for seven days: 2026-10-09 .. 2026-10-15.

Recording is not this repo's: record_day.sh in lidar_social_recognition_deploy
drives it (see CLAUDE.md, "Where recording actually runs"). This script only
reads that system's journals, logs and files, and posts through alert.sh like
the morning brief.

Two passes, because the facts do not all exist at the same time:

  snapshot   03:10, after the 03:00 stop and before the nightly archive
             reclaims the session (~04:30-05:00). Reads each segment's
             meta.json while the session is still on puget's disk -- per-node
             frame rates and gaps exist nowhere else afterwards: the NAS copy
             may only be stat'ed, never read. Writes
             $SERVER_STATE_DIR/data_report/<session>.json. Posts nothing.
  post       07:45, after archive + reclaim (lidar-reclaim.timer 07:30) and
             before the 08:00 start and the 08:30 brief. Reports yesterday's
             session(s) from the snapshot, the archive journal, the live track
             log, df and the freeze baseline.

Every fact says where it came from. A session with no snapshot (stopped and
reclaimed before 03:10 could see it, or a backfill) still gets per-node GB and
segment-close gaps from os.scandir over SSH on the NAS -- the one read the NAS
rule allows -- and its rates are reported as n/a, never guessed.

Stops itself: a post on or after LAST_POST disables both timers; any run after
LAST_POST disables them and exits without posting. Stop early with
    systemctl --user disable --now guardian-datareport.timer guardian-datasnap.timer

Usage:
    data_report.py snapshot
    data_report.py post [--date YYYY-MM-DD] [--print] [--backfill]
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from lib.common import load_env, parse_env_text, run, state_dir, write_json, read_json  # noqa: E402

WINDOW_START = dt.date(2026, 10, 6)
WINDOW_DAYS = 21                     # 2026-10-06 .. 2026-10-26
LAST_POST = dt.date(2026, 10, 15)    # the 7th scheduled post (2026-10-09 .. 10-15)
TIMERS = ("guardian-datareport.timer", "guardian-datasnap.timer")
MIN_HZ = 9.0                         # a 10 Hz node below this is reported
GAP_S = 300                          # "gap > 5 min"
SEG_S = 600                          # RECORD_ROTATE_SEC, read from the env when present

# Frozen for the analysis window (sha256 measured 2026-10-08 on puget, equal to
# ~/walk_post_20261006/out/provenance.json where both list a file). Any change
# inside the window is a ✗, whatever the reason.
FROZEN = {
    "detector checkpoint": ("~/sm89_build/ckpt/dataset_v1_B_fourday_gts_off_ep091.pth",
                            "68d85a71effe3f57505134d8fd52bd7d72b5029665d08d33d5f97b4167418e72"),
    "daily config.json": ("~/sm89_build/lidar_repo/tracking/daily_pipeline/config.json",
                          "e62abb479bd27ae854a1a2044b498b588f1c7407ed6bde468754a70084c83b67"),
    "model cfg": ("~/sm89_build/cfg/dataset_v1_voxelnext.yaml",
                  "cf5154b16aa6231d89695697e62e66945252ccb7c1d6e1fc277ebe803e389a5e"),
    "exclusion_zone.json": ("~/sm89_build/lidar_repo/tracking/exclusion_zone.json",
                            "7a1209111d33cf20849ff843c59acb8401137e46ea2bc0af5906b97a510a75b1"),
    "static-FP registry": ("~/sm89_build/lidar_repo/tracking/output/geo_20260810_w1430/static_suspects.json",
                           "9d4f626a1c95cf7508617751e2053ca9776e370338f4387975155e318b1205b8"),
    "furniture_roi_v2.yaml (deploy)": ("~/lidar_social_recognition_deploy/furniture_roi_v2.yaml",
                                       "a9ce4c87c8a522bd91046a91de600add97908fa493d911afc4ac3d37c35ca885"),
    "furniture_roi_v2.yaml (sm89)": ("~/sm89_build/lidar_repo/furniture_roi_v2.yaml",
                                     "a9ce4c87c8a522bd91046a91de600add97908fa493d911afc4ac3d37c35ca885"),
    "nodes_config.yaml (deploy)": ("~/lidar_social_recognition_deploy/config/nodes_config.yaml",
                                   "8f704bf08e73b8a7040a87c7ba52f7f29714dc6d9cfeb42094af2495bef1d6fe"),
    "nodes_config.yaml (sm89)": ("~/sm89_build/lidar_repo/config/nodes_config.yaml",
                                 "8f704bf08e73b8a7040a87c7ba52f7f29714dc6d9cfeb42094af2495bef1d6fe"),
    "recording_schedule.env": ("~/lidar_social_recognition_deploy/config/recording_schedule.env",
                               "8d8a1f38d0ba87098c35f78ce2abf1412a9b55be89ca9d3ab9dbd010da497dc2"),
    "geo_filter_node.py (deploy)": ("~/lidar_social_recognition_deploy/pipeline/00_start_driver_rosbridge/geo_filter_node.py",
                                    "95626b37a21b63ed55742acdefe36c5891d131ab9d4b88fdaee5604aa58e4d4a"),
    "build_geo_filter_assets.py (deploy)": ("~/lidar_social_recognition_deploy/scripts/build_geo_filter_assets.py",
                                            "9e542fbac6743cbf40f4bc4673440a0635397f06b8b8a326740e3a2f700afb4e"),
    "geo_filter_assets.npz (deploy)": ("~/lidar_social_recognition_deploy/geo_filter_assets.npz",
                                       "4f6c83c2054e7ad4e3cfd70935c60fc5b5e832fa323deb495e31344e97ff51a6"),
}
FROZEN_GIT_HEAD = {"~/lidar_social_recognition_deploy": "3bc101d",
                   "~/sm89_build/lidar_repo": "3bc101d"}


# -- pure helpers (tested in tests/test_data_report.py) ---------------------

def day_number(d: dt.date) -> int:
    return (d - WINDOW_START).days + 1


def segment_stats(segs: list[tuple[float, int]], stop: float | None,
                  seg_s: int = SEG_S) -> dict:
    """segs: [(t0_s, n_frames)] per closed segment of one node, any order.

    Rate per segment = frames / (next segment's t0 - this t0); the last segment
    runs to `stop` when known. Gaps > GAP_S are reported two ways: a t0 jump
    longer than a rotation plus GAP_S, and a segment whose frame count is short
    of its span at 10 Hz by more than GAP_S worth of frames.
    """
    segs = sorted(segs)
    rates, gaps = [], []
    for i, (t0, n) in enumerate(segs):
        t1 = segs[i + 1][0] if i + 1 < len(segs) else stop
        if t1 is None or t1 <= t0:
            continue
        span = t1 - t0
        rates.append(n / span)
        if span > seg_s + GAP_S:
            gaps.append((t0 + n / 10.0, t1))
        elif n < (span - GAP_S) * 10:
            gaps.append((t0, t1))
    return {"median_hz": round(statistics.median(rates), 2) if rates else None,
            "gaps": gaps, "segments": len(segs)}


def should_stop(today: dt.date) -> bool:
    return today >= LAST_POST


# -- small I/O helpers --------------------------------------------------------

def sha256(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def journal(unit: str, since: str, until: str | None = None) -> list[str]:
    argv = ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager", "-o", "short-iso"]
    if until:
        argv += ["--until", until]
    return run(argv, timeout=60)[1].splitlines()


def hm(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def fmt_gaps(gaps: list) -> str:
    return ", ".join(f"{hm(a)}–{hm(b)}" for a, b in gaps)


def read_recording_env(path: Path) -> dict:
    """recording_schedule.env through lib.common's parser, minus bare `KEY=` lines.

    parse_env_text raises IndexError on an empty value (`value[:1] in "\"'"` is
    true for ""), and RECORD_EXCLUDE_NODES= has been empty since 2026-10-02. An
    empty value means unset here, so those lines are dropped rather than the
    shared parser being changed under the bot.
    """
    text = "\n".join(l for l in path.read_text().splitlines()
                     if not re.match(r"^\s*(export\s+)?[A-Za-z_]\w*=\s*$", l))
    return parse_env_text(text)


class Ctx:
    def __init__(self):
        self.conf = load_env()
        env_path = Path(os.path.expanduser(self.conf.get("RECORDING_SCHEDULE_ENV")
                        or "~/lidar_social_recognition_deploy/config/recording_schedule.env"))
        self.rec = read_recording_env(env_path)
        self.deploy = env_path.parents[1]
        # record_day.sh writes sessions to <deploy>/data/clouds (server.env's
        # RECORDING_DATA_DIR names <deploy>/data, one level up).
        self.data_dir = self.deploy / "data" / "clouds"
        self.snap_dir = state_dir(self.conf) / "data_report"
        self.start_h = int(self.rec.get("RECORD_START_HOUR", 8))
        self.stop_h = int(self.rec.get("RECORD_STOP_HOUR", 3))
        self.floor_gb = float(self.rec.get("RECORD_MIN_FREE_GB", 35))
        self.prefix = self.rec.get("RECORD_SESSION_PREFIX", "geo")

    def nas(self):
        sys.path.insert(0, str(self.deploy / "pipeline" / "00_start_driver_rosbridge"))
        try:
            from nas_archive import is_nas_configured, _ssh_run
        except Exception:  # noqa: BLE001
            return None, None
        t = is_nas_configured()
        return (t, _ssh_run) if t else (None, None)


def running_sessions() -> set[str]:
    rc, out, _ = run(["pgrep", "-af", "record_supervisor.py"])
    return set(re.findall(r"--base-session (\S+)", out))


# -- snapshot -----------------------------------------------------------------

def snapshot_session(ctx: Ctx, session: str) -> dict:
    sdir = ctx.data_dir / session
    man = read_json(sdir / "supervisor_manifest.json", {}) or {}
    targets = man.get("targets") or sorted({p.name.split("__")[0] for p in sdir.glob("node*__seg*")})
    log = Path(ctx.rec.get("RECORD_STATE_DIR", "/tmp")) / f"rec_{session}.log"
    text = log.read_text(errors="replace") if log.exists() else ""
    stop = log.stat().st_mtime if log.exists() else None
    nodes = {}
    for n in targets:
        segs, nbytes = [], 0
        for s in sorted(p for p in sdir.glob(f"{n}__seg*") if p.is_dir()):
            nbytes += sum(f.stat().st_size for f in s.iterdir() if f.is_file())
            m = read_json(s / "meta.json")
            if m and (s / "frames.npz").exists():
                segs.append((m["t0_ms"] / 1000.0, int(m["n_frames"])))
        st = segment_stats(segs, stop, int(ctx.rec.get("RECORD_ROTATE_SEC", SEG_S)))
        nodes[n] = {"GB": round(nbytes / 1e9, 2), **st}
    return {"session": session, "taken_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": f"local meta.json + file sizes in {sdir}", "targets": targets,
            "stop_ts": stop, "stop_source": f"mtime of {log}" if log.exists() else None,
            "disk_floor_hit": "DISK FLOOR HIT" in text, "nodes": nodes}


def cmd_snapshot(ctx: Ctx) -> int:
    live = running_sessions()
    done = []
    for sdir in sorted(ctx.data_dir.glob(f"{ctx.prefix}_*")):
        out = ctx.snap_dir / f"{sdir.name}.json"
        if sdir.name in live or out.exists() or not sdir.is_dir():
            continue
        write_json(out, snapshot_session(ctx, sdir.name))
        done.append(sdir.name)
    print(f"snapshot: {done or 'nothing new'} (live: {sorted(live) or 'none'})")
    return 0


# -- post -----------------------------------------------------------------------

def sessions_for(ctx: Ctx, d: dt.date) -> list[tuple[str, dt.datetime]]:
    out = []
    for l in journal("lidar-record-start.service", f"{d} 00:00", f"{d + dt.timedelta(days=1)} 00:00"):
        m = re.search(r"^(\S+) .*\[record_day\] started: (\S+)", l)
        if m:
            out.append((m.group(2), dt.datetime.fromisoformat(m.group(1))))
    return out


def archive_status(d: dt.date, session: str) -> dict:
    res = {"line": None}
    for l in journal("lidar-record-archive.service", f"{d}") + \
            run(["journalctl", "--user", "-t", "record_day.sh", "--since", f"{d}", "--no-pager",
                 "-o", "short-iso"], timeout=60)[1].splitlines():
        m = re.search(rf"\[nightly\]\s+{re.escape(session)}\s+([\d.]+) GB\s+(.*)$", l)
        if m:
            res = {"line": l, "GB": float(m.group(1)), "status": m.group(2)}
    s = res.get("status") or ""
    res["uploaded"] = "uploaded" in s
    res["verified"] = "sha256-ok" in s
    res["reclaimed"] = "RECLAIMED" in s
    return res


NAS_SCAN = ("import os,json,sys\nr=sys.argv[1]\nout={}\n"
            "try:\n  es=list(os.scandir(r))\nexcept FileNotFoundError:\n  print('null'); sys.exit(0)\n"
            "for e in es:\n  if e.is_dir():\n    out[e.name]={f.name:[f.stat().st_size,f.stat().st_mtime] "
            "for f in os.scandir(e.path)}\nprint(json.dumps(out))\n")


def nas_fallback(ctx: Ctx, session: str) -> dict | None:
    """Per-node GB and segment-close gaps from os.scandir on the NAS (stat only)."""
    t, ssh_run = ctx.nas()
    if not t:
        return None
    path = f"{t.root.rstrip('/')}/clouds/{session}"
    r = ssh_run(t, f"python3 -c {shlex.quote(NAS_SCAN)} {shlex.quote(path)}")
    scan = json.loads(r.stdout or "null") if r.returncode == 0 else None
    if not scan:
        return None
    nodes = {}
    for n in sorted({k.split("__")[0] for k in scan if "__seg" in k}):
        segs = sorted(k for k in scan if k.startswith(f"{n}__seg"))
        closes = sorted(scan[k]["frames.npz"][1] for k in segs if "frames.npz" in scan[k])
        gaps = [(a, b) for a, b in zip(closes, closes[1:]) if b - a > SEG_S + GAP_S]
        nodes[n] = {"GB": round(sum(v[0] for k in segs for v in scan[k].values()) / 1e9, 2),
                    "median_hz": None, "gaps": gaps, "segments": len(segs),
                    "last_close": closes[-1] if closes else None}
    return {"session": session, "source": "session reclaimed before any snapshot; sizes and segment close times from NAS os.scandir",
            "nodes": nodes, "targets": list(nodes),
            "stop_ts": max((v["last_close"] or 0) for v in nodes.values()) or None,
            "stop_source": "last segment close on the NAS", "disk_floor_hit": None}


def track_log(ctx: Ctx, d: dt.date) -> dict:
    files = sorted(Path.home().glob(f"track_log/tracks_{d:%Y%m%d}_*.jsonl.gz"))
    rows, first, last = 0, None, None
    for f in files:
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        buf = b""
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                try:
                    buf += dec.decompress(chunk)
                except zlib.error:
                    break
                *lines, buf = buf.split(b"\n")
                rows += sum(l.count(b'{"id":') for l in lines)
                rx = [m for l in (lines[:1] + lines[-1:]) for m in re.findall(rb'^\{"rx":([\d.]+)', l)]
                if rx:
                    first = first or float(rx[0])
                    last = float(rx[-1])
    pauses = [l for l in journal("track-log-recorder.service", f"{d}", f"{d + dt.timedelta(days=1)}")
              if "DISK GUARD" in l or "disk guard cleared" in l]
    return {"files": [f.name for f in files], "bytes": sum(f.stat().st_size for f in files),
            "rows": rows, "guard_events": len(pauses), "first": first, "last": last}


def df_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def nas_free_gb(ctx: Ctx) -> float | None:
    t, ssh_run = ctx.nas()
    if not t:
        return None
    r = ssh_run(t, "df -B1 /volume1 | tail -1")
    parts = (r.stdout or "").split()
    return int(parts[3]) / 1e9 if r.returncode == 0 and len(parts) >= 4 else None


def avg_gb_per_day(today: dt.date) -> float | None:
    sizes = []
    for l in run(["journalctl", "--user", "-t", "record_day.sh", "--since", str(WINDOW_START),
                  "--no-pager", "-o", "cat"], timeout=60)[1].splitlines():
        m = re.search(r"\[nightly\]\s+\S+_(\d{8})_\d+\s+([\d.]+) GB\s+.*uploaded", l)
        if m and dt.datetime.strptime(m.group(1), "%Y%m%d").date() >= WINDOW_START:
            sizes.append(float(m.group(2)))
    return sum(sizes) / len(sizes) if sizes else None


def freeze_check() -> list[str]:
    bad = []
    for name, (p, want) in FROZEN.items():
        got = sha256(Path(os.path.expanduser(p)))
        if got is None:
            bad.append(f"{name} missing")
        elif got != want:
            bad.append(f"{name} changed")
    for repo, head in FROZEN_GIT_HEAD.items():
        rc, out, _ = run(["git", "--no-optional-locks", "-C", os.path.expanduser(repo),
                          "rev-parse", "--short=7", "HEAD"])
        if out.strip() != head:
            bad.append(f"{repo} HEAD {out.strip() or '?'} != {head}")
    return bad


def build(ctx: Ctx, d: dt.date) -> tuple[str, str]:
    x: list[str] = []                     # ✗ items, repeated on the first line
    lines: list[str] = []
    sched0 = dt.datetime.combine(d, dt.time(ctx.start_h))
    sched1 = dt.datetime.combine(d + dt.timedelta(days=1 if ctx.stop_h <= ctx.start_h else 0),
                                 dt.time(ctx.stop_h))
    sched_h = (sched1 - sched0).total_seconds() / 3600
    sess = sessions_for(ctx, d)
    if not sess:
        x.append("no session started")
        lines.append(f"Recording: no session started on {d} (scheduled "
                     f"{sched0:%H:%M}–{sched1:%H:%M}, {sched_h:.0f} h)")
    total_h, total_gb, per_node_gb, notes = 0.0, 0.0, {}, []
    node_lines, gap_lines, arch_lines = [], [], []
    for name, started in sess:
        snap = read_json(ctx.snap_dir / f"{name}.json")
        if not snap and (ctx.data_dir / name).is_dir() and name not in running_sessions():
            snap = snapshot_session(ctx, name)
        if not snap:
            snap = nas_fallback(ctx, name) or {"nodes": {}, "targets": [], "source": "no data"}
        if snap.get("disk_floor_hit") is None:       # NAS fallback: the recorder's own log, if /tmp kept it
            log = Path(ctx.rec.get("RECORD_STATE_DIR", "/tmp")) / f"rec_{name}.log"
            if log.exists():
                snap["disk_floor_hit"] = "DISK FLOOR HIT" in log.read_text(errors="replace")
        stop_ts = snap.get("stop_ts")
        stop = dt.datetime.fromtimestamp(stop_ts) if stop_ts else None
        hrs = ((stop - started.replace(tzinfo=None)).total_seconds() / 3600) if stop else 0.0
        total_h += hrs
        why = " — stopped early: disk floor" if snap.get("disk_floor_hit") else ""
        lines.append(f"Recording: {started:%H:%M}–{stop:%H:%M} = {hrs:.1f} h of {sched_h:.0f} h scheduled "
                     f"({sched0:%H:%M}–{sched1:%H:%M}){why}  [{name}]" if stop else
                     f"Recording: started {started:%H:%M}, stop unknown  [{name}]")
        if started.replace(tzinfo=None) > sched0 + dt.timedelta(minutes=15):
            x.append(f"late start {started:%H:%M}")
        if stop and stop < sched1 - dt.timedelta(minutes=15):
            x.append(f"stopped early {stop:%H:%M}" + (" (disk floor)" if snap.get("disk_floor_hit") else ""))
        nodes = snap.get("nodes", {})
        targets = snap.get("targets") or list(nodes)
        rated = {n: v["median_hz"] for n, v in nodes.items() if v.get("median_hz") is not None}
        if rated:
            ok = [n for n, hz in rated.items() if hz >= MIN_HZ]
            node_lines.append(f"Nodes streaming: {len(ok)}/{len(targets)}, median Hz " +
                              " ".join(f"{n[4:]}:{hz:.1f}" for n, hz in sorted(rated.items())))
            if len(ok) < len(targets):
                x.append(f"nodes {len(ok)}/{len(targets)}")
        else:
            node_lines.append(f"Nodes with data: {len(nodes)}/{len(targets) or 7}, median Hz n/a "
                              f"({snap.get('source')})")
            if len(nodes) < 7:
                x.append(f"nodes {len(nodes)}/7")
        g = [f"{n}: {fmt_gaps(v['gaps'])}" for n, v in sorted(nodes.items()) if v.get("gaps")]
        gap_lines.append("Gaps > 5 min: " + ("; ".join(g) if g else "none"))
        if g:
            x.append("gaps")
        for n, v in nodes.items():
            per_node_gb[n] = per_node_gb.get(n, 0.0) + v["GB"]
        a = archive_status(d, name)
        total_gb += a.get("GB") or sum(v["GB"] for v in nodes.values())
        mark = lambda b: "✓" if b else "✗"  # noqa: E731
        arch_lines.append(f"NAS archive: done {mark(a['uploaded'])}  verified (sha256) {mark(a['verified'])}  "
                          f"local reclaimed {mark(a['reclaimed'] and not (ctx.data_dir / name).exists())}")
        if not (a["uploaded"] and a["verified"] and a["reclaimed"]):
            x.append("archive")
    lines += node_lines + gap_lines
    if sess:
        lines.append(f"Size: {total_gb:.1f} GB total — " +
                     ", ".join(f"{n} {gb:.1f}" for n, gb in sorted(per_node_gb.items())))
    lines += arch_lines
    tl = track_log(ctx, d)
    lines.append(f"Live track log: {tl['bytes'] / 1e6:.1f} MB, {tl['rows']:,} rows"
                 + (f", {hm(tl['first'])}–{hm(tl['last'])}" if tl["first"] else "")
                 + (f" ({tl['guard_events']} disk-guard pause/resume events)" if tl["guard_events"] else ""))
    if not tl["files"]:
        x.append("track log missing")
    pf, nf, avg = df_gb(ctx.data_dir), nas_free_gb(ctx), avg_gb_per_day(d)
    p_days = (pf - ctx.floor_gb) / avg if avg else None
    n_days = nf / avg if (nf is not None and avg) else None
    lines.append(f"Disk now: puget {pf:.0f} GB free, NAS {nf:,.0f} GB free; headroom at "
                 f"{avg:.0f} GB/day: puget {p_days:.1f} d (above the {ctx.floor_gb:.0f} GB floor), "
                 f"NAS {n_days:,.0f} d" if avg and nf is not None else
                 f"Disk now: puget {pf:.0f} GB free, NAS {'?' if nf is None else f'{nf:,.0f}'} GB free")
    if p_days is not None and p_days < 1:
        x.append(f"puget headroom {p_days:.1f} d")
    left = (WINDOW_START + dt.timedelta(days=WINDOW_DAYS) - d).days
    if n_days is not None and n_days < left:
        x.append("NAS headroom")
    bad = freeze_check()
    lines.append("Freeze check " + ("✓" if not bad else "✗ " + "; ".join(bad)))
    if bad:
        x.append("freeze")
    subject = f"Data collection — {d} (day {day_number(d)} of {WINDOW_DAYS})"
    if x:
        subject += " — ✗ " + "; ✗ ".join(x)
    return subject, "\n".join(lines)


def disable_timers() -> None:
    run(["systemctl", "--user", "disable", "--now", *TIMERS], timeout=30)


def cmd_post(ctx: Ctx, a) -> int:
    today = dt.date.today()
    if not a.backfill and not a.date and today > LAST_POST:
        print(f"{today} is after the last report day {LAST_POST}: disabling {TIMERS}, nothing posted")
        disable_timers()
        return 0
    d = dt.date.fromisoformat(a.date) if a.date else today - dt.timedelta(days=1)
    subject, body = build(ctx, d)
    if a.print:
        print(subject)
        print(body)
        return 0
    rc = subprocess.run([str(REPO / "server" / "alert.sh"), subject, body],
                        env={**os.environ, "GUARDIAN_HOME": str(REPO)}).returncode
    log = ctx.snap_dir / "posted.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "date": str(d),
                            "backfill": a.backfill, "rc": rc, "subject": subject, "body": body},
                           ensure_ascii=False) + "\n")
    if not a.backfill and should_stop(today):
        print(f"last scheduled report ({LAST_POST}) posted: disabling {TIMERS}")
        disable_timers()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    p = sub.add_parser("post")
    p.add_argument("--date", help="session date YYYY-MM-DD (default: yesterday)")
    p.add_argument("--print", action="store_true", help="print, send nothing")
    p.add_argument("--backfill", action="store_true", help="manual post for a past day; never stops the timers")
    a = ap.parse_args()
    ctx = Ctx()
    if a.cmd == "snapshot":
        if dt.date.today() > LAST_POST:
            disable_timers()
            return 0
        return cmd_snapshot(ctx)
    return cmd_post(ctx, a)


if __name__ == "__main__":
    sys.exit(main())
