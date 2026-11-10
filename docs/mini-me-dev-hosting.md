# Mini Me — Hosting: Fly.io → Render free (decision §5.22, Q47)

_Done 11 Nov 2026 in the Dev 6 chat. Neon Postgres is unchanged (no data moves). Code is on `main` (commit `21bc5a7`); **329 tests pass** (320 + 9 in `tests/test_hosting.py`). Render itself was **not** set up or checked from this session: the steps below are for Braj (PowerShell + browser)._

## 1. What changed in the code

| Change | Detail |
|---|---|
| `POST /tasks/sweep` (new, `app/tasks.py`) | Header `X-Sweep-Secret` must equal the new env var `SWEEP_SECRET`. Wrong or missing → 403. Secret not set → 503 (the endpoint is off). Checked **before** anything else, so strangers learn nothing about the bot. Runs the existing 24 h capture sweep (`sweep_expired`): each thread is claimed once, so repeat or overlapping calls change nothing (tested). Returns `{"ok": true, "marked_unknown": N}`. |
| Old endpoint | `POST /telegram/sweep` (webhook secret) is kept, unchanged. The in-process loop (every `TELEGRAM_SWEEP_MINUTES`) stays as a best-effort extra; nothing depends on it. The sweep also still runs on every Telegram update. |
| Port | `Dockerfile` now starts `uvicorn … --port ${PORT:-8080}`. Render sets `PORT` (default 10000); Fly and local runs still get 8080. |
| Migrations | **No change needed.** The app already creates missing tables at startup (`db.init_db()` in the lifespan), so the new `api_tokens` table appears on the first start. I could not confirm whether Render's free tier offers a pre-deploy command, so none is used. Leave "Pre-Deploy Command" empty. `python -m app.migrate` still works by hand. |
| Webhook address | New setting `PUBLIC_BASE_URL`. Order used: `PUBLIC_BASE_URL` → Render's own `RENDER_EXTERNAL_URL` (set by Render automatically) → the old `TELEGRAM_PUBLIC_BASE_URL`. The Fly hostname is no longer a default. If none is set the webhook is not registered (a warning is logged; the app still starts). |
| Health check | `/healthz` unchanged. |
| Smoke script | `--url` is now required (or env `MINI_ME_URL`). |
| Fly files | `fly.toml` (tracked in the repo, untouched), and your local `fly.toml` / `.dockerignore` / `.github/workflows/fly-deploy.yml` were not touched. |

**Stopping the Fly workflow.** `.github/workflows/fly-deploy.yml` is **not in the GitHub repo** (the repo has no `.github` folder), so GitHub never runs it; it only exists on your laptop. Nothing to stop. If a "Fly Deploy" workflow ever shows up in the repo's Actions tab, open it → "⋯" → **Disable workflow**. Never `git add .` on your laptop.

## 2. Render setup (browser)

Facts used (Render docs, checked 11 Nov 2026): free web services spin down after **15 minutes** without inbound traffic and take **up to about a minute** to wake; **750 free instance hours per workspace per calendar month**; spun-down time does not use hours; Render **may restart a free service at any time**; default `PORT` is 10000; `RENDER_EXTERNAL_URL` is set for web services.

1. **Sign up (no card).** https://render.com → **Get Started** → **GitHub** → authorise. You should land on the dashboard with a free (Hobby) workspace and **no** payment prompt.
2. **New service.** **+ New** → **Web Service** → **Build and deploy from a Git repository**. If `braj5765/mini-me` is not listed: **Configure account** → choose **Only select repositories** → `mini-me` → Save.
3. **Settings.**
   - Name `mini-me` (your address will be `https://mini-me.onrender.com`, or with a suffix if the name is taken).
   - Language / Runtime: **Docker**. Branch `main`. Root directory: empty. Dockerfile path `./Dockerfile`.
   - Region: **Singapore** (Southeast Asia).
   - Instance type: **Free**.
   - Environment variables: see section 3.
   - **Advanced** → Health Check Path `/healthz`. Pre-Deploy Command: empty. Auto-Deploy: On Commit.
4. **Create Web Service.** You should see the build log (a few minutes), then "Your service is live".
5. **Check.** `Invoke-RestMethod https://<your-url>/healthz` → `ok : True` and the version. The first call after sleep can take up to a minute.
6. **Telegram.** Send the bot `/help`. You should get the help text. On start the app points Telegram's webhook at the new address by itself. In Render → **Logs**, there must be no "could not register the Telegram webhook" line.

## 3. Environment variables

From your local `.env` (Fly secrets cannot be read back). Names only:

`DATABASE_URL` (Neon pooled string, starts `postgresql://` or `postgres://`) · `APP_SECRET` · `PSEUDONYM_KEY` · `LOGIN_PASSWORD_HASH` · `APP_ENV=prod` · `TELEGRAM_BOT_TOKEN` · `TELEGRAM_WEBHOOK_SECRET` · `TELEGRAM_ALLOWED_USER_ID` · `SWEEP_SECRET` (new) · `LLM_PROVIDER` · `LLM_MODEL` · `OPENAI_COMPAT_API_KEY` · `OPENAI_COMPAT_BASE_URL` · `OPENAI_COMPAT_REASONING_EFFORT` (if you use it) · `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` (only if set) · `DRAFTER` · `OWN_DATA_ONLY_MODE=true` (as on Fly; own or made-up text only) · `CLASSIFIER_PROVIDER` / `CLASSIFIER_MODEL` / `CLASSIFIER_NOTIFY` (only if set).
Do **not** copy `TELEGRAM_PUBLIC_BASE_URL` (old Fly address) or `APP_ENV=local`. `PUBLIC_BASE_URL` is optional (Render's own address is used).

If a value is not in `.env`: `APP_SECRET`, `TELEGRAM_WEBHOOK_SECRET` → make new random ones (only logs you out / re-registers the webhook). `LOGIN_PASSWORD_HASH` → `python -m scripts.hash_password` (you choose a password). `TELEGRAM_BOT_TOKEN` → Telegram → @BotFather → `/mybots` → your bot → **API Token**. `TELEGRAM_ALLOWED_USER_ID` → message @userinfobot. `PSEUDONYM_KEY` → **only** if no copy exists, make a new one: contact pseudonyms made before then will not match new ones (own / made-up data only so far; see flag R3).

## 4. Outside scheduler (cron-job.org)

1. https://cron-job.org → **Sign up** → confirm the email.
2. **Create cronjob.** Title `Mini Me sweep`. URL `https://<your-url>/tasks/sweep`. Schedule: **every 10 minutes**.
3. **Advanced**: Request method **POST**. Headers: add `X-Sweep-Secret` = your `SWEEP_SECRET` value. Body: empty. Save.
4. **Test run.** You should see HTTP 200 and a body `{"ok":true,"marked_unknown":0}`. After sleep, the first test can time out (cron-job.org waits about 30 s); the request still wakes the app, so run it again.

Why every 10 minutes: it runs the 24 h sweep and, since idle time before sleep is 15 minutes, keeps the app awake: about 720–744 h a month, inside the 750-hour budget. The 750 hours are **shared by the whole workspace**: do not run another free service there.

**Backup (not added to the repo): GitHub Actions.** Only if cron-job.org fails. File `.github/workflows/sweep.yml`; repo → Settings → Secrets and variables → Actions → secret `SWEEP_SECRET`, variable `APP_URL`. GitHub may delay scheduled runs, and disables them after 60 days without repo activity.
```yaml
name: sweep
on:
  schedule: [{ cron: "*/10 * * * *" }]
  workflow_dispatch: {}
jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - run: curl -fsS -X POST -H "X-Sweep-Secret: ${{ secrets.SWEEP_SECRET }}" "${{ vars.APP_URL }}/tasks/sweep"
```

## 5. Limits and flags (host-specific)

- **R1. Free tier can restart or be slow.** Render may restart a free service at any time and a cold start takes up to ~1 min. A Telegram message sent during a wake-up is answered when the app is up (Telegram retries). The share shortcut's first call after a long idle can take about a minute (set its timeout to 90 s).
- **R2. 750-hour budget** is per workspace. A 31-day month of always-awake use is 744 h: little headroom. If the budget runs out, Render suspends free services until the next month.
- **R3. `PSEUDONYM_KEY`.** Use the same value as before if you have it.
- **R4. No shell on free instances.** Token commands (`python -m scripts.api_token …`) run from your laptop against Neon, which is how they already worked.
- **R5. `/tasks/sweep` needs the bot configured** (token + webhook secret); otherwise it answers 503.
- **R6. Auto-deploy.** Every push to `main` redeploys (a brief restart).
- Fly: the old app can be left alone or deleted later; the new service re-points Telegram's webhook on its first start. The Fly app's secrets are lost with it, so keep `.env` safe.
