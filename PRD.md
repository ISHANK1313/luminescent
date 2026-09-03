# PRD — Vera Message Engine (magicpin AI Challenge)

**Status**: Draft v1.0 (pre-implementation)
**Last updated**: 2026-08-30
**Owner**: Solo participant (this project)
**Sources of truth**: `challenge-brief.md`, `challenge-testing-brief.md`, `engagement-design.md`, `engagement-research.md`, `judge_simulator.py`, `examples/api-call-examples.md`, `examples/case-studies.md`

---

## 1. One-line summary

Build a deterministic, stateful HTTP bot that composes WhatsApp messages for the fictional "Vera" merchant-growth assistant from 4 structured contexts — `compose(category, merchant, trigger, customer?) → {body, cta, send_as, suppression_key, rationale}` — and serves it at a public URL that the magicpin judge harness scores live.

---

## 2. Problem statement

magicpin's production Vera talks to 6,000–10,000 merchants/day on WhatsApp. Its known weaknesses (from `challenge-brief.md` §3):

1. **Auto-reply pollution** — 40–70% of "merchant replies" are WhatsApp Business canned auto-replies; production wastes 2–3 turns detecting them.
2. **Intent-handoff failures** — when a merchant says "let's do it", production re-asks qualifying questions instead of acting.
3. **Generic copy** — "10% off" style offers don't engage Indian merchants; service+price ("Dental Cleaning @ ₹299") does.
4. **Low engagement frequency** — purely functional nudges are too rare; needs curiosity/knowledge-driven conversations.

We win by beating production Vera on exactly these axes, judged by an LLM on 5 rubric dimensions.

## 3. Goals (measurable)

| # | Goal | Measure |
|---|---|---|
| G1 | Correct endpoint contract | All 5 endpoints return spec-exact schemas; `judge_simulator.py` warmup passes 100% |
| G2 | High message quality | Average ≥ 40/50 per action in local simulator (`full_evaluation`); no dimension avg < 7 |
| G3 | Conversation handling | Simulator scenarios `auto_reply_hell`, `intent_transition`, `hostile` all PASS |
| G4 | Operational reliability | Zero timeouts, zero malformed responses, healthz green for full local run |
| G5 | Determinism | Same inputs → byte-identical `body` across repeated calls |
| G6 | Adaptability | Bot grounds messages in *injected* context (not hardcoded dataset facts) — the real judge injects fresh digest items, perf shifts, and triggers we have never seen |

## 4. Non-goals

- No real WhatsApp/Meta integration (judge simulates it; template params are just strings in the response).
- No database requirement — in-memory state is explicitly allowed by the spec.
- No external retrieval/scraping. Bots must not send merchant/customer payload data to non-LLM external APIs (testing brief §11).
- Multi-turn beyond what `/v1/reply` requires (no outbound scheduling infrastructure — the judge drives time via `/v1/tick`).
- Growth/engagement analytics dashboards.

## 5. Users & stakeholders

| Stakeholder | What they need |
|---|---|
| **Judge harness (primary "user")** | Timely, schema-correct, deterministic HTTP behavior across `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata` |
| **LLM judge (scorer)** | Grounded, specific, voice-correct message bodies + honest `rationale` fields it can verify against the pushed contexts |
| **Simulated merchants/customers** | Messages worth replying to: one clear reason-why-now + one low-effort CTA |
| **magicpin team (post-eval)** | A `README.md` explaining approach + tradeoffs; a bot that stays live during evaluation |

## 6. Functional requirements

### FR-1 — `GET /v1/healthz`
Return 200 with `{status: "ok", uptime_seconds, contexts_loaded: {category, merchant, customer, trigger counts}}`. Judge polls every 60s; **3 consecutive failures = disqualified for the slot**. Must be effectively free (no heavy work on this path).

### FR-2 — `GET /v1/metadata`
Return `{team_name, team_members[], model, approach, contact_email, version, submitted_at}`.

### FR-3 — `POST /v1/context`
- Body: `{scope ∈ {category, merchant, customer, trigger}, context_id, version, payload, delivered_at}`.
- **Idempotent** on `(scope, context_id, version)`: re-post of same/lower version → `{accepted: false, reason: "stale_version", current_version}` (HTTP 409 semantics per testing brief; the skeleton returns 200 with `accepted: false` — implement per brief: accept-no-op; see Open Question Q3).
- Higher version replaces prior **atomically**.
- 200: `{accepted: true, ack_id, stored_at}`.
- 400 on invalid scope/malformed body with `{accepted: false, reason, details}`.
- Payload cap 500 KB.

### FR-4 — `POST /v1/tick`
- Body: `{now, available_triggers[]}`.
- For each active, unsuppressed, unexpired trigger whose merchant (+customer if scoped) context is available: compose and emit an action.
- Response: `{actions: [...]}` where each action has `conversation_id` (new, unique — never reuse an existing one here), `merchant_id`, `customer_id`, `send_as`, `trigger_id`, `template_name`, `template_params[]`, `body`, `cta`, `suppression_key`, `rationale`.
- Hard caps: **≤ 20 actions/tick**, **< 30s wall clock** (simulator allows only 15s), empty `actions: []` is legal and preferred over spam ("Restraint is rewarded; spam is penalized").
- One action per `(merchant_id, conversation_id)` per tick.

### FR-5 — `POST /v1/reply`
- Body: `{conversation_id, merchant_id, customer_id, from_role, message, received_at, turn_number}`.
- Response is exactly one of:
  - `{action: "send", body, cta, rationale}`
  - `{action: "wait", wait_seconds, rationale}`
  - `{action: "end", rationale}`
- Must handle, at minimum: auto-reply detection (same/similar canned text repeatedly → detect within ≤ 2 turns, exit gracefully), explicit intent transitions ("ok let's do it" → action mode, never re-qualify), hostile/opt-out (apologize + `end`, honor STOP forever), off-topic/curveball questions (acknowledge, stay on mission or exit politely), multi-language switches mid-thread.

### FR-6 — `POST /v1/teardown` (optional per brief §11)
Wipe all in-memory state. Cheap to add; add it.

## 7. The composer contract (core product logic)

`compose(category, merchant, trigger, customer?) -> {body, cta, send_as, suppression_key, rationale}`

Hard invariants (from brief §5 + §11):

1. **Single primary CTA** — one binary YES/STOP *or* one open-ended ask; buried CTAs and multi-CTA messages are penalized. Pure-information triggers may have none (`cta: "none"`).
2. **No fabrication** — every number, date, offer, citation, competitor name must exist in the pushed contexts. Judge penalty: −2 per fabrication; hallucination is called out as a top failure mode.
3. **Specificity-first** — anchor on verifiable fact (number/date/headline/peer stat). Service+price beats % discount.
4. **Voice match** — category voice governs vocabulary, tone, and taboos (e.g., dentists: clinical peer, never "cure"/"guaranteed").
5. **Language match** — honor `identity.languages`; hi-en code-mix where preference says so.
6. **Anti-repetition** — never send a body sent before (same conversation, and ideally globally per merchant): −2 per repeat.
7. **WhatsApp rules** — first outbound to a recipient uses template shape (`template_name` + `template_params`); free-form only within an active 24h session.
8. **send_as** — `"vera"` for merchant-facing, `"merchant_on_behalf"` for customer-facing.
9. **Suppression** — never send an action whose `suppression_key` has already been fired; respect trigger `expires_at`.
10. **`rationale` is scored** — it must accurately describe the actual decision (judge cross-checks rationale vs. output).

## 8. Trigger taxonomy to support

| Family | Kinds | Scope |
|---|---|---|
| External | `festival_upcoming`, `weather_heatwave`, `local_news_event`, `category_research_digest_release` / `research_digest`, `regulation_change`, `competitor_opened`, `category_trend_movement` | merchant |
| Internal | `perf_spike`, `perf_dip`, `milestone_reached`, `dormant_with_vera`, `review_theme_emerged`, `scheduled_recurring` / `curious_ask_due`, `recall_due` (cust), `customer_lapsed_soft/hard` (cust), `appointment_tomorrow` (cust), `unplanned_slot_open` (cust) | merchant / customer |

Unknown `kind` values must degrade gracefully (generic-but-grounded handler), because the real harness injects *new* triggers.

## 9. Scoring rubric (what we optimize)

| Dimension (0–10) | What maximizes it |
|---|---|
| Decision quality | Correct signal selection: pick the best trigger for this merchant *now*; skip when nothing is worth saying |
| Specificity | Real numbers, ₹ prices, dates, locality, source citations — all from context |
| Category fit | Voice/vocab/tone match the vertical; taboos avoided |
| Merchant fit | Merchant's own metrics/offers/history/language; never generic |
| Engagement compulsion | ≥1 lever: loss aversion, curiosity, social proof, effort externalization, reciprocity, asking-the-merchant; ends with the CTA |

Penalties: fabrication −2, internal jargon leaked to merchant −1, repeated body −2, malformed −2, timeouts −1 each, healthz failures −10.

## 10. Non-functional requirements

| Requirement | Budget |
|---|---|
| Tick/reply latency | < 15s worst case (real harness allows 30s; simulator client allows 15s) — target < 2s |
| Throughput | ≥ 10 req/s sustained from judge |
| Determinism | Same contexts + same request ⇒ identical response body (no RNG, no wall-clock in composition except the tick's `now`, no unordered-iteration leaks) |
| Statefulness | Contexts + conversations persist in-process for the whole test window; no restarts mid-test |
| Deployability | Single process, public URL, `http://` acceptable locally, `https://` for submission |
| Privacy | No context data sent anywhere except (optionally) an LLM API; teardown wipes state |

## 11. Deliverables

1. Public bot URL exposing all endpoints.
2. `README.md` (≤ 1 page): approach, model choice, tradeoffs, what extra context would help.
3. Repo with source + these docs.
4. Submission via the challenge portal (manual step, owner = participant).

## 12. Manual vs. agent responsibilities (operating agreement)

| Owner | Tasks |
|---|---|
| **Participant (manual)** | Downloading packs, providing any API keys (if an LLM is used), running `judge_simulator.py` locally, choosing/paying for hosting, deploying, keeping the bot alive, portal submission, authorship verification |
| **Coding agent (this repo)** | All design docs, all source code, unit tests, local self-test harness/scripts, README draft, debugging from simulator logs |

## 13. Open questions (tracked in `reference.md`)

- **Q1**: LLM-assisted composition vs. fully deterministic rules engine? (Current decision: deterministic rules-first; see `architecture.md` ADR-001.)
- **Q2**: Hosting target for the public URL?
- **Q3**: Context version conflict: return HTTP 200 with `accepted:false` (skeleton style) or HTTP 409 (brief §2.1 style)? Default: 409 per brief, but confirm against `examples/api-call-examples.md` before coding FR-3.
- **Q4**: Identifier for `decision_quality` dimension labelled `trigger_relevance` in some judge prompts — treat as the same dimension (simulator maps both).

## 14. Definition of done (whole project)

- [ ] All G1–G6 goals met locally with green simulator output.
- [ ] Public URL live and stable; metadata filled with real team info.
- [ ] README.md written.
- [ ] Submitted on portal; bot kept live for the evaluation window.
