"""FastAPI application exposing the judge-facing contract.

Endpoints (challenge-testing-brief.md §2):
    GET  /v1/healthz    liveness + contexts_loaded counts
    GET  /v1/metadata   bot identity
    POST /v1/context    version-aware context push
    POST /v1/tick       proactive-send decision point
    POST /v1/reply      inbound merchant/customer turn
    POST /v1/teardown   optional state wipe (testing brief §11)

Design notes
------------
* The store starts EMPTY; the dataset on disk is never read at runtime (decision D3).
* `/v1/tick` and `/v1/reply` parse the body leniently: a missing or oddly-typed field
  must never produce a 422 or a 500, because the judge scores a malformed response at
  -2 and a crash costs the whole slot. Only `/v1/context` is strictly validated, and
  its failure mode is the documented 400 shape.
* Every handler is wrapped so that an unexpected exception still returns a
  schema-valid response instead of a 500.
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import metadata as build_metadata
from .store import MAX_PAYLOAD_BYTES, ContextStore

START_TIME = time.time()

app = FastAPI(title="Vera Message Engine", version=build_metadata()["version"])

# Process-wide state. Single process by design: the judge never restarts us mid-test.
store = ContextStore()

# Lightweight in-process request log (rule R27: no raw PII, truncated bodies).
REQUEST_LOG: List[Dict[str, Any]] = []
MAX_LOG_ENTRIES = 2000


def _log(endpoint: str, duration_ms: float, summary: str) -> None:
    if len(REQUEST_LOG) >= MAX_LOG_ENTRIES:
        del REQUEST_LOG[0 : len(REQUEST_LOG) // 2]
    REQUEST_LOG.append(
        {
            "ts": time.time(),
            "endpoint": endpoint,
            "duration_ms": round(duration_ms, 2),
            "summary": summary[:200],
        }
    )


async def _read_json(request: Request) -> Any:
    """Parse a JSON body, tolerating empty or invalid content."""
    raw = await request.body()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------- healthz


@app.get("/v1/healthz")
async def healthz() -> JSONResponse:
    """Must stay trivially cheap — the judge polls it every 60s with a 5s timeout."""
    return JSONResponse(
        {
            "status": "ok",
            "uptime_seconds": int(time.time() - START_TIME),
            "contexts_loaded": store.counts(),
        }
    )


# -------------------------------------------------------------------- metadata


@app.get("/v1/metadata")
async def metadata() -> JSONResponse:
    return JSONResponse(build_metadata())


# --------------------------------------------------------------------- context


@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    started = time.perf_counter()

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_PAYLOAD_BYTES:
                return JSONResponse(
                    status_code=400,
                    content={
                        "accepted": False,
                        "reason": "payload_too_large",
                        "details": f"payload exceeds {MAX_PAYLOAD_BYTES} bytes",
                    },
                )
        except ValueError:
            pass

    body = await _read_json(request)
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={
                "accepted": False,
                "reason": "malformed_body",
                "details": "request body must be a JSON object",
            },
        )

    scope = body.get("scope")
    context_id = body.get("context_id")
    version = body.get("version", 1)
    payload = body.get("payload")

    # Some harnesses send version as a numeric string; accept it rather than 400.
    if isinstance(version, str) and version.strip().lstrip("-").isdigit():
        version = int(version.strip())

    try:
        status, response = store.push(scope, context_id, version, payload)
    except Exception:  # pragma: no cover - defensive
        traceback.print_exc()
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "internal_error", "details": "push failed"},
        )

    _log(
        "/v1/context",
        (time.perf_counter() - started) * 1000,
        f"{scope}/{context_id} v{version} -> {status}",
    )
    return JSONResponse(status_code=status, content=response)


# ------------------------------------------------------------------------ tick


@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    """Decide whether to proactively send. Always returns `{"actions": [...]}`.

    Phase 2 wires the composer in here. Until then the honest answer is "nothing
    worth sending", which is an explicitly valid response ("restraint is rewarded").
    """
    started = time.perf_counter()
    body = await _read_json(request)
    body = body if isinstance(body, dict) else {}

    now: Optional[str] = body.get("now") if isinstance(body.get("now"), str) else None
    raw_triggers = body.get("available_triggers")
    available: List[str] = [t for t in raw_triggers if isinstance(t, str)] if isinstance(raw_triggers, list) else []

    actions: List[Dict[str, Any]] = []
    try:
        from .composer.engine import build_tick_actions  # noqa: WPS433 (optional in P1)
    except Exception:
        build_tick_actions = None  # type: ignore[assignment]

    if build_tick_actions is not None:
        try:
            actions = build_tick_actions(store=store, now=now, available_triggers=available)
        except Exception:  # pragma: no cover - never let compose break the contract
            traceback.print_exc()
            actions = []

    _log(
        "/v1/tick",
        (time.perf_counter() - started) * 1000,
        f"{len(available)} triggers -> {len(actions)} actions",
    )
    return JSONResponse({"actions": actions[:20]})


# ----------------------------------------------------------------------- reply


@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    """Handle one inbound turn. Response is exactly one of send / wait / end.

    Phase 3 wires the conversation handler in here.
    """
    started = time.perf_counter()
    body = await _read_json(request)
    body = body if isinstance(body, dict) else {}

    response: Dict[str, Any]
    try:
        from .conversation.handler import handle_reply  # noqa: WPS433 (optional in P1)
    except Exception:
        handle_reply = None  # type: ignore[assignment]

    if handle_reply is not None:
        try:
            response = handle_reply(store=store, request_body=body)
        except Exception:  # pragma: no cover - defensive
            traceback.print_exc()
            response = {
                "action": "wait",
                "wait_seconds": 3600,
                "rationale": "Internal error while composing a reply; backing off instead of sending anything unsafe.",
            }
    else:
        response = {
            "action": "wait",
            "wait_seconds": 3600,
            "rationale": "Conversation handling not yet enabled; holding off rather than sending an ungrounded reply.",
        }

    _log(
        "/v1/reply",
        (time.perf_counter() - started) * 1000,
        f"conv={body.get('conversation_id')} turn={body.get('turn_number')} -> {response.get('action')}",
    )
    return JSONResponse(response)


# -------------------------------------------------------------------- teardown


@app.post("/v1/teardown")
async def teardown() -> JSONResponse:
    removed = store.clear()
    try:
        from .state import runtime_state

        runtime_state.clear()
    except Exception:
        pass
    REQUEST_LOG.clear()
    return JSONResponse({"wiped": True, "contexts_removed": removed})


# ------------------------------------------------------------------ diagnostics


@app.get("/v1/_debug/log")
async def debug_log(limit: int = 50) -> JSONResponse:
    """Local debugging aid. Not part of the judge contract."""
    return JSONResponse({"entries": REQUEST_LOG[-max(1, min(limit, 500)) :]})


if __name__ == "__main__":  # pragma: no cover
    import os

    import uvicorn

    uvicorn.run(
        "bot.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        log_level="warning",
    )
