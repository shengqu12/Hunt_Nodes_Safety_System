"""The explaining layer.

The model's entire job is to turn a bundle of already-collected facts into a
short answer. It is given no tools, no shell, and no way to fetch anything, so
the worst it can do is describe the evidence badly — it can never invent a
system state and then act on it.

Backends are pluggable because the tradeoff is real: a local model keeps fleet
telemetry on the lab network and costs nothing, a hosted one reasons better.
The prompt is identical either way.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

SYSTEM_PROMPT = """You are the diagnostic assistant for a ceiling-LiDAR \
sensor fleet in a university library.

The system has two kinds of machine, and confusing them is the most damaging \
mistake you can make:
- **puget**, the lab server. It monitors the fleet and it is also where \
RECORDING runs (record_supervisor.py). No recording process runs on a node.
- **node1..node7**, Jetson Nanos on the ceiling, each with a Livox LiDAR. \
Each node runs a watchdog that writes a status.json heartbeat.

You will be given EVIDENCE: facts already collected by read-only probes, each \
marked [OK], [WARN], [BAD], [??] (could not be determined) or [--] (context). \
Each fact says what was measured and on which host.

Rules you must follow:
1. State only what the EVIDENCE supports. Never invent a file, process, \
command, log line, number, node or error that is not there.
2. If the evidence does not answer the question, say exactly what is missing. \
"I cannot tell from this" is a correct and useful answer.
3. Respect the freshness each fact declares. A fact from the last monitor \
pass is a recollection, not a current measurement; do not present it as live.
4. Never attribute a fact to the wrong machine. If a fact says it was \
measured on puget, do not describe it as something happening on a node.
5. Lead with the most likely cause, then the single most useful next step for \
a human. Name the specific thing to look at.
6. A node being off the network, a node whose heartbeat is stale, and a node \
whose LiDAR has failed are three different faults with three different next \
steps. Do not merge them.
7. If the evidence is all [OK], say the system looks healthy and name the one \
number that would change your mind.
8. Be brief: at most 4 short sentences. This is read on a phone, in Slack.
9. Answer in the language the user asked in."""


class LLMError(RuntimeError):
    pass


class Backend:
    def complete(self, question: str, evidence: str) -> str:
        raise NotImplementedError


class OllamaBackend(Backend):
    """A local model over ollama's HTTP API. No key, no egress, no cost."""

    def __init__(self, model: str = "gemma3:27b",
                 url: str = "http://localhost:11434", timeout: float = 120.0,
                 num_predict: int = 260, keep_alive: str = "30m"):
        self.model = model
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.num_predict = num_predict
        # Hold the model in VRAM between questions. A 17 GB model takes ~10s
        # to load, and ollama answers 500 to anything that arrives while it is
        # loading — which is exactly what a duplicate Slack delivery did.
        self.keep_alive = keep_alive

    def complete(self, question: str, evidence: str) -> str:
        payload = {
            "model": self.model,
            "system": SYSTEM_PROMPT,
            "prompt": f"EVIDENCE:\n{evidence}\n\nQUESTION: {question}\n\nANSWER:",
            "stream": False,
            # Low temperature: this is reading comprehension over facts, not a
            # creative task, and a confident wrong diagnosis is the main risk
            # this whole design is arranged against.
            "options": {"temperature": 0.2, "num_predict": self.num_predict},
            "keep_alive": self.keep_alive,
        }
        body = json.dumps(payload).encode()

        last = None
        # One retry, and only for a server-side failure. A cold load takes
        # ~10s and returns 500 to anything arriving during it; retrying a
        # model that is merely slow would just queue another 17 GB load.
        for attempt in (1, 2):
            req = urllib.request.Request(
                f"{self.url}/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                last = exc
                if attempt == 1 and 500 <= exc.code < 600:
                    time.sleep(3)
                    continue
                raise LLMError(
                    f"ollama at {self.url} returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LLMError(
                    f"ollama at {self.url} did not answer: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise LLMError(f"ollama returned non-JSON: {exc}") from exc

            text = (data.get("response") or "").strip()
            if not text:
                raise LLMError("ollama returned an empty answer")
            return text
        raise LLMError(f"ollama at {self.url} failed twice: {last}")


class ClaudeBackend(Backend):
    """Anthropic Messages API. Better at this; sends the evidence off-site."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-5",
                 timeout: float = 60.0, max_tokens: int = 400):
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is empty")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._ssl = ssl.create_default_context()

    def complete(self, question: str, evidence: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user",
                          "content": f"EVIDENCE:\n{evidence}\n\nQUESTION: {question}"}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ssl) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise LLMError(f"Claude API HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Claude API unreachable: {exc}") from exc
        parts = [b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise LLMError("Claude returned an empty answer")
        return text


def from_config(conf: dict) -> Backend:
    backend = (conf.get("LLM_BACKEND") or "ollama").strip().lower()
    if backend == "claude":
        key = conf.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        return ClaudeBackend(key, model=conf.get("LLM_MODEL") or "claude-sonnet-5")
    if backend == "ollama":
        return OllamaBackend(
            model=conf.get("LLM_MODEL") or "gemma3:27b",
            url=conf.get("OLLAMA_URL") or "http://localhost:11434")
    raise LLMError(f"unknown LLM_BACKEND {backend!r}; use 'ollama' or 'claude'")
