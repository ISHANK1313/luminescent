"""Unit tests for ContextStore version semantics and indexes."""

from __future__ import annotations

import pytest

from bot.store import ContextStore


@pytest.fixture()
def store() -> ContextStore:
    return ContextStore()


def test_starts_empty_with_all_scopes_reported(store: ContextStore) -> None:
    assert store.counts() == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}


def test_first_push_is_accepted(store: ContextStore) -> None:
    status, body = store.push("category", "dentists", 1, {"slug": "dentists"})
    assert status == 200
    assert body["accepted"] is True
    assert body["ack_id"] == "ack_dentists_v1"
    assert body["stored_at"].endswith("Z")
    assert store.counts()["category"] == 1


def test_same_version_is_idempotent_noop_not_an_error(store: ContextStore) -> None:
    store.push("merchant", "m_001", 1, {"merchant_id": "m_001", "views": 1})
    status, body = store.push("merchant", "m_001", 1, {"merchant_id": "m_001", "views": 999})
    assert status == 200
    assert body["accepted"] is True
    # No-op: the stored payload must NOT have been replaced.
    assert store.get("merchant", "m_001")["views"] == 1
    assert store.counts()["merchant"] == 1


def test_higher_version_replaces_atomically(store: ContextStore) -> None:
    store.push("merchant", "m_001", 1, {"merchant_id": "m_001", "views": 2410})
    status, body = store.push("merchant", "m_001", 2, {"merchant_id": "m_001", "views": 2580})
    assert status == 200 and body["accepted"] is True
    assert store.get("merchant", "m_001")["views"] == 2580
    assert store.get_version("merchant", "m_001") == 2
    assert store.counts()["merchant"] == 1


def test_lower_version_is_rejected_with_409(store: ContextStore) -> None:
    store.push("merchant", "m_001", 5, {"merchant_id": "m_001"})
    status, body = store.push("merchant", "m_001", 4, {"merchant_id": "m_001"})
    assert status == 409
    assert body == {"accepted": False, "reason": "stale_version", "current_version": 5}


@pytest.mark.parametrize(
    "scope,context_id,version,payload,reason",
    [
        ("bogus", "x", 1, {}, "invalid_scope"),
        ("merchant", "", 1, {}, "invalid_context_id"),
        ("merchant", "m_001", "v1", {}, "invalid_version"),
        ("merchant", "m_001", 1, "not-a-dict", "invalid_payload"),
    ],
)
def test_malformed_pushes_return_400(store, scope, context_id, version, payload, reason) -> None:
    status, body = store.push(scope, context_id, version, payload)
    assert status == 400
    assert body["accepted"] is False
    assert body["reason"] == reason


def test_indexes_and_sorted_ids(store: ContextStore) -> None:
    store.push("trigger", "trg_b", 1, {"id": "trg_b", "merchant_id": "m_001"})
    store.push("trigger", "trg_a", 1, {"id": "trg_a", "merchant_id": "m_001"})
    store.push("customer", "c_001", 1, {"customer_id": "c_001", "merchant_id": "m_001"})
    assert store.triggers_for_merchant("m_001") == ["trg_a", "trg_b"]
    assert store.customers_for_merchant("m_001") == ["c_001"]
    assert store.ids("trigger") == ["trg_a", "trg_b"]


def test_resolve_bundle_reports_what_is_missing(store: ContextStore) -> None:
    store.push("trigger", "trg_1", 1, {"id": "trg_1", "merchant_id": "m_001", "customer_id": None})
    bundle = store.resolve_bundle("trg_1")
    assert bundle["trigger"]["id"] == "trg_1"
    assert bundle["missing"] == ["merchant", "category"]

    store.push("merchant", "m_001", 1, {"merchant_id": "m_001", "category_slug": "dentists"})
    assert store.resolve_bundle("trg_1")["missing"] == ["category"]

    store.push("category", "dentists", 1, {"slug": "dentists"})
    bundle = store.resolve_bundle("trg_1")
    assert bundle["missing"] == []
    assert bundle["category"]["slug"] == "dentists"
    assert bundle["customer"] is None


def test_resolve_bundle_flags_unknown_trigger(store: ContextStore) -> None:
    assert store.resolve_bundle("nope")["missing"] == ["trigger"]


def test_resolve_bundle_requires_pushed_customer(store: ContextStore) -> None:
    store.push("trigger", "trg_c", 1, {"id": "trg_c", "merchant_id": "m_001", "customer_id": "c_001"})
    store.push("merchant", "m_001", 1, {"merchant_id": "m_001", "category_slug": "dentists"})
    store.push("category", "dentists", 1, {"slug": "dentists"})
    assert store.resolve_bundle("trg_c")["missing"] == ["customer"]


def test_clear_wipes_everything(store: ContextStore) -> None:
    store.push("category", "dentists", 1, {"slug": "dentists"})
    store.push("trigger", "trg_1", 1, {"id": "trg_1", "merchant_id": "m_001"})
    assert store.clear() == 2
    assert store.counts() == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    assert store.triggers_for_merchant("m_001") == []
