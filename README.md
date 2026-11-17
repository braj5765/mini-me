# Mini Me

**The twin that earns the right to act.**

Mini Me is an AI twin that drafts replies in your voice, measures how well it knows you from the way you edit its drafts, and only acts alone on the parts of your life where it has proven itself.

> **Status: v0.1 prototype (single user, test mode).** A working correction loop (draft, capture, classify the edit, log everything) that you can reach three ways: a private Telegram bot, an Android Quick Settings tile, and an installable web app. It runs on my own and made-up messages only. No accuracy, time-saved or user numbers are claimed yet.

**Demo videos**
1. Web app flow: https://youtube.com/shorts/VUW_0M8zQ8k
2. Quick Settings tile flow: https://youtube.com/shorts/dTH_LpEh6eM

**Product links**
[Clickable prototype (15 screens, fictional data)](https://claude.ai/artifact/EBYQkocT2hQ9GhfU3oGC6r) Â· [PRD](https://github.com/braj5765/mini-me/blob/main/docs/portfolio/Mini-Me-PRD-v0.3.pdf) Â· [Loop diagram](https://claude.ai/artifact/XCbHFKpdTbusvy4E26KTtd) Â· [Timeline](https://claude.ai/artifact/G2EKvaQad9Hvm7gxSwwKCD) Â· [One-pager](https://github.com/braj5765/mini-me/blob/main/docs/portfolio/Mini-Me-one-pager.pdf)

## Why

People use AI but few trust it to act for them. One-shot AI twins are trained once and then act, and no product shows how accurate the twin really is. Mini Me flips that: it starts by checking, shows its accuracy, and takes over a message type only after the data supports it. The signal is how much you edit a draft, not a rating, because almost nobody rates.

## What v0.1 does

1. You bring in a message. Three ways:
   - **Telegram bot:** paste or forward a message to a private bot.
   - **Android Quick Settings tile:** copy a message in WhatsApp, tap the tile, and the draft lands on the clipboard with a notification. You paste it yourself.
   - **Installable web app (PWA):** copy a message, open the app, tap **Paste message**, and see the draft with its confidence label and a one-line reason. An optional **From:** row tags the sender in one tap.
2. The message is masked locally (phone numbers and emails) before storage and before any model call. The bot drafts a reply from a small self-model of your voice.
3. **Copying is not sending.** A copied draft goes on a **waiting list** in the web app: you confirm **Sent as is**, **I edited it** (paste what you sent) or **Not replying**. Drafts you never copied appear under **Not copied**. An unconfirmed item after 24 hours is logged as "outcome unknown" and never scored, so the numbers are not inflated.
4. An LLM classifier labels the edit: **decision** (your stance changed), **fact** (a date, name or number changed), **voice** (same meaning, different wording) or **cosmetic**.
5. Every step is written to an append-only event log. Telegram's `/events` shows the steps and `/stats` shows counts and the share handled with no or light edits.

Nothing is ever sent for you: all three paths are draft-only. **Test mode** is on: shared message text is masked, used for the draft and not stored, and every draft says "Test mode â€” made-up or own messages only".

## The loop

| Step | What happens | v0.1 |
|---|---|---|
| Observe | Self-model (voice, decisions, facts) seeded from your own messages | Basic |
| Predict | Draft + confidence + reason | Built |
| Capture | One tap or paste of the final text; no capture is logged as "outcome unknown" and never scored | Built |
| Learn | Edit patterns become suggested rule changes you approve | Planned |
| Score | Voice, decision and fact fidelity over a rolling window | Planned |
| Earn autonomy | Decided per message from six factors; four hard limits always ask first | Planned (keyword stand-in for the limits) |
| Operate | Undo window, daily digest, overrides feed back | Planned |

## What is measured

The event schema covers 47 event types (v0.1 emits the subset from drafting to capture and classification). The north star is the share of messages handled with no or light edits. Other measures: voice, decision and fact fidelity, time saved, override rate, and an anchoring gap (some drafts are held back until you reply, about 1 in 5 on Telegram and 1 in 10 in the web app, to see whether seeing the draft sways you).

In v0.1, `/stats` shows a labelled range because the light/heavy edit boundary is not set yet, and says "a demo of the loop, not results".

## Limits (read these)

- Classifier accuracy is unmeasured. Confidence is uncalibrated and display-only.
- **Hard limits are a keyword stand-in.** Money words, a short emotional word list and your own always-ask topics make the app ask first instead of drafting. It is not meaning-aware: expect misses and false alarms. The real autonomy engine is on the roadmap.
- **Test mode:** own or made-up messages only. Shared text is not stored, but a real message would still reach the model once, so don't share real ones.
- The drafter often hedges when it has no rule, and drafts can differ in language mix from how you write.
- Masking covers phone numbers and emails only; names, addresses and URLs are planned for the privacy build. Sender-tag labels are encrypted at rest (the key sits in the host's environment).
- Data export and delete are commands, not a Settings screen yet.
- Single user (the builder). The web app's share target (for apps that share text) is built but untested; WhatsApp offers no Share for text, which is why the path is Copy, then the tile or the app. "Back to WhatsApp" opens WhatsApp, not the specific chat.
- Runs on a free host that sleeps when idle; the first request can take up to a minute.
- A code guard blocks third-party text to any model provider not yet marked as verified.

## Stack

Python 3.11+, FastAPI, SQLAlchemy Core, Postgres (Neon; SQLite in tests), Telegram Bot API (webhook), Render (free web service, Docker) with an outside scheduler for background sweeps, an installable web app (vanilla JS, service worker for static files only), the HTTP Shortcuts Android app for the tile, Fernet encryption for sender-tag labels, and a pluggable LLM layer (currently Groq `openai/gpt-oss-120b`). Prompts are versioned files under `app/*/prompts/`. 421 automated tests.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate    # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env              # local only, never commit
python -m scripts.hash_password   # paste the output into LOGIN_PASSWORD_HASH
python -m pytest                  # 421 tests at the time of writing
uvicorn app.main:app --reload     # http://localhost:8000/healthz, web app at /app/

python -m scripts.run_m1_loop     # whole loop on 29 made-up messages, offline scripted model (plumbing check)
python -m scripts.api_token create "phone shortcut"   # token for the Android tile
python -m scripts.data_controls summary               # row counts; `export` writes your data to a file
```

Deploy (Render + Neon), the Android tile setup and the Telegram secrets are described in `docs/`. The bot, the API and the web app are private to one user: anyone else gets silence or a sign-in page. `data_controls` also has `delete-activity` and `delete-self-model`; they wipe data, so read `docs/` before running them.

## Roadmap

Privacy baseline (7-day purge of third-party text, fuller masking, provider terms checked) â†’ shadow mode on Telegram and Gmail (the twin drafts and logs, never acts) â†’ scores, learning and the autonomy engine (replacing the keyword limits) â†’ UI design lock, then the remaining web screens including Settings with export and delete â†’ first live Gmail send behind an undo window. Dates and effort are in the timeline.

## Docs

Build notes per component are in `docs/` (foundation, Telegram bot, drafting, classifier, share-to-draft and web app, hosting). Product docs: [prototype](https://claude.ai/artifact/EBYQkocT2hQ9GhfU3oGC6r), [PRD](https://github.com/braj5765/mini-me/blob/main/docs/portfolio/Mini-Me-PRD-v0.3.pdf), [loop diagram](https://claude.ai/artifact/XCbHFKpdTbusvy4E26KTtd), [timeline](https://claude.ai/artifact/G2EKvaQad9Hvm7gxSwwKCD), [one-pager](https://github.com/braj5765/mini-me/blob/main/docs/portfolio/Mini-Me-one-pager.pdf).
