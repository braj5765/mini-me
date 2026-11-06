# Mini Me — Dev 2: Telegram bot (D4a, v0.1 part)

_Built 3 Nov 2026, UX build 7 Nov 2026 · Spoke: Dev 2 Telegram bot · Status: **deployed on Fly (mini-me.fly.dev) and tested by Braj on real Telegram, 6 Nov**; 93 tests passing on SQLite (38 foundation + 55 for the bot). First build on 3 Nov; the 7 Nov build adds the chat-clarity changes from that testing (section 4a)._

Source of truth: decisions 5.7, 5.7a (Telegram note), 5.13 pts 1–3 and 6, 5.14, 5.15 pt 3, 5.16; foundation doc; timeline section 3 (v0.1 rules). Where the PRD differs, decisions win.

## 1. What it is

A **private assistant bot for Braj only**. He forwards (or pastes) a message; the bot masks it, drafts a reply through a **drafter interface**, and shows the draft in the DM. He taps **Sent as is**, or replies with what he actually sent, or discards. About 1 in 5 drafts are **blind**: held back until he has replied first. Nothing is ever sent for him: Telegram is draft-only (5.13 pt 1).

The drafter is a **stub** (fixed template, no model call). D8a replaces it. Everything around it (masking, events, capture, blind sample, outcome unknown) is final-shape code.

## 2. Code map

| File | Role |
|---|---|
| `app/telegram/bot.py` | All bot logic. `handle_update(update, deps)`; `sweep_expired(deps)`; hook points |
| `app/telegram/router.py` | `POST /telegram/webhook`, `POST /telegram/sweep` (both need the shared secret); `get_bot_deps()` wires everything |
| `app/telegram/client.py` | Thin Bot API client over httpx; token never reaches logs or errors |
| `app/telegram/store.py` | `ThreadStore`: thread rows, atomic claim, known-contact lookup |
| `app/telegram/format.py` | All user-facing copy and the button builder |
| `app/telegram/runtime.py` | Webhook registration on startup; background sweep loop |
| `app/drafting/base.py` | `Drafter` protocol, `DraftRequest`, `DraftResult`, `UsedItem` |
| `app/drafting/stub.py` | `StubDrafter` (placeholder) |
| `app/db.py` | + table `telegram_threads` (additive; Telegram ids and timers only, **no text**) |
| `app/config.py` | + Telegram settings (section 6) |
| `tests/test_telegram.py` | 41 tests |

## 3. Commands and buttons

| Control | What it does |
|---|---|
| Forward or paste a message | New message: mask → pseudonymise → draft |
| **✓ Sent as is** (button under a draft) | Records the draft as what he sent |
| **Discard** (button) | Records the draft as discarded |
| Reply to a draft with text | Records that text as what he sent (an unchanged text counts as as-is). "Reply" = Telegram's own feature: swipe the draft left on a phone, or right-click it and choose Reply on a computer |
| `/sent <text>` | Same, but for the **newest** waiting message (no Reply needed). Careful: a plain message you just sent is itself the newest one |
| `/pending` | Lists what is waiting: `#4 · "re: first 40 characters (masked)"`; blind items marked "(my draft is held back)" without showing the draft |
| `/stats` | Stub: "Stats are coming soon…" (D11a) |
| `/help`, `/start` | Short usage text |

Copy follows design-core: calm, no "unlock / gate / level up" (there is a test for it).

## 4. Flows

**Ingest (all messages).** Only Braj, only in a private chat (anyone else: ignored silently, no reply, no event, no write; no allowed id configured: ignore everyone). Text → `mask_text` (local, before storage and before the drafter) → sender → `pseudonymise` → `message_records` upsert (masked `incoming_text`, 7-day expiry) → `message_received` (`sender_type`, `origin` forwarded/pasted, `masked_count`). Telegram redelivering an update is a no-op (message id is derived from chat + Telegram message id).

**Shown (about 4 in 5).** Drafter → `draft_generated` → draft message with confidence (High/Medium/Low + 3-segment bar + number), one-line "Why", the draft text, and the two buttons → `draft_shown`. Captured by button or reply → `draft_acted` + `draft_sent` (or `draft_discarded`), record gets `sent_text`, `outcome`, `time_to_action_ms`, buttons are removed, the **D9a hook** fires.

**Blind (config ratio, default 0.2; 5.15 pt 3).** Same draft is generated and stored, but the DM only gets: "I've drafted this one; reply first and I'll show you mine." No buttons, no "Sent as is". → `draft_held_blind`. When he replies to that note (or `/sent`), the reply is stored as `sent_text`, `draft_acted` + `draft_sent` are logged, then the held draft is shown with "Same as / Different from what you sent" → `draft_released_after_reply`, then the D9a hook. A blind message has no `draft_shown` event by design (the release is the first sight).

**Outcome unknown (5.13 pt 2).** Every shown or blind message has a capture window (default **24 h**, placeholder). Nothing captured when it ends → record `outcome=unknown`, `scorable=False`, event `outcome_unknown` (`reason`, `window_hours`, `shown_state`; `occurred_at` = window end). Never counted as an accept. A late button press cannot revive it. The sweep runs: on every incoming update, every 10 min while the machine is awake, and via `POST /telegram/sweep`.

**4a. Reading the chat (7 Nov build, after real-Telegram testing).** Braj could not tell when a message was done, or what to do mid-way.
- **Numbered drafts:** `Draft #3 ... Re: "<first 40 characters, masked>"`. The number is the thread's position in creation order (derived, not stored; no schema change). Confirmations use it: `Saved #3: ...`, `Here's the draft I held back for #3:`.
- **Visible "done":** after **Sent as is** or **Discard** the draft message itself is rewritten (buttons removed, the "Edited it?" hint replaced by `✓ Recorded: sent as is` or `Discarded: not sent`). Falls back to just removing the buttons if Telegram gives no text or refuses the edit. A reply-capture still gets the numbered "Saved #N" message.
- **Mid-way / starting something new:** just send a new message; any number of drafts can be open, each with its own buttons. Unfinished ones expire to outcome unknown after the window. Discard is the only explicit way to drop one.
- **Nudge:** a *plain* (not forwarded) message sent while another is open is still treated as new (decision: only Reply or `/sent` capture), but the bot adds: "I treated that as a new message. If it was your final reply to #N, reply to my message for #N instead (swipe it left, or right-click and choose Reply)." Never sent for forwards or when nothing is open.
- Editing one of your own sent messages in Telegram does nothing (edits are not listened to).

**Contact type (5.13 pt 6).** Forwarded from a user → individual; forwarded from a chat/channel → group; **pasted → group** (default). Known = Braj has already replied (sent / edited / user-authored) to the same pseudonym, else new. Pasted messages get a unique pseudonym each, so they can never become "known" (see flag 3).

## 5. Interfaces for later components

```python
# D8a: implement this and swap it in get_bot_deps() (app/telegram/router.py) -> drafter=YourDrafter()
class Drafter(Protocol):
    def draft(self, req: DraftRequest) -> DraftResult: ...
# DraftRequest: message_id, masked_text (ALWAYS masked), channel, domain, contact_type, contact_pseudonym
# DraftResult : text, confidence (0..1), rationale (one line), items_used [UsedItem(layer,item_id,label)],
#               drafter, drafter_version, is_stub
# A drafter that calls a model must use app.llm.get_llm() with contains_third_party_text=True.

# D9a: after every captured sent text. Pass it in get_bot_deps(): on_captured=your_function
def on_captured(message_id: str, capture: str) -> None   # capture = "button" | "reply_text"
# Read message_records (draft_text, sent_text), then write edit_class + classifier_version on the record and
# log edit_classified (once per message). NOT called for discards. The bot leaves edit_class empty today
# (even for sent-as-is), so D9a owns it entirely and no event/record mismatch can arise.

# D11a: /stats. Pass stats_provider=your_function in get_bot_deps()
def stats_provider(deps: BotDeps) -> str     # return the reply text; deps.writer / deps.engine give data access
```

`BotDeps` also injects `client`, `rng`, `now`, so tests (and D10) can drive time and the blind dice. D10 (full shadow logic) can reuse `shown_state`, the thread store and the events above.

**Events used (no new event names):** message_received, draft_generated, draft_shown, draft_held_blind, draft_released_after_reply, draft_acted, draft_sent, draft_discarded, outcome_unknown. **Not logged here:** `autonomy_decision` (record field `autonomy_decision=draft_notify` is set; the event belongs to D10/D22), `edit_classified` (D9a), `not_me_tapped` (F7, Should).
`draft_acted.payload.action` = `sent` | `edited_sent` | `discarded`; `capture` = `button` | `reply_text`.

## 6. Config (Fly secrets / env)

| Setting | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | **Secret.** Bot is off (503) until set |
| `TELEGRAM_WEBHOOK_SECRET` | – | **Secret.** Random string; checked on every webhook call |
| `TELEGRAM_ALLOWED_USER_ID` | – | Braj's numeric Telegram id. Empty = reject everyone |
| `TELEGRAM_PUBLIC_BASE_URL` | `https://mini-me.fly.dev` | Where Telegram sends updates |
| `TELEGRAM_BLIND_RATIO` | `0.2` | ~1 in 5 [adjustable after testing] |
| `TELEGRAM_CAPTURE_WINDOW_HOURS` | `24` | **Placeholder**; no capture inside it = outcome unknown |
| `TELEGRAM_SWEEP_MINUTES` | `10` | Background sweep interval while awake |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | Only changed in tests / fakes |

## 7. How to run and test

```bash
pip install -e ".[dev]" && python -m pytest          # 93 tests, no network, no token
```
Local end-to-end without Telegram: run the app with the Telegram env vars set and `TELEGRAM_API_BASE` pointing to any stub server, then POST updates to `/telegram/webhook` with the `X-Telegram-Bot-Api-Secret-Token` header (this is how the manual run below was done).

**Manual run (3 Nov 2026, made-up messages, real app + real HTTP client against a fake Telegram API):** `/start`; forward → draft → ✓ Sent as is; pasted message → edited reply; forward from a group → Discard; `/pending`, `/stats`; blind forced → draft held → reply → draft released; 0.0005 h window → left alone → next update marks outcome unknown. Result: 5 message records (4 scorable, 1 unknown), 9 event types logged, wrong secret = 403, stranger = silence, webhook registered once (not again after restart), **no raw phone / email / Telegram id in any table, bot token absent from the log**.

**Real-Telegram test (6 Nov, Braj, made-up messages, bot live on Fly):** `/start`, drafts with both buttons, discard, reply-with-edit, reply-identical (as is), `/sent`, phone/email masking, blind notice and release (reply and `/sent`, match and mismatch), `/pending`, `/stats`, and outcome unknown with a shortened window. Queried the database: 19 message rows; outcomes sent_as_is / edited_sent / discarded / unknown / waiting all correct; `scorable` true only for known outcomes; text masked (`[PHONE]`, `[EMAIL]`); every contact type `new_group` (all pasted). One planned check did not apply: a blind message created under the 24 h window stayed `waiting` after the window setting was shortened, because **the deadline is fixed when each message arrives** (intended; now documented and tested).

## 8. Build-level choices

- **Webhook, not polling.** Fly scale-to-zero stops an idle machine and an incoming webhook wakes it; polling would keep it running all day. The app registers the webhook itself on startup if Telegram's current URL differs (one `getWebhookInfo` call per cold start). Telegram sends the secret in a header; compared in constant time. The handler always answers 200 after auth so a poison update is not retried forever.
- **No bot library**: raw Bot API over httpx (already a dependency). Plain text only (no Markdown parsing, so no escaping bugs).
- **New table `telegram_threads`** (message id → Telegram message ids, state, timers). Needed to map a reply to its draft and to survive restarts. No message text. Created by `create_all`; nothing existing changed.
- **Capture window 24 h**, blind ratio 0.2, sweep every 10 min: all placeholders in config.
- **Capture by reply or `/sent` only.** A plain paste or forward is always a new message (see flag 2).
- **Stored masked everywhere:** incoming text, drafts (built from masked text) and the user's own sent text.
- **Strangers get silence**, not "private bot" (no reply to spam, nothing to probe).
- **Claim-then-act** for every capture: closing the thread is an atomic update, so double taps and Telegram retries record once.
- **Confidence wording** High ≥ 0.75, Medium ≥ 0.50, else Low, with a 3-segment bar. Display only; sets nothing; not autonomy thresholds (Q1 untouched).
- **`device_type` is NULL** for Telegram events (the bot cannot know phone vs desktop).
- **Mode** is `shadow` on all Telegram events (live autonomy is Gmail only, 5.13 pt 1). `autonomy_decision` on the record is `draft_notify`.
- **Telegram token safety:** httpx request logging is turned down (it prints URLs containing the token); client errors never include the URL.
- `outcome_unknown.occurred_at` is the window end, not the wake-up time; `recorded_at` shows when it was written.
- Stub draft quotes the first 60 characters of the **masked** incoming text, so the loop can be exercised. Shortened quotes (stub and the `Re:` line) never cut through a `[PHONE]` / `[EMAIL]` token (`snippet()` in `app/drafting/base.py`).
- **Capture deadline is fixed per message** when it arrives (`telegram_threads.deadline_at`). Changing `TELEGRAM_CAPTURE_WINDOW_HOURS` affects only new messages.
- **Draft numbers are derived** (creation order within the chat), so no new column and no migration; relies on threads never being deleted.
- **Status line via `editMessageText`**, using the message text Telegram sends with the button tap (nothing extra stored).
- **Nudge** is a separate short message after the draft, only for pasted messages while another is open.

## 9. Deploy steps (for Braj; not possible from this session: no Fly access)

1. In Telegram, talk to **@BotFather** → `/newbot` → copy the token. Get your numeric id from **@userinfobot**.
2. Generate a webhook secret (e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`).
3. Set secrets (PowerShell, single quotes): `fly secrets set TELEGRAM_BOT_TOKEN='...' TELEGRAM_WEBHOOK_SECRET='...' TELEGRAM_ALLOWED_USER_ID='...'`
4. `git pull` then `fly deploy` (the release command creates the new table). Then `fly scale count 1 --yes` if still on 2 machines.
5. Check: `curl https://mini-me.fly.dev/healthz`, then message the bot `/start`. First message after a cold start may take a few seconds.
6. Try made-up messages only (v0.1 rule). Keep the LLM provider flags as they are; this version calls no model.

## 10. Not done / deferred

Real drafting (D8a), classifier (D9a), `/stats` content (D11a), self-model, extended masking (D3b, 5.16: names, IDs, addresses, URLs are still **not** masked), 7-day purge job (D3b), full shadow logic and `autonomy_decision` events (D10), "Not me" chips (F7), Gmail, web UI, real third-party data.

## 11. Flags and questions (not decided here)

1. **Foundation bug risk (Dev 1 / D3b).** `normalise_identifier` treats any string with 8+ digits as a phone number and keeps only its last 10 digits. Telegram user ids and group ids are long numbers (a supergroup id can end in the same 10 digits as a user id), so they would collide. D4a avoids it by building identifiers with an `@` (`user-<id>@tg.invalid`). Suggest the foundation take an explicit `kind` instead.
2. **Capture ambiguity.** Braj's plan says he can paste or forward the final text back, but a pasted or forwarded message is also how a new message arrives (and v0.1 uses his own messages, which he may well forward from himself). So only a **reply to the bot's message** or **`/sent <text>`** counts as final text. Confirm that is acceptable.
3. **Known contacts are rarely knowable for pasted WhatsApp text (product-level).** Pasting carries no sender. Pasted messages are `new_group` and can never become `known`. Per 5.13 pt 6 and the new-contact hard limit, nearly all dogfood group queries would later count as new contacts (always ask first, scored from shadow data). Also, a Telegram forward from a group shows the original sender, so group vs individual is a guess. Options: accept; or let him add a tiny sender tag when pasting (e.g. first line `from: Riya`) so the same person maps to one pseudonym; or a `/as known` hint. Needs a decision before D22 / the autonomy share is read.
4. **Own replies are stored masked.** Phones and emails he types in his own reply become `[PHONE]` / `[EMAIL]` (follows "never store raw unmasked text", 5.14 pt 2). Consequence: a fact edit that only changes such a value is invisible to the classifier. 5.13 says own replies are kept; confirm masking them is intended.
5. **Outcome-unknown timing.** With scale-to-zero the sweep runs only when the machine is awake (and on the next update). Scores are unaffected (a waiting message is unscored too), only the `outcome_unknown` row appears late. Fix later if needed with `min_machines_running = 1` or an external cron on `/telegram/sweep`.
6. **Blind messages cannot be skipped on purpose.** No reply simply expires to unknown after 24 h, so "I chose not to answer" and "I forgot" look the same. Fine for now.
7. **Edit class for sent-as-is is left to D9a** (so `/stats` cannot count classes until then). `autonomy_decision` events are left to D10/D22.
8. **Timeline note:** D4a is done in about the planned scope; webhook auto-registration and the thread table were small additions (about 1–2 h inside D4a's 7 h). No date change.
9. _(Fixed 7 Nov)_ The stub quote could be cut through a `[PHONE]` token; shortened quotes now drop a partial token.
10. `device_type` is NULL for Telegram (foundation example suggested `mobile`). Only matters if M11 should count Telegram.
11. **Stub test rows are mixed with future data.** Every `draft_generated` event from this bot carries `payload.is_stub = true`. D9a / D11a must exclude those (or the rows must be cleared) before real dogfood numbers are read, otherwise stub accepts and discards will pollute the as-is / edit rates. Test rows from 6 Nov (19) are all stub rows.
12. **Edits to your own messages are ignored** (the bot does not subscribe to edited messages). The way to correct a final text is to Reply to the draft with it.
13. **Blind ratio and capture window can be changed without code** (`fly secrets set TELEGRAM_BLIND_RATIO=1` forces every draft blind; `TELEGRAM_CAPTURE_WINDOW_HOURS=0.02` is about a minute). Useful for testing; set back to `0.2` and `24` afterwards. A forgotten short window would expire real messages in about a minute.
