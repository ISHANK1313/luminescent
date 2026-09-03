# Deploying the Vera Bot — Hosting Runbook

The judge calls your public URL for a **60-simulated-minute window** (~30–45 real minutes) at **10 req/s max**, polling `/v1/healthz` every 60s. Three consecutive healthz failures = disqualified. So the only hard hosting requirements are: always-on process, stable URL, no cold start during the window.

## Option comparison

| Option | Setup | Cost | Cold-start / uptime risk | Verdict |
|---|---|---|---|---|
| **Fly.io** | `fly launch` (Python builder detects `bot.main:app`), `fly deploy` | free allowance covers a shared-cpu-1x 256MB VM | always-on; ~1–5s cold start only on first deploy | **Best stability** |
| **Railway** | push repo to GitHub → connect in dashboard (or `railway up`) | ~$5 Hobby plan (trial credits usually cover it) | always-on on paid; **free tier sleeps** | **Best if already on GitHub** |
| **ngrok → localhost** | `ngrok http 8080`, submit the tunnel URL | free | free URL **rotates on restart**; your machine must stay awake the whole window | Acceptable fallback; keep machine awake (`powercfg /change standby-timeout-ac 0`) |
| **Render free tier** | GitHub connect, web service, `uvicorn bot.main:app` | free | spins down after 15 min idle → first healthz poll takes >5s → **disqualification risk** | Avoid unless paid |

Recommendation: **Fly.io or Railway**. If you must use ngrok, test the tunnel with the external smoke check below right before submission and don't let the machine sleep.

## Deploy-time environment

Set these in the host's env vars (values come from your local `secrets.local.json`, which is git-ignored and never deployed by accident — copy values across manually):

```
VERA_LLM_PROVIDER=zai                      # or anthropic-compatible
VERA_LLM_MODEL=glm-4.7-flash
VERA_LLM_BASE_URL=https://api.z.ai/api/anthropic
VERA_LLM_API_KEY=<your GLM key>
```

Fallback chain keys (`LLM_FALLBACKS`) currently live only in `secrets.local.json`. If the deploy can't read that file, set `VERA_LLM_ENABLED=0` — the bot runs fully deterministic (scoring-capable; verified) — or embed the chain via env in a wrapper.

For the submission identity (when you fill it in):
```
VERA_TEAM_NAME=<your name>
VERA_TEAM_MEMBERS=<comma-separated>
VERA_CONTACT_EMAIL=<your email>
```

**Optional but recommended:** copy `runs/llm_cache.json` into the deployed image. The composer is cache-first, so warm-cache ticks take ~60ms vs up to 9s cold. Without it the bot still works — every cache miss falls back to the deterministic body inside the 9s budget.

## Health during the window

`/v1/healthz` is a dict lookup — sub-30ms under the 10 req/s load test (600 requests, p95 < 30ms, 0 errors). The bot holds all state in memory; **do not restart it mid-test** (the testing brief allows in-memory state but not restarts).

## External smoke check (run before submitting the URL)

From any network other than the host:

```bash
curl -s https://<your-url>/v1/healthz     # {"status":"ok",...}
curl -s https://<your-url>/v1/metadata    # identity + model string
curl -s -X POST https://<your-url>/v1/tick \
     -H "Content-Type: application/json" \
     -d '{"now":"2026-04-26T10:30:00Z","available_triggers":[]}'   # {"actions":[]}
```

Then run the full local harness against the *public* URL before submitting:

```bash
python tools/run_local_judge.py --bot-url https://<your-url> warmup --offline
python tests/integration_judge_sim.py --score    # local; but run_local_judge with --bot-url covers the public one
```

## Submission checklist (P7)

- [ ] Public URL responds on all five endpoints (smoke check above)
- [ ] `python tools/run_local_judge.py --bot-url <url> warmup --offline` passes against the public URL
- [ ] `/v1/metadata` shows real team identity (or placeholders intentionally, if still unfilled)
- [ ] Bot stays live through the evaluation window (Fly/Railway: nothing to do; ngrok: keep machine awake)
- [ ] URL submitted via the challenge portal
- [ ] After evaluation, expect the `POST /v1/teardown` wipe (already implemented)
