# Memory.md — Running Checklist & Temporary Info

**Status**: Living document — update after every coding session.
**Purpose**: Track what's been done, what's pending, open decisions, simulator quirks, and transient data so (a) I stay consistent across sessions and (b) any downstream coding agent can pick up context instantly.

---

## 1. Session Log (today: 2026-08-30)

| Item | Details | Status |
|---|---|---|
| Pack extracted | `magicpin-ai-challenge.zip` → `dataset/`, `examples/` | ✅ Done |
| Source files read | `challenge-brief.md` (544 lines), `challenge-testing-brief.md` (557 lines), `engagement-design.md` (325 lines), `engagement-research.md` (198 lines), `judge_simulator.py` (962 lines) | ✅ Done |
| Dataset seeds inspected | `merchants_seed.json` (10 seeds, 314 lines first chunk), `customers_seed.json` (15 seeds), `triggers_seed.json` (25 seeds), 5 category JSONs (dentists, salons, restaurants, gyms, pharmacies) | ✅ Done |
| Judge simulator quirks noted | - Auto-reply test: sends 4 turns with **fresh** `conversation_id` each turn (`conv_auto_1`, `conv_auto_2`, …) — detection must be merchant-level, not conversation-level<br>- Intent test: merchant message `"Ok lets do it. Whats next?"`; checks body for actioning words `["done","sending","draft","here","confirm","proceed","next"]` AND absence of qualifying words `["would you","do you","can you tell","what if","how about"]`<br>- Hostile test: expects `action: "end"` OR `action: "send"` body containing `["sorry","apolog","won't"]`<br>- Scoring: 5 dims 0-10, penalties −2 (fabrication) / −1 (internal jargon leaked)<br>- Timeouts: healthz 5s, metadata 5s, context 10s, tick 15s, reply 15s<br>- Dataset loader reads seeds (not expanded); `generate_dataset.py` expands to 50 merchants / 200 customers / 100 triggers / 30 test pairs<br>- `vocab_taboo` field in category voice (simulator) vs brief's `taboos` — treat as same concept | ✅ Done |
| Doc drafts written | PRD.md, architecture.md, rules.md, phases.md, plan.md | ✅ Done |
| Open questions (track in reference.md) | Q1: LLM-assisted compose vs deterministic rules-first? (Current ADR-001: deterministic rules-first, optional temp=0 polish behind budget guard)<br>Q2: Context version conflict handling — 409 per brief §2.1 or 200 with `accepted:false` (skeleton style)?<br>Q3: Hosting choice for public URL?<br>Q4: `vocab_taboo` vs `taboos` naming mismatch — document as known divergence | ✅ Logged |
| Next coding step | Implement `main.py` FastAPI skeleton + 5 endpoint models | ⏳ Upcoming |

---

## 2. Key Decisions (ADRs referenced in architecture.md)

| ADR | Decision | Rationale | Last touched |
|---|---|---|---|
| ADR-001 | Deterministic rules-first composer (no LLM inside bot v1) | Guarantees < 2s tick latency, byte-identical outputs, avoids −2 hallucination penalty; LLM only optional polish behind strict token budget | 2026-08-30 |
| ADR-002 | In-memory store only | Spec allows; no DB complexity; satisfies determinism; warmup populates all 255 base contexts | 2026-08-30 |
| ADR-003 | Single CTA per message | Clarity for merchant; scoring penalty −2 for multiple; anti-repetition easier | 2026-08-30 |
| ADR-004 | Context versioning: higher replaces, same/lower no-op | Per testing brief §2.1; atomically prevents stale data | 2026-08-30 |
| ADR-007 | Auto-reply detection within 2 turns | Matches production Vera pain; aligns with judge_simulator `_auto_reply` test | 2026-08-30 |

---

## 3. Open Decisions (not yet resolved; awaiting implementation or participant input)

| ID | Question | Options | Default / Note |
|---|---|---|---|
| Q1 | LLM-assisted composition vs fully deterministic rules engine? | A) Fully deterministic rules-first (v1); optional temp=0 LLM polish later<br>B) LLM-powered compose from day 1 (higher token cost, risk −2 hallucination) | ADR-001 favors A; but judge uses LLM to score — bot should be LLM-agnostic |
| Q2 | Context version conflict: return 409 (brief) or 200 no-op (skeleton)? | A) HTTP 409 with `{accepted: false, reason: "stale_version", current_version}`<br>B) HTTP 200 with `{accepted: true}` but no-op (don't store) | Brief §2.1 seems A; but skeleton code returns B; need to pick one before coding FR-3 |
| Q3 | Public URL hosting target? | A) Railway (easiest free tier)<br>B) Render<br>C) Fly.io<br>D) ngrok tunnel to localhost (for local testing only) | Participant choice; docs will reflect whichever is chosen |

---

## 4. Code Progress Tracker (populate as we code)

| File | Purpose | Lines | Last updated | Completion % |
|---|---|---|---|---|
| `main.py` | FastAPI app + 5 endpoints | — | — | 0% |
| `store.py` | ContextStore (version-aware dict) | — | — | 0% |
| `composer/engine.py` | compose() pipeline | — | — | 0% |
| `conversation/handler.py` | /v1/reply handler (auto-reply, intent, hostile) | — | — | 0% |
| `guardrails.py` | Anti-repetition, CTA placement, template scaffolding | — | — | 0% |
| `tests/test_compose.py` | Unit tests for compose engine | — | — | 0% |
| `tests/test_converse.py` | Unit tests for conversation handler | — | — | 0% |

---

## 5. Simulator State (after each `judge_simulator.py` run)

| Metric | Value | Notes |
|---|---|---|
| Avg Specificity | /10 | Fill after each run |
| Avg Category Fit | /10 | Fill after each run |
| Avg Merchant Fit | /10 | Fill after each run |
| Avg Decision Quality | /10 | Fill after each run |
| Avg Engagement | /10 | Fill after each run |
| Total avg /50 | /50 | Fill after each run |
| Pass/fail per scenario | warmup / phase2_short / auto_reply_hell / intent / hostile | Fill after each run |

---

## 6. daily Coding Session Checklist (update before/after each session)

[ ] Pull latest docs from repo (memory.md, reference.md, all .md files)
[ ] Run `judge_simulator.py` (if code is at point where bot is running)
[ ] Note any new simulator quirks or failure modes in memory.md
[ ] Update progress tracker in memory.md (completion %)
[ ] Add any new open questions to the "Open Decisions" section
[ ] Update reference.md checklist (see §7)
[ ] Before stopping: record what will be done next session (so you can resume immediately)

---

## 7. reference.md Checklist (to be created / maintained separately)

[ ] Documentation complete: all 11 docs written ✅ (done in this chat)
[ ] Python env set + challenge pack extracted
[ ] main.py skeleton + 5 endpoints functional
[ ] ContextStore version-aware push working
[ ] compose() deterministic engine implemented
[ ] TriggerRouter with 3+ registered strategies
[ ] SlotExtractor + VoiceRenderer + CTA Policy
[ ] ConversationHandler: auto-reply + intent + hostile
[ ] Guardrails + anti-repetition active
[ ] judge_simulator.py full_evaluation: avg total ≥ 35/50
[ ] All 3 scenario tests: auto_reply_hell, intent_transition, hostile → PASS
[ ] Determinism: 2 runs same inputs → byte-identical body
[ ] Public URL live + metadata filled + README submitted
[ ] Bot kept live for evaluation window

*(toggle each line as you complete the corresponding task)*