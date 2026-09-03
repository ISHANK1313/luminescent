"""Contract tests for the five judge-facing endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.main import app, store

DATASET = Path(__file__).resolve().parents[1] / "dataset"


@pytest.fixture()
def client() -> TestClient:
    store.clear()
    with TestClient(app) as c:
        yield c
    store.clear()


def test_healthz_reports_zero_contexts_before_any_push(client: TestClient) -> None:
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    # The judge's warmup compares these counts with exactly what it pushed.
    assert body["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}


def test_metadata_has_every_required_field(client: TestClient) -> None:
    body = client.get("/v1/metadata").json()
    for field in (
        "team_name",
        "team_members",
        "model",
        "approach",
        "contact_email",
        "version",
        "submitted_at",
    ):
        assert field in body, field
    assert isinstance(body["team_members"], list)


def test_context_push_accept_idempotent_and_stale(client: TestClient) -> None:
    payload = {"slug": "dentists", "voice": {"tone": "peer_clinical"}}
    push = {
        "scope": "category",
        "context_id": "dentists",
        "version": 1,
        "payload": payload,
        "delivered_at": "2026-04-26T09:45:00Z",
    }
    r = client.post("/v1/context", json=push)
    assert r.status_code == 200 and r.json()["accepted"] is True

    # Same version again -> idempotent no-op, still accepted (simulator prints PASS).
    r = client.post("/v1/context", json=push)
    assert r.status_code == 200 and r.json()["accepted"] is True

    # Higher version replaces.
    push_v2 = dict(push, version=2, payload={"slug": "dentists", "voice": {"tone": "peer_clinical"}, "digest": [1]})
    r = client.post("/v1/context", json=push_v2)
    assert r.status_code == 200 and r.json()["accepted"] is True

    # Lower version -> 409 stale.
    r = client.post("/v1/context", json=push)
    assert r.status_code == 409
    assert r.json() == {"accepted": False, "reason": "stale_version", "current_version": 2}

    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 1


def test_context_rejects_bad_scope_and_garbage_without_crashing(client: TestClient) -> None:
    r = client.post("/v1/context", json={"scope": "nope", "context_id": "x", "version": 1, "payload": {}})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_scope"

    r = client.post("/v1/context", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["accepted"] is False

    r = client.post("/v1/context", json=[1, 2, 3])
    assert r.status_code == 400


def test_tick_always_returns_actions_list(client: TestClient) -> None:
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": []})
    assert r.status_code == 200
    assert isinstance(r.json()["actions"], list)

    # Unknown trigger ids must not raise.
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": ["ghost"]})
    assert r.status_code == 200 and r.json()["actions"] == []

    # Malformed body must still return a valid envelope, never a 422.
    r = client.post("/v1/tick", content=b"", headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["actions"] == []


def test_reply_returns_exactly_one_valid_action_shape(client: TestClient) -> None:
    r = client.post(
        "/v1/reply",
        json={
            "conversation_id": "conv_001",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "customer_id": None,
            "from_role": "merchant",
            "message": "Yes please send the abstract",
            "received_at": "2026-04-26T10:42:00Z",
            "turn_number": 2,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["action"] in {"send", "wait", "end"}
    assert body.get("rationale")
    if body["action"] == "send":
        assert body.get("body")
        assert "cta" in body
    if body["action"] == "wait":
        assert isinstance(body["wait_seconds"], int)


def test_reply_survives_an_empty_body(client: TestClient) -> None:
    r = client.post("/v1/reply", json={})
    assert r.status_code == 200
    assert r.json()["action"] in {"send", "wait", "end"}


def test_teardown_wipes_state(client: TestClient) -> None:
    client.post(
        "/v1/context",
        json={"scope": "category", "context_id": "salons", "version": 1, "payload": {"slug": "salons"}},
    )
    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 1
    r = client.post("/v1/teardown")
    assert r.status_code == 200 and r.json()["wiped"] is True
    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 0


def test_real_dataset_payloads_are_accepted(client: TestClient) -> None:
    """Push the actual seed data the way the judge harness does."""
    pushed = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}

    for f in sorted((DATASET / "categories").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        r = client.post(
            "/v1/context",
            json={"scope": "category", "context_id": data["slug"], "version": 1, "payload": data},
        )
        assert r.status_code == 200 and r.json()["accepted"] is True
        pushed["category"] += 1

    for name, key, scope in (
        ("merchants_seed.json", "merchant_id", "merchant"),
        ("customers_seed.json", "customer_id", "customer"),
        ("triggers_seed.json", "id", "trigger"),
    ):
        data = json.loads((DATASET / name).read_text(encoding="utf-8"))
        for item in data[scope + "s"]:
            r = client.post(
                "/v1/context",
                json={"scope": scope, "context_id": item[key], "version": 1, "payload": item},
            )
            assert r.status_code == 200 and r.json()["accepted"] is True
            pushed[scope] += 1

    assert pushed == {"category": 5, "merchant": 10, "customer": 15, "trigger": 25}
    assert client.get("/v1/healthz").json()["contexts_loaded"] == pushed

    # Every seed trigger must resolve to a full 4-context bundle.
    for tid in store.ids("trigger"):
        bundle = store.resolve_bundle(tid)
        assert bundle["missing"] == [], f"{tid} could not be resolved: {bundle['missing']}"
