"""Warm the composer's on-disk LLM cache for every seed trigger.

The composer's LLM layer is cache-first and keyed by (provider, model, exact prompt)
at temperature 0, so a warmed cache makes `/v1/tick` produce the model-polished body
with zero network calls and zero latency risk - which is exactly what the challenge's
determinism rule wants (challenge-brief.md §7.1, architecture.md ADR-001).

Every rewrite is put through the same fact fence the request path uses
(`engine.finalize` -> `guardrails.validate_body`). A rewrite that drifts is NOT
cached, so the bot falls back to the deterministic draft for that trigger.

Usage
-----
    python tools/warm_llm_cache.py              # direct provider call
    python tools/warm_llm_cache.py --bridge     # route through runs/bridge (no HTTPS here)
    python tools/warm_llm_cache.py --bridge --only trg_001_research_digest_dentists
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm_bridge  # noqa: E402
from bot.composer import engine, llm  # noqa: E402

DATASET = ROOT / "dataset"
REPORT_PATH = ROOT / "runs" / "llm_warm_report.json"


def load_dataset() -> Dict[str, Dict[str, Any]]:
    categories: Dict[str, Any] = {}
    for path in sorted((DATASET / "categories").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        categories[data.get("slug", path.stem)] = data

    def load_list(name: str, container: str, key: str) -> Dict[str, Any]:
        data = json.loads((DATASET / name).read_text(encoding="utf-8"))
        items = data.get(container, data.get(container.rstrip("s"), []))
        return {item[key]: item for item in items if key in item}

    return {
        "categories": categories,
        "merchants": load_list("merchants_seed.json", "merchants", "merchant_id"),
        "customers": load_list("customers_seed.json", "customers", "customer_id"),
        "triggers": load_list("triggers_seed.json", "triggers", "id"),
    }


def extract_body(text: str) -> Optional[str]:
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return None
    try:
        body = json.loads(match.group()).get("body")
    except Exception:
        return None
    return body.strip() if isinstance(body, str) and body.strip() else None


PROMPTS_PATH = ROOT / "runs" / "prompts_keyed.json"
INGEST_REPORT = ROOT / "runs" / "llm_ingest_report.json"


def _bundles() -> List[Dict[str, Any]]:
    """Every seed trigger that produces a draft, with its prompt and cache key."""
    ds = load_dataset()
    out: List[Dict[str, Any]] = []
    provider, model = llm.provider(), llm.model_name()
    for trigger_id in sorted(ds["triggers"]):
        trigger = ds["triggers"][trigger_id]
        merchant = ds["merchants"].get(str(trigger.get("merchant_id") or ""))
        if not merchant:
            continue
        category = ds["categories"].get(str(merchant.get("category_slug") or ""))
        if not category:
            continue
        customer = ds["customers"].get(str(trigger.get("customer_id") or "")) if trigger.get("customer_id") else None
        prepared = engine.prepare(category, merchant, trigger, customer)
        if prepared is None:
            continue
        out.append(
            {
                "trigger_id": trigger_id,
                "kind": trigger.get("kind"),
                "prompt": prepared["prompt"],
                "cache_key": llm.cache_key(prepared["prompt"], model, provider),
                "deterministic": prepared["deterministic_body"],
                "system": llm.SYSTEM_RULES,
                "_prepared": prepared,
            }
        )
    return out


def dump_prompts() -> int:
    """Step 1 of the split flow: emit prompts + cache keys for an external fetcher.

    The fetch step can then run in whatever process actually has outbound HTTPS, and
    step 3 (`--ingest`) brings the bodies back through the fact fence before caching.
    """
    bundles = _bundles()
    already = sum(1 for b in bundles if llm.lookup_cached(b["prompt"]) is not None)
    PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPTS_PATH.write_text(
        json.dumps(
            {
                "provider": llm.provider(),
                "model": llm.model_name(),
                "system": llm.SYSTEM_RULES,
                "prompts": [
                    {k: v for k, v in b.items() if k not in ("_prepared", "system")}
                    for b in bundles
                    if llm.lookup_cached(b["prompt"]) is None
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"triggers={len(bundles)} already_cached={already} to_fetch={len(bundles) - already}")
    print(f"-> {PROMPTS_PATH}")
    return 0


def ingest(path: Path) -> int:
    """Step 3: fence-validate externally fetched bodies, then cache the survivors."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    bodies: Dict[str, str] = payload.get("bodies") if isinstance(payload, dict) else {}
    if not isinstance(bodies, dict):
        print("ingest file has no 'bodies' object")
        return 1

    accepted = rejected = unknown = 0
    rows: List[Dict[str, Any]] = []
    by_key = {b["cache_key"]: b for b in _bundles()}

    for cache_key, body in sorted(bodies.items()):
        bundle = by_key.get(cache_key)
        if not bundle or not isinstance(body, str) or not body.strip():
            unknown += 1
            continue
        finalized = engine.finalize(dict(bundle["_prepared"]), body.strip())
        if finalized["source"] == "llm":
            llm.cache_put(cache_key, body.strip())
            accepted += 1
            status = "accepted"
        else:
            rejected += 1
            status = "rejected_by_fence"
        rows.append(
            {
                "trigger_id": bundle["trigger_id"],
                "kind": bundle["kind"],
                "status": status,
                "deterministic": bundle["deterministic"],
                "llm": body.strip(),
            }
        )

    INGEST_REPORT.write_text(
        json.dumps({"accepted": accepted, "rejected_by_fence": rejected, "unknown_keys": unknown, "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"accepted={accepted} rejected_by_fence={rejected} unknown_keys={unknown}")
    print(f"report -> {INGEST_REPORT}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", action="store_true", help="route calls through runs/bridge")
    parser.add_argument("--only", default=None, help="warm a single trigger id")
    parser.add_argument("--force", action="store_true", help="re-ask even if already cached")
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="write runs/prompts_keyed.json (prompt + cache key per trigger) and exit",
    )
    parser.add_argument(
        "--ingest",
        default=None,
        help="read {cache_key: body} JSON, fence-validate each body, cache the survivors",
    )
    args = parser.parse_args()

    if args.dump_only:
        return dump_prompts()
    if args.ingest:
        return ingest(Path(args.ingest))

    ds = load_dataset()
    provider, model = llm.provider(), llm.model_name()
    print(f"provider={provider} model={model} bridge={args.bridge}")
    print(f"triggers={len(ds['triggers'])} merchants={len(ds['merchants'])} categories={len(ds['categories'])}")

    rows: List[Dict[str, Any]] = []
    cached_hits = accepted = rejected = failed = skipped = 0

    for trigger_id in sorted(ds["triggers"]):
        if args.only and trigger_id != args.only:
            continue
        trigger = ds["triggers"][trigger_id]
        merchant = ds["merchants"].get(str(trigger.get("merchant_id") or ""))
        if not merchant:
            skipped += 1
            continue
        category = ds["categories"].get(str(merchant.get("category_slug") or ""))
        customer = ds["customers"].get(str(trigger.get("customer_id") or "")) if trigger.get("customer_id") else None
        if not category:
            skipped += 1
            continue

        prepared = engine.prepare(category, merchant, trigger, customer)
        if prepared is None:
            skipped += 1
            rows.append({"trigger_id": trigger_id, "status": "no_draft"})
            print(f"  [skip] {trigger_id}: composer declined to draft")
            continue

        prompt = prepared["prompt"]
        key = llm.cache_key(prompt, model, provider)
        deterministic = prepared["deterministic_body"]

        if not args.force and llm._cache_get(key) is not None:  # noqa: SLF001
            cached_hits += 1
            print(f"  [cached] {trigger_id}")
            continue

        try:
            if args.bridge:
                raw = llm_bridge.ask(prompt, llm.SYSTEM_RULES, kind="compose")
                body = extract_body(raw)
            else:
                body = llm.call_once(prompt, timeout=60.0)
        except Exception as exc:
            failed += 1
            rows.append({"trigger_id": trigger_id, "status": "call_failed", "error": str(exc)})
            print(f"  [fail] {trigger_id}: {exc}")
            continue

        if not body:
            failed += 1
            rows.append({"trigger_id": trigger_id, "status": "empty"})
            print(f"  [fail] {trigger_id}: empty/unparseable response")
            continue

        # Same fence the request path applies.
        finalized = engine.finalize(dict(prepared), body)
        if finalized["source"] == "llm":
            llm.cache_put(key, body)
            accepted += 1
            rows.append(
                {
                    "trigger_id": trigger_id,
                    "kind": trigger.get("kind"),
                    "status": "accepted",
                    "deterministic": deterministic,
                    "llm": body,
                }
            )
            print(f"  [ok] {trigger_id}")
        else:
            rejected += 1
            rows.append(
                {
                    "trigger_id": trigger_id,
                    "kind": trigger.get("kind"),
                    "status": "rejected_by_fence",
                    "deterministic": deterministic,
                    "llm": body,
                }
            )
            print(f"  [rejected] {trigger_id}: rewrite failed the fact fence, keeping deterministic")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "provider": provider,
                "model": model,
                "accepted": accepted,
                "rejected_by_fence": rejected,
                "already_cached": cached_hits,
                "failed": failed,
                "skipped": skipped,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"\naccepted={accepted} rejected_by_fence={rejected} already_cached={cached_hits} "
        f"failed={failed} skipped={skipped}\nreport -> {REPORT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
