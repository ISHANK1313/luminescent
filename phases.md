# Phases — Project Development Milestones

**Status**: Draft v1.0 (pre-implementation)
**Last updated**: 2026-08-30
**Owner**: Solo participant
**Reference**: `challenge-testing-brief.md` §4 (judge harness phases) + `engagement-design.md` §9 (phased rollout)

---

## Legend

| Symbol | Meaning |
|---|---|
| ✅ | Exit criteria (must pass before advancing) |
| ⏱️ | Estimated effort (person-days, solo participant) |
| 📦 | Key deliverable at this phase |

---

## P0 — Pre-Start Setup (0 days, done before Phase 1)

| Task | Owner | Details |
|---|---|---|
| P0.1 | Participant | Download challenge pack (zip), extract, read `challenge-brief.md`, `challenge-testing-brief.md`, `engagement-design.md`, `engagement-research.md`, `judge_simulator.py`, `examples/api-call-examples.md`, `examples/case-studies.md`. |
| P0.2 | Participant | Set up Python 3.11+ env; `pip install fastapi uvicorn`. |
| P0.3 | Participant | Acquire LLM API key (OpenAI/Anthropic/Gemini) for judge_simulator; **not** strictly needed for bot v1 (deterministic rules-first), but needed for `judge_simulator.py` local self-test. |
| P0.4 | Participant | Choose public URL target (Railway/Render/Fly/ngrok); note URL for later deployment. |
| P0.5 | Agent (me) | Write all documentation docs (PRD, architecture, rules, phases, plan, memory, handover, reference). |
| **P0 Exit** | — | Pack read; env ready; docs drafted. |

---

## Phase 1 — Skeleton Endpoints + Context Store (⏱️ 2 days)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P1.1 | Agent | Create `main.py` FastAPI app with **all 5 endpoints**: `/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply` + optional `/v1/teardown`. Models match testing brief schemas exactly. | `judge_simulator.py` warmup scenario runs → all 5 endpoints return correct schemas; healthz + metadata reachable; context push accepted (5 categories + 5 merchants + 1 customer = 11 pushes). |
| P1.2 | Agent | Implement `ContextStore` class (`store.py`): thread-safe dict keyed by `(scope, context_id)`; version-aware push (higher replaces, same/lower no-op); `get(scope, cid)` returns payload. | Warmup pushes all succeed with idempotent behavior; re-posting same version returns `{accepted: true}` (no-op); lower version returns `{accepted: false, reason: "stale_version", current_version}`. |
| P1.3 | Agent | Add `/v1/metadata` returning `{team_name, team_members, model, approach, contact_email, version, submitted_at}` with placeholder values (will update in README). | endpoint tested via curl or simulator. |
| P1.4 | Agent | Write unit tests for ContextStore (idempotency, version conflict, get-after-push). | `pytest tests/test_store.py` passes 100%. |

**Phase 1 Deliverable**: `main.py` + `store.py` + tests; bot reachable at local URL `http://localhost:8080`.

---

## Phase 2 — ComposerEngine v1 (Merchant-Facing Core) (⏱️ 3 days)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P2.1 | Agent | Implement `compose()` engine (`composer/engine.py`) that takes `{category, merchant, trigger, customer?}` and returns `{body, cta, send_as, suppression_key, rationale}`. Core: TriggerRouter → SignalSelector → SlotExtractor → VoiceRenderer → CTA Policy → SuppressionCheck → RationaleBuilder. **Deterministic**: no RNG, pure functions, sorted iteration. | `judge_simulator.py` `_phase2_short` scenario: bot returns actions for 3 triggers; each action has all required fields; body is grounded in context (has at least one specific fact/number/date). |
| P2.2 | Agent | Implement TriggerRouter with registered strategies for at least 3 trigger kinds: `research_digest`, `perf_spike`, `recall_due` (merchant). Unknown kind → graceful degradation (compose generic grounded nudge or skip). | Bot produces a distinct message body for each of the 3 trigger kinds when given appropriate contexts. |
| P2.3 | Agent | Implement SlotExtractor: pull concrete facts from `trigger.payload`, `merchant.performance`, `merchant.offers`, `category.offer_catalog`, `category.peer_stats`. If < 3 mandatory slots filled → skip trigger (emit no action). | No message body contains invented facts; every number/price/date present in the provided contexts. |
| P2.4 | Agent | Implement VoiceRenderer: enforce category voice/tone/vocab rules; enforce taboos; inject Hindi-English code-mix where `languages` includes `hi`. | Dentist message uses clinical peer tone, avoids "cure"/"guaranteed"; salon message uses warm friendly tone; body length reasonable. |
| P2.5 | Agent | Implement CTA Policy: exactly one CTA per message; binary YES/STOP for action triggers; none for pure-information triggers; CTA in last sentence. | Every `/v1/tick` action has exactly one CTA field; no multi-CTA bodies. |
| P2.6 | Agent | Implement SuppressionCheck: respect trigger `suppression_key` and `expires_at`; global anti-repetition hash of `(body, merchant_id)`. | Bot never re-sends an expired suppression key within the same session. |
| P2.7 | Agent | Write unit tests for compose engine (at least 5 scenarios covering research_digest, perf_spike, recall_due). | `pytest tests/test_compose.py` passes; each scenario scores ≥ 7/10 on Specificity + Category Fit per manual LLM check. |
| **P2 Exit** | — | `compose()` produces correct, specific, voice-matched messages for merchant-facing triggers. |

**Phase 2 Deliverable**: Full composer engine + unit tests; simulator `_phase2_short` scores ≥ 30/50 average across 3 actions.

---

## Phase 3 — ConversationHandler + /v1/reply (⏱️ 2 days)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P3.1 | Agent | Implement `conversation/handler.py` with 3 code paths: auto-reply detection (same canned reply 2×+ → `action: "end"`), intent transition (merchant says `"ok lets do it"` or equivalent → switch to action mode, never re-qualify), graceful exit / off-topic (`action: "end"` with polite rationale). | `judge_simulator.py` `_auto_reply` test: bot ends after detecting auto-reply pattern (turns 1–4). |
| P3.2 | Agent | Implement explicit intent detection: if merchant message contains any actioning word `["done","sending","draft","here","confirm","proceed","next"]` AND does NOT contain any qualifying word `["would you","do you","can you tell","what if","how about"]` → route to action mode. | `judge_simulator.py` `_intent` test: bot correctly switches to ACTION mode after `"Ok lets do it. Whats next?"`. |
| P3.3 | Agent | Implement hostile/opt-out handling: merchant says `["stop","don't message me","not interested","unsubscribe"]` → `{action: "end", rationale:"merchant ended conversation"}`. | `judge_simulator.py` `_hostile` test: bot ends on hostile message or apologizes gracefully. |
| P3.4 | Agent | Write unit tests for each conversation handler path (≥ 3 test cases per path). | `pytest tests/test_converse.py` passes. |
| **P3 Exit** | — | Bot handles auto-reply, intent transition, and hostile scenarios per simulator. |

**Phase 3 Deliverable**: Conversation handler + tests.

---

## Phase 4 — Customer-Facing + Consent (⏱️ 2 days)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P4.1 | Agent | Extend `compose()` to handle `customer` context when populated; differentiate `send_as: "vera"` (merchant-facing) vs `"merchant_on_behalf"` (customer-facing). | Bot produces different `send_as` and body tone when customer context populated vs absent. |
| P4.2 | Agent | Implement recall_due customer-facing message template: include patient name, last visit date, due date, 2–3 real open slots from merchant offers, ₹ price from active offer, CTA "Reply 1 for X, 2 for Y" or open-ended. | Customer-facing message has real catalog price, real slots, patient name, language preference honored. |
| P4.3 | Agent | Add consent-awareness: if customer `consent.scope` does not include "recall_reminders", skip sending; `rationale="customer consent scope does not cover reminders"`. | Bot never sends to a customer without adequate consent. |
| P4.4 | Agent | Write unit tests for customer-facing compose (2 scenarios: consented + non-consented). | `pytest tests/test_customer_compose.py` passes. |
| **P4 Exit** | — | Customer-facing messages grounded, consent-aware, correct send_as. |

**Phase 4 Deliverable**: Customer compose path + consent logic.

---

## Phase 5 — Guardrails + Anti-Repetition (⏱️ 1 day)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P5.1 | Agent | Implement global anti-repetition: hash `body + merchant_id`; maintain small recent set (last 7 days equivalent, or last 10 actions) in memory; skip send if repeat detected, fallback to `rationale="anti-repetition: skipping repeat message"`. | Bot never sends same body twice to same merchant within window. |
| P5.2 | Agent | Add CTA placement validation: if CTA not in last sentence → auto-fix or log warning; during tick, if body generated without CTA where one should be → emit `actions: []` rather than malformed. | Every sent body has CTA in last sentence. |
| P5.3 | Agent | Add template scaffolding: first outbound in a 24h session uses `template_name` + `template_params[]` (even if just fill strings); subsequent free-form within window. | Bot returns `template_name` and `template_params` in tick actions when appropriate. |
| **P5 Exit** | — | Guardrails active; anti-repetition working. |

**Phase 5 Deliverable**: Guardrails module.

---

## Phase 6 — Simulator Iteration + Hardening (⏱️ 2 days)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P6.1 | Agent | Run `judge_simulator.py` `full_evaluation` scenario → record per-dimension scores + total. Identify any dimension avg < 7 → iterate composer rules, slot extraction, voice rendering. | ✅ 2026-09-03 via `tests/integration_judge_sim.py --score`: **35.05/50 gate-PASS** (run 1; 10/20 judge calls hit GLM capacity errors and were heuristically rescored — clean-scored 10 averaged **41.6/50**). 2026-08-31 baseline: 40.30 mean, no dimension < 7. |
| P6.2 | Agent | Run `judge_simulator.py` `auto_reply_hell`, `intent_transition`, `hostile` individually → all PASS. | ✅ 2026-09-03: all three PASS against a live local bot (also re-verified after every code change in sessions 5–6). |
| P6.3 | Agent | Check rate limits: 10 req/sec sustained from judge; no timeout > 15s on tick or reply. | ✅ 2026-09-03 via `tools/load_test.py`: 600 requests over 60s at exactly 10 req/s, 0 connection errors, 0 unexpected non-200s, 39 correct 409 stale-version responses, p95 < 30 ms on all endpoints (healthz/context/tick/reply), single cold-tick max 9.1 s < 15 s. |
| P6.4 | Agent | Verify determinism: run same 30 canonical test pairs twice → byte-identical `body` output (same seed, temp=0). | Determinism confirmed. |
| **P6 Exit** | — | All simulator scenarios pass; scores acceptable; bot stable. |

**Phase 6 Deliverable**: Green simulator run.

---

## Phase 7 — Deployment + Submission (⏱️ 1 day)

| # | Task | Owner | Exit Criteria ✅ |
|---|---|---|---|
| P7.1 | Participant | Deploy bot to public URL (Railway/Render/Fly/ngrok). Test reachability from internet. | `curl https://your-bot.example/v1/healthz` returns 200 from anywhere. |
| P7.2 | Participant | Fill `/v1/metadata` with real team info (name, members, model choice, approach summary, contact email, version). | metadata endpoint reflects participant's actual data. |
| P7.3 | Participant | Write `README.md` (≤ 1 page): approach, model choice, tradeoffs, what extra context would help most. | README present and readable. |
| P7.4 | Participant | Submit bot URL via challenge portal; keep bot live for evaluation window. | Submission confirmed; bot URL reachable. |
| **P7 Exit** | — | Bot live, docs complete, submission done. |

**Phase 7 Deliverable**: Public URL + README + portal submission.

---

## Phase 8 — Post-Submission Adaptation (after submission, judge injects new context)

| # | Task | Owner | Notes |
|---|---|---|---|
| P8.1 | Participant | After submission, judges inject fresh digest items, metric shifts, new triggers, and optional customer contexts (per brief §3 Phase 3). Bot must ground messages in *new* context without hallucinating. | This is why deterministic grounding in context is critical — bots that pattern-match the 30 canonical pairs will fail. |
| P8.2 | Participant | Review score report; note adaptation bonus/penalties. | Learn for future iterations. |

---

## Summary Timeline

| Phase | Days | Key Output |
|---|---|---|
| P0 | 0 | Pack read, env ready |
| 1 | 2 | Endpoints + context store |
| 2 | 3 | ComposerEngine v1 (merchant-facing) |
| 3 | 2 | ConversationHandler + /v1/reply |
| 4 | 2 | Customer-facing + consent |
| 5 | 1 | Guardrails + anti-repetition |
| 6 | 2 | Simulator iteration + hardening |
| 7 | 1 | Deployment + README + submission |
| **Total** | **14** | End-to-end ready bot |