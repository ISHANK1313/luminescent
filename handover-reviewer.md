# Handover Reviewer — Quality Check Prompt

**For**: An agent or reviewer who evaluates the Vera bot's output quality, compliance, and readiness for submission.
**Version**: 1.0 (2026-08-30)
**Goal**: Provide a checklist mapped to the 5 scoring dimensions + endpoint contract + rules compliance; output a quick pass/fail report.

---

## 1. Review Checklist (per‑message or per‑run)

### A. Endpoint Contract Check (run once, before any scoring)

| # | Check | Expected | Pass? |
|---|---|---|---|
| E1 | `GET /v1/healthz` returns 200 `{status: "ok", uptime_seconds, contexts_loaded: {category, merchant, customer, trigger counts}}` | 200 + all 4 counts present | |
| E2 | `GET /v1/metadata` returns 200 with `team_name`, `team_members`, `model`, `approach`, `contact_email`, `version`, `submitted_at` | 200 + all 7 fields non-empty | |
| E3 | `POST /v1/context` accepts new context, idempotent on same version | 200 `{accepted: true, ack_id, stored_at}`; re‑post same version → still 200 (no-op) | |
| E4 | `POST /v1/context` rejects lower version with 409 or `{accepted: false, reason: "stale_version"}` | Lower version after higher → correct rejection | |
| E5 | `POST /v1/tick` returns `{actions: [...]}` or `{actions: []}` within 15s (simulator) / 30s (judge) | 200 + valid action schema(s) or empty list | |
| E6 | `POST /v1/reply` returns exactly one of: `{action: "send", ...}`, `{action: "wait", ...}`, `{action: "end", ...}` within timeout | 200 + one of the three schemas | |
| E7 | Rate limit: bot handles ≥ 10 req/sec from judge (no 502/504) | Sustain 10 req/sec for 60s → no failures | |

### B. Per‑Message Quality Check (run for each `compose()` output or `/v1/tick` action)

| # | Dimension | Checklist item | Score (0‑10) | Notes |
|---|---|---|---|---|
| S1 | **Specificity** | Body contains ≥1 verifiable fact: number, ₹ price, date, locality, source citation | | |
| S2 | **Category fit** | Voice/tone matches vertical; taboos avoided (e.g., no "cure"/"guaranteed" for dentists) | | |
| S3 | **Merchant fit** | Merchant name/identity used; language preference honored (hi‑en code‑mix if `languages` includes `hi`) | | |
| S4 | **Decision quality** | Message clearly communicates *why now* (the trigger); not a generic nudge | | |
| S5 | **Engagement compulsion** | One strong CTA + at least one compulsion lever (loss aversion, curiosity, social proof, effort externalization, reciprocity, asking‑the‑merchant) | | |
| S6 | **Rationale accuracy** | `rationale` accurately describes the actual decision (cross‑check vs. body + contexts) | | |
| S7 | **No fabrication** | Every number/price/date/citation in body exists in the pushed contexts | | |
| S8 | **Single CTA** | Exactly one CTA; none for info‑only triggers; CTA in last sentence | | |
| S9 | **Anti‑repetition** | Body not sent before in same conversation (or globally within 7‑day window) | | |
| S10| **Voice compliance** | No category‑inappropriate vocabulary (e.g., casual "AMAZING DEAL!" for dentists) | | |

### C. Run‑Level Checks (after `judge_simulator.py` scenario completes)

| # | Check | Target | Pass? |
|---|---|---|---|
| R1 | **Avg total score** ≥ 35/50 (full_evaluation) or ≥ 30/50 (phase2_short) | | |
| R2 | **No dimension avg < 6/10** | | |
| R3 | **All 3 scenario tests pass**: auto_reply_hell, intent_transition, hostile | | |
| R4 | **Determinism**: 2 runs of same 3 canonical test pairs → byte‑identical `body` | | |
| R5 | **No timeouts** on tick or reply (>15s simulator / >30s judge) | | |
| R6 | **healthz stable**: 0 failures across full run | | |
| R7 | **Rate limit**: sustained 10 req/sec → bot stays online | | |

---

## 2. Pass/Fail Report Template (fill after each simulator run)

```
magicpin AI Challenge — Review Report
=====================================

Run date: _______________
Simulator scenario: _______________
LLM provider/model: _______________

--- Endpoint Contract ---
E1 healthz: PASS / FAIL (brief explanation)
E2 metadata: PASS / FAIL
E3 context push: PASS / FAIL
E4 version conflict: PASS / FAIL
E5 tick endpoint: PASS / FAIL
E6 reply endpoint: PASS / FAIL
E7 rate limit: PASS / FAIL

--- Per‑Message Scores (average across all scored messages) ---
Specificity: ___ /10   (target: ≥ 7)
Category fit: ___ /10  (target: ≥ 7)
Merchant fit: ___ /10  (target: ≥ 7)
Decision quality: ___ /10  (target: ≥ 7)
Engagement compulsion: ___ /10  (target: ≥ 7)
Rationale accuracy: ___ /10  (target: ≥ 7)

--- Run‑Level Results ---
Avg total score: ___ /50
Pass/fail R1 (avg total ≥ threshold): ____
Pass/fail R2 (no dim < 6): ____
Pass/fail R3 (3 scenarios): ____
Pass/fail R4 (determinism): ____
Pass/fail R5 (no timeouts): ____
Pass/fail R6 (healthz stable): ____
Pass/fail R7 (rate limit): ____

--- Overall Status ---
☐ GREEN: Ready to advance / submit
☐ YELLOW: Issues noted — iterate on lowest‑scoring dimensions
☐ RED: Fundamental problems — refactor required before proceeding

Open issues / next steps:
_______________________________________________
_______________________________________________
```

---

## 3. Compliance Cross‑Check (rules.md reference)

| Rule # | Description | Verified? (Y/N) | Evidence |
|---|---|---|---|
| R1 | No RNG in composition | | |
| R2 | All composition steps pure functions of inputs | | |
| R3 | Context store version‑aware, idempotent | | |
| R4 | No external data fetch during tick/reply | | |
| R5 | Dict keys sorted where order matters | | |
| R6 | Single primary CTA per message | | |
| R7 | No fabrication (all facts from context) | | |
| R8 | Specificity‑first (at least one anchor per message) | | |
| R9 | Voice match per category | | |
| R10 | Language match (hi‑en code‑mix where appropriate) | | |
| R11 | Anti‑repetition (per conversation + global) | | |
| R12 | CTA placement: last sentence | | |
| R13 | No long preambles (hook by sentence 3) | | |
| R14 | Single binary commitment (YES/STOP or open‑ended) | | |
| R15 | Suppression key respected (expires_at + dedup) | | |
| R16 | /v1/healthz < 5s, < 3 failures → disqualified | | |
| R17 | /v1/metadata filled with real data | | |
| R18 | /v1/context idempotent per (scope,cid,version) | | |
| R19 | /v1/tick ≤ 15s / ≤ 30s; empty actions OK | | |
| R20 | /v1/reply one of 3 actions; ≤ 15s / ≤ 30s | | |
| R21 | /v1/teardown wipes state (optional) | | |
| R22 | Malformed JSON → 400 error info | | |
| R23 | Timeout handling: return empty/Wait/End | | |
| R24 | Unknown trigger kind → graceful degradation | | |
| R25 | Missing context → graceful exit | | |
| R26 | Response schema validation (extra fields OK, missing required → score 0) | | |

---

## 4. Scoring Rubric for the Reviewer Itself (how strictly to judge)

| Total score /50 | Classification | Action |
|---|---|---|
| 45–50 | **Excellent** | Green; proceed to next scenario or deploy |
| 35–44 | **Good** | Yellow; iterate on lowest‑scoring dimensions before advancing |
| 25–34 | **Needs improvement** | Yellow/Red; refactor composer/guardrails/handoff before proceeding |
| < 25 | **Below expectations** | Red; stop and re‑design (likely ADR‑001 reconsideration: LLM vs deterministic) |

---

## 5. Escalation Triggers (if any of these are true, stop and flag)

- [ ] Fabrication detected in any body (judge explicitly calls out hallucinated fact)
- [ ] Any dimension avg < 4/10 across the run
- [ ] Determinism check fails (2 runs → different bodies)
- [ ] healthz fails 3 consecutive times in a row
- [ ] Tick/reply consistently times out > 20s
- [ ] Anti‑repetition not working (same body sent twice, no −2 penalty but also no suppression)
- [ ] Judge reports “inconsistent with context” on > 20% of messages

If any escalation trigger fires, document in `memory.md` and iterate before proceeding.

---
*End of handover reviewer. Use this checklist after every `judge_simulator.py` run to keep quality consistent.*