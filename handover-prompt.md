# Handover Prompt — Main Capabilities Brief

**For**: Any coding agent who takes over this project after the documentation phase.
**Version**: 1.0 (2026-08-30)
**Goal**: This agent should be able to pick up where I left off and implement the Vera message engine bot — without needing to re-read all 11 docs from scratch.

---

## 1. What This Project Is

A deterministic, stateful HTTP bot (Python FastAPI) that implements `compose(category, merchant, trigger, customer?) → {body, cta, send_as, suppression_key, rationale}` for the magicpin AI Challenge. It serves 5 endpoints (`/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply`) and gets scored by an LLM judge across 5 dimensions (specificity, category fit, merchant fit, decision quality, engagement compulsion, total 50).

**Key design choice**: v1 is **rules-first, deterministic**. No LLM inside the tick/reply path (avoids −2 hallucination penalty and 30s timeout). Optional LLM polish only behind strict budget guard, temp=0.

---

## 2. Repository Structure (what to create / what exists)

```
magicpin-challenge/
├── challenge-brief.md              ← external (read-only)
├── challenge-testing-brief.md      ← external (read-only)
├── engagement-design.md            ← external (read-only)
├── engagement-research.md          ← external (read-only)
├── PRD.md                          ← WRITTEN IN CHAT (this file)
├── architecture.md                 ← WRITTEN IN CHAT
├── rules.md                        ← WRITTEN IN CHAT
├── phases.md                       ← WRITTEN IN CHAT
├── plan.md                         ← WRITTEN IN CHAT
├── memory.md                       ← WRITTEN IN CHAT (living checklist)
├── reference.md                    ← TO BE CREATED/UPDATED IN CHAT
├── bot/                          ← CREATE ME
│   ├── __init__.py
│   ├── main.py                     ← FastAPI app + 5 endpoints
│   ├── store.py                    ← ContextStore (version-aware)
│   ├── composer/
│   │   ├── __init__.py
│   │   ├── engine.py               ← compose() pipeline (DETERMINISTIC)
│   │   ├── trigger_router.py       ← kind → strategy fn dispatch
│   │   ├── signal_selector.py      ← merchant signals vs trigger urgency
│   │   ├── slot_extractor.py       ← pull concrete facts from contexts
│   │   ├── voice_renderer.py       ← category tone/vocab/taboos + lang mix
│   │   ├── cta_policy.py           ← one CTA, last sentence, binary/open-ended
│   │   └── suppression_check.py    ← exp_key expiry + global dedup hash
│   ├── conversation/
│   │   ├── __init__.py
│   │   └── handler.py              ← /v1/reply: auto-reply, intent, hostile, off-topic
│   └── guardrails.py               ← anti-repetition, CTA placement validation, template scaffolding
├── tests/
│   ├── test_store.py
│   ├── test_compose.py
│   ├── test_converse.py
│   └── __init__.py
├── README.md                       ← 1-page approach/tradeoffs (participant delivers)
└── reference.md                    ← living checklist + progress tracker (updates per session)
```

---

## 3. How to Run Locally (before any coding)

```bash
# 1. Extract challenge pack (if not already)
unzip magicpin-ai-challenge.zip          # creates dataset/, examples/

# 2. Set up Python env
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn

# 3. Optional: LLM key for judge_simulator (not strictly needed for bot v1)
#    If you want to run judge_simulator.py locally:
export LLM_API_KEY="sk-..."              # OpenAI/Anthropic/Gemini key
#    If using deterministic rules-only bot, LLM_API_KEY can be left empty;
    judge_simulator.py will still work for warmup/phase2_short (no scoring
    needing LLM, just endpoint contract checks), but full_evaluation needs it.

# 4. Run the bot
uvicorn bot.main:app --host 0.0.0.0 --port 8080

# 5. In another terminal, run judge simulator
export BOT_URL=http://localhost:8080
python judge_simulator.py                 # starts with warmup scenario
```

**Expected**: healthz returns 200; metadata returns team info; context push accepted; tick may return empty `actions: []` (since compose not yet fully grounded in our contexts — that's the implementation task).

---

## 4. Key Code Entry Points

| File | Function | What It Does |
|---|---|---|
| `bot/main.py` | `create_app()` or top-level FastAPI instance | Registers all 5 routes + healthz probe. On startup, warmup-populates `ContextStore` from all 5 category JSONs + 50 merchants + 200 customers (read from `dataset/`). |
| `bot/store.py` | `ContextStore.push(scope, cid, version, payload)` | Idempotent: same version → no-op (accepted:true); higher version → replaces atomically; lower version → returns `{accepted:false, reason:"stale_version", current_version}`. `get(scope, cid)` returns payload dict. |
| `bot/composer/engine.py` | `compose(category, merchant, trigger, customer?) → dict` | Entry point the judge harness calls via `/v1/tick`. Pipeline: TriggerRouter → SignalSelector → SlotExtractor → VoiceRenderer → CTAPolicy → SuppressionCheck → RationaleBuilder. Returns `{body, cta, send_as, suppression_key, rationale}`. **Deterministic**: no RNG, pure functions, sorted dict iteration. |
| `bot/composer/trigger_router.py` | `dispatch(trigger) → (Action|None)` | Looks up `trigger.kind` in a `strategies: dict[str, Callable]` table. Registered kinds (v1 minimum): `research_digest`, `perf_spike`, `recall_due`. Unknown kind → graceful degradation: compose generic-but-grounded nudge from available data, or skip (emit no action). |
| `bot/composer/signal_selector.py` | `evaluate(merchant, trigger) → bool` | Checks merchant.performance signals against trigger urgency/payload. E.g., for `perf_spike`: yesterday's views +28% vs 30d avg; for `perf_dip`: calls dropped 40% week-over-week. Returns `True` if signal is compelling enough to message about. |
| `bot/composer/slot_extractor.py` | `extract(category, merchant, trigger, customer?) → dict` | Pulls concrete values: from `trigger.payload` (e.g., `{last_visit, due_date, top_item}`), `merchant.performance` (views/calls/ctr/deltas), `merchant.offers` (active offer title + price), `category.offer_catalog` (fallback service+price patterns), `category.peer_stats` (benchmarks), `merchant.identity.locality` (city). If < 3 mandatory slots filled → skip trigger (return None, compose will emit no action). |
| `bot/composer/voice_renderer.py` | `render(body, category) → body` | Enforces category voice/tone/vocab: dentists → clinical peer, taboos `["cure","guaranteed"]`; salons → warm/friendly; restaurants → operator-to-operator; gyms → coaching/motivational; pharmacies → trustworthy/precise. Injects Hindi-English code-mix where `identity.languages` includes `hi`. Removes any leftover commentary/# symbols. |
| `bot/composer/cta_policy.py` | `validate_and_finalize(body, trigger) → (body, cta)` | Ensures exactly one CTA; binary YES/STOP for action triggers; none for pure-information; CTA must land in last sentence of body. If body has 0 CTA → either inject appropriate one or return `(body, cta="none")`. If >1 CTA → strip secondary CTAs, keep primary; log warning. |
| `bot/composer/suppression_check.py` | `should_suppress(suppression_key, merchant_id, actions_fired_today) → bool` | Checks trigger `expires_at`; global dedup hash of `(body, merchant_id)` to enforce anti-repetition (last 7 days equivalent, or last 10 actions). Returns `True` if this action should be suppressed. |
| `bot/conversation/handler.py` | `handle_reply(reply_body) → {action, body?, wait_seconds?, rationale?}` | Three code paths: 1) Auto-reply detection: if last 2–3 incoming messages are verbatim canned WA Business reply → `{action: "end", rationale:"auto-reply detected"}`. 2) Intent transition: if merchant message contains any actioning word `["done","sending","draft","here","confirm","proceed","next"]` AND NO qualifying word `["would you","do you","can you tell","what if","how about"]` → switch to action mode (emit next nudge, `cta: "open_ended"`). 3) Hostile/off-topic: merchant says `["stop","don't message me","not interested","unsubscribe"]` OR asks curveball outside vertical → `{action: "end", rationale:"merchant ended / off-topic"}`. Default: continue conversation with next nudge. |
| `bot/guardrails.py` | Various validation functions | Anti-repetition hash check; CTA placement validation (last sentence); template scaffolding (first outbound in 24h session uses `template_name` + `template_params[]`); body sanitization (remove any leftover `#` / `//` comments). |

---

## 5. Simulator Quirks to Remember (from judge_simulator.py reading)

| Quirk | Detail | Code Implication |
|---|---|---|
| Fresh conv_ids per auto-reply turn | `_auto_reply` sends 4 turns, each with `conv_auto_1`, `conv_auto_2`, `conv_auto_3`, `conv_auto_4` — different conversation IDs. Detection must be per‑merchant (last 2–3 messages identical), not per‑conversation. | In `converse/handler.py`, track last N messages by `merchant_id`, not by `conversation_id`. |
| Intent keyword check | Body `"Ok lets do it. Whats next?"` → check `any(word in body_lower for word in actioning)` AND `not any(word in body_lower for word in qualifying)`. Qualifying words: `["would you","do you","can you tell","what if","how about"]`. | In handler, after receiving merchant message, parse these word sets. |
| Hostile expectations | Expects `action: "end"` OR `action: "send"` body with `["sorry","apolog","won't"]`. | Handler must handle both; if merchant is hostile, either exit gracefully or apologize + exit. |
| Scoring penalties | Fabrication −2; internal jargon leaked to merchant −1; repeated body −2; malformed −2; timeout −1 each; healthz 3 failures −10. | Every compose step must verify: no invented facts; no category‑inappropriate vocabulary (e.g., casual promo talk for dentists); no body repeats within conversation or 7‑day window. |
| Timeouts | healthz 5s, metadata 5s, context 10s, tick 15s (simulator) / 30s (judge), reply 15s (simulator) / 30s (judge). | All compose + tick/reply work must complete within 15s wall clock. If > 15s, return `actions: []` from tick immediately. |
| Context version conflict | Re‑posting same version is no-op; higher version replaces atomically; 409 on stale version per brief §2.1. | In `store.py`, `push()`: `if cur and cur["version"] >= body.version: return {accepted: false, reason: "stale_version", current_version}`. But wait — the skeleton code returns `{accepted: true}` for same version no-op. Need to decide: follow brief §2.1 (409) or skeleton (200 no-op). This is Q2 in memory.md. |
| Dataset seeds vs expanded | `judge_simulator.py` DatasetLoader reads `dataset/merchants_seed.json`, `customers_seed.json`, `triggers_seed.json` (the 10/15/25 seeds). `generate_dataset.py` expands them to 50/200/100/30 test pairs. The base dataset the judge loads is the seeds, not the expanded set. | When warmup pushes contexts, it pushes the seed data. The expanded set is what participants will compose for (the 30 canonical test pairs). |
| `vocab_taboo` vs `taboos` | Simulator's LLM scorer references `category.get("voice", {}).get("vocab_taboo", [])` while brief defines `VoiceProfile.vocab_taboo`. Treat as same concept — map one to the other. | In voice_renderer, pull taboo words from whichever key the category context uses. |

---

## 6. What the Next Agent Should Do First

1. **Create the repo structure** above (folders + empty `__init__.py` files).
2. **Implement `bot/store.py`** — ContextStore version-aware push/get. Write unit tests.
3. **Implement `bot/main.py`** — FastAPI skeleton with all 5 routes + warmup population.
4. **Run `judge_simulator.py` warmup** — verify all endpoints return correct schemas.
5. **Implement `bot/composer/engine.py`** — the `compose()` pipeline starting with TriggerRouter + SignalSelector (2 strategies: research_digest, perf_spike).
6. **Run `judge_simulator.py` phase2_short** — iterate until 3 triggers produce actions with grounded bodies.
7. **Implement remaining composer steps** + ConversationHandler + Guardrails.
8. **Run full evaluation** — iterate until scores are green.
9. **Deploy + submit** — participant handles the final steps.

---

## 7. Quick Reference: compose() Input / Output

**Input dicts** (already deserialized from the JSON contexts the judge pushes):

| Key | Source | Key fields |
|---|---|---|
| `category` | `POST /v1/context` scope="category" payload | `slug`, `voice` (tone, vocab_allowed, vocab_taboo), `offer_catalog`, `peer_stats`, `digest`, `seasonal_beats`, `trend_signals` |
| `merchant` | `POST /v1/context` scope="merchant" payload | `merchant_id`, `identity` (name, city, locality, languages, verified), `subscription`, `performance` (views, calls, ctr, delta_7d), `offers` (active+paused), `conversation_history`, `customer_aggregate`, `signals` |
| `trigger` | `POST /v1/context` scope="trigger" payload | `id`, `scope` (merchant/customer), `kind`, `source`, `payload` (kind-specific), `urgency` 1-5, `suppression_key`, `expires_at` |
| `customer` | optional, `POST /v1/context` scope="customer" | `customer_id`, `merchant_id`, `identity` (name, phone, language_pref), `relationship` (first/last visit, visits_total, services), `state` (new/active/lapsed_soft/lapsed_hard/churned), `preferences`, `consent` |

**Output** (exactly one dict returned by `compose()`):

```json
{
  "body": "Dr. Meera, JIDA's Oct issue landed. One item relevant to your high-risk adult patients — 2,100-patient trial showed 3-month fluoride recall cuts caries recurrence 38% better than 6-month. Worth a look (2-min abstract). Want me to pull it + draft a patient-ed WhatsApp you can share?  — JIDA Oct 2026 p.14",
  "cta": "open_ended",           // or "yes_no" or "none"
  "send_as": "vera",            // or "merchant_on_behalf"
  "suppression_key": "research:dentists:2026-W17",
  "rationale": "External research digest with merchant-relevant clinical anchor; merchant is a dentist with high-risk-adult patient cohort"
}
```

---

## 8. Scoring Quick Reference (5 dims, 0-10 each, total 50)

| Dimension | What judge looks for | Min score to aim for |
|---|---|---|
| **Specificity** | Real numbers, ₹ prices, dates, source citations, concrete facts from context | ≥ 7 |
| **Category fit** | Voice/tone/taboos match the vertical; e.g., dentists clinical peer, not retail promo | ≥ 7 |
| **Merchant fit** | Merchant's own name/metrics/offers/language honored; never generic | ≥ 7 |
| **Decision quality** | Correct signal picked for this moment; skip when nothing is worth saying | ≥ 7 |
| **Engagement compulsion** | ≥1 lever: loss aversion, curiosity, social proof, effort externalization, reciprocity, asking-the-merchant; one clear CTA | ≥ 7 |

**Penalties**: fabrication −2; internal jargon −1; repeated body −2; malformed response −2; each timeout −1; 3 healthz failures −10.

---

## 9. Escalation / Questions

If anything is unclear, refer to:

- `challenge-brief.md` (§4–§7) — the 4-context framework, compose contract, base dataset
- `challenge-testing-brief.md` (§1–§5) — endpoints, timeouts, harness lifecycle, rate limits
- `engagement-design.md` (§1–§9) — composer pipeline, trigger taxonomy, engagement loops
- `engagement-research.md` — how existing system loads merchant/customer data (context for adapters)
- `judge_simulator.py` — CONFIGURATION section (BOT_URL, LLM_PROVIDER, LLM_API_KEY), scenarios (_warmup, _phase2_short, _auto_reply, _intent, _hostile, _all, _full)
- `memory.md` — this file, updated after each session
- `reference.md` — progress checklist (also updated per session)

If still stuck, flag in `memory.md` under "Open Decisions" and move on — don't let one blocker stall the whole project. The participant (you) will resolve open questions when resuming.

---
*End of handover prompt. Keep this file updated; any new findings, code paths, or simulator quirks should be appended so the next agent has a clean map.*