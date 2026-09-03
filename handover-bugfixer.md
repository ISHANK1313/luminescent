# Handover Bugfixer — Focused Prompt for Fixing Issues

**For**: A coding agent tasked with debugging the Vera bot after an initial implementation or after simulator runs fail.
**Version**: 1.0 (2026-08-30)
**Goal**: Quickly diagnose and fix the most common failure modes without re-reading the full docs.

---

## 1. Failure Mode Map (what went wrong → how to fix)

| Symptom | Likely Cause | Fix Location | Test |
|---|---|---|---|
| **Fever: `judge_simulator.py` warmup fails — healthz returns non-200** | `main.py` startup doesn't populate ContextStore; or `store.py` `push()` throws exception | `bot/main.py` `on_event("startup")` or FastAPI lifespan — read all seed JSONs and push them via `store.push()`. Add try/except around each push; if one fails, log and continue (partial load is OK for warmup). | Run `python judge_simulator.py` → warmup. If PASS, move on. |
| **Fever: context push returns 409 stale_version** | Version conflict: re-posting same version, or earlier version being posted after higher one already stored | `bot/store.py` — ensure `push()` checks `if cur and cur["version"] >= body.version: return {accepted: false, reason: "stale_version", current_version}`. Verify that warmup pushes versions 1,2,3 in order (or any order) and that re-posting same version yields no-op. | After fix, re-run warmup; 409 should only appear if you genuinely send a lower version after a higher one. |
| **Fever: `/v1/tick` times out (>30s) or returns 500** | `compose()` takes too long; unhandled exception in tick handler; infinite loop in slot extraction | `bot/main.py` tick route: wrap the whole body in `try/except Exception as e: return {"actions": []}`. Add timeout guard: `import signal; signal.alarm(15); ...; signal.alarm(0)` or use `asyncio.wait_for(tick_handler(), timeout=15)`. | After fix, run tick with 3 triggers; should return < 15s. |
| **Fever: bodies are generic, no specificity (score ≤ 4)** | SlotExtractor not pulling concrete facts; VoiceRenderer not enforcing category tone; CTA Policy missing CTA | `bot/composer/slot_extractor.py` — ensure it pulls from: trigger.payload → merchant.performance → category.offer_catalog → merchant.identity.locality → category.peer_stats. Add at least 1 number/price/date per message. `bot/composer/voice_renderer.py` — enforce taboos, language code-mix. `bot/composer/cta_policy.py` — ensure CTA in last sentence. | After fix, run `_phase2_short`; check Specificity scores ≥ 7. |
| **Fever: Category fit score ≤ 4** | VoiceRenderer not enforcing taboos; promotional tone for regulated vertical; wrong `send_as` | `bot/composer/voice_renderer.py` — load `category.voice.tone`, `category.voice.vocab_taboo`, `category.voice.vocab_allowed`. Add rule: if category_slug == "dentists" and body contains "cure" or "guaranteed" → strip or rephrase. `bot/composer/cta_policy.py` — ensure `send_as` is `"vera"` for merchant-facing, `"merchant_on_behalf"` only when customer context populated. | After fix, re-run simulator; Category Fit ≥ 7. |
| **Fever: Merchant fit score ≤ 4** | Body uses generic "your business" instead of merchant name/identity; language preference ignored (pure English for hi-en merchant) | `bot/composer/slot_extractor.py` — always include `merchant.identity.name` in body opening; if `languages` includes `"hi"`, inject `{hi}` code-mix or Hindi phrase. `bot/composer/suppression_check.py` — not directly related, but ensure global dedup doesn't strip personalized messages. | After fix, re-run; Merchant Fit ≥ 7. |
| **Fever: Engagement compulsion score ≤ 4** | No CTA or multiple CTAs; buried CTA; no urgency/reason-why-now; generic "reply yes" | `bot/composer/cta_policy.py` — enforce exactly one CTA; ensure it's in last sentence; for action triggers use `"yes_no"`; for info triggers use `"none"`. Add at least one compulsion lever: loss aversion ("your CTR is below peer median"), curiosity ("worth a look"), social proof ("3 dentists in your area did this"). | After fix, re-run; Engagement ≥ 7. |
| **Fever: Repeated body −2 penalty** | Anti-repetition not working; same body sent twice in same conversation | `bot/guardrails.py` — implement global dedup: hash `(body, merchant_id)`; store last N (e.g., 10) in memory; before emitting action, check hash. In `converse/handler.py`, also track per `conversation_id` recent bodies. | After fix, send same message twice via simulator; should get −2 penalty or be suppressed. |
| **Fever: Auto-reply not detected** (simulator `_auto_reply` fails) | Handler doesn't check for verbatim repetition; or uses `conversation_id` instead of `merchant_id` for dedup | `bot/conversation/handler.py` — auto-reply detection: track last 2–3 incoming messages per `merchant_id` (NOT per `conversation_id`); if all are identical to WhatsApp Business canned reply `"Thank you for contacting us! Our team will respond shortly."` → `{action: "end", rationale:"auto-reply detected: merchant auto-responded 3+ times"}`. | After fix, run `_auto_reply`; bot should end after turn 2 or 3. |
| **Fever: Intent transition fails** (simulator `_intent` fails) | Handler doesn't recognize "ok lets do it" as action command; or still qualifying after commitment | `bot/conversation/handler.py` — intent check: `body_lower = body.lower()`; `actioning = any(w in body_lower for w in ["done","sending","draft","here","confirm","proceed","next"])`; `qualifying = any(w in body_lower for w in ["would you","do you","can you tell","what if","how about"])`; if `actioning and not qualifying` → switch to action mode (emit next nudge, `cta: "open_ended"`). | After fix, run `_intent`; bot should switch to ACTION mode. |
| **Fever: Hostile message not handled** (simulator `_hostile` fails) | Handler doesn't recognize "stop"/"not interested"; or responds with another nudge instead of exiting | `bot/conversation/handler.py` — hostile check: if any `stop_word in body_lower for word in ["stop","don't message me","not interested","unsubscribe","spam"]` → `{action: "end", rationale:"merchant ended conversation"}`. Also handle off-topic curveballs (e.g., "can you also help me file GST?") → acknowledge politely then exit or redirect to mission. | After fix, run `_hostile`; bot should end or apologize + exit. |
| **Fever: Scores drop after judge injects new context** (real harness Phase 3) | Bot's compose is hardcoded to seed data only; doesn't ground in newly pushed digest items, perf shifts, new triggers | Ensure every compose step reads from the **current** context store, not from hardcoded constants. The `compose()` function receives the latest category/merchant/trigger/customer dicts — they will have new data (e.g., new digest items). SlotExtractor must read from those dicts, not from cached/stale values. | After fix, the real harness will test this — bot must adapt. |
| **Fever: Determinism check fails** (2 runs same inputs → different body) | Non-deterministic RNG somewhere; dict iteration order varies; `time.time()` or `uuid.uuid4()` in compose | Audit `compose()` pipeline: no `random.*`, no `uuid.*`, no `time.*` (except the `now` injected by judge tick). Ensure all dict iterations are `sorted(...)`. Use `hash(frozenset(...))` if set/dict order matters. | After fix, run same 3 test pairs twice; compare `body` output byte-for-byte. |

---

## 2. Debugging Workflow (step-by-step)

**Step 1**: Run the specific simulator scenario that's failing.

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py   # -> selects scenario via TEST_SCENARIO config, or use CLI args if any
```

**Step 2**: Read the output carefully. Note:
- Which dimension(s) scored low
- What the judge's `hint` says
- Any `penalties` listed
- The actual `body` the bot sent (first 200 chars)

**Step 3**: Match the symptom to the table above. Most have a direct "Fix Location" column.

**Step 4**: Apply the fix in the indicated file.

**Step 5**: Re-run the same simulator scenario. If it still fails, check:
- Did the fix introduce a new issue? (Run warmup to verify endpoints still work)
- Is the issue actually in a different module? (Check the "Symptom" column for overlapping causes)
- Is the simulator injecting context I'm not reading? (Check `memory.md` Open Decisions + `reference.md` checklist)

**Step 6**: If the fix is subtle (e.g., a missing `sorted()` call), add a unit test in `tests/` so it doesn't regress.

**Step 7**: Once the scenario passes, run the full evaluation (`TEST_SCENARIO=full_evaluation`) to check overall scores.

---

## 3. Common "Gotchas" (assumed knowledge, easy to miss)

| Gotcha | Why it bites | Fix |
|---|---|---|
| **Forgetting `sorted()` on dict iteration** | Python 3.7+ preserves insertion order, but when dicts are constructed from different JSON sources or across restarts, order can vary → non-deterministic `body` | Add `sorted(merchant["offers"], key=lambda o: o["id"])` wherever offer order matters. |
| **Using `conversation_id` for auto-reply dedup** | Judge sends fresh `conv_auto_1`, `conv_auto_2`, … each turn — dedup must be `merchant_id`-level | In `converse/handler.py`, track last N messages by `merchant_id` only. |
| **Hardcoding prices/names from seed data** | Real harness injects fresh context after submission; bot must read from the `category/merchant/trigger/customer` dicts passed to `compose()` | Every value in `body` must come from the function's input dicts, not from module-level constants. |
| **Mixing up `vocab_taboo` and `taboos`** | Simulator scorer uses `vocab_taboo`; brief defines `VoiceProfile.vocab_taboo` | In code, read whichever key the category dict uses; or normalize at load time: `taboos = category.voice.get("vocab_taboo", category.voice.get("taboos", []))`. |
| **Sending the same `suppression_key` twice** | Trigger may fire within the same tick or across ticks; no dedup → −2 penalty or merchant irritation | `suppression_check.py` maintains a set of recently fired keys (last 7 days / last 10 actions). Check before emitting. |
| **CTA not in last sentence** | Judge penalizes −1; merchant confusion | `cta_policy.py` final step: `sentences = body.split(".")`; assert `cta_text in sentences[-1]` or re‑place. |
| **Forgetting `customer` is optional** | `compose()` called without customer context (merchant-facing messages) | In all composer steps, guard with `if customer is None: take merchant-only path`. Never assume `customer.identity.name` exists. |
| **Template params vs free-form confusion** | First outbound in 24h session should use `template_name` + `template_params[]`; subsequent can be free-form | `guardrails.py`: track whether a `(merchant_id, conversation_id)` pair has had a prior send; if first, fill template; else free-form. |

---

## 4. Quick Test Scripts (put in `tests/`)

```python
# tests/test_store.py
import pytest
from bot.store import ContextStore

def test_idempotent_same_version():
    store = ContextStore()
    payload = {"test": "data"}
    r1 = store.push("merchant", "m_001", 1, payload)
    assert r1["accepted"] == True
    r2 = store.push("merchant", "m_001", 1, payload)  # same version
    assert r2["accepted"] == True  # no-op
    r3 = store.push("merchant", "m_001", 2, {})       # higher version
    assert r3["accepted"] == True
    # lower version after higher should fail
    r4 = store.push("merchant", "m_001", 1, payload)
    assert r4["accepted"] == False
    assert r4["reason"] == "stale_version"

def test_get_after_push():
    store = ContextStore()
    store.push("category", "dentists", 1, {"slug": "dentists"})
    got = store.get("category", "dentists")
    assert got is not None
    assert got["payload"]["slug"] == "dentists"
```

```python
# tests/test_compose.py
from bot.composer.engine import compose

# Minimal contexts that judge_simulator pushes for phase2_short
category = {"slug": "dentists", "voice": {"tone": "peer_clinical", "vocab_taboo": ["cure", "guaranteed"]]}
merchant = {
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "identity": {"name": "Dr. Meera's Dental Clinic", "languages": ["en", "hi"]},
    "performance": {"views": 2410, "calls": 18, "ctr": 0.021},
    "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
}
trigger = {
    "id": "trg_test_research_digest",
    "scope": "merchant",
    "kind": "research_digest",
    "source": "external",
    "payload": {"category": "dentists", "top_item": {"title": "3-mo fluoride recall"}},
    "urgency": 2,
    "suppression_key": "research:dentists:2026-W17",
    "expires_at": "2026-05-03T00:00:00Z",
}
customer = None

result = compose(category, merchant, trigger, customer)
assert "body" in result
assert "cta" in result
assert "send_as" in result
assert "suppression_key" in result
assert "rationale" in result
# Basic specificity check: body should contain at least one number
assert any(ch.isdigit() for ch in result["body"]), f"Body has no numbers: {result['body']}"
print("comote() basic check passed:", result["body"][:80])
```

```python
# tests/test_converse.py
from bot.conversation.handler = handle_reply

# Auto-reply detection: 3 identical canned replies
 replies = [
    {"from_role": "merchant", "message": "Thank you for contacting us! Our team will respond shortly.", "turn_number": 1},
    {"from_role": "merchant", "message": "Thank you for contacting us! Our team will respond shortly.", "turn_number": 2},
    {"from_role": "merchant", "message": "Thank you for contacting us! Our team will respond shortly.", "turn_number": 3},
 ]
# (handler should return action: "end" after 3 identical messages)
```

---

## 5. When to Escalate (hand over to participant / human)

If after applying all fixes in this handover the simulator still fails with **unexplained errors**, do one of:

1. **Record the exact failure output** in `memory.md` under a new "Unresolved Failure" entry.
2. **Skip the failing scenario** and move on to the next one — don't let one blocker stall the whole project. The participant (you) will resolve it later.
3. **Flag in `reference.md`** as an open question for the next agent cycle.

**Never** spend more than 90 minutes on a single simulator failure without making progress. If stuck, move to the next task and come back with fresh eyes.

---
*End of handover bugfixer. Keep this file updated with any new failure modes you discover.*