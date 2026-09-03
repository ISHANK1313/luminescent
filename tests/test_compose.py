"""Composer tests — grounding, voice, CTA discipline, determinism, suppression."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import guardrails
from bot.composer.engine import allowed_numbers_for, build_tick_actions, compose, consent_ok, prepare
from bot.state import runtime_state
from bot.store import ContextStore

DATASET = Path(__file__).resolve().parents[1] / "dataset"


def _load():
    categories = {}
    for f in sorted((DATASET / "categories").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        categories[data["slug"]] = data
    merchants = {m["merchant_id"]: m for m in json.loads((DATASET / "merchants_seed.json").read_text(encoding="utf-8"))["merchants"]}
    customers = {c["customer_id"]: c for c in json.loads((DATASET / "customers_seed.json").read_text(encoding="utf-8"))["customers"]}
    triggers = {t["id"]: t for t in json.loads((DATASET / "triggers_seed.json").read_text(encoding="utf-8"))["triggers"]}
    return categories, merchants, customers, triggers


CATEGORIES, MERCHANTS, CUSTOMERS, TRIGGERS = _load()


def bundle(trigger_id: str):
    trigger = TRIGGERS[trigger_id]
    merchant = MERCHANTS[trigger["merchant_id"]]
    category = CATEGORIES[merchant["category_slug"]]
    customer = CUSTOMERS.get(trigger.get("customer_id")) if trigger.get("customer_id") else None
    return category, merchant, trigger, customer


@pytest.fixture(autouse=True)
def _clean_state():
    runtime_state.clear()
    yield
    runtime_state.clear()


@pytest.mark.parametrize("trigger_id", sorted(TRIGGERS))
def test_every_seed_trigger_produces_a_valid_grounded_message(trigger_id: str) -> None:
    category, merchant, trigger, customer = bundle(trigger_id)
    result = compose(category, merchant, trigger, customer, use_llm=False)
    assert result is not None, f"{trigger_id} produced nothing"

    body = result["body"]
    prepared = prepare(category, merchant, trigger, customer)
    allowed = allowed_numbers_for(prepared["draft"], prepared["deterministic_body"])

    ok, problems = guardrails.validate_body(body, category, allowed_numbers=allowed)
    assert ok, f"{trigger_id}: {problems}\n{body}"
    assert guardrails.has_specific_anchor(body), f"{trigger_id} has no verifiable anchor"
    assert result["rationale"]
    assert result["suppression_key"]


@pytest.mark.parametrize("trigger_id", sorted(TRIGGERS))
def test_composition_is_deterministic(trigger_id: str) -> None:
    category, merchant, trigger, customer = bundle(trigger_id)
    first = compose(category, merchant, trigger, customer, use_llm=False)
    second = compose(category, merchant, trigger, customer, use_llm=False)
    assert first["body"] == second["body"]
    assert first["rationale"] == second["rationale"]


def test_send_as_follows_scope() -> None:
    category, merchant, trigger, customer = bundle("trg_001_research_digest_dentists")
    assert compose(category, merchant, trigger, customer, use_llm=False)["send_as"] == "vera"

    category, merchant, trigger, customer = bundle("trg_003_recall_due_priya")
    result = compose(category, merchant, trigger, customer, use_llm=False)
    assert result["send_as"] == "merchant_on_behalf"
    assert "Priya" in result["body"]


def test_dentist_gets_doctor_salutation_and_clinical_source() -> None:
    category, merchant, trigger, customer = bundle("trg_001_research_digest_dentists")
    body = compose(category, merchant, trigger, customer, use_llm=False)["body"]
    assert body.startswith("Dr. Meera")
    assert "JIDA" in body
    assert "2,100" in body


def test_hindi_speaking_merchant_gets_code_mixed_ask() -> None:
    category, merchant, trigger, customer = bundle("trg_004_perf_dip_bharat")
    body = compose(category, merchant, trigger, customer, use_llm=False)["body"]
    assert any(word in body.lower() for word in ("kar dun", "chalu", "dijiye", "rakhun"))


def test_customer_facing_message_uses_real_price_and_real_slots() -> None:
    category, merchant, trigger, customer = bundle("trg_003_recall_due_priya")
    body = compose(category, merchant, trigger, customer, use_llm=False)["body"]
    assert "₹299" in body  # merchant's actual active offer
    assert "Wed 5 Nov, 6pm" in body  # slot straight from the trigger payload
    assert "Thu 6 Nov, 5pm" in body


def test_consent_gate_blocks_a_kind_the_customer_did_not_opt_into() -> None:
    _category, _merchant, trigger, customer = bundle("trg_003_recall_due_priya")
    assert consent_ok(trigger, customer)[0] is True

    narrow = json.loads(json.dumps(customer))
    narrow["consent"]["scope"] = ["delivery_notifications"]
    ok, reason = consent_ok(trigger, narrow)
    assert ok is False and "does not cover" in reason


def test_unknown_trigger_kind_degrades_without_inventing() -> None:
    category, merchant, trigger, _customer = bundle("trg_001_research_digest_dentists")
    injected = json.loads(json.dumps(trigger))
    injected["kind"] = "some_kind_invented_after_submission"
    injected["payload"] = {}
    result = compose(category, merchant, injected, None, use_llm=False)
    assert result is not None
    prepared = prepare(category, merchant, injected, None)
    allowed = allowed_numbers_for(prepared["draft"], prepared["deterministic_body"])
    ok, problems = guardrails.validate_body(result["body"], category, allowed_numbers=allowed)
    assert ok, problems


def test_starved_context_is_skipped_rather_than_faked() -> None:
    category, merchant, trigger, _customer = bundle("trg_001_research_digest_dentists")
    empty_merchant = {"merchant_id": "m_x", "category_slug": "dentists", "identity": {"name": "X"}}
    empty_category = {"slug": "dentists", "voice": {}, "peer_stats": {}, "digest": [], "offer_catalog": []}
    injected = json.loads(json.dumps(trigger))
    injected["payload"] = {}
    assert compose(empty_category, empty_merchant, injected, None, use_llm=False) is None


# ----------------------------------------------------------------- tick wiring


def _seeded_store() -> ContextStore:
    store = ContextStore()
    for slug, data in CATEGORIES.items():
        store.push("category", slug, 1, data)
    for mid, data in MERCHANTS.items():
        store.push("merchant", mid, 1, data)
    for cid, data in CUSTOMERS.items():
        store.push("customer", cid, 1, data)
    for tid, data in TRIGGERS.items():
        store.push("trigger", tid, 1, data)
    return store


def test_tick_emits_one_action_per_trigger_with_every_required_field() -> None:
    store = _seeded_store()
    ids = sorted(TRIGGERS)[:5]
    actions = build_tick_actions(store, "2026-04-26T10:35:00Z", ids, use_llm=False)
    assert actions

    required = {
        "conversation_id",
        "merchant_id",
        "customer_id",
        "send_as",
        "trigger_id",
        "template_name",
        "template_params",
        "body",
        "cta",
        "suppression_key",
        "rationale",
    }
    for action in actions:
        assert required <= set(action), required - set(action)
        assert action["trigger_id"] in ids
        assert action["body"].strip()

    conversation_ids = [a["conversation_id"] for a in actions]
    assert len(conversation_ids) == len(set(conversation_ids))


def test_tick_never_repeats_and_carries_the_overflow_to_the_next_tick() -> None:
    """The 20/tick cap defers the remainder; nothing is ever sent twice."""
    store = _seeded_store()
    ids = sorted(TRIGGERS)

    first = build_tick_actions(store, "2026-04-26T10:35:00Z", ids, use_llm=False)
    assert len(first) == 20

    second = build_tick_actions(store, "2026-04-26T10:40:00Z", ids, use_llm=False)
    assert len(second) == len(ids) - 20

    sent_first = {a["trigger_id"] for a in first}
    sent_second = {a["trigger_id"] for a in second}
    assert sent_first.isdisjoint(sent_second)
    assert sent_first | sent_second == set(ids)

    third = build_tick_actions(store, "2026-04-26T10:45:00Z", ids, use_llm=False)
    assert third == []


def test_tick_respects_the_20_action_cap_and_urgency_order() -> None:
    store = _seeded_store()
    actions = build_tick_actions(store, "2026-04-26T10:35:00Z", sorted(TRIGGERS), use_llm=False)
    assert len(actions) <= 20
    urgencies = [int(TRIGGERS[a["trigger_id"]].get("urgency") or 0) for a in actions]
    assert urgencies == sorted(urgencies, reverse=True)


def test_tick_skips_merchants_who_opted_out() -> None:
    store = _seeded_store()
    runtime_state.opt_out("m_001_drmeera_dentist_delhi", "test")
    actions = build_tick_actions(store, "2026-04-26T10:35:00Z", sorted(TRIGGERS), use_llm=False)
    assert all(a["merchant_id"] != "m_001_drmeera_dentist_delhi" for a in actions)


def test_tick_ignores_triggers_whose_contexts_were_never_pushed() -> None:
    store = ContextStore()
    store.push("trigger", "trg_001_research_digest_dentists", 1, TRIGGERS["trg_001_research_digest_dentists"])
    assert build_tick_actions(store, None, ["trg_001_research_digest_dentists"], use_llm=False) == []


def test_tick_uses_the_latest_context_version() -> None:
    """The judge injects updated performance mid-test; the message must move with it."""
    store = _seeded_store()
    updated = json.loads(json.dumps(MERCHANTS["m_002_bharat_dentist_mumbai"]))
    updated["performance"]["views"] = 4242
    store.push("merchant", "m_002_bharat_dentist_mumbai", 2, updated)

    actions = build_tick_actions(store, None, ["trg_005_renewal_due_bharat"], use_llm=False)
    assert actions and "4,242" in actions[0]["body"]
