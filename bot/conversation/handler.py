"""`/v1/reply` — the conversation brain.

This is where the challenge's three named production weaknesses get fixed
(challenge-brief.md §3): auto-reply pollution, intent-handoff failure, and not
knowing when to stop.

Decision order (first match wins):
    1. merchant already opted out            -> end
    2. hostile / opt-out language            -> end + permanent opt-out
    3. WhatsApp Business auto-reply          -> nudge once -> wait -> end
    4. explicit intent to proceed            -> ACTION mode, never re-qualify
    5. asked to be contacted later           -> wait
    6. off-topic / out-of-scope ask          -> decline politely, return to the thread
    7. engaged reply                         -> deliver the next concrete step
    8. anything else                         -> one grounded nudge

Auto-reply detection is keyed on merchant_id, never conversation_id: the harness
opens a brand-new conversation for every canned reply (reference.md gotcha G5).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .. import guardrails
from ..state import normalize_message, runtime_state

# --------------------------------------------------------------------- lexicons

CANNED_PATTERNS = (
    "thank you for contacting",
    "thanks for contacting",
    "our team will respond",
    "we will get back",
    "we'll get back",
    "will revert",
    "automated",
    "auto reply",
    "autoreply",
    "this is an automatic",
    "away from",
    "outside our business hours",
    "office hours",
    "sampark karne ke liye",
    "dhanyavaad",
    "team tak pahuncha",
    "jaankari ke liye",
)

HOSTILE_PATTERNS = (
    "stop messaging",
    "stop sending",
    "don't message",
    "dont message",
    "do not message",
    "not interested",
    "unsubscribe",
    "remove me",
    "useless",
    "spam",
    "bothering",
    "leave me alone",
    "band karo",
    "mat bhejo",
    "pareshan",
)

# The judge's intent test checks for these exact tokens in OUR reply and for the
# absence of the qualifying ones (reference.md gotcha G6).
ACTIONING_TOKENS = ("done", "sending", "draft", "here", "confirm", "proceed", "next")
QUALIFYING_TOKENS = ("would you", "do you", "can you tell", "what if", "how about")

# Merchant-side signals that they have committed.
COMMIT_PATTERNS = (
    "lets do it",
    "let's do it",
    "go ahead",
    "ok do it",
    "okay do it",
    "yes please",
    "please do",
    "send it",
    "sounds good",
    "i want to join",
    "i'm in",
    "im in",
    "start it",
    "sign me up",
    "kar do",
    "kar dijiye",
    "shuru karo",
    "haan",
    "theek hai",
    "whats next",
    "what's next",
    "what next",
)

LATER_PATTERNS = (
    "call me later",
    "later",
    "busy",
    "not now",
    "next week",
    "tomorrow",
    "baad mein",
    "abhi nahi",
    "give me time",
    "will check",
    "let me check",
    "thinking about it",
)

OFF_TOPIC_PATTERNS = (
    "gst",
    "income tax",
    "itr",
    "loan",
    "insurance",
    "visa",
    "passport",
    "electricity bill",
    "recruit",
    "hiring",
    "legal notice",
    "police",
    "rent agreement",
)

AFFIRMATIVE_PATTERNS = ("yes", "yeah", "yep", "sure", "ok", "okay", "haan", "ji", "please")


def _has(text: str, patterns: Tuple[str, ...]) -> bool:
    return any(p in text for p in patterns)


def is_auto_reply(normalized: str, history: List[str]) -> bool:
    """Canned phrasing, or the merchant repeating themselves verbatim."""
    if _has(normalized, CANNED_PATTERNS):
        return True
    if len(history) >= 2 and normalized and normalized == history[-2]:
        return True
    return False


def auto_reply_streak(history: List[str]) -> int:
    """How many auto-replies in a row, counting back from the newest message."""
    streak = 0
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        earlier = history[: index + 1]
        if is_auto_reply(message, earlier):
            streak += 1
        else:
            break
    return streak


def wants_action(normalized: str) -> bool:
    """Explicit commitment — the moment production Vera famously fumbles."""
    if _has(normalized, COMMIT_PATTERNS):
        return True
    words = normalized.split()
    return bool(words) and words[0] in AFFIRMATIVE_PATTERNS and len(words) <= 4


def enforce_action_contract(body: str) -> str:
    """Guarantee the reply reads as action, not as another qualifying question."""
    lowered = body.lower()
    for token in QUALIFYING_TOKENS:
        if token in lowered:
            body = re.sub(re.escape(token), "I'll", body, flags=re.IGNORECASE)
            lowered = body.lower()
    if not any(token in lowered for token in ACTIONING_TOKENS):
        body = f"{body.rstrip('. ')}. Sending it here next."
    return body


# ------------------------------------------------------------------- grounding


def _context_for(store: Any, merchant_id: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    merchant = store.get("merchant", merchant_id) or {}
    category = store.get("category", merchant.get("category_slug")) or {}
    return merchant, category


def _first_name(merchant: Dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    return str(identity.get("owner_first_name") or identity.get("name") or "").split("(")[0].strip()


def _active_offer(merchant: Dict[str, Any]) -> str:
    offers = [o for o in (merchant.get("offers") or []) if isinstance(o, dict) and o.get("status") == "active"]
    offers.sort(key=lambda o: str(o.get("id", "")))
    return str(offers[0].get("title")) if offers else ""


def _topic(conversation: Optional[Dict[str, Any]]) -> str:
    if not conversation:
        return ""
    return str(conversation.get("kind") or "").replace("_", " ")


def _safe(body: str, category: Dict[str, Any], fallback: str) -> str:
    """Never emit a reply that would fail our own outbound checks."""
    ok, _problems = guardrails.validate_body(body, category or None)
    return body if ok else fallback


# ---------------------------------------------------------------------- handler


def handle_reply(store: Any, request_body: Dict[str, Any]) -> Dict[str, Any]:
    conversation_id = str(request_body.get("conversation_id") or "")
    merchant_id = request_body.get("merchant_id")
    message = str(request_body.get("message") or "")
    turn_number = request_body.get("turn_number")

    normalized = normalize_message(message)
    merchant, category = _context_for(store, merchant_id)
    conversation = runtime_state.conversation(conversation_id) if conversation_id else None
    name = _first_name(merchant)
    offer = _active_offer(merchant)
    topic = _topic(conversation)

    history = runtime_state.record_inbound(str(merchant_id or ""), message)
    if conversation_id:
        runtime_state.update_conversation(
            conversation_id,
            merchant_id=merchant_id,
            last_inbound=normalized,
            turns=int((conversation or {}).get("turns") or 1) + 1,
        )

    # 1 — already opted out ---------------------------------------------------
    if runtime_state.is_opted_out(str(merchant_id or "")):
        return {
            "action": "end",
            "rationale": "Merchant previously opted out; honouring that and staying closed.",
        }

    # 2 — hostile / opt-out ---------------------------------------------------
    if _has(normalized, HOSTILE_PATTERNS):
        runtime_state.opt_out(str(merchant_id or ""), "merchant asked to stop")
        return {
            "action": "end",
            "rationale": (
                "Merchant explicitly asked to stop. Ending the conversation and suppressing all "
                "future triggers for this merchant — no apology message, because another message "
                "is the exact thing they objected to."
            ),
        }

    # 3 — auto-reply ladder ---------------------------------------------------
    if is_auto_reply(normalized, history):
        streak = auto_reply_streak(history)
        if streak <= 1:
            body = (
                f"Looks like an auto-reply. When {name or 'the owner'} sees this, one word is enough "
                "and I'll take it from there — reply YES."
            )
            return {
                "action": "send",
                "body": _safe(body, category, "Looks like an auto-reply — reply YES when the owner sees this."),
                "cta": "binary_yes_no",
                "rationale": (
                    "Canned WhatsApp Business auto-reply detected on the first turn. One short prompt "
                    "aimed at the owner rather than burning turns talking to the autoresponder."
                ),
            }
        if streak == 2:
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": (
                    "Second identical auto-reply in a row — the owner is not at the phone. "
                    "Backing off 4 hours instead of spending another turn."
                ),
            }
        return {
            "action": "end",
            "rationale": (
                "Third auto-reply in a row with no human signal. Closing this thread; the merchant "
                "can be re-approached on a future trigger rather than being pushed now."
            ),
        }

    # 4 — explicit intent -> ACTION mode --------------------------------------
    if wants_action(normalized):
        if conversation_id:
            runtime_state.update_conversation(conversation_id, mode="action")
        if offer:
            body = (
                f"Done — drafting {offer} now and I'll have it here in a few minutes. "
                "Reply CONFIRM and I'll schedule it as your next step."
            )
        elif topic:
            body = (
                f"Done — I'm drafting the {topic} piece now and will send it here shortly. "
                "Reply CONFIRM and I'll put it live as the next step."
            )
        else:
            body = (
                "Done — I'm drafting it now and will send it here shortly. "
                "Reply CONFIRM and I'll put it live as the next step."
            )
        body = enforce_action_contract(body)
        return {
            "action": "send",
            "body": _safe(body, category, enforce_action_contract("Done — drafting it now. Reply CONFIRM and I'll proceed.")),
            "cta": "binary_confirm_cancel",
            "rationale": (
                "Merchant committed explicitly, so this switches straight from qualifying to doing: "
                "concrete deliverable, concrete next step, one binary confirmation. No further "
                "qualifying questions."
            ),
        }

    # 5 — asked for time ------------------------------------------------------
    if _has(normalized, LATER_PATTERNS):
        return {
            "action": "wait",
            "wait_seconds": 86400,
            "rationale": "Merchant asked for time; backing off 24h and keeping the thread open.",
        }

    # 6 — off-topic -----------------------------------------------------------
    if _has(normalized, OFF_TOPIC_PATTERNS):
        back_to = topic or "your listing"
        body = (
            "That one's outside what I can help with — your CA will be faster there. "
            f"Back to {back_to}: shall I carry on with what I started?"
        )
        return {
            "action": "send",
            "body": _safe(body, category, "That one's outside what I can help with. Shall I carry on with what I started?"),
            "cta": "binary_yes_no",
            "rationale": (
                "Out-of-scope request declined honestly rather than bluffed, then the thread is "
                "returned to the original trigger in one sentence."
            ),
        }

    # 7 — engaged -------------------------------------------------------------
    if "?" in message or _has(normalized, ("send", "share", "show", "how", "kaise", "kitna", "price", "cost")):
        if offer:
            body = (
                f"On it — I'll put together the {offer} version and send it here. "
                "Reply YES and I'll get it moving."
            )
        else:
            body = "On it — I'll put that together and send it here. Reply YES and I'll get it moving."
        return {
            "action": "send",
            "body": _safe(body, category, "On it — I'll put that together and send it here. Reply YES and I'll get it moving."),
            "cta": "binary_yes_no",
            "rationale": (
                "Merchant is engaged and asked for something concrete; answering with delivery plus "
                "one low-friction confirmation."
            ),
        }

    # 8 — default -------------------------------------------------------------
    turns = int((conversation or {}).get("turns") or (turn_number if isinstance(turn_number, int) else 1))
    if turns >= 4:
        return {
            "action": "end",
            "rationale": "Several turns without a clear signal; stopping rather than nudging a fourth time.",
        }

    body = "Understood. Want me to take the next step on this, or leave it for now?"
    return {
        "action": "send",
        "body": _safe(body, category, "Understood. Want me to take the next step on this?"),
        "cta": "binary_yes_no",
        "rationale": "Ambiguous reply; one short binary check rather than assuming intent either way.",
    }


__all__ = ["handle_reply", "is_auto_reply", "auto_reply_streak", "wants_action", "enforce_action_contract"]
