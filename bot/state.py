"""Runtime (non-context) state: what we have already said and to whom.

Kept separate from ContextStore because this is *our* memory, not the judge's data.
Everything here is per-process and wiped by `/v1/teardown`.

Notes
-----
* Auto-reply detection is keyed by **merchant_id**, not conversation_id: the harness
  opens a fresh conversation for each canned reply (reference.md gotcha G5).
* Anti-repetition is keyed by (merchant_id, body-hash) so we never resend a body,
  in or across conversations (rule R11, judge penalty -2).
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Dict, List, Optional

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_message(text: str) -> str:
    """Lowercase, strip punctuation/whitespace — for comparing canned replies."""
    if not text:
        return ""
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def body_hash(merchant_id: str, body: str) -> str:
    digest = hashlib.sha256(f"{merchant_id}||{normalize_message(body)}".encode("utf-8"))
    return digest.hexdigest()[:16]


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # merchant_id -> list of normalized inbound messages (most recent last)
        self._inbound: Dict[str, List[str]] = {}
        # merchant_id -> permanently opted out
        self._opted_out: Dict[str, str] = {}
        # (merchant_id, suppression_key) -> True
        self._fired_keys: Dict[str, bool] = {}
        # body hashes already sent
        self._sent_bodies: Dict[str, bool] = {}
        # conversation_id -> conversation record
        self._conversations: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------- inbound log

    def record_inbound(self, merchant_id: str, message: str) -> List[str]:
        key = merchant_id or "_unknown_"
        with self._lock:
            history = self._inbound.setdefault(key, [])
            history.append(normalize_message(message))
            if len(history) > 25:
                del history[0 : len(history) - 25]
            return list(history)

    def inbound_history(self, merchant_id: str) -> List[str]:
        with self._lock:
            return list(self._inbound.get(merchant_id or "_unknown_", ()))

    # ------------------------------------------------------------------ opt-out

    def opt_out(self, merchant_id: str, reason: str) -> None:
        if not merchant_id:
            return
        with self._lock:
            self._opted_out[merchant_id] = reason

    def is_opted_out(self, merchant_id: Optional[str]) -> bool:
        if not merchant_id:
            return False
        with self._lock:
            return merchant_id in self._opted_out

    # -------------------------------------------------------------- suppression

    def has_fired(self, merchant_id: str, suppression_key: str) -> bool:
        if not suppression_key:
            return False
        with self._lock:
            return f"{merchant_id}||{suppression_key}" in self._fired_keys

    def mark_fired(self, merchant_id: str, suppression_key: str) -> None:
        if not suppression_key:
            return
        with self._lock:
            self._fired_keys[f"{merchant_id}||{suppression_key}"] = True

    # ---------------------------------------------------------- anti-repetition

    def body_already_sent(self, merchant_id: str, body: str) -> bool:
        with self._lock:
            return body_hash(merchant_id, body) in self._sent_bodies

    def mark_body_sent(self, merchant_id: str, body: str) -> None:
        with self._lock:
            self._sent_bodies[body_hash(merchant_id, body)] = True

    # ------------------------------------------------------------ conversations

    def open_conversation(self, conversation_id: str, record: Dict[str, Any]) -> None:
        with self._lock:
            self._conversations[conversation_id] = dict(record)

    def conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            found = self._conversations.get(conversation_id)
            return dict(found) if found else None

    def update_conversation(self, conversation_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            record = self._conversations.setdefault(conversation_id, {})
            record.update(fields)
            return dict(record)

    def has_conversation_for_trigger(self, merchant_id: str, trigger_id: str) -> bool:
        with self._lock:
            for record in self._conversations.values():
                if record.get("merchant_id") == merchant_id and record.get("trigger_id") == trigger_id:
                    return True
            return False

    # -------------------------------------------------------------------- admin

    def clear(self) -> None:
        with self._lock:
            self._inbound.clear()
            self._opted_out.clear()
            self._fired_keys.clear()
            self._sent_bodies.clear()
            self._conversations.clear()


runtime_state = RuntimeState()

__all__ = ["RuntimeState", "runtime_state", "normalize_message", "body_hash"]
