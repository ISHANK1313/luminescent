"""The composer: turn available triggers into sendable actions.

    compose(category, merchant, trigger, customer?) -> {body, cta, send_as, suppression_key, rationale}

Pipeline per trigger:
    resolve contexts -> consent/opt-out/suppression gate -> deterministic grounded draft
    -> category voice + language -> validate -> (optional) Gemini rewrite at temp 0
    -> re-validate against the same fact fence -> anti-repetition -> action

Selection rules the judge cares about:
  * one action per (merchant_id, conversation_id) per tick, max 20 actions
  * never re-fire a suppression_key, never message an opted-out merchant
  * skip rather than invent when the contexts do not support a grounded message
  * `available_triggers` decides what is live now; `expires_at` only ranks (decision D1)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import guardrails
from ..state import runtime_state
from . import llm, voice
from .strategies import Ctx, Draft, build_draft

# Which consent scopes make a customer-facing trigger kind legitimate.
CONSENT_SCOPES: Dict[str, Tuple[str, ...]] = {
    "recall_due": ("recall_reminders", "appointment_reminders"),
    "chronic_refill_due": ("refill_reminders", "delivery_notifications"),
    "customer_lapsed_hard": ("winback_offers", "renewal_reminders"),
    "customer_lapsed_soft": ("winback_offers", "renewal_reminders"),
    "trial_followup": ("kids_program_updates", "appointment_reminders", "program_updates"),
    "wedding_package_followup": ("bridal_package_followup", "appointment_reminders"),
    "appointment_tomorrow": ("appointment_reminders",),
}


def conversation_id_for(merchant_id: str, trigger_id: str) -> str:
    """Deterministic and unique per (merchant, trigger) — no uuid, no clock (rule R1)."""
    return f"conv_{merchant_id}_{trigger_id}"


def consent_ok(trigger: Dict[str, Any], customer: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Customer-facing sends require consent that actually covers this kind."""
    if not customer:
        return True, ""
    consent = customer.get("consent") or {}
    scopes = [str(s) for s in (consent.get("scope") or [])]
    if not scopes:
        return False, "customer has no recorded consent scope"

    kind = str(trigger.get("kind") or "")
    wanted = CONSENT_SCOPES.get(kind)
    if wanted and any(s in scopes for s in wanted):
        return True, ""
    if wanted:
        # An injected/unknown kind should not be blocked by an over-strict map, but a
        # kind we DO know about, with consent that does not cover it, must not be sent.
        return False, f"consent scope {scopes} does not cover {kind}"

    opted_in = bool((customer.get("preferences") or {}).get("reminder_opt_in"))
    if opted_in:
        return True, ""
    return False, "customer is not opted in to reminders"


def allowed_numbers_for(draft: Draft, body: str) -> set:
    """Every number the model is permitted to use: the ones already in our own facts."""
    allowed = guardrails.numbers_in(" ".join(draft.facts))
    allowed |= guardrails.numbers_in(body)
    allowed |= guardrails.numbers_in(" ".join(str(p) for p in draft.template_params))
    return allowed


def identifies_sender(body: str, ctx: Ctx) -> bool:
    """A message sent on the merchant's behalf must say who is writing.

    A rewrite that tightens the copy by deleting the business name leaves the customer
    with an unattributed WhatsApp message, which is both a trust problem and a
    merchant-fit loss. Merchant-facing messages are exempt: there the sender is Vera.
    """
    if not ctx.is_customer_facing:
        return True
    name = (ctx.biz or "").strip()
    if not name:
        return True
    if name.lower() in body.lower():
        return True
    # Accept a shortened form: the distinctive first word of the business name.
    head = name.split()[0].strip(".,'").lower()
    return len(head) > 2 and head in body.lower()


def addresses_recipient(body: str, ctx: Ctx) -> bool:
    """The recipient's name must survive the rewrite.

    Dropping 'Dr. Meera,' / 'Hi Sumitra,' turns a personal message into a broadcast,
    which reads as generic and costs merchant fit. Customer-facing messages address
    the customer (or their guardian); merchant-facing ones address the owner or
    business. Skipped only when we never had a name to begin with.
    """
    if ctx.is_customer_facing:
        target = (ctx.cust_guardian or ctx.cust_name or "").strip()
    else:
        target = (ctx.owner or ctx.biz or "").strip()
    if not target:
        return True
    first = target.split()[0].strip(".,'()").lower()
    if len(first) < 2:
        return True
    return first in body.lower()


def compose(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]] = None,
    use_llm: bool = True,
) -> Optional[Dict[str, Any]]:
    """Public composition entry point. Returns None when nothing should be sent."""
    prepared = prepare(category, merchant, trigger, customer)
    if prepared is None:
        return None

    if use_llm and prepared["prompt"]:
        polished = llm.compose_many([prepared["prompt"]])[0]
        prepared = finalize(prepared, polished)
    else:
        prepared = finalize(prepared, None)

    return prepared["result"]


def prepare(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Deterministic half: draft, render, validate, and build the LLM prompt."""
    ctx = Ctx(category, merchant, trigger, customer)

    ok, reason = consent_ok(trigger, customer)
    if not ok:
        return None

    draft = build_draft(category, merchant, trigger, customer)
    if draft.skip:
        return None

    body = voice.render(draft, ctx)
    taboo = guardrails.taboo_words(category)
    valid, problems = guardrails.validate_body(body, category, allowed_numbers=None)
    if not valid:
        return None
    if not guardrails.has_specific_anchor(body):
        return None

    language = "Hindi-English mix" if voice.wants_hindi(ctx) else "English"
    audience = (
        f"the merchant's own customer ({ctx.cust_name}), written on behalf of {ctx.biz}"
        if ctx.is_customer_facing
        else f"{ctx.biz} in {ctx.locality}, {ctx.city} — a {category.get('slug')} business owner"
    )
    prompt = llm.build_prompt(
        facts=draft.facts,
        draft_body=body,
        voice=category.get("voice") or {},
        taboo=taboo,
        language=language,
        audience=audience,
        why_now=str(trigger.get("kind") or "").replace("_", " "),
    )

    return {
        "ctx": ctx,
        "draft": draft,
        "deterministic_body": body,
        "category": category,
        "prompt": prompt,
        "result": None,
        "source": "deterministic",
    }


def finalize(prepared: Dict[str, Any], polished: Optional[str]) -> Dict[str, Any]:
    """Accept the LLM rewrite only if it survives the same fact fence, else fall back."""
    draft: Draft = prepared["draft"]
    ctx: Ctx = prepared["ctx"]
    category = prepared["category"]
    body = prepared["deterministic_body"]
    source = "deterministic"

    if polished:
        allowed = allowed_numbers_for(draft, body)
        valid, _problems = guardrails.validate_body(polished, category, allowed_numbers=allowed)
        if (
            valid
            and guardrails.has_specific_anchor(polished)
            and identifies_sender(polished, ctx)
            and addresses_recipient(polished, ctx)
        ):
            body = polished
            source = "llm"

    trigger = ctx.trigger
    prepared["source"] = source
    prepared["result"] = {
        "body": body,
        "cta": draft.cta,
        "send_as": "merchant_on_behalf" if ctx.is_customer_facing else "vera",
        "suppression_key": str(trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('id')}"),
        "rationale": draft.rationale,
        "template_name": draft.template_name,
        "template_params": [str(p) for p in draft.template_params if p not in (None, "")],
        "levers": draft.levers,
        "source": source,
    }
    return prepared


# --------------------------------------------------------------------- the tick


def _urgency(trigger: Dict[str, Any]) -> int:
    try:
        return int(trigger.get("urgency") or 0)
    except (TypeError, ValueError):
        return 0


def build_tick_actions(
    store: Any,
    now: Optional[str],
    available_triggers: List[str],
    use_llm: bool = True,
) -> List[Dict[str, Any]]:
    """Decide what to send this tick. Returns the `actions` array for `/v1/tick`."""
    candidates: List[Dict[str, Any]] = []
    seen_conversations = set()

    for trigger_id in available_triggers:
        bundle = store.resolve_bundle(trigger_id)
        if bundle["missing"]:
            continue

        trigger = bundle["trigger"]
        merchant = bundle["merchant"]
        merchant_id = str(merchant.get("merchant_id") or trigger.get("merchant_id") or "")
        if not merchant_id:
            continue

        if runtime_state.is_opted_out(merchant_id):
            continue

        suppression_key = str(trigger.get("suppression_key") or "")
        if suppression_key and runtime_state.has_fired(merchant_id, suppression_key):
            continue

        conversation_id = conversation_id_for(merchant_id, trigger_id)
        if conversation_id in seen_conversations:
            continue
        if runtime_state.conversation(conversation_id) is not None:
            continue

        prepared = prepare(bundle["category"], merchant, trigger, bundle["customer"])
        if prepared is None:
            continue

        seen_conversations.add(conversation_id)
        candidates.append(
            {
                "prepared": prepared,
                "trigger_id": trigger_id,
                "merchant_id": merchant_id,
                "conversation_id": conversation_id,
                "customer_id": trigger.get("customer_id"),
                "urgency": _urgency(trigger),
            }
        )

    if not candidates:
        return []

    # Highest urgency first; ties broken deterministically by trigger id.
    candidates.sort(key=lambda c: (-c["urgency"], c["trigger_id"]))
    candidates = candidates[:20]

    prompts = [c["prepared"]["prompt"] if use_llm else None for c in candidates]
    polished = llm.compose_many(prompts) if use_llm else [None] * len(candidates)

    actions: List[Dict[str, Any]] = []
    for candidate, polish in zip(candidates, polished):
        prepared = finalize(candidate["prepared"], polish)
        result = prepared["result"]
        merchant_id = candidate["merchant_id"]

        if runtime_state.body_already_sent(merchant_id, result["body"]):
            continue

        action = {
            "conversation_id": candidate["conversation_id"],
            "merchant_id": merchant_id,
            "customer_id": candidate["customer_id"],
            "send_as": result["send_as"],
            "trigger_id": candidate["trigger_id"],
            "template_name": result["template_name"],
            "template_params": result["template_params"],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        }
        actions.append(action)

        runtime_state.mark_body_sent(merchant_id, result["body"])
        runtime_state.mark_fired(merchant_id, result["suppression_key"])
        runtime_state.open_conversation(
            candidate["conversation_id"],
            {
                "merchant_id": merchant_id,
                "customer_id": candidate["customer_id"],
                "trigger_id": candidate["trigger_id"],
                "kind": prepared["ctx"].kind,
                "opened_at": now,
                "last_body": result["body"],
                "turns": 1,
                "mode": "pitch",
            },
        )

    return actions


__all__ = ["compose", "build_tick_actions", "conversation_id_for", "consent_ok", "identifies_sender"]
