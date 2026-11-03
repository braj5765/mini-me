# Mini Me — Dev 1: Foundation (D1a, D2a, D3a)

_Built 3 Nov 2026 · Spoke: Dev 1 Foundation · Status: code complete, 38 tests passing on SQLite; pushed to github.com/braj5765/mini-me (main); deployed to Fly at https://mini-me.fly.dev (region sin) on Neon Postgres; `/healthz` returns ok (3 Nov 2026)._

Source of truth: decisions 5.7, 5.12, 5.13, 5.14, 5.15; PRD F26, NFR-6/7; timeline section 3. Where PRD and decisions differ, decisions win.

## 1. Stack (build-level choices)

| Part | Choice | Why |
|---|---|---|
| Language / API | Python 3.12 + FastAPI | Mature Telegram and Gmail libraries; fast to build with AI help |
| Database | Postgres on Neon (SQLite in tests) | Managed, free-tier friendly; JSONB for event payloads |
| DB access | SQLAlchemy 2 Core + psycopg 3 | Same code on Postgres and SQLite, so tests run anywhere |
| Hosting | Fly.io (region `sin`), scale-to-zero | Low cost; Telegram webhooks wake it |
| Secrets | Fly secrets (env vars) | Never in files or chat; `.env` is local-only and git-ignored |
| Login | Single user "braj": scrypt password hash + signed cookie | No extra dependencies |
| Schema changes | `create_all` for now; Alembic arrives in D1b | Schema is final, so migrations are only for later changes |
| LLM | Pluggable layer, no provider wired yet | Chosen later (see section 6) |

## 2. Setup steps
1. Create private repo `braj5765/mini-me`; push this code (pending GitHub link).
2. Neon: project `mini-me`, copy the pooled connection string.
3. Local: `pip install -e ".[dev]"`, `python -m scripts.hash_password`.
4. Fly: `fly launch --no-deploy --copy-config`, then `fly secrets set DATABASE_URL=... APP_SECRET=... PSEUDONYM_KEY=... LOGIN_PASSWORD_HASH=...`, then `fly deploy`.
5. Check `https://<app>.fly.dev/healthz` returns `{"ok": true}`; POST `/login` with the password.
6. Back up `PSEUDONYM_KEY` (lost key = pseudonyms cannot be reproduced).

Prod refuses to start with placeholder secrets.

## 3. Final event schema

**Table `events`** (append-only, one row per event, never updated)

| Column | Type | Notes |
|---|---|---|
| event_id | uuid string | PK |
| idempotency_key | string, unique | Same key = same event, never a second row |
| event_name | string | Must be in `EventName` |
| occurred_at / recorded_at | timestamptz | |
| user_id | string | "braj" |
| session_id | string, null | |
| device_type | `mobile` / `desktop` / null | null for system events with no session (5.11) |
| channel | `telegram` / `email` / `web` | |
| domain | string | e.g. `group_queries` |
| contact_type | `new_individual` / `new_group` / `known_individual` / `known_group` | 5.13 pt 6 |
| app_version, self_model_version | string | |
| message_id | string | |
| mode | `shadow` / `live` | |
| classifier_version | string | Never cut |
| payload | JSON | Event-specific fields (PRD section 6) |

**Table `message_records`** (per-draft record, 5.7; one row per message, updated as it moves through the loop)

| Column | Notes |
|---|---|
| message_id (PK), user_id, created_at, updated_at | |
| channel, domain, contact_type, contact_pseudonym | Pseudonym only, never a real identifier |
| incoming_text, incoming_text_expires_at | **Masked only.** Expiry = +7 days (5.14 pt 3); purge job is D3b |
| draft_text, rationale, confidence (0.00–1.00) | |
| sent_text | The user's own words |
| outcome | `waiting`, `sent_as_is`, `edited_sent`, `discarded`, `not_me`, `user_authored`, `unknown` |
| scorable | True only for sent_as_is / edited_sent / discarded / not_me. **`unknown` is never scored** (5.13 pt 2) |
| edit_class | `decision`, `fact`, `voice`, `cosmetic`, `none`. "Reversed" is not an edit class (5.12) |
| signal | `decision_reversed`, `undo`, `flag_not_me` |
| autonomy_decision | `just_send`, `send_undo`, `draft_notify`, `ask_first`, `would_have_sent_shadow` |
| hard_limit, topic_triggered | `money`, `emotional`, `new_contact`, `low_confidence`, `custom_topic`, `none` |
| shown_state | `shown` / `blind` (5.15) |
| time_to_action_ms, device_type, classifier_version, mode | |

**Event names** (`app/events/schema.py`, `EventName`): message_received, draft_generated, draft_shown, draft_held_blind, draft_released_after_reply, autonomy_decision, hard_limit_triggered, topic_triggered, draft_acted, draft_sent, draft_discarded, outcome_unknown, edit_classified, shadow_comparison, undo_send, decision_reversed, not_me_tapped, digest_sent, digest_opened, digest_flag_not_me, calibration_sample_answered, calibration_me_not_me, ask_first_question_asked, ask_first_question_answered, selfmodel_item_edited, selfmodel_version_created, selfmodel_version_undone, suggestion_proposed, suggestion_approved, suggestion_set_aside, suggestion_restored, fidelity_snapshot, milestone_reached, risk_appetite_changed, topic_added, topic_removed, checkin_time_changed, integration_connected, integration_disconnected, session_started, csv_exported, note_added, data_exported, data_deleted.

New versus the PRD list: `draft_held_blind`, `draft_released_after_reply` (5.15), `outcome_unknown` (5.13), `data_exported`, `data_deleted` (5.14 pt 7).

**Idempotency rules.** Once-per-message events (message_received, draft_generated, autonomy_decision, edit_classified, draft_sent, draft_discarded, outcome_unknown, shadow_comparison, draft_held_blind, draft_released_after_reply) get the default key `<event_name>:<message_id>`. Every other event must pass `idempotency_key` (use `make_key(name, ...parts)`), otherwise the writer raises.

## 4. Masking rules (`app/privacy/masking.py`)
- Emails become `[EMAIL]`. Phone numbers become `[PHONE]`: 10–15 digits, or 8–15 if it starts with `+`; spaces, dots, dashes and brackets allowed inside.
- Dates (`2026-11-03`, `03/11/2026`) and ordinary numbers are left alone. Masking is idempotent.
- The event writer masks payload key `incoming_text` and the record field `incoming_text` automatically (defence in depth).
- `pseudonymise(identifier)` returns `contact-xxxxxxxx` (HMAC-SHA256 with `PSEUDONYM_KEY`). Same person gives the same pseudonym across formats (`+91 98765 43210` = `09876543210`).
- **Not masked in this version:** personal names inside free text, URLs, street addresses (flag 3).

## 5. Interfaces for later components

```python
from app.events import EventWriter, EventName
from app.events.writer import make_key
from app.privacy.masking import mask_text, pseudonymise

w = EventWriter()                        # uses DATABASE_URL; user "braj"

# D4a Telegram: ingestion. Mask first; raw text never enters the DB.
pid = pseudonymise(sender_handle)
w.upsert_message(mid, channel="telegram", domain="group_queries",
                 contact_type="known_group", contact_pseudonym=pid,
                 incoming_text=raw_text,          # masked inside
                 device_type="mobile")
w.log(EventName.message_received, message_id=mid, channel="telegram")

# D8a drafting
w.upsert_message(mid, draft_text=..., confidence=0.74, rationale=..., shown_state="shown",
                 autonomy_decision="draft_notify", mode="shadow", classifier_version=None)
w.log(EventName.draft_generated, message_id=mid, payload={"items_used": [...]})
w.log(EventName.draft_shown, message_id=mid, idempotency_key=make_key("draft_shown", mid, view_no))

# D4a capture ("Sent as is" / pasted text) and D9a classifier
w.upsert_message(mid, sent_text=..., outcome="edited_sent", edit_class="voice",
                 classifier_version="clf-0.1", time_to_action_ms=...)
w.log(EventName.edit_classified, message_id=mid, classifier_version="clf-0.1",
      payload={"edit_class": "voice", "light_or_heavy": None})

# No sent text captured:
w.mark_outcome_unknown(mid)              # excluded from every score
```
- `log_safe(...)` never raises (NFR-6); D2b adds a retry queue behind it.
- LLM: `from app.llm import get_llm, Message`; `get_llm().complete([...], contains_third_party_text=True)`. Never send third-party text without that flag.
- Tests: `tests/conftest.py` gives `engine` and `writer` fixtures on a temp SQLite DB.
- Run: `python -m pytest`; `uvicorn app.main:app`.

## 6. LLM layer (set up so models can be swapped)
`LLM_PROVIDER` and `LLM_MODEL` (Fly secrets) pick the model. Adapters: `gemini`, `anthropic`, `openai_compat` (Groq, OpenRouter, Ollama). Each provider has a `no_training_verified` flag, **all currently False**. Calls that include third-party text are refused for unverified providers. Free tiers therefore work for own and made-up text only. Before real group messages (after D3b) a provider must be verified by reading its current terms and flipping the flag. Nothing in D1a–D3a calls an LLM, and no model name is hard-coded.

## 7. Tests
38 tests, all passing on SQLite: masking (phones, emails, dates, idempotency, pseudonym stability and key dependence), writer (common fields, idempotency, repeatable-event keys, enum validation, unknown fields, outcome unknown never scorable, blind state, signal vs edit class, masked incoming text), auth, config, LLM guard.
**Verified on first deploy (3 Nov 2026):** Docker build succeeded; the release command `python -m app.migrate` created the tables on Neon Postgres; `/healthz` returns `{"ok":true,"version":"0.1.0"}`, so the app reaches the database.
**Not yet run:** the automated tests against Postgres (they run on SQLite only), and the live login (`POST /login`) on Fly. Braj chose to skip the login check for now.

## 8. Build-level choices
Names `events`, `message_records`, snake_case event names and enum values above; `message_records.scorable` boolean; default idempotency keys per message; `log_safe`; `create_all` until D1b; scale-to-zero Fly machine (256 MB); region `sin`.

## 9. Not done / deferred
Telegram bot (D4a), self-model, drafting, classifier, UI, Gmail, 7-day purge (D3b), retry queue (D2b), CI/backups (D1b), threshold values, real third-party data.

**Deploy notes.** Fly app `mini-me`, shared-cpu-1x, 256 MB. `fly launch` created 2 machines for high availability; run `fly scale count 1 --yes` to keep one (cost). The org had no payment method at first deploy; Fly may ask for one. Secrets (`DATABASE_URL`, `APP_SECRET`, `PSEUDONYM_KEY`, `LOGIN_PASSWORD_HASH`) are set as Fly secrets only. On Windows PowerShell use a conda env with Python 3.12 (system Python was 3.9) and single quotes around secret values (the hash contains `$`).
