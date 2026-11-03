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

## Rules
Own and made-up messages only until the privacy build (D3b) is done.
