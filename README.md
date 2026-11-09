# Mini Me

The twin that earns the right to act. This repo holds the real build (v0.1 foundation: setup, event logging, masking).
Full notes: `mini-me/dev/mini-me-dev-foundation.md` in the Claude Project.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # local only, never commit
python -m scripts.hash_password # paste the output into LOGIN_PASSWORD_HASH in .env
python -m pytest
uvicorn app.main:app --reload   # http://localhost:8000/healthz
```

## Deploy (Fly.io + Neon)
```bash
fly launch --no-deploy --copy-config      # unique app name, region sin
fly secrets set DATABASE_URL="<neon pooled string>" APP_SECRET="..." PSEUDONYM_KEY="..." LOGIN_PASSWORD_HASH="..."
fly deploy
curl https://<your-app>.fly.dev/healthz
```
Secrets live only in Fly secrets. Back up `PSEUDONYM_KEY`.

## Telegram bot (D4a)
Private bot for one user; draft-only. Notes: `docs/mini-me-dev-telegram.md`. Needs three Fly secrets:
```bash
fly secrets set TELEGRAM_BOT_TOKEN='...' TELEGRAM_WEBHOOK_SECRET='...' TELEGRAM_ALLOWED_USER_ID='...'
fly deploy      # the app registers its own webhook on startup
```
The bot stays off (503) until the token and secret are set.

## Edit classifier + results view (D9a, D11a)
Each captured reply is classified (decision / fact / voice / cosmetic; sent as is = none) and logged as
`edit_classified`. In Telegram: `/stats` (counts, class mix, shown vs blind, north-star range, data-mode labels) and
`/events` (latest steps; `/events 7` = every step of draft #7). Notes: `docs/mini-me-dev-classifier.md`.
```bash
python -m scripts.run_m1_loop                  # whole loop on 29 made-up messages, offline scripted model (plumbing check)
python -m scripts.run_m1_loop --live --pause 20  # same with the real model (needs LLM_PROVIDER / LLM_MODEL and a key)
python -m scripts.classify_pending             # classify edits that were captured while the model was down
```

## Rules
Own and made-up messages only until the privacy build (D3b) is done.
