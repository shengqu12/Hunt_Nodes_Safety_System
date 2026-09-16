#!/usr/bin/env python3
"""askbot.py — answer questions about the LiDAR fleet in Slack.

Reply "why" under an alert and it explains what the guardian found. Ask it how
a node is doing, whether recording started, or what happened overnight.

Socket Mode, not the Events API: the lab server sits on a private LAN behind
Tailscale with no public ingress, so Slack cannot POST to it. Socket Mode
dials out over a WebSocket instead — no inbound port, no reverse proxy, no
certificate. That needs its own Slack app; the incoming webhook the guardian
already uses for alerts can only send.

The model never runs a command and never chooses what to look at. It is handed
facts collected by lib/diagnose.py and asked to explain them, and those facts
are posted next to its answer so you can check it. This bot only reads.

Usage:
  askbot.py                      run the bot (needs SLACK_APP_TOKEN + SLACK_BOT_TOKEN)
  askbot.py --ask "why"          answer one question on the terminal, no Slack
  askbot.py --probe recording    print the raw evidence, no model at all
  askbot.py --check-slack        validate the Slack app setup and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import traceback
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import common, diagnose, llm      # noqa: E402

log = logging.getLogger("askbot")

# alert.sh prefixes every message it posts with this, which is how a reply is
# recognised as a reply to one of our own alerts. It is matched on the message
# TEXT, not on who posted it, so the alert webhook and this bot can be
# different Slack apps.
ALERT_MARKER = "[LiDAR Guardian]"

# How much evidence goes into a Slack message. Grouping the fleet by symptom
# took a `status` bundle from 69 facts / 10.8k characters to 19 / ~4k, so this
# now fits a broken fleet whole. Slack accepts far more in `text` and collapses
# a long message behind "Show more"; the cap is here so a runaway bundle cannot
# push a message past what Slack will render.
EVIDENCE_LIMIT = 5000


class SeenEvents:
    """Remember which messages have been answered.

    @mentioning the bot in a channel delivers TWO events for one message —
    `app_mention` and `message.channels` — and Slack additionally redelivers
    anything it thinks went unacknowledged. Both produced duplicate answers
    within 120ms of each other, and the pair of them hitting a cold ollama
    concurrently is what made the second one fail with HTTP 500.

    Keyed on the message, not the event, so both delivery paths collapse to
    one. Bounded, because this process is long-lived.
    """

    def __init__(self, limit: int = 512):
        self._seen: OrderedDict = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        """True the first time a key is seen, False every time after."""
        if not key:
            return True                  # cannot dedup it; answering once is
        with self._lock:                 # better than never answering
            if key in self._seen:
                return False
            self._seen[key] = True
            while len(self._seen) > self._limit:
                self._seen.popitem(last=False)
        return True


def event_key(event: dict) -> str:
    """Identify the MESSAGE, so its two event types collapse to one."""
    return (event.get("client_msg_id")
            or f"{event.get('channel', '')}:{event.get('ts', '')}")

HELP = """*What you can ask me*
• `why` (under an alert, or on its own) — what the guardian found and the \
likeliest cause
• `why is node3 down` — everything measured about one node
• `status` / `现在怎么样` — probe the fleet now and report
• `recording` / `在录吗` — whether today's capture started, and if not, why
• `alerts` / `告警` — what fired in the last 24 h and what is still open
• `help`

I only read. I cannot restart anything, change a threshold, or touch a node — \
every answer comes with the raw evidence so you can check it."""


# -- intent ----------------------------------------------------------------

def route(text: str, in_alert_thread: bool, nodes: list) -> tuple:
    """Decide what was asked. Keyword rules, not a model.

    Routing is a small closed set, and a wrong route sends the whole answer
    off in the wrong direction. Rules are auditable and never surprise; the
    model's judgement is spent on the part that actually needs judgement.
    """
    lowered = (text or "").lower().strip()
    node = common.node_named(text, nodes)

    if re.search(r"\b(help|usage|commands)\b|帮助|怎么用|用法", lowered):
        return "help", "", None
    if re.search(r"\b(record|recording|capture|session|bag)\b|"
                 r"录制|在录|录像|采集|会话", lowered):
        return "ask", "recording", None
    if re.search(r"\b(alert|alarm|overnight|fired|open)\b|告警|报警|昨晚|夜里",
                 lowered):
        return "ask", "alerts", node
    if node:
        return "ask", "node", node
    if re.search(r"\b(status|state|health|fleet|how.{0,6}(is|are)|now)\b|"
                 r"状态|现在|怎么样|健康|集群|舰队", lowered):
        return "ask", "status", None
    if re.search(r"\b(why|what happened|cause|reason|explain)\b|"
                 r"为什么|为啥|怎么回事|原因|解释", lowered):
        return "ask", "general", None
    # A bare reply in an alert thread is almost always "why".
    if in_alert_thread:
        return "ask", "general", None
    return "ask", "general", None


# -- answering -------------------------------------------------------------

def answer(conf: dict, question: str, topic: str, node: str | None,
           backend: llm.Backend) -> tuple:
    """Collect evidence, then have the model explain it. Returns (reply, bundle)."""
    bundle = diagnose.collect(conf, topic, node_name=node)
    # The model is given exactly what the human is shown. Handing it facts
    # that were cut from the reply would make its answer uncheckable, which is
    # the whole reason the evidence is posted at all.
    evidence = bundle.as_text(max_chars=EVIDENCE_LIMIT)

    try:
        explanation = backend.complete(question, evidence)
    except llm.LLMError as exc:
        # The facts are the valuable part and they were gathered successfully.
        # Losing the model must not lose them.
        explanation = (f"_(the model is unavailable: {exc} — here are the raw "
                       f"findings)_")

    header = {"bad": ":rotating_light:", "warn": ":warning:",
              "unknown": ":grey_question:"}.get(bundle.worst, ":white_check_mark:")
    reply = (f"{header} {explanation}\n\n"
             f"_evidence ({bundle.topic}"
             + (f"/{node}" if node else "")
             + f"), collected in {bundle.elapsed}s_\n"
             + f"```\n{evidence}\n```")
    return reply, bundle


def handle(conf: dict, text: str, in_alert_thread: bool,
           backend: llm.Backend) -> str:
    nodes = common.load_nodes(conf)
    intent, topic, node = route(text, in_alert_thread, nodes)
    log.info("intent=%s topic=%s node=%s text=%r", intent, topic, node,
             (text or "")[:120])
    if intent == "help":
        return HELP
    reply, _ = answer(conf, text or "why", topic, node, backend)
    return reply


# -- setup checking --------------------------------------------------------

# What the bot needs, and why — printed when something is missing, because
# "missing_scope" alone sends you back to a settings page with 70 checkboxes.
REQUIRED_SCOPES = {
    "app_mentions:read": "see when you @mention the bot",
    "chat:write":        "post answers",
    "channels:history":  "read the alert thread in a public channel",
    "groups:history":    "same, in a private channel",
    "im:history":        "read direct messages to the bot",
}

REQUIRED_EVENTS = ["app_mention", "message.channels", "message.groups",
                   "message.im"]


def _slack_api(method: str, token: str, params: dict | None = None) -> tuple:
    """One Slack Web API call with stdlib only.

    No slack_sdk import: this check has to work *before* the venv exists,
    which is exactly when you need it.
    """
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            # Lower-case the keys. HTTP headers are case-insensitive and
            # urllib's own object honours that, but dict() over it does not —
            # Slack sends "x-oauth-scopes", a lookup for "X-Oauth-Scopes" finds
            # nothing, and the check then reports every scope as missing.
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return json.loads(resp.read()), headers
    except Exception as exc:
        return {"ok": False, "error": f"request failed: {exc}"}, {}


def check_slack(conf: dict) -> int:
    """Validate the Slack setup and say exactly what is still wrong.

    Anything this cannot verify prints [??] with the reason, never [FAIL]. A
    checker that calls a correct configuration broken costs you the time you
    would have spent on the real problem, and it has done exactly that before.
    """
    ok = True

    def line(good, label, detail=""):
        mark = "  OK  " if good else " FAIL "
        print(f"[{mark}] {label}" + (f"\n          {detail}" if detail else ""))

    app_token = conf.get("SLACK_APP_TOKEN", "").strip()
    bot_token = conf.get("SLACK_BOT_TOKEN", "").strip()

    print("=" * 66)
    print("Slack setup check — LiDAR Guardian ask-bot")
    print("=" * 66)

    if not app_token:
        line(False, "SLACK_APP_TOKEN is empty",
             "Basic Information -> App-Level Tokens -> Generate Token and Scopes")
        ok = False
    elif not app_token.startswith("xapp-"):
        line(False, f"SLACK_APP_TOKEN does not look right: {app_token[:12]}...",
             "An app-level token starts with 'xapp-'. 'xoxb-' is the bot token "
             "and belongs in SLACK_BOT_TOKEN.")
        ok = False
    else:
        body, _ = _slack_api("apps.connections.open", app_token)
        if body.get("ok"):
            line(True, "App token valid, Socket Mode can connect")
        else:
            err = body.get("error", "?")
            hint = {
                "invalid_auth": "the token is wrong, revoked, or from another app",
                "not_allowed_token_type": "this is not an app-level token — "
                                          "generate one under App-Level Tokens",
                "missing_scope": "the app-level token needs the "
                                 "connections:write scope",
            }.get(err, "check Settings -> Socket Mode is toggled on")
            line(False, f"App token rejected: {err}", hint)
            ok = False

    if not bot_token:
        line(False, "SLACK_BOT_TOKEN is empty",
             "OAuth & Permissions -> Install to Workspace, then copy the "
             "Bot User OAuth Token")
        return 1
    if not bot_token.startswith("xoxb-"):
        line(False, f"SLACK_BOT_TOKEN does not look right: {bot_token[:12]}...",
             "A bot token starts with 'xoxb-'.")
        return 1

    body, headers = _slack_api("auth.test", bot_token)
    if not body.get("ok"):
        line(False, f"Bot token rejected: {body.get('error')}",
             "Reinstall the app: OAuth & Permissions -> Reinstall to Workspace")
        return 1
    bot_handle = body.get("user") or "your bot"
    line(True, f"Bot token valid — @{bot_handle} in {body.get('team')}",
         f"bot user id {body.get('user_id')}")

    raw_scopes = headers.get("x-oauth-scopes")
    if raw_scopes is None:
        # Absence of the header is absence of evidence, not evidence of
        # absence. Reporting it as "all scopes missing" sends you to redo
        # setup that was already correct.
        print("[  ??  ] Slack did not return the scope header; cannot verify "
              "scopes here.\n          The connection test above is the real "
              "answer.")
    else:
        granted = {s.strip() for s in raw_scopes.split(",") if s.strip()}
        missing = [s for s in REQUIRED_SCOPES if s not in granted]
        if missing:
            line(False, f"Missing {len(missing)} bot scope(s)",
                 "\n          ".join(f"{s:20} — {REQUIRED_SCOPES[s]}"
                                     for s in missing)
                 + "\n\n          Add them under OAuth & Permissions -> Bot "
                   "Token Scopes,\n          then Reinstall to Workspace (new "
                   "scopes need a reinstall).")
            ok = False
        else:
            line(True, f"All {len(REQUIRED_SCOPES)} required bot scopes granted",
                 f"granted: {', '.join(sorted(granted))}")

    # users.conversations needs channels:read, which this bot deliberately does
    # not request — it reads history in channels it was invited to and never
    # browses the workspace. So a missing_scope here says nothing about the
    # setup and must not be reported as a failure.
    body, _ = _slack_api("users.conversations", bot_token,
                         {"types": "public_channel,private_channel", "limit": 200})
    if body.get("ok"):
        channels = [c["name"] for c in body.get("channels", [])]
        if channels:
            line(True, f"Bot is in {len(channels)} channel(s)",
                 ", ".join("#" + c for c in channels[:10]))
        else:
            line(False, "Bot is not in any channel",
                 f"In Slack, open the alert channel and run: /invite @{bot_handle}")
            ok = False
    elif body.get("error") == "missing_scope":
        print(f"[  ??  ] Cannot list channels (needs channels:read, which this "
              f"bot\n          does not require). Make sure you ran "
              f"/invite @{bot_handle} in\n          the alert channel — it is "
              f"the step most often missed.")
    else:
        print(f"[  ??  ] Could not list channels: {body.get('error')}")

    print("-" * 66)
    if ok:
        print("Everything checks out. Event subscriptions cannot be verified\n"
              "through the API — confirm under Event Subscriptions that these\n"
              "bot events are subscribed: " + ", ".join(REQUIRED_EVENTS) + "\n\n"
              "Then: ./install_server.sh")
    else:
        print("Fix the FAIL lines above and run this again.")
    return 0 if ok else 1


# -- Slack -----------------------------------------------------------------

def run_slack(conf: dict, backend: llm.Backend) -> int:
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError:
        print("slack_sdk is missing. Install it into the bot's venv:\n"
              "  python3 -m venv .venv && .venv/bin/pip install slack_sdk",
              file=sys.stderr)
        return 1

    app_token = conf.get("SLACK_APP_TOKEN", "").strip()
    bot_token = conf.get("SLACK_BOT_TOKEN", "").strip()
    if not (app_token.startswith("xapp-") and bot_token.startswith("xoxb-")):
        print("SLACK_APP_TOKEN (xapp-...) and SLACK_BOT_TOKEN (xoxb-...) must "
              "both be set in config/server.env.\nThe incoming webhook used "
              "for alerts cannot receive messages — Socket Mode needs its own "
              "Slack app.", file=sys.stderr)
        return 1

    web = WebClient(token=bot_token)
    bot_user_id = web.auth_test()["user_id"]
    log.info("connected as bot user %s", bot_user_id)
    socket = SocketModeClient(app_token=app_token, web_client=web)
    seen = SeenEvents()

    def is_alert_thread(channel: str, thread_ts: str | None) -> bool:
        """Did this thread start with one of our own alerts?

        Checked by fetching the parent rather than by remembering message
        timestamps, so it keeps working for alerts posted before the bot
        started, and across restarts.
        """
        if not thread_ts:
            return False
        try:
            parent = web.conversations_replies(
                channel=channel, ts=thread_ts, limit=1)["messages"][0]
        except Exception:
            return False
        return ALERT_MARKER in parent.get("text", "")

    def on_event(client, req) -> None:
        # Acknowledge first, always. Slack retries anything unacknowledged
        # within 3s, and probing seven Jetsons takes longer than that — without
        # this, one question becomes a pile of duplicate answers.
        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return

        event = req.payload.get("event", {})
        if event.get("type") not in ("app_mention", "message"):
            return
        if event.get("bot_id") or event.get("subtype") or \
                event.get("user") == bot_user_id:
            return                       # never answer ourselves

        channel = event.get("channel", "")
        text = re.sub(rf"<@{bot_user_id}>", "", event.get("text", "")).strip()
        thread_ts = event.get("thread_ts")
        mentioned = f"<@{bot_user_id}>" in event.get("text", "")
        is_dm = event.get("channel_type") == "im"

        in_alert = is_alert_thread(channel, thread_ts)
        if not (mentioned or is_dm or in_alert):
            return                       # stay quiet in normal conversation

        # Claimed only once we have decided to answer, so a message we ignore
        # never occupies a slot.
        key = event_key(event)
        if not seen.claim(key):
            log.info("duplicate delivery of %s (%s); already answered",
                     key, event.get("type"))
            return

        reply_ts = thread_ts or event.get("ts")
        try:
            reply = handle(conf, text, in_alert, backend)
        except Exception:
            log.error("handler failed:\n%s", traceback.format_exc())
            reply = (":boom: I broke while answering. The guardian itself is "
                     "unaffected — it does not depend on me.\n"
                     f"```\n{traceback.format_exc()[-600:]}\n```")
        try:
            web.chat_postMessage(channel=channel, thread_ts=reply_ts, text=reply)
        except Exception:
            log.error("could not post the reply:\n%s", traceback.format_exc())

    socket.socket_mode_request_listeners.append(on_event)
    socket.connect()
    log.info("listening")
    from threading import Event
    Event().wait()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--ask", default=None,
                        help="answer one question locally and exit")
    parser.add_argument("--probe", default=None, metavar="TOPIC",
                        help="print the raw evidence for a topic and exit "
                             "(general|status|node|recording|alerts); no model "
                             "is called")
    parser.add_argument("--node", default=None,
                        help="restrict --probe to one node")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--check-slack", action="store_true",
                        help="validate the Slack app setup and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conf = common.load_env(args.config)

    if args.check_slack:
        return check_slack(conf)

    # --probe deliberately never loads a backend: it is the way to see what
    # the model would have been given, including when the model is the thing
    # that is broken.
    if args.probe:
        bundle = diagnose.collect(conf, args.probe, node_name=args.node)
        print(bundle.as_text())
        print(f"\n({len(bundle.facts)} facts, worst={bundle.worst}, "
              f"{bundle.elapsed}s)")
        return 0

    backend = llm.from_config(conf)
    if args.ask:
        print(handle(conf, args.ask, False, backend))
        return 0
    return run_slack(conf, backend)


if __name__ == "__main__":
    sys.exit(main())
