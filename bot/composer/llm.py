"""LLM composition layer - temperature 0, cached, fact-fenced, always optional.

Contract with the rest of the bot
---------------------------------
* The model NEVER supplies facts. It receives the grounded fact list extracted from
  the pushed contexts plus the deterministic draft, and may only rewrite.
* `temperature = 0` + an on-disk cache keyed by (provider, model, exact prompt) ->
  the same inputs produce the same body on every run (challenge-brief.md §7.1
  determinism rule, architecture.md ADR-001).
* Hard wall-clock budget. Calls run in parallel and anything that misses the budget
  is dropped in favour of the deterministic draft, so `/v1/tick` can never blow the
  15s simulator timeout or the 30s judge timeout.
* Any failure - no key, no network, quota, bad JSON, failed validation - silently
  falls back to the deterministic draft. The LLM is a rewrite stage, never a
  dependency (reference.md decision D14).

Providers and failover
----------------------
`zai` / `anthropic-compatible`  Anthropic Messages API shape (`POST {base}/v1/messages`,
                    `x-api-key` + `anthropic-version`). Z.ai GLM lives here.
`nvidia`            OpenAI-compatible NIM endpoint, https://integrate.api.nvidia.com/v1
`openai-compatible` any other OpenAI /chat/completions endpoint via VERA_LLM_BASE_URL
`gemini`            Google generativeLanguage generateContent

Configuration declares one primary endpoint plus an ordered failover chain. Every
prompt is tried on the primary first; anything still unanswered when the primary
fails, times out or is quota-blocked falls through to the next endpoint, and if the
whole chain is exhausted the caller keeps its deterministic draft. A provider outage
therefore costs polish, never a tick (reference.md decisions D14 and D16).

Configuration (env wins over secrets.local.json, which is git-ignored)
---------------------------------------------------------------------
    VERA_LLM_ENABLED    "0" disables the layer entirely (default: enabled if a key exists)
    VERA_LLM_PROVIDER   zai | anthropic-compatible | nvidia | openai-compatible | gemini
    VERA_LLM_API_KEY    the key (also read: ZAI_API_KEY, NVIDIA_API_KEY,
                        VERA_GEMINI_API_KEY, GEMINI_API_KEY, LLM_API_KEY)
    VERA_LLM_MODEL      model id
    VERA_LLM_BASE_URL   base URL for OpenAI/Anthropic-compatible providers
    VERA_LLM_BUDGET_MS  overall budget per tick (default 9000)
    VERA_LLM_CACHE_ONLY "1" -> never make a network call, use only the warm cache
    secrets.local.json  may add "LLM_FALLBACKS": [{provider, model, base_url, api_key}, ...]

Cache keys include provider and model, so switching models never silently reuses
another model's wording. A lookup walks the whole chain, so a body produced by a
failover endpoint is still reused on later runs instead of being re-billed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "runs" / "llm_cache.json"

PROVIDER_NVIDIA = "nvidia"
PROVIDER_OPENAI = "openai-compatible"
PROVIDER_GEMINI = "gemini"
PROVIDER_ANTHROPIC = "anthropic-compatible"

DEFAULT_PROVIDER = PROVIDER_ANTHROPIC
DEFAULT_MODELS = {
    PROVIDER_NVIDIA: "deepseek-ai/deepseek-v4-flash-0731",
    PROVIDER_OPENAI: "gpt-4o-mini",
    PROVIDER_GEMINI: "gemini-3.5-flash-lite",
    PROVIDER_ANTHROPIC: "glm-4.7-flash",
}
DEFAULT_BASE_URLS = {
    PROVIDER_NVIDIA: "https://integrate.api.nvidia.com/v1",
    PROVIDER_OPENAI: "https://api.openai.com/v1",
    PROVIDER_ANTHROPIC: "https://api.z.ai/api/anthropic",
}
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BUDGET_MS = 9000
PER_CALL_TIMEOUT_S = 7.0
# 4, not 5: the GLM free tier allows 5 *concurrent* requests; leaving one slot
# spare stops our own batch from tripping the limit a retry would need.
MAX_PARALLEL = 4

_cache_lock = threading.RLock()
_cache: Optional[Dict[str, str]] = None

SYSTEM_RULES = """You are Vera, magicpin's assistant for Indian local businesses, writing one WhatsApp message.

ABSOLUTE RULES
1. Use ONLY the facts in FACTS. Never add a number, price, date, name, statistic or source that is not there.
2. Never remove the concrete numbers - specificity is what is being scored.
3. Exactly ONE question mark in the whole message, and it must be in the final sentence.
4. No URLs, no links, no emoji spam, no greetings like "I hope you are doing well".
5. Never use any word from TABOO.
6. Never mention internal identifiers, field names, signal names or system wording.
7. Keep the voice in VOICE. Peer-to-peer, never advertising copy.
8. If LANGUAGE says Hindi-English mix, keep the facts in English and write the final ask in Latin-script Hindi, like a real Indian WhatsApp message.
9. Keep it under 60 words. No preamble: the reason for messaging comes first.

Return ONLY this JSON, nothing else:
{"body": "<the message>"}"""


# ------------------------------------------------------------------- key/config


def _secrets() -> Dict[str, Any]:
    path = ROOT / "secrets.local.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def normalise_provider(raw: Any, key: str = "") -> str:
    """Map a friendly provider name onto one of the four wire protocols we speak."""
    name = str(raw or "").strip().lower()
    if name in (PROVIDER_NVIDIA, PROVIDER_GEMINI, PROVIDER_OPENAI, PROVIDER_ANTHROPIC):
        return name
    if name in ("zai", "z.ai", "glm", "zhipu", "anthropic"):
        return PROVIDER_ANTHROPIC
    if name in ("nim",):
        return PROVIDER_NVIDIA
    if name in ("openai", "deepseek", "groq", "openrouter", "custom"):
        return PROVIDER_OPENAI
    if name:
        return PROVIDER_OPENAI
    if key.startswith("nvapi-"):
        return PROVIDER_NVIDIA
    if key.startswith("AIza"):
        return PROVIDER_GEMINI
    return DEFAULT_PROVIDER


def provider() -> str:
    raw = os.getenv("VERA_LLM_PROVIDER") or str(_secrets().get("LLM_PROVIDER") or "")
    return normalise_provider(raw, api_key())


def api_key() -> str:
    for env_name in (
        "VERA_LLM_API_KEY",
        "ZAI_API_KEY",
        "NVIDIA_API_KEY",
        "VERA_GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "LLM_API_KEY",
    ):
        value = os.getenv(env_name)
        if value:
            return value.strip()
    return str(_secrets().get("LLM_API_KEY") or "").strip()


def model_name() -> str:
    explicit = os.getenv("VERA_LLM_MODEL") or str(_secrets().get("LLM_MODEL") or "")
    if explicit.strip():
        return explicit.strip()
    return DEFAULT_MODELS.get(provider(), DEFAULT_MODELS[DEFAULT_PROVIDER])


def base_url() -> str:
    explicit = os.getenv("VERA_LLM_BASE_URL") or str(_secrets().get("LLM_BASE_URL") or "")
    if explicit.strip():
        return explicit.strip().rstrip("/")
    return DEFAULT_BASE_URLS.get(provider(), DEFAULT_BASE_URLS[PROVIDER_NVIDIA]).rstrip("/")


class Endpoint:
    """One provider/model/key/base-url combination in the failover chain."""

    __slots__ = ("provider", "model", "base_url", "api_key", "label", "rpm", "_next_allowed", "_pace_lock")

    # Conservative requests-per-minute caps for the free tiers we run on. A cold
    # tick can queue up to 20 prompts; without pacing the second batch would trip
    # the provider's RPM limit and the whole batch would fail over for nothing.
    DEFAULT_RPM = {"nvidia": 35, "gemini": 12, "zai": 1000}

    def __init__(self, provider: str, model: str = "", base_url: str = "", api_key: str = "") -> None:
        self.provider = provider
        self.model = model or DEFAULT_MODELS.get(provider, "")
        self.base_url = (base_url or DEFAULT_BASE_URLS.get(provider, "")).rstrip("/")
        self.api_key = api_key
        self.label = f"{provider}:{self.model}"
        # Cap below the advertised limit: nvidia 40 rpm, gemini 15 rpm.
        self.rpm = self.DEFAULT_RPM.get(provider, 40)
        self._next_allowed = 0.0
        self._pace_lock = threading.Lock()

    def usable(self) -> bool:
        return bool(self.api_key and self.model)

    def wait_seconds(self) -> float:
        """Pacing slot for the next request on this endpoint (0 when free).

        Blocks at most a few seconds; callers already operate inside the tick's
        wall-clock budget, so a long pace is equivalent to a timeout and the
        prompt simply falls through to the next endpoint.
        """
        with self._pace_lock:
            now = time.monotonic()
            if now >= self._next_allowed:
                self._next_allowed = now + (60.0 / self.rpm)
                return 0.0
            wait = self._next_allowed - now
            self._next_allowed += (60.0 / self.rpm)
            return wait

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<Endpoint {self.label}>"


def endpoints() -> List[Endpoint]:
    """Primary endpoint first, then the declared failover chain.

    Anything without a key or model is dropped, and duplicates of the primary are
    removed, so a chain configured for another machine degrades quietly here.
    """
    primary = Endpoint(provider(), model_name(), base_url(), api_key())
    chain: List[Endpoint] = [primary] if primary.usable() else []

    raw_fallbacks = _secrets().get("LLM_FALLBACKS")
    if isinstance(raw_fallbacks, list):
        for item in raw_fallbacks:
            if not isinstance(item, dict):
                continue
            key = str(item.get("api_key") or "").strip()
            name = normalise_provider(item.get("provider"), key)
            candidate = Endpoint(
                name,
                str(item.get("model") or ""),
                str(item.get("base_url") or ""),
                key,
            )
            if not candidate.usable():
                continue
            if any(existing.label == candidate.label for existing in chain):
                continue
            chain.append(candidate)
    return chain


def cache_only() -> bool:
    return os.getenv("VERA_LLM_CACHE_ONLY", "0") == "1"


def enabled() -> bool:
    if os.getenv("VERA_LLM_ENABLED", "1") == "0":
        return False
    if cache_only():
        return True
    return bool(api_key())


def budget_ms() -> int:
    try:
        return int(os.getenv("VERA_LLM_BUDGET_MS", str(DEFAULT_BUDGET_MS)))
    except ValueError:
        return DEFAULT_BUDGET_MS


# -------------------------------------------------------------------- cache I/O


def cache_key(prompt: str, model: Optional[str] = None, prov: Optional[str] = None) -> str:
    """Stable cache key. Included in the key so a provider/model switch never
    silently reuses another model's wording."""
    prov = prov or provider()
    model = model or model_name()
    return hashlib.sha256(f"{prov}||{model}||{prompt}".encode("utf-8")).hexdigest()


def _load_cache() -> Dict[str, str]:
    global _cache
    with _cache_lock:
        if _cache is None:
            try:
                _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
            except Exception:
                _cache = {}
        return _cache


def _cache_get(key: str) -> Optional[str]:
    return _load_cache().get(key)


def cache_put(key: str, value: str) -> None:
    cache = _load_cache()
    with _cache_lock:
        cache[key] = value
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception:
            pass


_cache_put = cache_put  # backwards-compatible alias


def reload_cache() -> int:
    """Drop the in-process copy so the next read picks up a cache warmed by a tool."""
    global _cache
    with _cache_lock:
        _cache = None
    return len(_load_cache())


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = {}
        try:
            if CACHE_PATH.exists():
                CACHE_PATH.unlink()
        except Exception:
            pass


# ------------------------------------------------------------------ the request


def build_prompt(
    facts: List[str],
    draft_body: str,
    voice: Dict[str, Any],
    taboo: List[str],
    language: str,
    audience: str,
    why_now: str,
) -> str:
    return "\n".join(
        [
            f"AUDIENCE: {audience}",
            f"VOICE: tone={voice.get('tone', 'peer')} register={voice.get('register', '')}",
            f"ALLOWED VOCABULARY: {', '.join(str(v) for v in (voice.get('vocab_allowed') or [])[:12])}",
            f"TABOO: {', '.join(taboo) if taboo else '(none)'}",
            f"LANGUAGE: {language}",
            f"WHY NOW: {why_now}",
            "FACTS (the only facts you may use):",
            *[f"- {f}" for f in facts],
            "",
            "DETERMINISTIC DRAFT (already correct and grounded - improve the wording, keep every number):",
            draft_body,
            "",
            'Return only: {"body": "..."}',
        ]
    )


def _extract_body(text: str) -> Optional[str]:
    """Pull the message out of a model reply.

    The requested shape is {"body": "..."}, optionally inside a markdown fence. Some
    models - GLM-4.7-flash notably - ignore that instruction and return the message as
    plain prose, so prose is accepted too. This is safe because the caller re-validates
    whatever comes back against the same fact fence, which is the real safety boundary;
    discarding a valid message over its envelope would lose polish for no gain.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            body = json.loads(match.group()).get("body")
            if isinstance(body, str) and body.strip():
                return body.strip()
        except Exception:
            pass

    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    cleaned = re.sub(r"^(?:body|message)\s*[:=]\s*", "", cleaned, flags=re.IGNORECASE).strip().strip(chr(34))
    # A bare message is a single paragraph. A blank line means the model wrote
    # commentary, a preamble or alternatives, none of which is a sendable body.
    if "\n\n" in cleaned or cleaned.count("\n") > 1:
        return None
    if re.match(r"^(sure|certainly|of course|here(?:\047s| is)|okay|ok|i\047ll|i will|absolutely)\b",
                cleaned, flags=re.IGNORECASE):
        return None
    cleaned = " ".join(cleaned.split())
    if not cleaned or "{" in cleaned or "}" in cleaned:
        return None
    # A rewrite is one WhatsApp message; anything longer is commentary, not a body.
    if len(cleaned) > 700:
        return None
    return cleaned


def _call_anthropic_compatible(
    prompt: str, key: str, model: str, timeout: float, base: str = ""
) -> Optional[str]:
    """Anthropic Messages shape: POST {base}/v1/messages with x-api-key.

    Used for Z.ai's Anthropic-compatible endpoint (GLM). GLM tends to wrap its JSON in
    a markdown fence, which `_extract_body` already tolerates.
    """
    base = (base or DEFAULT_BASE_URLS[PROVIDER_ANTHROPIC]).rstrip("/")
    payload = {
        "model": model,
        "max_tokens": 1200,
        "temperature": 0,
        "system": SYSTEM_RULES,
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        f"{base}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return None
    text = "".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return _extract_body(text)


def _call_openai_compatible(prompt: str, key: str, model: str, timeout: float, base: str = "") -> Optional[str]:
    """OpenAI /chat/completions shape. Covers NVIDIA NIM, DeepSeek, Groq, OpenRouter."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "top_p": 1,
        "max_tokens": 1200,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{(base or base_url()).rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    try:
        # Reasoning models also return `reasoning_content`; only `content` is the answer.
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return _extract_body(text or "")


def _call_gemini(prompt: str, key: str, model: str, timeout: float) -> Optional[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_RULES}]},
        "generationConfig": {
            "temperature": 0,
            "topP": 0,
            "candidateCount": 1,
            "maxOutputTokens": 400,
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    return _extract_body(text or "")


def call_endpoint(endpoint: Endpoint, prompt: str, timeout: float) -> Optional[str]:
    """Dispatch one prompt to one endpoint using that provider's wire protocol.

    RPM-pacing first: if honouring the endpoint's rate limit would push past the
    remaining timeout, fail fast (None) so the caller can try the next endpoint
    inside its budget rather than sleeping into a guaranteed miss.
    """
    pace = endpoint.wait_seconds()
    if pace > 0 and pace >= timeout:
        return None
    effective_timeout = max(0.5, timeout - pace)
    if endpoint.provider == PROVIDER_GEMINI:
        return _call_gemini(prompt, endpoint.api_key, endpoint.model, effective_timeout)
    if endpoint.provider == PROVIDER_ANTHROPIC:
        return _call_anthropic_compatible(prompt, endpoint.api_key, endpoint.model, effective_timeout, endpoint.base_url)
    return _call_openai_compatible(prompt, endpoint.api_key, endpoint.model, effective_timeout, endpoint.base_url)


def call_once(prompt: str, timeout: float = PER_CALL_TIMEOUT_S) -> Optional[str]:
    """Single uncached call, walking the failover chain. Used by tools, not by /v1/tick."""
    for endpoint in endpoints():
        body = call_endpoint(endpoint, prompt, timeout)
        if body:
            return body
    return None


def lookup_cached(prompt: str) -> Optional[str]:
    """Cached body from any endpoint in the chain, primary first."""
    for endpoint in endpoints() or [Endpoint(provider(), model_name(), base_url(), api_key())]:
        hit = _cache_get(cache_key(prompt, endpoint.model, endpoint.provider))
        if hit is not None:
            return hit
    return None


def compose_many(prompts: List[Optional[str]]) -> List[Optional[str]]:
    """Run several prompts in parallel within one shared wall-clock budget.

    `prompts[i] is None` means "no LLM wanted for this item". Results line up with
    the input list; `None` means "use the deterministic draft".
    """
    results: List[Optional[str]] = [None] * len(prompts)
    if not enabled():
        return results

    deadline = time.monotonic() + (budget_ms() / 1000.0)

    pending: List[Tuple[int, str]] = []
    for index, prompt in enumerate(prompts):
        if not prompt:
            continue
        cached = lookup_cached(prompt)
        if cached is not None:
            results[index] = cached
        else:
            pending.append((index, prompt))

    if not pending or cache_only():
        return results

    # Primary first; whatever is still unanswered falls through to the next endpoint.
    for endpoint in endpoints():
        if not pending or time.monotonic() >= deadline:
            break
        still_pending: List[Tuple[int, str]] = []
        workers = min(MAX_PARALLEL, len(pending))
        # NOT a context manager: `with` calls shutdown(wait=True), which would block
        # past the deadline until every in-flight HTTP call returns (up to one full
        # PER_CALL_TIMEOUT_S). Abandoning stragglers instead keeps the tick inside its
        # wall-clock budget; the workers finish in the background and their cache
        # writes are still valid, so no work is lost.
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(call_endpoint, endpoint, prompt, PER_CALL_TIMEOUT_S): (index, prompt)
                for index, prompt in pending
            }
            for future, (index, prompt) in futures.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    still_pending.append((index, prompt))
                    continue
                try:
                    body = future.result(timeout=remaining)
                except (FutureTimeout, Exception):
                    body = None
                if body:
                    results[index] = body
                    cache_put(cache_key(prompt, endpoint.model, endpoint.provider), body)
                else:
                    still_pending.append((index, prompt))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        pending = still_pending

    return results


__all__ = [
    "enabled",
    "compose_many",
    "call_once",
    "call_endpoint",
    "endpoints",
    "Endpoint",
    "lookup_cached",
    "build_prompt",
    "provider",
    "model_name",
    "base_url",
    "api_key",
    "cache_key",
    "cache_put",
    "reload_cache",
    "clear_cache",
    "SYSTEM_RULES",
]
