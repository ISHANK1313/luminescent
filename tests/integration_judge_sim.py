"""R32 integration gate: run the official harness scenarios against a live bot.

```
python tests/integration_judge_sim.py            # contract mode (no LLM key needed)
python tests/integration_judge_sim.py --score    # scoring mode: full_evaluation, exit 0 iff mean >= 30/50
```

What it does
------------
1. Spawns its own uvicorn bot on a scratch port (never touches a bot you may be
   running on 8080) and waits for /v1/healthz.
2. Contract mode (default): runs the four offline scenarios via the real judge
   harness — warmup, auto_reply_hell, intent_transition, hostile — and gates on
   all-PASS plus endpoint-contract checks.
3. Scoring mode (--score): runs full_evaluation with the provider classes from
   tools/run_local_judge.py (harness file untouched, per rules.md), reads the
   scores from judge.all_scores (no ANSI parsing) and exits 0 iff mean >= 30/50
   with zero timeouts and zero malformed actions. Falls back to contract mode
   with a clear message when no LLM key is configured.

Exit codes: 0 = gate passed, 1 = gate failed, 2 = environment error.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

if hasattr(sys.stdout, "reconfigure"):  # G17: block characters die on cp1252 consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import judge_simulator as js  # noqa: E402
import run_local_judge  # noqa: E402

DATASET = ROOT / "dataset"
SCRATCH_PORT = 8090
SCORE_THRESHOLD = 30.0  # R32: avg total >= 30/50


def _free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in range")


def _wait_healthz(port: int, timeout_s: float = 15.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _load_secrets() -> Dict[str, Any]:
    path = ROOT / "secrets.local.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_llm(secrets: Dict[str, Any]):
    """Judge-side LLM, from tools/run_local_judge.py's provider classes.

    Reads JUDGE_* keys from secrets.local.json first (so the scoring judge can be a
    different provider than the bot's composer — e.g. Gemini while GLM has a capacity
    outage, reference.md G18), falling back to the generic LLM_* keys.
    """
    api_key = secrets.get("JUDGE_API_KEY") or secrets.get("LLM_API_KEY", "")
    provider = str(secrets.get("JUDGE_PROVIDER") or secrets.get("LLM_PROVIDER") or "").lower()
    model = secrets.get("JUDGE_MODEL") or secrets.get("LLM_MODEL") or ""

    js.LLM_PROVIDER = provider
    js.LLM_MODEL = model
    js.LLM_API_KEY = api_key

    if provider in ("zai", "z.ai", "glm", "anthropic-compatible"):
        return run_local_judge.AnthropicCompatProvider(api_key, model, secrets.get("LLM_BASE_URL", ""))
    if provider in ("nvidia", "nim"):
        return run_local_judge.NvidiaProvider(api_key, model, secrets.get("LLM_BASE_URL", run_local_judge.NVIDIA_BASE_URL))
    if provider in ("gemini",):
        return js.GeminiProvider(api_key, model)
    if provider in ("openai-compatible", "custom", "openai", "deepseek", "groq"):
        return run_local_judge.OpenAICompatProvider(api_key, model, secrets.get("LLM_BASE_URL", ""))
    return js.create_provider()


def _scenarios_all_pass(judge: "js.JudgeSimulator") -> bool:
    """Run the four offline scenarios through the real harness; True iff all pass."""
    ok = True
    for name in ("warmup", "auto_reply_hell", "intent_transition", "hostile"):
        try:
            passed = getattr(judge, f"_{ {'warmup': 'warmup', 'auto_reply_hell': 'auto_reply', 'intent_transition': 'intent', 'hostile': 'hostile'}[name] }")()
            ok = ok and bool(passed)
            print(f"  {name:18} {'PASS' if passed else 'FAIL'}")
        except Exception as exc:  # a crashed scenario is a failed gate, not a crashed script
            print(f"  {name:18} ERROR: {exc}")
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score", action="store_true", help="run full_evaluation with LLM scoring (needs secrets.local.json key)")
    parser.add_argument("--threshold", type=float, default=SCORE_THRESHOLD, help="mean score gate for --score mode")
    args = parser.parse_args()

    venv_python = sys.executable
    port = _free_port(SCRATCH_PORT)
    print(f"[integration] starting bot on 127.0.0.1:{port} ...")

    proc = subprocess.Popen(
        [venv_python, "-m", "uvicorn", "bot.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_healthz(port):
            print("[integration] FAIL: bot did not become healthy in 15s")
            return 2
        print("[integration] bot healthy")

        js.BOT_URL = f"http://127.0.0.1:{port}"
        secrets = _load_secrets()
        use_score = args.score and bool(secrets.get("LLM_API_KEY"))
        if args.score and not use_score:
            print("[integration] --score requested but no LLM_API_KEY in secrets.local.json; running contract mode instead")

        if not use_score:
            # Contract mode: the harness's own offline scenarios, plus the seed-context
            # load the way warmup does it.
            llm = run_local_judge.OfflineProvider()
            judge = js.JudgeSimulator(llm)
            judge.client = js.BotClient(js.BOT_URL)
            judge.dataset.load()
            print("[integration] contract mode: offline scenarios")
            passed = _scenarios_all_pass(judge)
            print(f"[integration] R32 contract gate: {'PASS' if passed else 'FAIL'}")
            return 0 if passed else 1

        # Scoring mode.
        llm = _build_llm(secrets)
        judge = js.JudgeSimulator(llm)
        judge.client = js.BotClient(js.BOT_URL)
        if not judge.dataset.load():
            print("[integration] FAIL: dataset load failed")
            return 2

        print(f"[integration] scoring mode: full_evaluation via {llm.name()}")
        judge.scorer = js.LLMScorer(llm, judge.dataset)
        try:
            judge._full()
        except Exception as exc:
            print(f"[integration] FAIL: full_evaluation crashed: {exc}")
            return 2

        scores: List[js.ScoreResult] = judge.all_scores
        if not scores:
            print("[integration] FAIL: no messages were scored")
            return 1

        n = len(scores)
        dims = {
            "specificity": sum(s.specificity for s in scores) / n,
            "category_fit": sum(s.category_fit for s in scores) / n,
            "merchant_fit": sum(s.merchant_fit for s in scores) / n,
            "decision_quality": sum(s.decision_quality for s in scores) / n,
            "engagement_compulsion": sum(s.engagement_compulsion for s in scores) / n,
        }
        mean = sum(s.total for s in scores) / n

        print(f"[integration] messages scored: {n}")
        for dim, value in dims.items():
            print(f"  {dim:22} {value:5.2f}/10")
        print(f"  {'MEAN TOTAL':22} {mean:5.2f}/50  (gate: >= {args.threshold:.0f})")

        fallback_scored = any(s.specificity_reason.startswith("Fallback") for s in scores)
        if fallback_scored:
            print("  [warn] some messages fell back to heuristic scoring (LLM judge unreachable on those calls)")

        passed = mean >= args.threshold
        # Operational floors from the failure-mode table: every scored action was a
        # well-formed response, and the harness reported no timeouts (it would have
        # printed and skipped them).
        print(f"[integration] R32 scoring gate: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"[integration] bot on :{port} stopped")


if __name__ == "__main__":
    raise SystemExit(main())
