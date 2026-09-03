"""ContextStore — version-aware, thread-safe, in-memory context storage.

Implements the `/v1/context` contract from `challenge-testing-brief.md` §2.1.

Version semantics (reference.md decision D2 — resolves the brief/examples conflict):
    version >  current  -> replace atomically, HTTP 200 {accepted: true, ...}
    version == current  -> idempotent NO-OP,   HTTP 200 {accepted: true, ...}
    version <  current  -> stale,              HTTP 409 {accepted: false, reason: "stale_version", ...}

Why: the testing brief calls a same-version re-post "a no-op" and reserves 409 for
"you already have a higher version". `judge_simulator._warmup` only prints PASS when
`accepted` is truthy, and `_full` re-pushes version 1 for merchants that warmup already
pushed at version 1 — so treating an equal version as an error would manufacture
false failures on every re-run.

The store starts EMPTY and is only ever filled by `/v1/context` (decision D3): the
judge's warmup check compares `contexts_loaded` against exactly what it pushed, and
composing from data the judge never pushed would count as fabrication.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

VALID_SCOPES = ("category", "merchant", "customer", "trigger")

# `/v1/context` payload cap from the testing brief §5.
MAX_PAYLOAD_BYTES = 500 * 1024


def _utc_now_iso() -> str:
    """Wall-clock timestamp for acknowledgements only.

    Never used inside message composition (rule R1: composition must be deterministic).
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ContextStore:
    """`(scope, context_id) -> {"version": int, "payload": dict}` with a write lock.

    Also maintains derived indexes so the tick path never has to scan the whole store:
      * triggers by merchant_id
      * customers by merchant_id
    Index values are kept sorted for deterministic iteration (rule R5).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ctx: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._triggers_by_merchant: Dict[str, List[str]] = {}
        self._customers_by_merchant: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ writes

    def push(
        self, scope: str, context_id: str, version: int, payload: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """Store a context. Returns `(http_status, response_body)`."""
        if scope not in VALID_SCOPES:
            return 400, {
                "accepted": False,
                "reason": "invalid_scope",
                "details": f"scope must be one of {list(VALID_SCOPES)}, got {scope!r}",
            }
        if not context_id or not isinstance(context_id, str):
            return 400, {
                "accepted": False,
                "reason": "invalid_context_id",
                "details": "context_id must be a non-empty string",
            }
        if not isinstance(version, int) or isinstance(version, bool):
            return 400, {
                "accepted": False,
                "reason": "invalid_version",
                "details": "version must be an integer",
            }
        if not isinstance(payload, dict):
            return 400, {
                "accepted": False,
                "reason": "invalid_payload",
                "details": "payload must be a JSON object",
            }

        key = (scope, context_id)
        ack = {
            "accepted": True,
            "ack_id": f"ack_{context_id}_v{version}",
            "stored_at": _utc_now_iso(),
        }

        with self._lock:
            current = self._ctx.get(key)

            if current is not None and version < current["version"]:
                return 409, {
                    "accepted": False,
                    "reason": "stale_version",
                    "current_version": current["version"],
                }

            if current is not None and version == current["version"]:
                # Idempotent no-op: acknowledge without touching stored state.
                return 200, ack

            self._ctx[key] = {"version": version, "payload": payload}
            self._reindex(scope, context_id, payload)
            return 200, ack

    def _reindex(self, scope: str, context_id: str, payload: Dict[str, Any]) -> None:
        """Maintain merchant-keyed indexes. Caller must hold the lock."""
        if scope == "trigger":
            merchant_id = payload.get("merchant_id")
            if isinstance(merchant_id, str) and merchant_id:
                bucket = self._triggers_by_merchant.setdefault(merchant_id, [])
                if context_id not in bucket:
                    bucket.append(context_id)
                    bucket.sort()
        elif scope == "customer":
            merchant_id = payload.get("merchant_id")
            if isinstance(merchant_id, str) and merchant_id:
                bucket = self._customers_by_merchant.setdefault(merchant_id, [])
                if context_id not in bucket:
                    bucket.append(context_id)
                    bucket.sort()

    def clear(self) -> int:
        """Wipe everything (`/v1/teardown`). Returns the number of contexts removed."""
        with self._lock:
            removed = len(self._ctx)
            self._ctx.clear()
            self._triggers_by_merchant.clear()
            self._customers_by_merchant.clear()
            return removed

    # ------------------------------------------------------------------- reads

    def get(self, scope: str, context_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the stored payload, or None. Read-only: callers must not mutate it."""
        if not context_id:
            return None
        with self._lock:
            entry = self._ctx.get((scope, context_id))
            return entry["payload"] if entry else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        with self._lock:
            entry = self._ctx.get((scope, context_id))
            return entry["version"] if entry else None

    def has(self, scope: str, context_id: str) -> bool:
        with self._lock:
            return (scope, context_id) in self._ctx

    def counts(self) -> Dict[str, int]:
        """`contexts_loaded` for `/v1/healthz` — always reports all four scopes."""
        counts = {scope: 0 for scope in VALID_SCOPES}
        with self._lock:
            for scope, _ in self._ctx:
                if scope in counts:
                    counts[scope] += 1
        return counts

    def ids(self, scope: str) -> List[str]:
        """Sorted context_ids for a scope (deterministic iteration)."""
        with self._lock:
            return sorted(cid for (s, cid) in self._ctx if s == scope)

    def triggers_for_merchant(self, merchant_id: str) -> List[str]:
        with self._lock:
            return list(self._triggers_by_merchant.get(merchant_id, ()))

    def customers_for_merchant(self, merchant_id: str) -> List[str]:
        with self._lock:
            return list(self._customers_by_merchant.get(merchant_id, ()))

    # --------------------------------------------------------- resolution help

    def resolve_bundle(self, trigger_id: str) -> Dict[str, Any]:
        """Resolve the 4-context bundle for a trigger id.

        Returns `{"trigger", "merchant", "category", "customer", "missing"}` where
        `missing` lists whatever could not be resolved. The composer treats a
        non-empty `missing` (other than "customer") as "skip this trigger" rather
        than inventing anything.
        """
        trigger = self.get("trigger", trigger_id)
        if trigger is None:
            return {
                "trigger": None,
                "merchant": None,
                "category": None,
                "customer": None,
                "missing": ["trigger"],
            }

        missing: List[str] = []
        merchant_id = trigger.get("merchant_id")
        merchant = self.get("merchant", merchant_id) if merchant_id else None
        if merchant is None:
            missing.append("merchant")

        category = None
        if merchant is not None:
            category_slug = merchant.get("category_slug")
            category = self.get("category", category_slug) if category_slug else None
            if category is None:
                missing.append("category")
        else:
            missing.append("category")

        customer_id = trigger.get("customer_id")
        customer = self.get("customer", customer_id) if customer_id else None
        if customer_id and customer is None:
            missing.append("customer")

        return {
            "trigger": trigger,
            "merchant": merchant,
            "category": category,
            "customer": customer,
            "missing": missing,
        }


__all__ = ["ContextStore", "VALID_SCOPES", "MAX_PAYLOAD_BYTES"]
