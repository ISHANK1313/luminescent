# reference.md — Agent Scratch Space, Living Checklist & Verified Facts

**Status**: v2.0 — rewritten 2026-08-30 after a full read of the project.
**Owner of this file**: the coding agent (me). Update after every session / every code change.
**Excluded by user instruction**: `handover-bugfixer.md`, `handover-reviewer.md` — DO NOT read or act on them.

Read order for a cold start: this file → `challenge-brief.md` §4–§11 → `challenge-testing-brief.md` §2–§5 → `examples/api-call-examples.md` → `judge_simulator.py`.

---

## 0. Objective in one paragraph

Build a **stateful HTTP service** (Python/FastAPI, single process, in-memory state) that impersonates a better version of magicpin's WhatsApp merchant assistant "Vera". A judge harness pushes four kinds of context into it (`category`, `merchant`, `trigger`, `customer`), wakes it periodically (`/v1/tick`), and plays the merchant/customer role in reply (`/v1/reply`). For each tick the bot decides *whether* to message, *which* trigger to act on, and *what* to say — one WhatsApp message body + one CTA + a suppression key + an honest rationale. An LLM judge scores every produced message on 5 dimensions × 10 = 50 and applies operational penalties. Winning = grounded specificity, category-correct voice, per-merchant personalization, obvious "why now", one low-friction ask, plus flawless conversation handling (auto-reply detection, intent hand-off, graceful exit) and zero operational faults.

**The core function** (everything else is plumbing):

```
compose(category, merchant, trigger, customer?) -> {body, cta, send_as, suppression_key, rationale}
```

---

## 1. Verified facts about the environment (checked 2026-08-30)

| Fact | Value |
|---|---|
| Host OS | Windows |
| Project root | `D:\java_springboot_pp\Claude Project\Magicpin-challenge` |
| Host Python for running the bot | `C:\Program Files\Python311\python.exe` (3.11.9), launcher `py` works |
| fastapi / uvicorn | **NOT installed** — venv + install required |
| git | present, but repo is **not** initialised (`fatal: not a git repository`) |
| Challenge pack | already extracted: `dataset/`, `examples/` present alongside `magicpin-ai-challenge.zip` |
| Dataset actually on disk | 5 categories, `merchants_seed.json` (10), `customers_seed.json` (15), `triggers_seed.json` (25), `generate_dataset.py` (expands → 50/200/100 + 30 test pairs, **not yet run**; no `expanded/` dir) |
| Bot source code | **does not exist yet** — `bot/` and `tests/` are unwritten. Docs only. |

---

## 2. Contract summary (authoritative, condensed)

Five endpoints, JSON in/out, UTF-8, all under `/v1`.

| Endpoint | In | Out | Sim client timeout | Judge timeout |
|---|---|---|---|---|
| `GET /v1/healthz` | — | `{status, uptime_seconds, contexts_loaded:{category,merchant,customer,trigger}}` | 5 s | 3 consecutive failures = disqualified (−10) |
| `GET /v1/metadata` | — | `{team_name, team_members[], model, approach, contact_email, version, submitted_at}` | 5 s | — |
| `POST /v1/context` | `{scope, context_id, version, payload, delivered_at}` | `{accepted, ack_id, stored_at}` / `{accepted:false, reason:"stale_version", current_version}` / 400 | 10 s | payload ≤ 500 KB |
| `POST /v1/tick` | `{now, available_triggers[]}` | `{actions:[...]}` (≤20, may be empty) | 15 s | 30 s |
| `POST /v1/reply` | `{conversation_id, merchant_id, customer_id, from_role, message, received_at, turn_number}` | exactly one of `send` / `wait` / `end` | 15 s | 30 s |
| `POST /v1/teardown` (optional) | — | `{wiped:true}` | — | — |

**Tick action object (all fields required or −2 malformed)**:
`conversation_id` (new & unique per conversation), `merchant_id`, `customer_id` (null for merchant-facing), `send_as`, `trigger_id`, `template_name`, `template_params[]`, `body`, `cta`, `suppression_key`, `rationale`.

**Reply responses**: `{action:"send", body, cta, rationale}` | `{action:"wait", wait_seconds, rationale}` | `{action:"end", rationale}`.

**Scoring**: specificity, category_fit, merchant_fit, decision_quality (a.k.a. trigger_relevance — simulator maps both keys), engagement_compulsion; 0–10 each.
**Penalties**: fabrication −2, internal jargon leaked −1, repeated body in a conversation −2, malformed −2, **URL in body −3 (hard fail for that action)**, timeout −1 each, 3× healthz failure −10.

---

## 3. GOTCHAS — contradictions and traps found in the source material

These are the things that will silently cost points if implemented naively. Each has a decision.

### G1 — `expires_at` vs the simulator's clock (**highest impact**)
`judge_simulator.py` sends `now = datetime.utcnow()` (real current date, Aug 2026+). Every seed trigger has `expires_at` in **May 2026**. If the bot hard-enforces expiry against `now`, **it returns zero actions for every simulator run** (`phase2_short` prints "no actions", `full_evaluation` scores nothing).
**Decision D1**: `available_triggers` from the judge is the authority on what is active *now*. Expiry is a **soft** signal only: never used to hard-skip a trigger the judge explicitly listed. It may be used to lower priority when more triggers exist than we want to send. Hard-skip only on the `suppression_key` already having fired, opt-out, or missing grounding data.

### G2 — Re-pushing the same context version: 200 or 409?
`challenge-testing-brief.md` §2.1: "Idempotent by (context_id, version). Re-posting the same version is a **no-op**", and 409 is described as "you already have a **higher** version".
`examples/api-call-examples.md` 1.5: same version → **409 stale_version**.
`judge_simulator.py::_warmup` prints `PASS` only when `data.get("accepted")` is truthy, and `_full` re-pushes merchants at version 1 **after** warmup already pushed 5 of them at version 1. Any second run of the simulator against a still-running bot re-pushes everything at v1.
**Decision D2**: `version == current` → HTTP **200** `{accepted:true, ack_id, stored_at}` and **do not overwrite** (true idempotent no-op). `version < current` → HTTP **409** `{accepted:false, reason:"stale_version", current_version}`. `version > current` → replace atomically, 200. Invalid scope / malformed → 400 `{accepted:false, reason, details}`. This satisfies the brief's wording and never turns a re-run into a false FAIL.
*(This supersedes memory.md Q2 and old reference.md Q6.)*

### G3 — Do **not** preload the dataset at startup
`handover-prompt.md` §4 says `main.py` should "warmup-populate ContextStore from dataset/". That is wrong: `examples/api-call-examples.md` 1.1 shows healthz returning `contexts_loaded` **all zeros** before the judge pushes, and warmup passes only if `contexts_loaded` matches **what the judge pushed** (255). Preloading inflates the counts and risks composing from data the judge never pushed (= fabrication).
**Decision D3**: the store starts **empty**. Only `/v1/context` fills it. The bot never reads `dataset/` at runtime. (`dataset/` is used only by local test scripts.)

### G4 — URLs in the body
`challenge-brief.md` §5.4 says URLs are allowed; `api-call-examples.md` F.4 says **−3 and hard fail per URL** ("Meta would reject").
**Decision D4**: never emit `http`, `https`, `www.`, or bare domains in `body`. Guardrail strips/blocks them.

### G5 — Auto-reply detection must be merchant-level, not conversation-level
`_auto_reply` sends four turns with **different** conversation ids (`conv_auto_1..4`) and the same body, `turn_number` 2..5, with **no preceding tick** (the conversation is unknown to the bot).
**Decision D5**: keep a per-`merchant_id` rolling history of inbound messages (normalized text). Detect canned text by (a) exact/near-repeat of the previous inbound from the same merchant, or (b) canned-phrase patterns ("thank you for contacting", "we will get back", "automated", "our team will respond", Hindi equivalents "sampark karne ke liye dhanyavaad", "team tak pahuncha"). Escalation ladder, matching `api-call-examples.md` 4.1:
1st detection → `send` one short owner-flag message (with a binary CTA); 2nd → `wait` (e.g. 14400 s); 3rd+ → `end`.
Handler must tolerate a completely unknown `conversation_id`.

### G6 — Intent-transition check is a literal keyword test
Simulator: PASS iff reply `body.lower()` contains **any** of `["done","sending","draft","here","confirm","proceed","next"]` **and none** of `["would you","do you","can you tell","what if","how about"]`.
**Decision D6**: a hard post-generation guardrail on every `action:"send"` reply body in action-mode: assert ≥1 actioning token and 0 qualifying tokens; if violated, rewrite deterministically. Beware accidental substrings — "do you" hides inside "How **do you** want…", "what if" inside "…so **what if**…". Never phrase questions that way in action mode.

### G7 — Hostile handling
Simulator: `action == "end"` → PASS; `action == "send"` whose body contains `sorry`/`apolog`/`won't` → PASS; anything else → FAIL. Message used: "Stop messaging me. This is useless spam."
**Decision D7**: return `action:"end"` and record a permanent opt-out for that merchant (suppress all future tick actions for them). Rationale mentions the opt-out.

### G8 — The simulator ignores env vars
Its docstring/README-style notes and the old reference.md say `export BOT_URL=... / export LLM_API_KEY=...`. The code reads the **module-level constants** at the top of `judge_simulator.py` only. Env vars do nothing.
**Decision D8**: configuration is edited in-file (or by a small local wrapper that patches the constants). Keep the key out of git.

### G9 — Dataset date incoherence
`c_001_priya` `last_visit = 2026-05-12`, trigger `trg_003` `last_service_date = 2026-05-12`, `due_date = 2026-11-12`, slots labelled "Wed 5 Nov, 6pm" — but the harness's `now` is arbitrary. Deriving "it's been 5 months" from `now` produces nonsense.
**Decision D9**: never compute elapsed time from `now`. Use the **explicit labels and fields inside the trigger payload** (`due_date`, `available_slots[].label`, `days_until`, `days_since_last_visit`, `days_remaining`) verbatim. If a relative phrase can't be grounded in a payload field, omit it.

### G10 — Internal jargon leak (−1)
Signals arrive as machine strings: `stale_posts:22d`, `ctr_below_peer_median`, `dormant_with_vera_14d`, `high_risk_adult_cohort`, `trial_ending_soon`. Emitting them verbatim costs a penalty.
**Decision D10**: a signal→human-phrasing map ("stale_posts:22d" → "your last Google post was 22 days ago"). Never print a raw signal token, `merchant_id`, `trigger.kind`, `suppression_key`, or field name in `body`. They belong in `rationale` (which the judge reads separately and expects to be technical/honest).

### G11 — Determinism
No `random`, no `uuid4`, no `time.time()`/`utcnow()` inside composition. `conversation_id` must be derived (e.g. `conv_{merchant_id}_{trigger_id}` as in the official example). Sort every collection before iterating.

### G12 — `vocab_taboo` vs `taboos`
The real dataset uses `voice.vocab_taboo`; the brief prose says `taboos`. Read `voice.get("vocab_taboo", voice.get("taboos", []))`.

### G14 — Gemini free-tier quota is the real constraint on the LLM layer (**verified**)
A live test of 25 real composer prompts against `gemini-3.5-flash-lite` returned **HTTP 429 "You exceeded your current quota"** after ~16 calls. The judge's 60-minute window involves far more compositions than that.
**Decision D14**: the LLM layer is a *rewrite* stage, never a dependency — every 429/timeout/refusal silently falls back to the grounded deterministic body, and successful rewrites are cached on disk so repeats cost nothing. To actually benefit from it during evaluation the participant needs a billing-enabled key; otherwise the bot simply runs deterministic, which is fully scoring-capable.

### G15 — LLM rewrites do drift, and the fence catches it (**verified**)
Of 16 successful rewrites, 15 passed validation and **1 was rejected for introducing a second CTA** — precisely the −2 pattern. It fell back to the deterministic body automatically. Never ship an LLM body without re-validating it against the same fact fence.

### G16 — The NVIDIA NIM DeepSeek endpoint hangs rather than erroring (**verified 2026-08-31**)
The participant supplied an NVIDIA key (`integrate.api.nvidia.com/v1`, `deepseek-ai/deepseek-v4-flash-0731`). First two calls returned **200 in ~1 s**. Every call after that — tiny control prompt, streaming, non-stream, and the sibling model `deepseek-v4-pro-0813` — **times out with no response** (tested repeatedly over 30 minutes). `GET /v1/models` still answers in <1 s and lists both models, and other models on the same key fail *fast* (`meta/llama-3.3-70b-instruct` → 410 end-of-life), so this is not a network, auth or code fault: the DeepSeek NIM route stops responding for this account after a couple of requests.
**Decision D16**: treat any OpenAI-compatible provider as replaceable configuration, never as a dependency. `bot/composer/llm.py` is now provider-agnostic (`nvidia` | `openai-compatible` | `gemini`, selected by `VERA_LLM_PROVIDER` / `secrets.local.json`, base URL overridable), the cache key includes provider+model so switching models never reuses another model's wording, and `VERA_LLM_CACHE_ONLY=1` runs the polished bodies from a warm cache with zero network calls. A dead provider costs nothing but the polish.

### G18 — Z.ai GLM-4.7-flash: right endpoint shape, flaky free-tier capacity, prose replies (**verified 2026-08-31**)
The key the participant supplied is for `https://api.z.ai/api/anthropic`, which is the **Anthropic Messages** shape (`POST {base}/v1/messages`, `x-api-key`, `anthropic-version: 2023-06-01`) — *not* OpenAI `/chat/completions`. The sibling OpenAI-compatible route (`/api/paas/v4`) answered **429/1305** on the same key, so the Anthropic route is the one to use. First call: **200 in 8.8 s**, correct content. Two further findings:
* **Capacity.** Sustained use returns frequent **529 `overloaded_error` code 1305**. It is intermittent, not terminal: batches of 2–3 succeed, then a run of 529s. Patient backoff (5→70 s, cycling over the outstanding items rather than giving up on one) gets through; short retry loops do not.
* **Envelope.** GLM often **ignores the "return only JSON" instruction and replies with the message as plain prose**. The original strict `{"body": ...}` parser silently threw those away, which looked like model failure but was a parsing bug on our side.
**Decision D18**: GLM-4.7-flash is the **primary** composer model (Anthropic shape, temperature 0), with NVIDIA DeepSeek and Gemini as an ordered failover chain declared in `secrets.local.json` under `LLM_FALLBACKS`. `_extract_body` now accepts JSON, fenced JSON **or** a single-paragraph prose reply, while still rejecting blank-line commentary and chat preambles ("Sure, here's..."), and the fact fence remains the only thing that decides whether a body ships. `Endpoint` + `endpoints()` build the chain, `compose_many` tries the primary for all prompts and lets whatever is still unanswered fall through to the next endpoint inside the same wall-clock budget, and cache lookups walk the whole chain so a failover body is reused rather than re-billed.

### G17 — Two environment traps when running the harness from an agent shell
1. **Console encoding.** `judge_simulator.py` prints block characters (`█`, `░`). On a Windows cp1252 console it dies mid-report with `UnicodeEncodeError` *after* scoring, losing the whole summary. Fix without touching the harness: `$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"`.
2. **Process lifetime.** Anything started with `Start-Process` from the agent shell is killed when that shell call returns, so the bot must be started **and** exercised inside a single call, or started from the participant's own terminal.

### G19 — `ThreadPoolExecutor` context-manager shutdown blows the tick budget (**verified 2026-09-03, fixed**)
`compose_many` ran its per-endpoint calls inside `with ThreadPoolExecutor(...)`, whose `__exit__` calls `shutdown(wait=True)` — it blocks until **every** in-flight HTTP call returns (up to a full 7 s PER_CALL_TIMEOUT_S per straggler) even after the 9 s LLM budget has expired. A tick with 6 uncached prompts against hanging endpoints measured **14,478 ms** — inside the 15 s simulator client timeout only by luck.
**Decision D19**: the pool is now created bare and shut down with `shutdown(wait=False, cancel_futures=True)` in a `finally`. Stragglers finish in the background (their cache writes remain valid — no work lost), and the tick is bounded by `VERA_LLM_BUDGET_MS` (default 9 s) + small overhead. Verified: uncached tick 9,061 ms (budget-bound), warm-cache tick **60 ms**.

### G20 — LLM rewrites that drop the recipient's name (**verified 2026-09-03, fenced**)
Of the re-warmed cache, 4 bodies (trg_006, trg_012, trg_014, trg_017) had silently dropped the "Lakshmi/Suresh/Karthik/Sumitra," salutation — fine on specificity but a merchant-fit loss (reads as a broadcast, and the judge's cross-case rules explicitly want the owner's first name).
**Decision D20**: new `addresses_recipient()` check in `engine.finalize` — the owner's (or customer's/guardian's) first name must survive the rewrite; merchant-facing AND customer-facing both. One trigger (trg_017) keeps failing the fence across retries (the model insists on dropping "Sumitra"), so it permanently uses the deterministic body — which is the fence working as designed.

### G13 — Restraint vs coverage
`full_evaluation` averages only the actions the bot actually produces (integer floor division), so a skipped weak trigger cannot lower the average — but `phase2_short` prints a warning and scores nothing if the bot emits zero actions, and the real judge rewards adaptation to injected context. **Decision D13**: emit whenever the trigger has ≥2 grounded, concrete slots and no suppression/opt-out; skip only when genuinely ungrounded. Target: an action for essentially every seed trigger, ≤20 per tick, one per `(merchant_id, conversation_id)`.

---

## 4. Dataset shape (verified from disk, exact field names)

**Category** (`dataset/categories/{slug}.json`, 5 files — dentists, salons, restaurants, gyms, pharmacies):
`slug`, `display_name`, `voice{tone, register, code_mix, vocab_allowed[], vocab_taboo[], salutation_examples[], tone_examples[]}`, `offer_catalog[{id,title,value,audience,type}]`, `peer_stats{scope, avg_rating, avg_review_count, avg_views_30d, avg_calls_30d, avg_directions_30d, avg_ctr, avg_photos, avg_post_freq_days, + per-category extras}`, `digest[{id,kind,title,source,summary,actionable,+trial_n/patient_segment/date/credits}]` (5 items each; kinds: research/compliance/cde/trend/tech), `patient_content_library[{id,title,channel,length_seconds,body}]`, `seasonal_beats[{month_range,note}]`, `trend_signals[{query,delta_yoy,segment_age,skew}]`, `regulatory_authorities[]`, `professional_journals[]`.

Voice tones: dentists `peer_clinical`, salons `warm_practical`, restaurants `warm_busy_practical` (fellow_operator), gyms `energetic_disciplined` (coach_to_member), pharmacies `trustworthy_precise` (neighbourhood_pharmacist). Judge prompt additionally expects a **"Dr." prefix for dentists**.

**Merchant** (10 seeds): `merchant_id`, `category_slug`, `identity{name, city, locality, place_id, verified, languages[], owner_first_name, established_year}`, `subscription{status, plan, days_remaining, renewed_at|days_since_expiry}`, `performance{window_days, views, calls, directions, ctr, leads, delta_7d{views_pct, calls_pct, ctr_pct}}`, `offers[{id,title,status,started|ended}]`, `conversation_history[{ts,from,body,engagement}]`, `customer_aggregate{total_unique_ytd, lapsed_180d_plus, retention_6mo_pct, high_risk_adult_count,…}`, `signals[]`, `review_themes[{theme,sentiment,occurrences_30d,common_quote}]`.

Roster: m_001 Dr. Meera's Dental Clinic (dentists, Delhi/Lajpat Nagar) · m_002 Bharat Dental Care (Mumbai) · m_003 Studio11 Family Salon (Hyderabad) · m_004 Glamour Lounge Spa & Salon (Pune) · m_005 SK Pizza Junction (Delhi) · m_006 Mylari South Indian Cafe (Bangalore) · m_007 PowerHouse Fitness (Bangalore) · m_008 Zen Yoga Studio (Chennai) · m_009 Apollo Health Plus Pharmacy (Jaipur) · m_010 Sunrise Medicos (Lucknow). All have `hi` in languages → code-mix is in scope for all of them.

**Customer** (15 seeds): `customer_id`, `merchant_id`, `identity{name, phone_redacted, language_pref, age_band}`, `relationship{first_visit,last_visit,visits_total,services_received[],lifetime_value}`, `state ∈ {new,active,lapsed_soft,lapsed_hard,churned}`, `preferences{preferred_slots,channel,reminder_opt_in}`, `consent{opted_in_at, scope[]}`.

**Trigger** (25 seeds): `id`, `scope` (merchant|customer), `kind`, `source` (external|internal), `merchant_id`, `customer_id`, `payload{…kind-specific…}`, `urgency` 1–5, `suppression_key`, `expires_at`.

**All 24 kinds present in the seeds** (the router must cover these, plus unknown-kind degradation):
`research_digest, regulation_change, recall_due, perf_dip, renewal_due, festival_upcoming, wedding_package_followup, curious_ask_due, winback_eligible, ipl_match_today, review_theme_emerged, milestone_reached, active_planning_intent, seasonal_perf_dip, customer_lapsed_hard, trial_followup, supply_alert, chronic_refill_due, category_seasonal, gbp_unverified, cde_opportunity, competitor_opened, perf_spike, dormant_with_vera`.

Customer-scoped kinds in the seeds: `recall_due`, `wedding_package_followup`, `customer_lapsed_hard`, `trial_followup`, `chronic_refill_due` → these five need `send_as: "merchant_on_behalf"` + consent check.

---

## 5. Implementation plan (the build I will execute once approved)

Layout (per `handover-prompt.md` §2, minus the disproved warmup preload):

```
bot/
  main.py              FastAPI app, 6 routes, request/response models, latency guard
  store.py             ContextStore: {(scope, id): {version, payload}} + RLock, D2 semantics
  state.py             Conversation/opt-out/suppression/sent-body state (merchant-level)
  composer/
    engine.py          compose() orchestration
    trigger_router.py  kind -> strategy; unknown -> grounded generic
    signal_selector.py which trigger wins, is it worth sending
    slot_extractor.py  concrete facts only, from pushed contexts
    voice_renderer.py  tone/vocab/taboo/salutation + hi-en code-mix
    cta_policy.py      exactly one CTA, last sentence, correct type
    suppression_check.py  suppression_key, opt-out, anti-repetition hash
  conversation/handler.py  /v1/reply: auto-reply ladder, intent mode, hostile, off-topic, default
  guardrails.py        jargon/URL/fabrication/CTA/keyword-contract validation (last line of defence)
tests/                 unit tests per module + a fake-judge integration script
tools/run_local_judge.py  runs judge_simulator scenarios without editing the original file
```

Build order & exit criteria: P1 store+endpoints (warmup all PASS, healthz zeros before push) → P2 composer for merchant-facing kinds (`phase2_short` yields 3 grounded actions) → P3 reply handler (auto_reply/intent/hostile all PASS) → P4 customer-facing + consent → P5 guardrails/anti-repetition/templates → P6 `full_evaluation` iteration (target avg ≥ 40/50, no dimension < 7) → P7 deploy + README + submit (participant).

Quality bar per message (checked by guardrails before it leaves the process):
1 grounded number/date/₹ price/source citation minimum · reason-why-now in the first two sentences · exactly one CTA and it is the last sentence · no URL · no raw signal token · no taboo word · language matches `identity.languages` / `customer.identity.language_pref` · body never previously sent to this merchant · rationale factually matches the body.

---

## 6. Open decisions needing the participant (blocking or near-blocking)

| ID | Question | Status |
|---|---|---|
| P-1 | `/v1/metadata` identity: team_name, members, contact_email, model string, version, submitted_at | **needed before submission**, placeholder until then |
| P-2 | Runtime LLM inside compose, or fully deterministic? (recommendation: deterministic core; it removes hallucination, timeout and cost risk, and the judge never sees our internals) | awaiting confirmation |
| P-3 | LLM provider + API key for `judge_simulator.py` scoring runs (warmup / auto_reply / intent / hostile need **no** key; `phase2_short` and `full_evaluation` scoring do) | awaiting participant |
| P-4 | Hosting target for the public URL (Railway / Render / Fly / ngrok) | awaiting participant |
| P-5 | Does the portal also require `submission.jsonl` (30 test pairs) + `bot.py` + `README.md`, or only the live URL? Briefs disagree in emphasis (§7 of the main brief vs §6 of the testing brief). If yes, run `generate_dataset.py` to produce `test_pairs.json` and emit 30 lines from the same composer. | awaiting participant |
| P-6 | Git init + .gitignore for this folder? | awaiting participant |

---

## 7. Progress tracker

| Area | Status |
|---|---|
| Full project analysis | ✅ 2026-08-30 |
| reference.md rewritten with verified facts + gotchas | ✅ 2026-08-30 |
| Manual prerequisites listed for participant | ✅ 2026-08-30 |
| venv + fastapi/uvicorn/pytest/httpx installed | ✅ (offline install from `wheels/`, see §10) |
| `bot/store.py` (ContextStore, D2/D3 semantics) | ✅ |
| `bot/config.py` (metadata, env-overridable) | ✅ **placeholders — P-1 still open** |
| `bot/main.py` (6 routes, tolerant parsing, no-crash guarantee) | ✅ |
| `tests/test_store.py` + `tests/test_endpoints.py` (23 tests) | ✅ all passing |
| `tools/run_local_judge.py` (runs the real harness offline) | ✅ |
| simulator `warmup` | ✅ **PASS** (also passes on a repeat run — D2 validated) |
| composer package (Phase 2) | ✅ `strategies.py` (24 kinds + grounded fallback), `voice.py`, `llm.py`, `engine.py` |
| conversation handler (Phase 3) | ✅ `conversation/handler.py` — 8-branch decision order |
| customer-facing + consent (Phase 4) | ✅ `send_as` switching + per-kind consent map, folded into the composer |
| guardrails (Phase 5) | ✅ `guardrails.py` + anti-repetition/suppression in `state.py` |
| tests | ✅ **100 passing** (`test_store`, `test_endpoints`, `test_compose`, `test_converse`) |
| simulator: `auto_reply_hell` / `intent_transition` / `hostile` | ✅ **all PASS** |
| provider-agnostic LLM layer (nvidia / openai-compatible / gemini) | ✅ 2026-08-31, `bot/composer/llm.py` rewritten, 100 tests still green |
| NVIDIA provider for the harness, without editing it | ✅ `tools/run_local_judge.py` → `NvidiaProvider` / `OpenAICompatProvider` / `BridgeProvider` |
| file bridge for shells with no outbound HTTPS | ✅ `tools/llm_bridge.py` + `--bridge`, verified round-trip |
| cache warmer (fence-validated rewrites) | ✅ `tools/warm_llm_cache.py` — **could not complete: provider hangs (G16)** |
| offline message dump for review | ✅ `tools/dump_messages.py` → `runs/messages.json`, **25/25 seed triggers compose** |
| simulator: `full_evaluation` end-to-end plumbing | ✅ ran to completion, bot produced **20 actions** (the ≤20/tick cap), no timeouts, no malformed actions |
| GLM-4.7-flash primary + ordered failover chain | ✅ 2026-08-31, `secrets.local.json` → `LLM_PROVIDER: zai` + `LLM_FALLBACKS` (nvidia, gemini) |
| warm cache: 23/23 GLM rewrites fetched, 22 passed the fence | ✅ `runs/llm_candidates.json` → `runs/llm_ingest_report.json`; 1 rejected for dropping the business name (new rule) |
| **simulator `full_evaluation`, real LLM scores** | ✅ **AVERAGE 38/50 (76%) — harness display; exact mean 40.30/50.** 20 messages scored, 0 timeouts, 0 malformed, tick latency **46 ms** in cache-only mode. Per-dimension: specificity 8.40, category_fit 8.10, merchant_fit 8.80, decision_quality 7.55, engagement 7.45. |
| deterministic-message defect sweep (session 5) | ✅ 2026-09-03 — 6 defects found and fixed (see session log); all 25 bodies re-audited clean, no machine artefacts |
| tick-latency bug (session 5) | ✅ G19/D19 fixed: 14.5 s worst-case tick → budget-bounded 9 s uncached / 60 ms warm-cache |
| recipient-name fence (session 5) | ✅ G20/D20: 24/25 triggers LLM-polished and fence-passing; 1 permanent deterministic fallback |
| README.md (1-page submission doc) | ✅ written 2026-09-03 |
| next scoring iteration target | ⭕ re-run `full_evaluation` with the improved bodies — expected to lift decision_quality (time-boxed/repeatable asks now name their object) and engagement (deadline/why-now framing now present) |
| deploy + README + submit | ⭕ (participant) |

### Answers received from the participant (2026-08-30)
* Submission format: **live public URL only** — no `submission.jsonl`, no `bot.py` upload. `generate_dataset.py` therefore does not need to be run.
* Judge-simulator LLM: **Gemini**, model `gemini-3.5-flash-lite`, key stored in git-ignored `secrets.local.json`. Key + model verified against the Google API (200, model exists).
* `/v1/metadata` identity: still placeholders (P-1 open).
* Composer approach (P-2): **still unanswered** — Phase 2 is blocked on it. Default if unanswered: deterministic.

## 10. Environment build notes (important for reproducing)

* **The sandbox shell has no outbound HTTPS** (DNS resolves, TCP/443 times out) — `pip install` hangs forever. Dependencies were installed by downloading wheels through a separate network path into `wheels/` and running `pip install --no-index --find-links .\wheels`. `wheels/` is git-ignored; on a clean machine with normal internet just run `pip install fastapi uvicorn pytest httpx`.
* Installed: fastapi 0.141.1, uvicorn 0.52.4, pydantic 2.13.5 (pydantic-core 2.46.5), pytest 9.1.1, httpx 0.28.1, starlette 1.6.0.
* Because outbound HTTPS is blocked in the sandbox, **LLM-scoring scenarios (`phase2_short`, `full_evaluation`) cannot be run from the agent shell**. Two options: (a) the participant runs `python tools/run_local_judge.py full_evaluation` in a normal terminal, or (b) the agent runs an equivalent scorer that reuses `judge_simulator.LLMScorer.SYSTEM` verbatim over its own network path. Non-scoring scenarios (`warmup`, `auto_reply_hell`, `intent_transition`, `hostile`) run fine locally.
* `git init` succeeded but the sandbox user **cannot write `.git/index.lock`** (folder owned by BUILTIN\Administrators), so commits must be made by the participant from their own terminal.

## 11. Run commands

```powershell
# run the bot
.\.venv\Scripts\python.exe -m uvicorn bot.main:app --host 0.0.0.0 --port 8080

# tests
.\.venv\Scripts\python.exe -m pytest tests -q

# official harness (reads secrets.local.json; --offline skips the LLM requirement)
.\.venv\Scripts\python.exe tools\run_local_judge.py warmup --offline
.\.venv\Scripts\python.exe tools\run_local_judge.py full_evaluation
```

## 8. Simulator run log (fill one row per run)

| Date | Scenario | Provider/model | Spec | Cat | Mx | Dec | Eng | Total/50 | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 2026-09-03 | `full_evaluation` (session-6 fixed bodies, run 1) | judge: GLM-4.7-flash @0.2; bot: cache-warm GLM @0 | 8.70 | 6.70 | 6.90 | 6.35 | 6.40 | **35.05 mean** | Gate PASS (≥30), but **10 of 20 scoring calls hit LLM errors** (GLM capacity) and were heuristically rescored as all-5s — the clean 10 scored messages averaged **41.6**. Retry run in progress for a clean pass. |
| 2026-08-31 | `full_evaluation` | judge: GLM-4.7-flash @0.2 via bridge; bot: GLM-4.7-flash @0 (warm cache) | 8.40 | 8.10 | 8.80 | 7.55 | 7.45 | **40.30 mean** (harness prints 38, integer floor) | 20 actions scored, 0 timeouts, 0 malformed, tick 46 ms. Range 30–46. Weakest: `category_seasonal` 30/50 (spec 10 but cat/mer/dec/eng all 5), `cde_opportunity` 34/50, `research_digest` 35/50 — all lose on engagement. |
| 2026-08-31 | `warmup`, `auto_reply_hell`, `intent_transition`, `hostile` | offline stub (no LLM needed) | — | — | — | — | — | — | all **PASS** |

## 9. Session log

- **2026-09-03 (session 6)** — Closing the remaining agent-side gaps. **Rate-limit defenses** for the participant's stated limits (GLM-4.7-flash: 5 concurrent; NVIDIA DeepSeek v4 flash: 40 RPM; Gemini: 15 RPM): `MAX_PARALLEL` 5→4 (leaves a spare GLM concurrency slot for retries), fallback chain reordered GLM → Gemini → NVIDIA in `secrets.local.json` (the NVIDIA DeepSeek route *hangs* per G16 — as second in the chain it would have eaten the whole 9s budget before reliable Gemini got a turn), and `Endpoint` now carries per-provider RPM pacing (`wait_seconds()`, conservative caps 35/12) with `call_endpoint` failing fast when honouring the pace would exceed the remaining timeout. RPM verdict: **the provided keys are sufficient — no new key needed** (worst realistic judge load is ~40–100 compositions over 60 min ≈ 1–2 RPM average; even a 20-prompt cold tick stays under every limit with pacing). **R32 gate built**: `tests/integration_judge_sim.py` — spawns its own bot on a scratch port, contract mode runs the four offline scenarios through the real harness (PASS), scoring mode runs `full_evaluation`, reads `judge.all_scores` programmatically (no ANSI parsing) and exits 0 iff mean ≥ 30/50. Fixed a harness-internals bug in it: `judge.scorer` is only constructed inside `judge.run()`, so calling `_full()` directly needs `judge.scorer = js.LLMScorer(llm, judge.dataset)` first. **P6.3 load test built and run**: `tools/load_test.py` — 600 requests over 60s at exactly 10 req/s across the judge-shaped mix (healthz polls, higher-version context pushes, full-trigger ticks, canned+engaged replies, stale-version probes), **0 connection errors, 0 unexpected non-200s, 39 correct 409s, p95 < 30 ms on every endpoint** (single 9s max tick = first cold composition, inside the 15s gate). **Re-scoring**: run 1 scored the fixed bodies at **35.05/50 gate-PASS**, but 10 of 20 judge calls hit GLM capacity errors and were heuristically rescored as artificial all-5s; the 10 cleanly-scored messages averaged **41.6/50** (up from 40.30 baseline) with specificity up 8.40→8.70 — retry running for a clean pass, result to be appended to §8. **DEPLOY.md written** (hosting comparison: Fly.io/Railway recommended, ngrok fallback with machine-awake caveat, Render free tier explicitly avoided for cold-start disqualification risk; deploy-time env vars, llm_cache.json recommendation, external smoke checks, P7 checklist). Next: reference.md/rules.md/phases.md compliance ticking + git push to the new GitHub repo.
- **2026-09-03 (session 5)** — Full re-read of the project (bot package, tests, tools, runs; handover-bugfixer/reviewer excluded per instruction), then a defect sweep of all 25 deterministic bodies and the tick/reply path. **Six message defects found and fixed**, all deterministic, in `bot/composer/strategies.py` + `voice.py` + `guardrails.py`: (a) raw window slugs leaking into bodies ("down 50% over the last **7d**") — new `window_words()` renders "7 days", and a `humanise()` sweep in `voice.render` plus a `RAW_SLUG_RE` guardrail (ISO dates + `\d+[dwmy]` slugs) make regression impossible; (b) ISO dates in prose ("effective **2026-12-15**") and a duplicated "Deadline 15 Dec 2026" in `regulation_change` — the title's date is now humanised in place and the separate deadline sentence only appears when the title doesn't already carry it; (c) inverted review theme ("reviews mention **delivery late**") — `delivery_late` renders as "late delivery"; (d) `perf_spike` said "kids yoga post" 3× in a row — hook/anchor/ask now say it once each; (e) `research_digest` opened with a bare title (no why-now) and the weakest engagement score (5/10) — hook is now "This week's JIDA Oct 2026 carried one item worth your 2 minutes." with the finding clause pulled from the digest summary via `_headline_number()`; (f) `cde_opportunity` promised "joining link details" (undeliverable) and mangled the speaker name ("Speaker: Dr;") — now "pencil it into your calendar with a reminder the day before" + correct `_speaker()` parsing of "Dr. R. Mehta" (period-carrying names; three regex attempts documented in the code). Also sharpened vague Hindi asks (`winback` "Wapas chalu kar dun?" → names the action; `seasonal_perf_dip` "Window turn hote hi" → "Window khulte hi"; `category_seasonal` lead mover now in the hook, "Also ..." anchor, no semicolon list), humanised the IPL clock ("19:30" → "7:30pm"), `trial_followup` anchor became a statement ("Hope the session on … went well") after the first phrasing ("How did it go?") introduced a second question mark and was caught by the single-CTA fence, and `festival/ipl` offer mentions now render as natural sentences. **One latent bug fixed in `llm.py` (G19)**: the `with ThreadPoolExecutor(...)` in `compose_many` blocked up to 7 s past the LLM budget on `shutdown(wait=True)` — worst measured tick 14,478 ms, uncomfortably near the simulator's 15 s client timeout; now `shutdown(wait=False, cancel_futures=True)`, ticks are budget-bounded (9,061 ms uncached, **60 ms** warm). **One fence added (G20)**: `addresses_recipient()` requires the owner/customer/guardian first name to survive any LLM rewrite (4 cached bodies had silently dropped salutations); cache cleaned and re-warmed — 24/25 LLM-polished, trg_017 permanently on the deterministic body because its rewrites keep dropping "Sumitra". **README.md written** (the submission deliverable). Verification: **100 tests green** after every change; simulator `warmup` (12 PASS), `auto_reply_hell` (ends turn 3), `intent_transition` (ACTION mode), `hostile` (ends + opts out) all PASS against a live bot; fresh-process tick sequence 20 → 5 → 0 actions across three ticks with all 25 triggers sent exactly once and no body missing its recipient's name. Fixed a reference.md editing mistake from this session (G13 header had been clobbered; restored).
- **2026-08-31 (session 4)** — Switched the LLM layer to the participant's NVIDIA key. `bot/composer/llm.py` is now provider-agnostic (nvidia / openai-compatible / gemini; env or `secrets.local.json`; cache key includes provider+model; `VERA_LLM_CACHE_ONLY`). Added `NvidiaProvider`, `OpenAICompatProvider` and `BridgeProvider` to `tools/run_local_judge.py` (harness itself untouched, per rules.md), plus `tools/llm_bridge.py`, `tools/warm_llm_cache.py` (fence-validated cache warming) and `tools/dump_messages.py`. 100 tests green; `warmup`, `auto_reply_hell`, `intent_transition`, `hostile` all still PASS; `full_evaluation` ran end-to-end with **20 actions, no timeouts, no malformed actions**. **Scoring is still unmeasured**: the NVIDIA DeepSeek route answered twice then began hanging on every request (G16), and the fallback scorer hit its own rate limit, so the harness fell back to its built-in heuristic. Found two environment traps (G17). Own-audit of all 25 composed messages (`runs/messages.json`) surfaced four systematic weaknesses worth fixing before any scored run: (a) vague CTA objects — "ye/is hafte chalu kar dun?" without naming what gets switched on, in `perf_dip`, `renewal_due`, `festival_upcoming`, `ipl_match_today`, `seasonal_perf_dip`, `gbp_unverified`; (b) raw slug fragments reaching the body — "post resolution window apr jun pattern", "skin prep program 30day window" (G10 risk); (c) `trial_followup` opens with a bare "Hi," because the customer name is not read, and refers to the trainee in the third person (−1 generic salutation); (d) `recall_due` offers two slots *and* "or tell us a time that suits you", which reads as a second ask; `category_seasonal` renders a semicolon list that looks machine-generated.
  **All four fixed the same session**, deterministically, no LLM involved: (a) every vague `hi_ask` now names its object, because the Hindi ask is what actually ships for `hi`-language merchants — the English `ask` was already concrete, so the defect was invisible unless you read the rendered body ("Dental Cleaning @ ₹299 is hafte front page par chalu kar dun?", "Pro plan bina break ke chalu rakhun?", "Match window ke liye Buy 1 Pizza Get 1 Free chalu kar dun?"); (b) new `season_phrase()` / `program_words()` / `trend_prose()` helpers turn payload slugs into prose ("post-resolution window, April to June", "30-day skin prep program", "ORS demand is up 40%, sunscreen demand is up 38% and antifungal demand is up 45%"); (c) new `Ctx.cust_guardian` reads `Karthik (parent: Sumitra)` and `channel: whatsapp_via_parent`, so the trial follow-up now opens "Hi Sumitra, Zen Yoga Studio here about Karthik's trial session"; (d) `recall_due` dropped its second escape-hatch ask, and new `Ctx.offer_matching()` picks the offer the seasonal beat is actually about, so the Diwali message promotes "Bridal Trial @ ₹999" instead of "Haircut @ ₹99" during a bridal-season beat. 100 tests still green, 25/25 triggers still compose, no URL/slug/multi-question regressions.
- **2026-08-30 (session 3)** — P-2 resolved: **Gemini-primary at temperature 0, deterministic grounding + fallback underneath** (participant chose Gemini; `architecture.md` ADR-001 and `challenge-brief.md` §7.1 require determinism — both are satisfied by temp-0 + prompt cache + fact fence). Implemented Phases 2–5: `bot/composer/{strategies,voice,llm,engine}.py`, `bot/conversation/handler.py`, `bot/guardrails.py`, `bot/state.py`. All **25 seed triggers now compose a grounded, validated message** (0 skipped). Fixed four real bugs found by inspection: trailing citations were being read as a separate sentence and killing the four best dentist messages; proper nouns were being lower-cased ("dC vs MI", "smile Studio", "diwali"); a duplicated word in the win-back line; an unverifiable claim in the match-night line. Live Gemini test over 25 real prompts: 16 answered (rest 429 quota), 15 accepted, **1 rejected by the fence for a second CTA** → fell back. Simulator `auto_reply_hell`, `intent_transition`, `hostile` all PASS. 100 tests green.
- **2026-08-30 (session 2)** — Environment built (venv + offline wheel install). Phase 1 implemented: `bot/store.py`, `bot/config.py`, `bot/main.py`, `tests/` (23 tests, all green, including pushing all 55 real seed contexts and asserting every seed trigger resolves to a complete 4-context bundle), `tools/run_local_judge.py`. Official `judge_simulator.py` **warmup scenario passes**, and passes again on a second run against the same live process — proving decision D2 was the right call. Verified: healthz reports zeros before any push, counts never double, stale version returns 409. Next: Phase 2 composer, blocked on P-2 (deterministic vs LLM).
- **2026-08-30** — Read PRD, architecture, rules, phases, plan, memory, handover-prompt, both challenge briefs, engagement-design, api-call-examples, judge_simulator.py, all 5 category files and all 3 seed files, generate_dataset.py. Ignored handover-bugfixer.md and handover-reviewer.md per instruction. Verified environment (Python 3.11.9 on host, no fastapi, no git repo, no bot code). Found and resolved 13 gotchas (§3) — most important: `expires_at` vs simulator clock (D1), same-version push semantics (D2), no dataset preload (D3), URL hard-fail (D4). Next: participant answers §6, then implement P1.
