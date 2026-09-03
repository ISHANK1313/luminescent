"""File-based LLM bridge.

Why this exists
---------------
The agent shell that runs the bot and the harness has no outbound HTTPS (DNS
resolves, TCP/443 times out - see reference.md §10). A separate process that *does*
have network access watches `runs/bridge/` and answers requests, so both the judge
harness and the cache-warming tool can reach the model without either of them
opening a socket.

Protocol (one file per call, atomic rename so a watcher never reads a partial file)
----------------------------------------------------------------------------------
    runs/bridge/req_<seq>.json   {"id", "system", "prompt", "kind", "created_at"}
    runs/bridge/res_<seq>.json   {"id", "text"} or {"id", "error"}

Nothing here is used in production: the deployed bot talks to the provider directly
through `bot/composer/llm.py`. This is a local development shim only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = ROOT / "runs" / "bridge"
DEFAULT_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 0.25


def _next_seq() -> int:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    existing = [p.stem for p in BRIDGE_DIR.glob("req_*.json")]
    numbers = []
    for stem in existing:
        try:
            numbers.append(int(stem.split("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return (max(numbers) + 1) if numbers else 1


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def ask(prompt: str, system: Optional[str] = None, kind: str = "score",
        timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Blocking call. Raises RuntimeError on bridge error or timeout."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    seq = _next_seq()
    req_path = BRIDGE_DIR / f"req_{seq}.json"
    res_path = BRIDGE_DIR / f"res_{seq}.json"
    call_id = f"{kind}-{seq}"

    _write_atomic(
        req_path,
        {
            "id": call_id,
            "kind": kind,
            "system": system or "",
            "prompt": prompt,
            "created_at": time.time(),
        },
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if res_path.exists():
            try:
                data = json.loads(res_path.read_text(encoding="utf-8"))
            except Exception:
                time.sleep(POLL_INTERVAL_S)
                continue
            if data.get("error"):
                raise RuntimeError(f"bridge error: {data['error']}")
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                return text
            raise RuntimeError("bridge returned an empty response")
        time.sleep(POLL_INTERVAL_S)

    raise RuntimeError(f"bridge timeout after {timeout:.0f}s (no {res_path.name})")


def reset() -> int:
    """Delete all request/response files. Returns how many were removed."""
    if not BRIDGE_DIR.exists():
        return 0
    removed = 0
    for path in BRIDGE_DIR.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    for path in BRIDGE_DIR.glob("*.tmp"):
        try:
            path.unlink()
        except OSError:
            pass
    return removed


__all__ = ["ask", "reset", "BRIDGE_DIR"]
