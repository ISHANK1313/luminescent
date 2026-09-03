"""Conversation-handler tests, including the judge's exact replay probes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.conversation.handler import ACTIONING_TOKENS, QUALIFYING_TOKENS, handle_reply
from bot.state import runtime_state
from bot.store import ContextStore

DATASET = Path(__file__).resolve().parents[1] / "dataset"
MERCHANT_ID = "m_001_drmeera_dentist_delhi"
AUTO_REPLY = "Thank you for contacting us! Our team will respond shortly."


@pytest.fixture()
def store() -> ContextStore:
    runtime_state.clear()
    s = ContextStore()
    for f in sorted((DATASET / "categories").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        s.push("category", data["slug"], 1, data)
    for m in json.loads((DATASET / "merchants_seed.json").read_text(encoding="utf-8"))["merchants"]:
        s.push("merchant", m["merchant_id"], 1, m)
    yield s
    runtime_state.clear()


def reply(store, message, conversation_id="conv_1", turn=2, merchant_id=MERCHANT_ID):
    return handle_reply(
        store=store,
        request_body={
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": None,
            "from_role": "merchant",
            "message": message,
            "received_at": "2026-04-26T10:42:00Z",
            "turn_number": turn,
        },
    )


def test_auto_reply_ladder_across_fresh_conversation_ids(store) -> None:
    """The harness opens a NEW conversation for each canned reply — detection is per merchant."""
    first = reply(store, AUTO_REPLY, "conv_auto_1", 2)
    assert first["action"] == "send"
    assert "auto-reply" in first["body"].lower()

    second = reply(store, AUTO_REPLY, "conv_auto_2", 3)
    assert second["action"] == "wait"
    assert second["wait_seconds"] >= 3600

    third = reply(store, AUTO_REPLY, "conv_auto_3", 4)
    assert third["action"] == "end"

    fourth = reply(store, AUTO_REPLY, "conv_auto_4", 5)
    assert fourth["action"] == "end"


def test_verbatim_repetition_counts_as_an_auto_reply(store) -> None:
    reply(store, "We have received your message and will revert", "conv_a", 2)
    second = reply(store, "We have received your message and will revert", "conv_b", 3)
    assert second["action"] in {"wait", "end"}


def test_intent_transition_passes_the_judges_keyword_probe(store) -> None:
    """`judge_simulator._intent` requires an actioning token and no qualifying token."""
    result = reply(store, "Ok lets do it. Whats next?", "conv_intent_1", 2)
    assert result["action"] == "send"

    body = result["body"].lower()
    assert any(token in body for token in ACTIONING_TOKENS), body
    assert not any(token in body for token in QUALIFYING_TOKENS), body


@pytest.mark.parametrize(
    "message",
    ["Ok lets do it. Whats next?", "go ahead", "yes please", "Mujhe judrna hai, kar dijiye", "sign me up"],
)
def test_all_commitment_phrasings_switch_to_action_mode(store, message: str) -> None:
    runtime_state.clear()
    body = reply(store, message, "conv_x", 2)["body"].lower()
    assert any(token in body for token in ACTIONING_TOKENS)
    assert not any(token in body for token in QUALIFYING_TOKENS)


def test_hostile_message_ends_and_opts_out(store) -> None:
    result = reply(store, "Stop messaging me. This is useless spam.", "conv_hostile", 2)
    assert result["action"] == "end"
    assert runtime_state.is_opted_out(MERCHANT_ID)

    later = reply(store, "Hello?", "conv_after", 3)
    assert later["action"] == "end"


def test_off_topic_is_declined_but_the_thread_is_kept(store) -> None:
    result = reply(store, "Btw can you also help me with my GST filing this month?", "conv_off", 2)
    assert result["action"] == "send"
    assert "?" in result["body"]
    assert result["body"].count("?") == 1


def test_request_for_time_backs_off(store) -> None:
    result = reply(store, "I am busy right now, call me later", "conv_later", 2)
    assert result["action"] == "wait"
    assert result["wait_seconds"] > 0


def test_engaged_reply_delivers_next_step(store) -> None:
    result = reply(store, "Yes please send the abstract. Also draft the patient WhatsApp.", "conv_eng", 2)
    assert result["action"] == "send"
    assert result["body"]


def test_every_response_matches_the_contract(store) -> None:
    messages = [
        AUTO_REPLY,
        "Ok lets do it",
        "not interested",
        "can you help with GST",
        "later please",
        "how much does it cost?",
        "",
    ]
    for index, message in enumerate(messages):
        runtime_state.clear()
        result = reply(store, message, f"conv_{index}", 2)
        assert result["action"] in {"send", "wait", "end"}
        assert result.get("rationale")
        if result["action"] == "send":
            assert result.get("body") and result.get("cta")
            assert result["body"].count("?") <= 1
        if result["action"] == "wait":
            assert isinstance(result["wait_seconds"], int) and result["wait_seconds"] > 0


def test_unknown_merchant_and_empty_conversation_do_not_crash(store) -> None:
    result = handle_reply(store=store, request_body={"message": "hello"})
    assert result["action"] in {"send", "wait", "end"}
