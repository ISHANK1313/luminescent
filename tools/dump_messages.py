"""Dump the message the composer would send for every seed trigger.

Offline review aid: no bot process, no judge, no network. Useful for eyeballing
grounding, voice and CTA placement across the whole seed set at once, and for
diffing the deterministic body against a cached LLM rewrite.

    python tools/dump_messages.py                 # deterministic bodies
    python tools/dump_messages.py --with-cache    # use warm LLM cache where present
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.composer import engine, llm  # noqa: E402
from warm_llm_cache import load_dataset  # noqa: E402

OUT_PATH = ROOT / "runs" / "messages.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-cache", action="store_true", help="apply warm LLM rewrites")
    args = parser.parse_args()

    ds = load_dataset()
    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []

    for trigger_id in sorted(ds["triggers"]):
        trigger = ds["triggers"][trigger_id]
        merchant = ds["merchants"].get(str(trigger.get("merchant_id") or ""))
        if not merchant:
            skipped.append(f"{trigger_id}: no merchant")
            continue
        category = ds["categories"].get(str(merchant.get("category_slug") or ""))
        if not category:
            skipped.append(f"{trigger_id}: no category")
            continue
        customer = ds["customers"].get(str(trigger.get("customer_id") or "")) if trigger.get("customer_id") else None

        prepared = engine.prepare(category, merchant, trigger, customer)
        if prepared is None:
            skipped.append(f"{trigger_id}: composer declined")
            continue

        polished = None
        if args.with_cache:
            polished = llm._cache_get(llm.cache_key(prepared["prompt"]))  # noqa: SLF001
        finalized = engine.finalize(dict(prepared), polished)
        result = finalized["result"]

        rows.append(
            {
                "trigger_id": trigger_id,
                "kind": trigger.get("kind"),
                "scope": trigger.get("scope"),
                "urgency": trigger.get("urgency"),
                "merchant": merchant.get("identity", {}).get("name"),
                "category": category.get("slug"),
                "send_as": result["send_as"],
                "source": result["source"],
                "cta": result["cta"],
                "body": result["body"],
                "chars": len(result["body"]),
                "words": len(result["body"].split()),
                "rationale": result["rationale"],
                "levers": result.get("levers"),
                "facts": finalized["draft"].facts,
            }
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"count": len(rows), "skipped": skipped, "messages": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"composed={len(rows)} skipped={len(skipped)} -> {OUT_PATH}")
    for reason in skipped:
        print(f"  skipped {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
