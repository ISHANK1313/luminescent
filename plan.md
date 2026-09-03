# Plan — Actionable Project Plan

**Status**: Draft v1.0 (pre-implementation)
**Last updated**: 2026-08-30
**Owner**: Solo participant (this agent writes docs; participant implements code)
**Reference**: `phases.md` milestones, `PRD.md` goals, `rules.md` compliance checklist

---

## 1. High-Level Roadmap

| Week | Milestone | Deliverable | Go/No-Go |
|---|---|---|---|
| **W0 (now)** | Documentation complete | All 11 docs (PRD, architecture, rules, phases, plan, memory, 3 handover, reference) | ✅ Done (this chat) |
| **W1** | Phase 1–2 skeleton | `main.py`, `store.py`, `composer/engine.py`; all 5 endpoints functional; `judge_simulator.py` warmup + phase2_short passes | ✅ Go if: all endpoints return correct schemas; context push idempotent |
| **W2** | Phase 3–4 | `conversation/handler.py`; customer-facing compose with consent; `_auto_reply`, `_intent`, `_hostile` all PASS | ✅ Go if: simulator auto-reply/intent/hostile pass; compose scores ≥ 7/10 on Spec+CatFit+MerFit |
| **W3** | Phase 5–6 | Guardrails + anti-repetition; full `full_evaluation` run; avg total ≥ 35/50; determinism confirmed (2 runs = same body) | ✅ Go if: scores acceptable; no timeouts; healthz stable |
| **W4** | Phase 7 | Deploy public URL; `README.md`; portal submission; keep bot live | ✅ Go if: URL reachable externally; metadata filled; README ≤ 1 page |
| **W5 (post-sub)** | Phase 8 | Judge harness fresh-scoring; adapt to injected context; review score report | — |

---

## 2. Task Breakdown by Owner

| Task ID | Task | Owner | Effort (hrs) | Dependencies | Status |
|---|---|---|---|---|---|
| T1 | Write all documentation docs (PRD, architecture, rules, phases, plan, memory, 3 handover, reference) | Agent (me) | 12 | None | ✅ Done |
| T2 | Set up Python env; `pip install fastapi uvicorn`; extract challenge pack | Participant | 1 | T1 (docs provide context) | ⏳ Pending |
| T3 | Implement `main.py` FastAPI with all 5 endpoints + models | Agent | 6 | T2 env ready | ⏳ Queued |
| T4 | Implement `store.py` ContextStore (version-aware, idempotent) | Agent | 4 | T3 main.py scaffold | ⏳ Queued |
| T5 | Implement `composer/engine.py` compose() pipeline (deterministic) | Agent | 10 | T4 context store | ⏳ Queued |
| T6 | Implement `conversation/handler.py` (auto-reply, intent, hostile) | Agent | 6 | T5 compose engine | ⏳ Queued |
| T7 | Implement slot extraction, voice renderer, CTA policy in compose | Agent | 8 | T5 compose engine | ⏳ Queued |
| T8 | Write unit tests (`tests/test_*.py`) for all modules | Agent | 6 | T5–T7 code complete | ⏳ Queued |
| T9 | Run `judge_simulator.py` scenarios; iterate on failures | Participant | 10+ (ongoing) | T8 tests done + LLM API key | ⏳ Pending |
| T10 | Deploy to public URL; fill metadata; write README; submit | Participant | 4 | T9 simulator green | ⏳ Pending |
| T11 | Keep bot live during evaluation; monitor score report | Participant | ongoing | T10 submission done | ⏳ Pending |

---

## 3. Daily Stand-Format (participant, if running this as a real project)

Each day: what was done yesterday, what will be done today, blockers.

| Day | Done Yesterday | Plan Today | Blockers |
|---|---|---|---|
| 1 | Docs written (all 11) | Set up Python env + extract pack | — |
| 2 | Env ready | Implement `main.py` skeleton + 5 endpoints | — |
| 3 | Endpoints working | Implement `store.py` ContextStore | — |
| 4 | Context store idempotent | Start `composer/engine.py` TriggerRouter + SignalSelector | — |
| 5 | Core compose pipeline | Implement SlotExtractor + VoiceRenderer | — |
| 6 | CTA policy + suppression | Implement CTA Policy + SuppressionCheck | — |
| 7 | First end-to-end test | Run `judge_simulator.py` warmup | Any failure → iterate |
| 8 | Phase2_short passes | Add conversational handler | — |
| 9 | Auto-reply/intent/hostile pass | Add guardrails + anti-repetition | — |
| 10 | Full evaluation green | Deploy + README + submit | — |

---

## 4. Success Metrics (track in `reference.md`)

| Metric | Target | How Measured |
|---|---|---|
| M1 | All 5 endpoints return correct schemas | `judge_simulator.py` warmup: PASS |
| M2 | Avg total score ≥ 35/50 in `full_evaluation` | Simulator final summary bar |
| M3 | No dimension avg < 6/10 | Same as M2, per-dimension bars |
| M4 | All 3 scenario tests pass: auto_reply_hell, intent_transition, hostile | Simulator scenario results |
| M5 | Determinism: 2 runs of same inputs → byte-identical `body` | Manual check or hash compare |
| M6 | Bot survives 60-min test window without healthz failures | Judge logs: 0 healthz failures |
| M7 | No fabricated facts in any message body | Manual spot-check against contexts |

---

## 5. Risk Register

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | LLM API key not available / too expensive | Medium | High (judge sim requires it) | Use deterministic rules-first composer (v1 has NO LLM calls inside bot); LLM only optional polish outside 30s budget. |
| R2 | 30s timeout too tight for compose | Medium | High | Optimize slot extraction; if > 15s, return `actions: []` and skip tick; design fast-path templates. |
| R3 | Judge injects context bot can't ground | High (Phase 3 real harness) | High | Every compose step references ONLY values from pushed contexts; never invent numbers/dates/offers. |
| R4 | Auto-reply detection misses pattern | Medium | Medium | Check last 2–3 incoming messages for verbatim canned match; also check `turn_number` + `from_role`. |
| R5 | Missing CTA or multiple CTAs in body | Medium | Medium | CTA Policy validation step; post-compose check: if body has 0 or >1 CTA pattern → either add CTA or emit `actions: []`. |
| R5 | Version conflict mishandling in context push | Low | Medium | Follow brief §2.1 exactly: higher version replaces; same version no-op; lower version → 409/stale_version. |

---

## 6. Completion Checklist (toggle as you go)

- [x] T1: All documentation docs written (PRD, architecture, rules, phases, plan, memory, 3 handover, reference)
- [ ] T2: Python env set + challenge pack extracted
- [ ] T3: `main.py` with all 5 endpoints + models
- [ ] T4: `store.py` ContextStore version-aware
- [ ] T5: `compose()` deterministic engine
- [ ] T6: Conversation handler (auto-reply, intent, hostile)
- [ ] T7: Slot extraction, voice, CTA policy
- [ ] T8: Unit tests for all modules
- [ ] T9: `judge_simulator.py` all scenarios PASS
- [ ] T10: Public URL live + README + portal submission
- [ ] T11: Bot kept live during evaluation window

---

## 7. Handoff Notes (for any future coding agent)

- **Start here**: `reference.md` has the live checklist + temp info; `memory.md` has decisions made today.
- **Code lives** in: `main.py`, `store.py`, `composer/engine.py`, `conversation/handler.py`, `guardrails.py` (added Phase 5).
- **Test** via: `python judge_simulator.py` (set `BOT_URL=http://localhost:8080`, set `LLM_API_KEY`).
- **Simulator quirks** worth noting (see `reference.md` #Q1–Q4).
- **Never** modify `judge_simulator.py` config except `BOT_URL`, `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`.
- **Docs-update rule**: after every coding session, update `reference.md` checklist + add any new open questions to `memory.md`.

---

## 8. Tooling Commands (participant will run these)

```bash
# 1. Extract pack (already done)
unzip magicpin-ai-challenge.zip

# 2. Create env + install
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn python-dotnet-judger  # judge_simulator deps

# 3. Set LLM key (required for judge_simulator; optional for bot v1)
export LLM_API_KEY="sk-..."   # or leave empty if using deterministic rules only

# 4. Run bot locally
uvicorn main:app --host 0.0.0.0 --port 8080

# 5. Run judge simulator
python judge_simulator.py   # sets BOT_URL=http://localhost:8080 internally

# 6. Test endpoints
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
curl -sS http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{"scope":"category","context_id":"dentists","version":1,"payload":{...},"delivered_at":"2026-04-29T10:00:00Z"}'

# 7. Lint / format (if using black/isort)
black .
isort .

# 8. Quick unit test run
pytest tests/ -v
```