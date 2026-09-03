"""Run `judge_simulator.py` scenarios without editing the provided harness.

Why this exists
---------------
`judge_simulator.py` reads its configuration from module-level constants, not from
environment variables (reference.md gotcha G8), and its `main()` refuses to start
without a live LLM connection even for scenarios that never call the LLM. This
wrapper patches the constants from the git-ignored `secrets.local.json` and lets you
run the endpoint-contract scenarios offline.

Usage
-----
    python tools/run_local_judge.py warmup
    python tools/run_local_judge.py auto_reply_hell --offline
    python tools/run_local_judge.py full_evaluation          # needs LLM connectivity

Scenarios that need no LLM at all: warmup, auto_reply_hell, intent_transition, hostile.
Scenarios that score messages (and therefore need the key): phase2_short, full_evaluation, all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge_simulator as js  # noqa: E402
import llm_bridge  # noqa: E402

NO_LLM_SCENARIOS = {"warmup", "auto_reply_hell", "intent_transition", "hostile"}

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "deepseek-ai/deepseek-v4-flash-0731"


class OpenAICompatProvider(js.LLMProvider):
    """Any OpenAI-compatible /chat/completions endpoint (NVIDIA NIM, DeepSeek, Groq...).

    Added here rather than in `judge_simulator.py` because rules.md forbids editing
    the provided harness. Mirrors `js.OpenAIProvider` exactly (temperature 0.2, same
    message shape) so scores stay comparable with the official scorer; only the base
    URL differs and `max_tokens` is raised because reasoning models such as
    deepseek-v4 spend completion tokens on `reasoning_content` before the JSON.
    """

    def __init__(self, api_key: str, model: str = "", base_url: str = "", label: str = "OpenAI-compatible"):
        self.api_key = api_key
        self.model = model or NVIDIA_DEFAULT_MODEL
        self.base_url = (base_url or NVIDIA_BASE_URL).rstrip("/")
        self.label = label

    def name(self) -> str:
        return f"{self.label} ({self.model})"

    def complete(self, prompt: str, system: str = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 4000,
                "stream": False,
            }
        ).encode("utf-8")

        req = js.urlrequest.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        resp = js.urlrequest.urlopen(req, timeout=js.TIMEOUT_LLM)
        data = json.loads(resp.read().decode("utf-8"))
        # Reasoning models also return `reasoning_content`; only `content` holds the answer.
        return data["choices"][0]["message"]["content"]


class NvidiaProvider(OpenAICompatProvider):
    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        super().__init__(api_key, model or NVIDIA_DEFAULT_MODEL, base_url or NVIDIA_BASE_URL, "NVIDIA NIM")


class AnthropicCompatProvider(js.LLMProvider):
    """Anthropic Messages API against a non-Anthropic base URL (Z.ai GLM).

    `js.AnthropicProvider` hardcodes api.anthropic.com, so it cannot be reused; adding
    this here keeps `judge_simulator.py` untouched as rules.md requires. Temperature
    matches the harness's own 0.2 so scores stay comparable with the official scorer.

    Z.ai GLM free-tier capacity is intermittent (reference.md G18): sustained scoring
    runs hit HTTP 529 `overloaded_error`, and the harness's scorer has no retry — one
    529 permanently downgrades that message to the all-5s heuristic. This provider
    retries 529s with patient backoff (5→70 s, reference.md G18's verified curve),
    capped so a single hard outage cannot hang the run forever.
    """

    RETRY_STATUSES = (529, 500, 502, 503, 504)
    BACKOFF_S = (5, 10, 20, 35, 50, 70)

    def __init__(self, api_key: str, model: str = "", base_url: str = ""):
        self.api_key = api_key
        self.model = model or "glm-4.7-flash"
        self.base_url = (base_url or "https://api.z.ai/api/anthropic").rstrip("/")

    def name(self) -> str:
        return f"Z.ai / Anthropic-compatible ({self.model})"

    def complete(self, prompt: str, system: str = None) -> str:
        payload = {"model": self.model, "max_tokens": 4000, "temperature": 0.2,
                   "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        req = js.urlrequest.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json", "x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01"},
        )
        last_error: Exception | None = None
        for attempt, backoff in enumerate(self.BACKOFF_S):
            try:
                resp = js.urlrequest.urlopen(req, timeout=js.TIMEOUT_LLM)
                data = json.loads(resp.read().decode("utf-8"))
                return "".join(
                    str(b.get("text") or "") for b in data.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            except js.urlerror.HTTPError as e:
                last_error = e
                if e.code in self.RETRY_STATUSES and attempt < len(self.BACKOFF_S) - 1:
                    time.sleep(backoff)
                    continue
                raise
        raise last_error if last_error else RuntimeError("retry loop exhausted")


class BridgeProvider(js.LLMProvider):
    """Route scoring calls through `runs/bridge/` (see tools/llm_bridge.py).

    Used when this shell has no outbound HTTPS: a companion process with network
    access answers the request files. The harness itself is untouched, so scenario
    logic, prompts and pass/fail rules stay exactly as provided.
    """

    def __init__(self, label: str = "file-bridge"):
        self.label = label

    def name(self) -> str:
        return f"{self.label} -> {js.LLM_MODEL or NVIDIA_DEFAULT_MODEL}"

    def complete(self, prompt: str, system: str = None) -> str:
        # Free-tier providers answer behind long 529 backoffs; the harness itself has
        # no deadline here, so give the bridge room rather than losing the score.
        return llm_bridge.ask(prompt, system, kind="score", timeout=1200.0)


class OfflineProvider(js.LLMProvider):
    """Stand-in provider for scenarios that never actually score a message."""

    def name(self) -> str:
        return "offline-stub (no scoring)"

    def complete(self, prompt: str, system: str = None) -> str:  # noqa: D102
        raise RuntimeError(
            "This scenario tried to score a message, which needs a real LLM. "
            "Re-run without --offline and make sure secrets.local.json has a working key."
        )


def load_secrets() -> dict:
    path = ROOT / "secrets.local.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?", default="warmup")
    parser.add_argument("--bot-url", default=None)
    parser.add_argument("--offline", action="store_true", help="use a stub LLM provider")
    parser.add_argument("--bridge", action="store_true", help="route LLM calls through runs/bridge")
    args = parser.parse_args()

    secrets = load_secrets()
    js.BOT_URL = args.bot_url or secrets.get("BOT_URL", js.BOT_URL)
    js.LLM_PROVIDER = secrets.get("LLM_PROVIDER", js.LLM_PROVIDER)
    js.LLM_MODEL = secrets.get("LLM_MODEL", js.LLM_MODEL)
    js.LLM_API_KEY = secrets.get("LLM_API_KEY", js.LLM_API_KEY)

    offline = args.offline or (args.scenario in NO_LLM_SCENARIOS and not js.LLM_API_KEY)
    if offline:
        llm = OfflineProvider()
    elif args.bridge:
        llm = BridgeProvider()
    elif str(js.LLM_PROVIDER).lower() in ("zai", "z.ai", "glm", "anthropic-compatible"):
        llm = AnthropicCompatProvider(
            js.LLM_API_KEY, js.LLM_MODEL, secrets.get("LLM_BASE_URL", "")
        )
    elif str(js.LLM_PROVIDER).lower() in ("nvidia", "nim"):
        llm = NvidiaProvider(
            js.LLM_API_KEY, js.LLM_MODEL, secrets.get("LLM_BASE_URL", NVIDIA_BASE_URL)
        )
    elif str(js.LLM_PROVIDER).lower() in ("openai-compatible", "custom"):
        llm = OpenAICompatProvider(
            js.LLM_API_KEY, js.LLM_MODEL, secrets.get("LLM_BASE_URL", "")
        )
    else:
        llm = js.create_provider()

    judge = js.JudgeSimulator(llm)
    # BotClient captured BOT_URL at import time in some versions; rebuild it to be safe.
    judge.client = js.BotClient(js.BOT_URL)
    ok = judge.run(args.scenario)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
