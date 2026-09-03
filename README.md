# Vera Message Engine — magicpin AI Challenge

A stateful FastAPI bot that composes WhatsApp messages for merchants (and their customers) from four pushed contexts — `compose(category, merchant, trigger, customer?) → {body, cta, send_as, suppression_key, rationale}` — and handles multi-turn replies.

## Approach

**Deterministic grounded composer with an optional LLM polish layer.**

1. **Trigger-kind router** — 25 strategies (one per trigger kind in the dataset + a grounded generic fallback for judge-injected kinds). Each strategy assembles a draft *only* from facts present in the pushed contexts; missing facts drop clauses, and starved contexts skip the send entirely. Never invents.
2. **Category voice renderer** — per-vertical salutation (dentists get "Dr."), vocabulary, taboos, and Hindi-English code-mix when `identity.languages` includes `hi` (facts stay in English — they're quoted — the ask switches to Hindi, matching real Vera transcripts).
3. **Guardrail fence** — every body (deterministic *and* LLM-polished) is validated before send: exactly one CTA as the last sentence, no URLs (hard fail per the test contract), no fabricated numbers (every digit must exist in the packed facts), no internal jargon/slug leakage, no taboo words, no repeats. Failures fall back to the deterministic body.
4. **Conversation handler** (`/v1/reply`) — auto-reply detection keyed by *merchant* (the harness opens fresh conversation IDs per canned reply), escalation ladder send→wait→end; explicit intent ("ok let's do it") switches straight to action mode with a hard keyword-contract guard so a qualifying question can never leak; hostile messages end and permanently opt the merchant out.
5. **Tick scheduler** — urgency-ranked, ≤20 actions/tick, suppression-key and anti-repetition dedup, 24h-conversation overlap, opt-out respected.

## Model choice

- **Primary composer polish**: GLM-4.7-flash via Z.ai's Anthropic-compatible endpoint, **temperature 0**, cache-first (keyed by provider+model+prompt so identical inputs → identical output). Ordered failover: NVIDIA DeepSeek → Gemini flash.
- **The LLM is a rewrite stage, never a dependency.** It receives only the extracted fact list and a deterministic draft; its output must survive the same fact fence or it's discarded. No key, no quota, dead endpoints → the bot runs fully deterministic (which is scoring-capable on its own).

## Tradeoffs

- **Determinism over eloquence**: the deterministic core guarantees no hallucination, no timeout risk, byte-identical output. The LLM only ever *improves wording* within those constraints.
- **Skip over fake**: when a trigger's payload lacks grounding data, the bot declines to send rather than composing something generic ("Restraint is rewarded").
- **Merchant-level auto-reply memory**: slightly more state, but required because the judge's replay test uses fresh conversation IDs for each canned reply.
- **What extra context would have helped most**: real open-slot data per merchant (recall messages currently lean on trigger-provided slot labels), and an explicit merchant timezone to render match times correctly.

## Run

```bash
pip install fastapi uvicorn pytest httpx
uvicorn bot.main:app --host 0.0.0.0 --port 8080

# tests (100)
python -m pytest tests -q

# official harness scenarios (offline ones need no LLM key)
python tools/run_local_judge.py warmup --offline
python tools/run_local_judge.py auto_reply_hell --offline   # also: intent_transition, hostile
```

Configuration: `secrets.local.json` (git-ignored) or env vars — `VERA_LLM_PROVIDER` (zai|nvidia|gemini|openai-compatible), `VERA_LLM_API_KEY`, `VERA_LLM_CACHE_ONLY=1` (serve from warm cache only), `VERA_LLM_BUDGET_MS` (default 9000).
