"""P6.3 load check: 10 req/s sustained against a live bot, per the testing brief.

```
python tools/load_test.py                # 60s at 10 req/s
python tools/load_test.py --seconds 20   # quicker smoke
```

Simulates the judge's request mix during the test window:
  * /v1/healthz poll (cheap, every poll)
  * /v1/context re-pushes at *higher* versions (adaptive injection, atomic replace)
  * /v1/tick with the full seed trigger list (first ticks compose, later ones are
    suppressed — both paths get load coverage)
  * /v1/reply turns (engaged text and canned auto-replies, exercising the
    conversation handler under load)

Gates (exit 1 on failure):
  * zero connection errors
  * zero unexpected non-200s (a 409 on a stale-version probe is CORRECT behavior)
  * healthz p95 < 5s, tick p95 < 15s, reply p95 < 15s (simulator client timeouts)
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

DATASET = ROOT / "dataset"
SCRATCH_PORT = 8095
RATE_PER_SEC = 10
LATENCY_GATES_MS = {"healthz": 5000, "tick": 15000, "reply": 15000, "context": 10000}

random.seed(20260903)  # deterministic request mix


def _free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in range")


def _wait_healthz(port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _load_seed_data() -> Dict[str, List[dict]]:
    def items(name: str, container: str) -> List[dict]:
        return json.loads((DATASET / name).read_text(encoding="utf-8"))[container]

    categories = []
    for path in sorted((DATASET / "categories").glob("*.json")):
        categories.append(json.loads(path.read_text(encoding="utf-8")))
    return {
        "categories": categories,
        "merchants": items("merchants_seed.json", "merchants"),
        "customers": items("customers_seed.json", "customers"),
        "triggers": items("triggers_seed.json", "triggers"),
    }


def _post(port: int, path: str, body: Optional[dict], timeout: float = 30.0) -> Tuple[int, Optional[dict], float]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload, (time.perf_counter() - start) * 1000
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = None
        return e.code, payload, (time.perf_counter() - start) * 1000


def _pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rate", type=int, default=RATE_PER_SEC)
    args = parser.parse_args()

    data = _load_seed_data()
    trigger_ids = [t["id"] for t in data["triggers"]]

    port = _free_port(SCRATCH_PORT)
    print(f"[load] starting bot on 127.0.0.1:{port} ...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bot.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    latencies: Dict[str, List[float]] = {"healthz": [], "context": [], "tick": [], "reply": []}
    errors: List[str] = []
    bad_status: List[str] = []
    expected_409 = [0]

    def record(name: str, status: int, body: Optional[dict], ms: float) -> None:
        latencies[name].append(ms)
        if status == 409 and isinstance(body, dict) and body.get("reason") == "stale_version":
            expected_409[0] += 1  # correct idempotency behaviour, not a failure
        elif status != 200:
            bad_status.append(f"{name} -> {status}")

    def do_healthz() -> None:
        status, body, ms = _post(port, "/v1/healthz", None, timeout=5)
        record("healthz", status, body, ms)

    def do_context(i: int) -> None:
        # Rotate merchants at rising versions: judge-style adaptive injection.
        merchant = data["merchants"][i % len(data["merchants"])]
        version = 10 + (i % 3)
        status, body, ms = _post(port, "/v1/context", {
            "scope": "merchant", "context_id": merchant["merchant_id"],
            "version": version, "payload": merchant, "delivered_at": "2026-04-26T11:00:00Z",
        })
        record("context", status, body, ms)

    def do_context_stale(i: int) -> None:
        # A stale-version probe: the bot MUST answer 409 (correct, counted as such).
        merchant = data["merchants"][i % len(data["merchants"])]
        status, body, ms = _post(port, "/v1/context", {
            "scope": "merchant", "context_id": merchant["merchant_id"],
            "version": 2, "payload": merchant, "delivered_at": "2026-04-26T11:00:00Z",
        })
        record("context", status, body, ms)

    def do_tick() -> None:
        status, body, ms = _post(port, "/v1/tick", {"now": "2026-04-26T11:00:00Z", "available_triggers": trigger_ids}, timeout=30)
        record("tick", status, body, ms)

    def do_reply(i: int) -> None:
        canned = i % 3 == 0
        message = "Thank you for contacting us! Our team will respond shortly." if canned else "Ok looks good, how do we start?"
        status, body, ms = _post(port, "/v1/reply", {
            "conversation_id": f"conv_load_{i}", "merchant_id": data["merchants"][i % len(data["merchants"])]["merchant_id"],
            "customer_id": None, "from_role": "merchant", "message": message,
            "received_at": "2026-04-26T11:00:00Z", "turn_number": 2,
        })
        record("reply", status, body, ms)

    try:
        if not _wait_healthz(port):
            print("[load] FAIL: bot never became healthy")
            return 2
        print("[load] bot healthy; pushing seed dataset (warmup shape) ...")

        # Warmup push: every category, merchant, customer, trigger — like the judge.
        for cat in data["categories"]:
            _post(port, "/v1/context", {"scope": "category", "context_id": cat["slug"], "version": 1, "payload": cat, "delivered_at": "2026-04-26T10:00:00Z"})
        for scope, key in (("merchant", "merchant_id"), ("customer", "customer_id")):
            for item in data["customers"] if scope == "customer" else data["merchants"]:
                _post(port, "/v1/context", {"scope": scope, "context_id": item[key], "version": 1, "payload": item, "delivered_at": "2026-04-26T10:00:00Z"})
        for trg in data["triggers"]:
            _post(port, "/v1/context", {"scope": "trigger", "context_id": trg["id"], "version": 1, "payload": trg, "delivered_at": "2026-04-26T10:00:00Z"})
        # Seed a version-9 merchant so the stale probe (v2) gets its 409.
        m0 = data["merchants"][0]
        _post(port, "/v1/context", {"scope": "merchant", "context_id": m0["merchant_id"], "version": 9, "payload": m0, "delivered_at": "2026-04-26T10:00:00Z"})

        print(f"[load] sustaining {args.rate} req/s for {args.seconds}s ...")
        interval = 1.0 / args.rate
        counter = 0
        start = time.monotonic()
        next_send = start
        while time.monotonic() - start < args.seconds:
            now = time.monotonic()
            if now >= next_send:
                # Judge-shaped mix: mostly cheap polls, ticks and replies, some context.
                kind = counter % 10
                if kind in (0, 3, 6, 8):
                    do_healthz()
                elif kind in (1, 5):
                    do_tick()
                elif kind in (2, 7):
                    do_reply(counter)
                elif kind == 4:
                    do_context(counter)
                elif kind == 9:
                    do_context_stale(counter)
                counter += 1
                next_send += interval
            else:
                time.sleep(min(0.05, max(0.0, next_send - now)))

        total = sum(len(v) for v in latencies.values())
        print(f"\n[load] sent {total} requests over {args.seconds}s (~{total / args.seconds:.1f} req/s)")
        print(f"[load] expected 409 stale-version responses (correct idempotency): {expected_409[0]}")

        ok = True
        for name in ("healthz", "context", "tick", "reply"):
            values = latencies[name]
            if not values:
                print(f"  {name:9} no samples")
                continue
            gate = LATENCY_GATES_MS[name]
            p50, p95, mx = _pct(values, 50), _pct(values, 95), max(values)
            line = f"  {name:9} n={len(values):4}  p50={p50:7.1f}ms  p95={p95:7.1f}ms  max={mx:8.1f}ms  (gate p95 < {gate}ms)"
            passed = p95 < gate
            ok = ok and passed
            print(line + ("" if passed else "  <-- FAIL"))

        if errors:
            ok = False
            print(f"[load] connection errors: {len(errors)}")
            for e in errors[:5]:
                print(f"    {e}")
        else:
            print("[load] connection errors: 0")
        if bad_status:
            ok = False
            print(f"[load] unexpected non-200s: {len(bad_status)}")
            for b in bad_status[:5]:
                print(f"    {b}")
        else:
            print("[load] unexpected non-200s: 0")

        print(f"[load] P6.3 gate: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"[load] bot on :{port} stopped")


if __name__ == "__main__":
    raise SystemExit(main())
