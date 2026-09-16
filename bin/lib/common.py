"""Config loading, node inventory, state paths and formatting.

The guardian this bot explains is written in bash and its configs are sourced
by bash. We parse them instead of sourcing them: `server.env` is read by a
long-lived network daemon, and handing a config file to a shell is a much
larger promise than reading keys out of it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV = REPO_ROOT / "config" / "server.env"
DEFAULT_NODES = REPO_ROOT / "config" / "nodes.list"


# -- config ---------------------------------------------------------------

def load_env(path: Path | str | None = None) -> dict:
    """Read a shell-style KEY=value file without executing it."""
    path = Path(path or os.environ.get("GUARDIAN_SERVER_ENV") or DEFAULT_ENV)
    if not path.exists():
        raise SystemExit(
            f"config not found: {path}\n"
            f"Copy config/server.env.example to {path} and fill it in."
        )
    return parse_env_text(path.read_text())


def parse_env_text(text: str) -> dict:
    """The parser itself, so it can be tested without a file on disk.

    A quoted value ends at its closing quote and anything after it is a
    trailing comment. Testing the first and last character instead — the
    obvious shortcut — silently keeps the quotes on every line written as
    `KEY="123"   # note`, and the number then fails int() and falls back to a
    default. In this repo that pattern is everywhere: server.env ships
    `MAIL_TO="shengq@andrew.cmu.edu"   # TODO: append Kieran's address`.
    """
    conf: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if value[:1] in "\"'":
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = value.split("#", 1)[0].strip()
        conf[key] = expand(value)
    # Environment wins, so a systemd unit or an operator can override one knob
    # without editing a file the bash guardian also reads.
    for key in list(conf):
        if key in os.environ:
            conf[key] = os.environ[key]
    return conf


def expand(value: str) -> str:
    return os.path.expandvars(value).replace("$HOME", str(Path.home()))


def conf_int(conf: dict, key: str, default: int) -> int:
    try:
        return int(str(conf.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def conf_float(conf: dict, key: str, default: float) -> float:
    try:
        return float(str(conf.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def conf_bool(conf: dict, key: str, default: bool = False) -> bool:
    return str(conf.get(key, default)).strip().lower() in ("1", "true", "yes", "on")


def state_dir(conf: dict) -> Path:
    return Path(conf.get("SERVER_STATE_DIR")
                or (Path.home() / ".lidar-guardian-server"))


def recording_env(conf: dict) -> dict:
    """The recording schedule, which lives in a different repo on the same host.

    Recording is not part of this repo: it is driven by record_day.sh in
    lidar_social_recognition_deploy. We read its config rather than restating
    its numbers, because a copy of a threshold is a threshold that goes stale.
    """
    path = conf.get("RECORDING_SCHEDULE_ENV", "").strip()
    if not path:
        return {}
    try:
        return parse_env_text(Path(path).read_text())
    except OSError:
        return {}


# -- node inventory -------------------------------------------------------

def load_nodes(conf: dict, path: Path | str | None = None) -> list[dict]:
    """Parse nodes.list: `<name> <tailscale_ip> <ssh_user>` per line."""
    path = Path(path or conf.get("NODES_LIST") or DEFAULT_NODES)
    nodes = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return nodes
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        nodes.append({"name": parts[0], "ip": parts[1],
                      "user": parts[2] if len(parts) > 2 else "jetson"})
    return nodes


NODE_RE = re.compile(r"\bnode\s*(\d+)\b", re.IGNORECASE)


def node_named(text: str, nodes: list[dict]) -> str | None:
    """Pull a node name out of a question, e.g. "why is node3 down"."""
    match = NODE_RE.search(text or "")
    if not match:
        return None
    wanted = f"node{int(match.group(1))}"
    for node in nodes:
        if node["name"].lower() == wanted:
            return node["name"]
    return None


# -- running commands -----------------------------------------------------

def run(argv: list[str], timeout: float = 15.0,
        stdin_text: str | None = None) -> tuple[int, str, str]:
    """Run a local read-only command with a hard ceiling. Never raises.

    stdin is closed unless `stdin_text` is given. Closing it matters: the bash
    guardian needs `ssh -n` because it runs inside a `while read` loop over
    nodes.list, and an ssh that inherits that stdin eats the rest of the node
    list. Feeding a script to `ssh ... sh -s` is the one case that genuinely
    wants stdin, and it must ask for it explicitly.
    """
    try:
        if stdin_text is None:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        else:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, input=stdin_text)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except (OSError, ValueError) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def write_json(path: Path, data) -> None:
    """Write via a temp file in the same directory, then rename.

    rename(2) is atomic within a filesystem, so a run killed mid-write leaves
    the previous good file rather than half a JSON document.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


# -- formatting -----------------------------------------------------------

def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 0:
        return f"-{human_duration(-seconds)}"
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def human_gb(gb: float | None) -> str:
    if gb is None:
        return "unknown"
    return f"{gb:.0f} GB" if gb >= 10 else f"{gb:.1f} GB"
