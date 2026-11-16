# Mini Me — Dev 6: Share-to-draft backend + Android shortcut (§5.20 pt 7 steps a and b)

_Built 10 Nov 2026, host moved 11 Nov 2026 · Spoke: Dev 6 · Status: code on `main` (commits `63c7243` … see section 11), **329 tests passing on SQLite** (274 before + 46 in `tests/test_share.py` + 9 in `tests/test_hosting.py`), run end to end locally against a real HTTP server with the stub drafter. **Hosting is now Render free (decision §5.22; see `mini-me-dev-hosting.md`); Fly.io is no longer used.** Deployed on Render and verified on Braj's Android phone (13-14 Nov 2026), see flag 10._

Source of truth: decisions §5.20, §5.21, §5.13 pts 2 and 5, §5.14, §5.16, §5.17, §5.18, §5.7; design-core (wording); share-flow design §6 and §8. Where this doc and a decision differ, the decision wins. No decision was changed; items needing a view are in section 10.

## 1. What it is

One backend front door for Android share entry points. Share a WhatsApp message to the **Mini Me** shortcut: the app masks it, drafts a reply in Braj's voice, and the shortcut puts the draft on the clipboard with a notification. Braj pastes it in WhatsApp. Nothing is ever sent for him. The same service function is what Dev 7's web share target will call.

Pipeline (the existing one, unchanged): **mask locally → store masked → content limits → LLM guard → drafter → event log**. `OWN_DATA_ONLY_MODE` stays on (own / made-up text only until D3b / M2). Domain `club`, device `mobile`, `message_received.origin = shared`, mode `shadow`.

## 2. Endpoint contract (for Dev 7)

Base URL: your Render address, `https://<your-render-url>` (for example `https://mini-me.onrender.com`). All routes need `Authorization: Bearer <token>`. Auth runs **before** the body is read: a wrong, missing, malformed or revoked token gets **401** and nothing is stored or logged (tested).

### `POST /api/draft`

| Part | Detail |
|---|---|
| Body | The shared text. Accepted as raw `text/plain` (what the shortcut sends), JSON `{"text","title","url","channel"}`, or a form with the same fields. |
| Query | `format=text\|json` (default json; `Accept: text/plain` also selects text), optional `title`, `url`, `channel`. **The text itself is never read from the URL** (keeps it out of access logs). |
| Compose | A URL is appended to the text if not already in it (design §8 note 1). A title is used only when there is no text. Max `SHARE_MAX_CHARS` (4000) → 413; empty → 422; body over 64 KB → 413. |
| `channel` | `shortcut` (default). `web_share` is **reserved**: this token route answers 422. Dev 7 calls `app.share.service.create_share_draft(engine, writer, settings, drafter, text=..., channel="web_share")` from its cookie-authenticated route, so no token is needed there and no auth rule was decided for it. |

**Text response (`format=text`)** — for the shortcut:

| Case | Status | Body | Headers |
|---|---|---|---|
| Draft | 200 | just the draft, real values restored, ready to paste | `X-Mini-Me-Message-Id: sh-…`, `X-Mini-Me-Action: draft`, `X-Mini-Me-Confidence: High\|Medium\|Low` |
| Ask first | **409** | `Asked you first: …` plus one line of why | id, `X-Mini-Me-Action: ask_first` |
| Draft failed | 502 | `I couldn't draft this one: …` (same wording as Telegram) | id, `X-Mini-Me-Action: failed` |

Ask-first is deliberately **not 2xx** in text mode, so any client that copies "success" bodies to the clipboard never copies the reason.

**JSON response (`format=json`)** — for Dev 7:

```json
{ "message_id": "sh-3f9c1a7e0b2d4c55", "action": "draft",
  "draft": "…real values restored…", "confidence_label": "Medium", "confidence": 0.62,
  "why": "Short casual reply, like your other quick answers.",
  "masked_text": "Is the club meet on Friday? Call [PHONE]", "masked_count": 1,
  "unrestored_tokens": [], "contact_type": "unknown", "domain": "club", "channel": "shortcut",
  "shown_state": "shown", "confirm_within_hours": 24.0,
  "copied_path": "/api/draft/sh-3f9c1a7e0b2d4c55/copied" }
```
- Ask first: **200**, `"action": "ask_first"`, `"draft": null`, `"reason": {"code": "money|emotional|custom_topic|low_confidence", "headline": "Asked you first: …", "detail": "…", "topic": null|"fees"}`. `headline` names the trigger (§5.12 pt 2).
- Failed: **502**, `"action": "failed"`, `"error": "<code>"` (`provider_not_verified`, `rate_limited`, `llm_error`, `bad_output`, `error`), `"message"`.
- `masked_text` is what the model and the database saw; show that, not the raw text (§5.21 pt 2: incoming stays masked on screen). `confidence` is display-only.

### `POST /api/draft/{message_id}/copied`

Call it **after** the clipboard write has happened. Logs `draft_copied` and returns `{"ok": true, "message_id", "copy_no", "logged", "outcome": "waiting"}`. Copy never changes the outcome and never writes `draft_sent` / `draft_acted` (tested). 404: unknown id or not a share item; 409: nothing to copy (ask-first or failed). Optional `?copy_no=N` makes a retry idempotent; without it the server counts (1, 2, …). Key: `draft_copied:<message_id>:<copy_no>`.

**Why a second call (choice):** the shortcut script is the only place that knows the copy worked. Its `Run on Success` script copies, **verifies the clipboard now holds the draft** (`getClipboardContent()`, which is `null` when run in the background), and only then triggers the second shortcut. If the copy fails, no event is written. Simplest reliable option: no timers, no guessing, no server push.

### What Dev 7 can rely on
- Items open with a `telegram_threads` row (the 24 h window and sweep are Dev 2's, same code, one setting `TELEGRAM_CAPTURE_WINDOW_HOURS`). No capture = `outcome_unknown`, never scored. Ask-first items are swept the same way.
- `get_message(mid)` has `draft_text` **masked**. Real values exist only in the response of the request that made the draft. After that, copies of the draft show tokens until the D3b mapping exists (§5.21 pt 2). Dev 7's waiting list should show the masked draft or re-restore only if D3b has landed.
- Re-run after tagging (§5.21 pt 1) is **not built**. The first `autonomy_decision` event already carries `trigger: "initial"` and `rerun_of: null`; the re-run writes a second one with an explicit idempotency key (the default once-per-message key would block it).

## 3. Token handling

- Table `api_tokens` (new; `create_all` adds it on deploy). Stores `token_id` (`tk_…`), name, **SHA-256 hash**, first characters (for recognising it), created / last used / revoked times. The token is 32 random bytes (`mm_` + url-safe text), so a plain hash is enough. It is shown **once**, at creation.
- `python -m scripts.api_token create "<name>"`, `list` (never shows the secret), `revoke <token_id>`, `revoke --all`. Uses `DATABASE_URL` (Neon for production). Run it from your laptop (Neon); free Render instances have no shell.
- Revocation applies on the next request. A lost phone: revoke, create a new token, update the phone variable (section 8).
- No rate limit or lockout (a 256-bit random token is not guessable); flagged in section 10.

## 4. Events, fields, enums

New values (enums only unless noted; nothing else renamed):

| Field | New values |
|---|---|
| `channel` | `shortcut`, `web_share` |
| `contact_type` | `unknown` (§5.17 pt 1) |
| `outcome` | `not_replying` (§5.17 pt 4; was planned, now in the enum) |
| event names | `draft_copied`, `nudge_sent` (nothing sends it yet: Dev 7), `not_replying` |
| payload `origin` | `shared` (also documented as `Origin` enum) |
| payload `capture` | `share`, `paste` (enum only: `Capture`; Dev 7 uses them) |

Events written for a shortcut draft, in order: `message_received` (origin `shared`, `sender_type: unknown`, `masked_count`) → `draft_generated` (as Telegram, plus `blind_eligible: false`, `entry: shortcut`) → `autonomy_decision` (`draft_notify`, `autonomy_eligible: false`, reasons `draft_only_channel`, `untagged_sender`) → `draft_shown` (`surface: api_response`, logged when the response is returned). Ask first: `message_received` → `hard_limit_triggered` (`limit`, `source: content`) **or** `topic_triggered` (`topic`) → `autonomy_decision` (`ask_first`); no `draft_generated`, and no model call. Failed: `outcome_unknown` (`reason: draft_failed`). Later: `draft_copied`, then the capture events (`draft_acted` + `draft_sent` with `channel: shortcut`, or `not_replying`, or `outcome_unknown` from the sweep).

- **Never blind (§5.20 pt 4):** `shown_state` is always `shown`, no `draft_held_blind` is ever written, and `draft_generated.blind_eligible = false`. `/stats` keeps `shortcut` rows **out of the shown-vs-blind comparison** and prints them as "shared from phone, never blind N", so the anchoring gap is only computed where blind items exist.
- **No shadow "would have sent" (§5.21 pt 1):** an untagged / unknown sender gets draft & notify, never autonomy-eligible; `would_have_sent_shadow` is never written on this channel (tested).
- **Contact:** `contact_type = unknown`, a unique pseudonym per share (like pasted Telegram text), so it can never become "known".

## 5. Content-based hard limits (interim stand-in, FLAGGED)

The brief needs the ask-first path now but the autonomy engine (D22) is out of scope, and no limit detection existed. So `app/share/limits.py` is a small, conservative **keyword check on the masked text**, run before any model call (an ask-first message never reaches the provider):

| Limit | How |
|---|---|
| money | currency sign / code with an amount (₹, Rs, INR, $ …), payment words (pay, paid, payment, refund, reimburse, invoice, UPI, loan, salary, EMI, cheque), Hinglish (paisa, paise, rupaye). "fee(s)" alone is **not** a built-in (the decisions example makes it a custom topic). |
| emotional | a short word list (sad, anxious, depressed, grief, funeral, died, therapy, miss you, … plus a few Hinglish words) |
| your topics | `SHARE_ALWAYS_ASK_TOPICS="fees,attendance waiver"` (comma-separated, whole-word, case-insensitive). The reason names the topic. Only ever adds caution. |
| confidence below the floor | `SHARE_CONFIDENCE_FLOOR`. **Unset = off** (the value is deferred to post-testing, Q1). Wired and tested, not decided. If hit, the draft is generated but never stored or returned. |

Not applied: the contact-based limit (§5.21 pt 1). Keywords are not meaning-matching, so expect some misses and some false ask-firsts; replace with D22.

## 6. Capture for shortcut drafts until Dev 7's waiting list exists (Telegram)

Cheap enough, so built. A shared item gets a `telegram_threads` row for chat = `TELEGRAM_ALLOWED_USER_ID` (no schema change). In the bot:
- `/pending` lists shared drafts with a marker, then sends each one that has no card yet as a **draft card** with **[✓ Sent as is] [Discard] [Not replying]** and the usual Reply capture. A second `/pending` does not resend the card. Ask-first items are listed with "I asked you first, no draft" and have no card (`/sent #N` on one answers that nothing can be confirmed; a reply to an ask-first item is Dev 7's "share your reply").
- **`/sent #N <text>`** answers draft #N (any open item). Plain `/sent <text>` and the "plain paste" nudge still apply only to Telegram-native items, so a draft made in another app can never be captured by accident.
- **Not replying** exists now for shared cards only (button `nrep`, outcome `not_replying`, event `not_replying`, never scored, counted separately in `/stats`). The brief called it existing; it was planned (D2b / D4b). D4b should add it to every item and `/skip`.
- Captured events carry the record's own channel / domain / device (`shortcut` / `club` / `mobile`), not Telegram's. The classifier runs as for any capture. `time_to_action_ms` runs from when the draft was made.
- The card shows the **masked** draft (what is stored). Telegram already holds masked drafts, so nothing new leaves the app; real values are only ever on the phone's clipboard.
- Unconfirmed after the window: the existing sweep (every 10 min while awake, on every Telegram update, `POST /telegram/sweep`) → `outcome_unknown`. The sweep now tolerates a thread with no Telegram message or chat id.

## 7. Deploy and live check (Braj; PowerShell)

**Host: Render free (decision §5.22).** First do the Render and cron-job.org setup in `mini-me-dev-hosting.md` (sections 2 to 4), so that `https://<your-render-url>/healthz` answers. Then:

1. **Token.** In the repo folder with the env active (`.env` holds the Neon `DATABASE_URL`; Neon did not change):
   `python -m scripts.api_token create "phone shortcut" | Select-String '^mm_' | ForEach-Object { $_.Line } | Set-Clipboard`
   You should see no output; the token is on your clipboard and shown only once. `python -m scripts.api_token list` should show one `tk_…` row.
2. **Smoke test with a made-up message.**
   `$env:MINI_ME_TOKEN = (Get-Clipboard).Trim()` then `python -m scripts.share_smoke --url https://<your-render-url>`.
   You should see seven `[OK]` lines and `All checks passed.` (the first call after sleep can take up to a minute). Any `[FAIL]` line: copy it to me. Then `Remove-Item Env:MINI_ME_TOKEN`.
3. **Check the log in Telegram.** `/events` shows `message_received`, `draft_generated`, `autonomy_decision`, `draft_shown`, `draft_copied`. `/pending` shows the smoke drafts as cards: tap **Not replying** on each so test items stay out of your numbers.
4. **Sweep check.** In cron-job.org, open the job's history: runs every 10 minutes with HTTP 200.

## 8. Android setup: HTTP Shortcuts (phone)

**Rule until D3b / M2: share only your own or made-up messages (use WhatsApp "Message yourself"). The Mini Me entry appears for every message, so there is no technical block on sharing a real group message. Do not.**

I could not open the app from this session. The names below come from the app's documentation (variables, scripting and sharing pages); menu labels can differ slightly by app version. Where a label is not confirmed, it is marked *(label may differ)*. Tell me anything that does not match.

**A. Install.** Play Store (or F-Droid): "HTTP Shortcuts" by Roland Meyer. Open it once. You should see an empty shortcut list.

**B. Three variables.** Three-dot menu (top right) → **Global Variables** → **+** → **Static Variable**. Create:
1. Name `mini_me_token`. Value: paste the `mm_…` token. Tick **Treat value as secret** and **Exclude stored value from exports**. Save (✓ top right).
2. Name `message_id`. Leave the value empty. Save. (The script fills it in each time.)
3. Name `share_text`. Leave the value empty. Tick **Allow Receiving Value from Share Dialog**; in the dropdown that appears choose **text**. Save.
4. (Copy flow, needed for WhatsApp) Name `clip_text`. Type **Clipboard Content**. Save. In shortcut C step 4 use `clip_text` in the body instead of `share_text`.
You should see the variables in the list.

**C. Shortcut "Mini Me".** Back on the main screen: **+** → **Regular HTTP Shortcut** → **Create from scratch**.
1. Name: `Mini Me`.
2. Method **POST**. URL: `https://<your-render-url>/api/draft?format=text`.
3. Request headers → add: name `Authorization`, value `Bearer ` (with the space) then tap **{ }** and pick `mini_me_token`. You should see the variable appear in purple after `Bearer `.
4. Request body → type **Custom Text** *(label may differ)*; in the body field tap **{ }** and pick `share_text`; content type `text/plain` *(label may differ)*. You should see only the purple `share_text` placeholder in the body.
5. Request timeout: set **90 seconds** *(label may differ; the default is short and the first call after a long idle can take up to a minute on the free host)*.
6. Response handling: choose the option that shows **nothing** (silent) *(label may differ)*. The script below shows the result.
7. **Trigger & Execution Settings**: tick **Direct Share** (Android 11 or newer) so "Mini Me" appears in the share sheet. Do **not** choose "run in the background": Android blocks clipboard access in the background.
8. **Scripting** → **Run on Success**. Paste exactly:
```js
const draft = response.body;
const id = response.getHeader('X-Mini-Me-Message-Id');
const conf = response.getHeader('X-Mini-Me-Confidence') || '';
copyToClipboard(draft);
const now = getClipboardContent();
if (now !== null && now.trim() === draft.trim()) {
  showNotification('Draft copied' + (conf ? ' (' + conf + ' confidence)' : ''), draft);
  setVariable('message_id', id);
  executeShortcut('Mini Me copied');
} else {
  showDialog(draft, 'Mini Me: could not copy. Press and hold to copy this text.');
}
```
9. **Scripting** → **Run on Failure**. Paste exactly:
```js
if (response && response.statusCode == 409) {
  showDialog(response.body, 'Mini Me asks you first');
} else if (response && response.statusCode == 401) {
  showDialog('The token was refused. It may have been revoked. Create a new one and update mini_me_token.', 'Mini Me');
} else if (response) {
  showDialog(response.body, 'Mini Me could not draft');
} else {
  showDialog('No connection, or the server is still waking up. Share it again in a few seconds.', 'Mini Me');
}
```
10. Save (✓).

**D. Shortcut "Mini Me copied".** **+** → **Regular HTTP Shortcut** → **Create from scratch**. Name `Mini Me copied`. Method **POST**. URL `https://<your-render-url>/api/draft/` then **{ }** → `message_id`, then type `/copied`. You should see `…/api/draft/` + purple `message_id` + `/copied`. Header `Authorization` = `Bearer ` + `mini_me_token` (as above). No body. Response handling: nothing shown. Save. (It only runs when "Mini Me" calls it; it logs `draft_copied` after the copy worked.)

**E. Test with WhatsApp "Message yourself" (made-up message only).**
1. WhatsApp → new chat (pencil / chat icon) → your own name marked (You) = "Message yourself".
2. Type `Is the club meet on Friday at 5? Call Test on 9876543210` and send.
3. **WhatsApp has no Share item for text messages** (verified on Braj's phone, 13 Nov). Long-press the message → tap **Copy** → open the **Mini Me** shortcut from the home-screen icon (HTTP Shortcuts → shortcut → add to home screen). The shortcut reads the clipboard through the global variable `clip_text` (type Clipboard Content) instead of `share_text`. Copy first: if the clipboard still holds the previous draft, the new draft is made from that draft.
   You should see HTTP Shortcuts flash briefly, then a notification "Draft copied (… confidence)" with the draft text. First call after a long idle can take up to a minute; the 10-minute scheduler normally keeps the app awake.
4. In the same chat, tap the message box, long-press → **Paste**. You should see the draft (with the phone number as the real one, if the draft used it). Delete it or send it to yourself; nothing goes to anyone else.
5. In Telegram, send the bot `/events`: you should see `draft_copied`. Send `/pending`: the draft shows as a card. Tap **✓ Sent as is** or **Not replying**; you should see "✓ Recorded: sent as is" or "Marked: not replying".
6. Ask-first check: send yourself `Please pay Rs 500 for the hall` and share it the same way. You should see a dialog "Mini Me asks you first: this looks like it involves money." and **no** draft on the clipboard.

**F. Lost phone or leaked token.** On your laptop (PowerShell, repo folder, env active): `python -m scripts.api_token list` → find the row (name and `last used`) → `python -m scripts.api_token revoke tk_<id>` (or `python -m scripts.api_token revoke --all`). You should see "Revoked tk_… It stops working on the next request." Test: share again from the phone; you should see "The token was refused…". Then `python -m scripts.api_token create "phone shortcut"` and paste the new token into the `mini_me_token` variable (HTTP Shortcuts → menu → Global Variables → `mini_me_token`).  Free Render instances have no shell, so token commands run from your laptop (they talk to Neon directly).

**Troubleshooting.**
- *No "Mini Me" in the share sheet:* confirm **Direct Share** is ticked (Android 11+). Otherwise share to **HTTP Shortcuts** itself and pick the shortcut from its list.
- *WhatsApp has no Share item:* expected. Use the Copy flow in E3 (variable `clip_text`, home-screen icon). Share-sheet entry works only from apps that offer Share for text.
- *"could not copy" dialog:* Android blocks the clipboard when an app runs in the background. Make sure the shortcut is not set to run in the background.
- *Nothing happens / timeout:* the app may be waking from idle. Share again after 10 s. Check the Logs tab of the Render service.
- *Draft in English:* the self-model voice note controls this (see the drafting doc section 14), not this feature.

## 9. Tests (46 new here + 9 for hosting; 329 in total, all passing on SQLite)

Tokens: only the hash is stored, verify, revoke, revoke-all, name required, CLI create / list / revoke (the secret is never listed). Auth: wrong, missing, non-bearer, revoked all 401 with **nothing stored** (no records, events, threads; the text and phone number are in no table; the drafter is never reached); 401 before a bad body; copied call needs a token. Draft path: plain-text response is just the draft, record fields and event order, origin `shared`, contact `unknown`, no contact-based limit, **no shadow would-have-sent**, **never blind over 25 shares** and tagged `blind_eligible: false`, **masked before storage and before the model** (phone and email in no table), **real values only in the response**, ambiguous token left unrestored, JSON contract fields, Accept header, JSON / form / url / title bodies, empty / too long / reserved channel / bad JSON, text not read from the URL. Ask first: money (three phrasings), emotional, custom topic (names itself), ordinary group queries not blocked, confidence floor off then on, nothing to copy, drafter never called. Guard: the **real** Gemini client refuses with the mode off and nothing is sent; own-data mode declares the text own and the provider flags stay False; drafter crash and `rate_limited` are clean failures with `outcome_unknown`. `draft_copied`: not logged by the draft call, logged by the copied call only, outcome stays waiting, no `draft_sent` / `draft_acted`, copy again, idempotent retry, 404 / 409. Sweep: 23 h nothing, 25 h → `outcome_unknown` (never scored) for draft and ask-first, no-Telegram-chat share survives the sweep. `/pending`: card and buttons, no resend, Sent as is, Not replying (and a second tap), `/sent #N`, plain `/sent` untouched, ask-first listed without a card, no nudge. Stats: shortcut kept out of shown-vs-blind; ask-first-without-draft counted. Enums and the web-share service path.
**Not run:** tests on Postgres (same as Dev 1–5); the real model through `/api/draft` in the test suite (the live Render run used it). A local end-to-end run with the stub drafter on a real uvicorn server passed the smoke script (all seven checks).

## 10. Build-level choices and flags

**Build-level (recorded here):** `sh-<16 hex>` message ids; token = `mm_` + 32 random bytes, SHA-256 hash; text-mode ask-first is HTTP **409** while JSON is 200 with `action`; headers carry id / action / confidence; second-call design for `draft_copied` with clipboard verification; the shared service function is the shared front door (the token route is a thin wrapper); `draft_shown` is written when the response is returned; shared items reuse the Telegram thread table, window and sweep (no schema change); a title is used only when there is no text; unrestored-token reporting; domain from `SHARE_DEFAULT_DOMAIN` (default `club`, which differs from Telegram's `group_queries` until D6b renames it).

**Flags (product-level, for the Hub):**
1. **Hard-limit stand-in.** Keyword rules (money, emotional, your topics via env) stand in for D22 so the ask-first path exists now. Confirm it is acceptable for dogfood, or tell me to ship the endpoint with only the floor wired. Expect some misses and false ask-firsts (a false ask-first costs one retype; a miss costs a draft that should have been asked first, reviewed by Braj before pasting).
2. **Not replying** was described as existing; it was planned. Built minimally (shared cards, enum, event, stats line). D4b should generalise it and add `/skip`.
3. **No technical block on sharing real group messages** from the shortcut before D3b / M2 (own-data mode only declares). The only guard is Braj's rule. A technical guard (for example, require a token flag or a per-share confirm) is a product call.
4. **Real values in drafts** rely on un-numbered tokens today: a draft token is restored only if every value behind it is the same; otherwise it stays a token and is reported. Numbered tokens and the encrypted mapping (D3b) fix this. Names are not masked yet, so they are sent to the model and stored masked-as-written until D3b.
5. **Telegram `/pending` cards** put the masked draft into the Telegram chat (Telegram already holds masked drafts). Stated for the privacy record.
6. **Sweep now runs from an outside scheduler** (`POST /tasks/sweep`, §5.22), which also keeps the free instance awake; it needs the Telegram bot configured (else 503). Free-tier limits are in `mini-me-dev-hosting.md` section 5. Dev 7 confirm (design flag F5): the sweep covers held blind items; nothing counts held items in a nudge yet.
7. **"Sent as is" on a shared card is self-reported**, and `time_to_action_ms` starts when the draft was made, not when the card appeared (design F6).
8. **Domain value mismatch** persists: shortcut records use `club`, Telegram records `group_queries`. `/stats` does not group by domain, so nothing breaks now; D6b aligns.
9. **No token rate limit** (not needed for a 256-bit token on a single-user app).
10. **Verified on Braj's Android phone (13-14 Nov):** Copy-based drafting, notification "Draft copied (… confidence)", automatic second call logging `draft_copied` (same second as `draft_shown`), manual "Mini Me copied" run (`logged` true, `copy_no` 2), `/pending` cards, and the ask-first path (#48: `hard_limit_triggered`, no `draft_generated`). cron-job.org sweep returns 200 every 10 minutes. Not verified: Share-sheet entry (WhatsApp offers none for text). Also fixed during live test: `logged` flag on Postgres (commit c7ab208).
11. **`conftest.py` untouched.** The pending `OPENAI_COMPAT_REASONING_EFFORT` reset still needs Braj's OK; new tests clear their own `SHARE_*` variables. Run `pytest` with that variable unset.
12. **Dev 7 needs:** re-run after tagging (§5.21 pt 1), `nudge_sent` sender, web login route calling `create_share_draft`, restore after D3b, `share` / `paste` capture handling, blind 1 in 10 on `web_share`.

## 11. Files

New: `app/share/{__init__,tokens,limits,restore,service,router}.py`, `scripts/api_token.py`, `scripts/share_smoke.py`, `tests/test_share.py`, this doc.
Changed: `app/events/schema.py` (enums), `app/events/writer.py` (`not_replying` once per message), `app/privacy/masking.py` (+`mask_with_values`; `mask_text` unchanged), `app/db.py` (+`api_tokens`), `app/config.py` (+`SHARE_*`), `app/main.py` (router), `app/telegram/{bot,store,format}.py` (section 6), `app/results/stats.py` (never-blind line, ask-first-without-draft).
Commits on `main`: `63c7243` (enums), `134ecec` (tokens + CLI), `129d77f` (endpoint), `c411692` (Telegram capture), `58838b6` (stats), `e11ccb0` (tests), `eb616fa` (smoke script), `83f146b` (this doc), `c7ab208` (`logged` flag fix), `3c23d2e` (phone verification notes), `21bc5a7` (Render move: `/tasks/sweep`, `PUBLIC_BASE_URL`, `$PORT`, `tests/test_hosting.py`).

Config (Render environment variables; all optional): `SHARE_DEFAULT_DOMAIN` (club), `SHARE_MAX_CHARS` (4000), `SHARE_ALWAYS_ASK_TOPICS` (empty), `SHARE_CONFIDENCE_FLOOR` (unset = off). Set in the Render service's Environment tab (for example `SHARE_ALWAYS_ASK_TOPICS` = `fees`), then it redeploys.

---

## 12. Dev 7: the installable web app and capture (added 14 Nov)

Source: decisions 5.20, 5.21, 5.22, 5.23, 5.13, 5.15, 5.17, 5.18; design `mini-me-design-core.md` (Ink & Apricot) and `mini-me-share-flow-design.md`. Live: https://mini-me-bm81.onrender.com/app/

### 12.1 Phone test (Step 0, Braj's Android)
| Question | Result |
|---|---|
| (a) Does WhatsApp offer Share / Mini Me for message text? | **No.** No Share item and no "Select text" for text. B stays "Copy, then Mini Me". |
| (b) Can an HTTP Shortcuts Quick Settings tile read the clipboard? | **Yes** (Braj, 14 Nov): tile tap gives the "Draft copied" notification. **B's trigger is the Quick Settings tile** (enable "Allow triggering via Quick Settings Tile" in the shortcut's Trigger & Execution Settings, then add the tile in the Quick Settings panel). Home-screen icon stays as fallback. |
| (d) "Back to WhatsApp" (design §8 note 6) | First build (`window.close`): did not switch apps. Fixed (14 Nov, `2ffb594`): the button opens WhatsApp through an `intent://…package=com.whatsapp` link, with a recent-apps hint if the phone refuses. **Works on Braj's phone** (returns to WhatsApp; WhatsApp may show a harmless "Couldn't open this chat link" toast). Tried a launcher intent (`3a79981`) to avoid the toast: it opened the Play Store page on his phone, so reverted (`2005b2c`). |
| (c) Can an installed web app read the clipboard on a button tap? | **Yes.** C's entry screen is **Paste message**. A manual textarea is built as fallback. |

### 12.2 What is built
- **Web app** at `/app/`: manifest (standalone, `share_target` POST to `/share-target`), service worker (static files only; never caches API or text), vanilla JS, mobile first, Ink & Apricot (light and dark), self-hosted Fraunces and Hanken Grotesk. Existing single-user login (cookie `mm_session`). Shared text is embedded in the page only as escaped JSON, never shown before login, never stored in the browser (no localStorage, IndexedDB, or cache); shown with `textContent` only.
- **Screens:** sign in; home (Paste message + waiting list); Paste message (clipboard read on tap, manual fallback); share-target arrival; "Is this your reply?" (when a held item is open); drafting; **draft** (draft, confidence label, one-line why, big Copy, From: row); copied; noted; held; held-reply-received; reveal; ask first (with optional "share your reply"); draft failed (retry); offline (no queue, no retry); signed out; copy-first.
- **Test-mode guard (5.23 pt 3, `TEST_MODE`, default on):** incoming text is masked, used for the draft, **not stored** (`incoming_text` is null, `message_received.text_kept=false`). Label "Test mode — made-up or own messages only" on the web draft, the shortcut JSON/header (`X-Mini-Me-Test-Mode`), the Telegram card; the waiting list says "message not kept in test mode". Removed at M2 by setting `TEST_MODE=false`.
- **Copy-first guard (5.23 pt 1):** if the incoming text equals the last shown draft (masked or restored), nothing is created. Shortcut: HTTP 409 + `X-Mini-Me-Action: copy_first` (JSON: 200 with `action: copy_first`). Web: copy-first screen.
- **Copy event:** `draft_copied` is logged by `POST /app/api/items/{id}/copied` only after the browser clipboard write succeeds. Copy is not sent.
- **From: row (5.21 pt 1):** tagging a sender re-runs the decision server-side: a second `autonomy_decision` (`trigger=sender_tag`, `rerun_of=<first event_id>`); both are kept. Always draft-only. Content limits carry over from the first decision. New tables `sender_tags`, `item_tags`.
- **Blind sample (5.21 pt 4):** web share only, `WEB_BLIND_RATIO` (default 0.1). The held draft is stored masked server side and never goes to the browser until the reply is captured and the user taps "Show my draft" (`draft_released_after_reply`). Shortcut has none. Held items get no Telegram card.
- **Waiting list:** copied drafts and held drafts, soonest first, "Confirm within N h"; actions [Sent as is] [I edited it → paste] [Not replying]; outcomes are held 10 s with Undo then committed (`POST /app/api/confirm`). An edit identical to the draft counts as sent as is. 24 h unconfirmed → `outcome_unknown` via the existing sweep (covers held items; tested).
- **Ask first:** optional "share your reply" → `draft_acted` with outcome `user_authored`, no score.
- **Nudge:** `maybe_send_nudge` on the existing `/tasks/sweep`: once a day within 3 h after 20:00 IST, "N copied drafts waiting · Confirm within N h", a Review link to `/app/`, no message text, copied only (held excluded), logs `nudge_sent`.
- **Restored values:** real values are restored only in the response, from an in-memory 30-minute cache; DB and logs stay masked.

### 12.3 Events and fields added or used
`message_received` payload: `origin` (`shared`/`pasted`), `entry` (`share`/`paste`), `text_kept`, `retry_of`. `draft_generated`: `blind_eligible`, `shown_state`. `draft_shown`: surface `web_app`. `draft_held_blind`, `draft_released_after_reply`, `draft_copied` (`copy_no`), `draft_acted` (`capture`: `button`/`paste`/`share`), `draft_sent`, `not_replying`, `autonomy_decision` (`trigger`, `rerun_of`), `outcome_unknown`, `nudge_sent`, `session_started` (`entry_screen`). Channel `web_share`, device `mobile`.

### 12.4 B fixes
Copy-first guard (above). Test-mode label in the notification: HTTP Shortcuts "Run on Success" script reads header `X-Mini-Me-Test-Mode` and appends "Test mode — made-up or own messages only". The 409 copy-first message appears in the Run on Failure dialog.

### 12.5 Phone setup
1. Chrome → open `https://mini-me-bm81.onrender.com/app/` → sign in. 2. Chrome menu → **Install app** (uninstall the old temporary "MM test" first). 3. Share target: in any app that offers Share for text, pick **Mini Me**. 4. WhatsApp: long-press message → Copy → open Mini Me → **Paste message**. 5. Quick Settings tile (chosen B trigger): HTTP Shortcuts → Mini Me → Edit → Trigger & Execution Settings → tick **Allow triggering via Quick Settings Tile** → save; then pull down the panel twice → pencil → drag the HTTP Shortcuts tile in. Copy the message in WhatsApp, tap the tile. Home-screen icon is the fallback.

### 12.6 Limits
Never-copied drafts are not on the waiting list (superseded in Dev 7b: they now appear under "Not copied", section 13). No offline queue. Share target works only from apps that offer Share for text (not WhatsApp). The detail toggle is absent (D8b). "Back to WhatsApp" opens WhatsApp but not the specific chat (no deep link to a chat from a shared text). Own or made-up messages only until D3b.

### 12.7 Tests
`tests/test_web.py`: 45 new. **Total 374 passing** (329 before). Not run: Postgres, a real browser in the suite. Live on Braj's phone (14 Nov): paste, draft, copy, waiting list, not replying (batch commit), ask first, copy-first guard, test-mode label, Quick Settings tile, Back to WhatsApp. Telegram nudge verified live (15 Nov, after 20:00 IST). Not yet verified live: share target (no app offers it for text).

### 12.8 Commits
`dc0d409` guards and re-run, `a13efbb` capture, waiting list, tags, nudge, `e59656c` web app, `74952c6` tests, `2ffb594` Back-to-WhatsApp intent, one `session_started` per login, copy time on waiting cards; `591f0bc` Windows UTF-8 test fixes; `3a79981` launcher intent (reverted), `2005b2c` restore; clip-test `5afb12d`.

---

## 13. Dev 7b: sender-tag labels, New group, "Not copied" (added 15 Nov, decisions 5.24)

Source: decisions 5.24 (pts 1 to 3), 5.13 pts 2 and 6, 5.14 pt 7, 5.16, 5.17 pt 1 and 4, 5.21, 5.23; design-core (Ink & Apricot, wording). Nothing was cut. No decision was changed; items needing a view are in 13.8.

### 13.1 What was built
1. **Sender-tag labels are encrypted** (`sender_tags.label_enc`, Fernet, `app/privacy/crypto.py`). The label is masked for phones / emails, then encrypted. It is never sent to a model, never in an event or a log (events and the waiting list refer to a tag by id / pseudonym; the label is decrypted only for the signed-in user's own screens). Tests prove it is in no other table and not in the log output.
2. **Export my data / Delete everything** as functions plus a CLI (`app/privacy/data_controls.py`, `scripts/data_controls.py`). No Settings screen (U1 to U3). Tags are in the export (readable) and removed by delete.
3. **Unused tags are deleted after 30 days** (`SENDER_TAG_RETENTION_DAYS`, default 30) by the existing `POST /tasks/sweep` (`tags_deleted` appears in its reply when it deletes). "Used" = a tag was applied to an item (`last_used_at`); never-applied counts from creation.
4. **New group** chip next to **Someone new** on the From: row: `new_group` (own unique pseudonym, never becomes known, both decisions logged, content limits still apply). Someone new stays `new_individual`; untagged stays `unknown`. A label hint on the From: card: "Short labels like \u201cClub senior\u201d work best." (placeholder in the Add form too). **Telegram has no From: row**, so nothing was added there.
5. **"Not copied"** section on the waiting list (web-app drafts that were shown but never copied; held drafts, ask-first, failed and shortcut drafts are not in it). Quiet heading, no count chip, not in the nudge. Two buttons: **Replied my own way** (optional paste screen) and **Not replying**. Same 10 s Undo batch as Dev 7 (`POST /app/api/confirm`, new action `replied_own`). Untouched for 24 h: `outcome_unknown` by the existing sweep (tested).

### 13.2 Encryption, key and migration
- Keys: `DATA_ENCRYPTION_KEY` (one or more Fernet keys, comma-separated; the first encrypts). A key derived from `APP_SECRET` is always the last key: the app works with no dedicated key (and in tests) but **set one** (13.6). When a dedicated key appears, the next start re-encrypts every label under it automatically (`app/migrations.py`, idempotent); `python -m scripts.data_controls rewrap-tags` does the same by hand.
- **The 5.16 "encrypted pseudonym mapping" does not exist yet** (D3b), so there was no approach to copy. `app/privacy/crypto.py` is the helper D3b should reuse.
- **Migration of existing plaintext tags** (`app/migrations.py`, run by `db.init_db()` at every start and by `python -m app.migrate`): adds `label_enc`; encrypts each label and reads it back; only if all read back, overwrites the plaintext and **drops the `label` column**; sets `label_enc` NOT NULL on Postgres. One transaction on Postgres (all or nothing; if it fails the app still starts and logs it, `python -m app.migrate` shows the error). Idempotent. Tested on SQLite and on a real Postgres 16 (legacy table, run twice, export, purge, delete). **Limit:** Postgres may keep the old text in unused storage until autovacuum and Neon keeps short-term history, so old plaintext can linger a little; optional: run `VACUUM FULL sender_tags;` in the Neon SQL editor. Today's labels are own or made-up.
- Pseudonyms (HMAC of the label) are unchanged, so tags keep their identity. After a tag is purged, the pseudonym left in older message records cannot give the label back; retyping the same label gives the same pseudonym again (and "known" status if replies exist).

### 13.3 Export / Delete (CLI, laptop PowerShell, repo folder, env active, `.env` has `DATABASE_URL` and the same `DATA_ENCRYPTION_KEY` as Render)
- `python -m scripts.data_controls summary`: row counts per table, how many tag labels are readable with this key, whether a dedicated key is set.
- `python -m scripts.data_controls export --out my-data.json`: message records, events, self-model items, sender tags (labels readable), item-tag links, Telegram thread ids / timers, API-token metadata (never a token or hash). Logs `data_exported` (counts only).
- **Two delete commands (Braj's decision, 17 Nov; the self-model is the hardest part to rebuild):**
  - `python -m scripts.data_controls delete-activity --confirm "delete my activity"`: removes messages, events, sender tags, tag links and Telegram thread records. **Keeps the self-model**, API tokens and the login.
  - `python -m scripts.data_controls delete-self-model --confirm "delete my self-model"`: removes only the self-model items.
  - Each logs one `data_deleted` event with counts and a `scope` (`activity` / `self_model`), never content. The old `delete` command and the phrase "delete everything" are gone from the CLI. `delete_everything()` (both) remains as a function for a future Settings button.
- `python -m scripts.data_controls rewrap-tags`.

### 13.4 "Not copied": events and scoring
- Replied my own way: `draft_acted` (`action: discarded`, `capture: button|paste`, `reply_shared`, `was_copied`) + `draft_discarded` (`reason: replied_own_way`); outcome `discarded`, scorable (counts as a discard in `/stats`). With a pasted reply it is stored masked in `sent_text` and handed to the classifier. No `draft_sent`.
- **Classifier change (build-level, flagged):** `EditClassifier._skip_reason` now also reads a discard that carries a reply, so the classifier can learn from the gap between the unused draft and the reply (`edit_classified` payload has `outcome: discarded`). Stats ignore edit_class on discards, so the score stays a discard either way. A discard with no reply is still skipped. To undo: remove the `discard_with_reply` lines.
- Not replying: as before (`not_replying`, never scored). The same two actions are accepted server-side for copied drafts too, but only Not copied shows Replied my own way.
- The nudge counts copied drafts only (tested: 1 copied + 2 not copied gives "1 copied draft waiting"; only-not-copied sends nothing).

### 13.5 Tests
44 new in `tests/test_dev7b.py` (crypto, label encrypted and only readable through the app, label absent from every other table / events / logs / model requests, wrong key shows "(unreadable)", migration encrypt / idempotent / failure keeps plaintext / rerun finishes, re-wrap on key set, 30-day purge rules and the sweep, export / delete / CLI, New group, Not copied listing and actions, classifier on a discard with a reply, nudge). One Dev 7 test updated (it asserted never-copied drafts are not listed; 5.24 replaces that). **Total 421 passing** (374 before; 418 at first, +3 on 17 Nov for the two delete commands and the environment reset). `tests/conftest.py` now clears `OPENAI_COMPAT_REASONING_EFFORT` for every test (Braj's OK, 17 Nov), so the extra PowerShell line is no longer needed. Also checked the page script in a jsdom run (list, own-way screen, queue, From row, New group call). Not run: the suite on Postgres, a real browser on the phone.

### 13.6 Braj: set the key, deploy check (PowerShell, repo folder)
1. `git pull origin main` should end with a fast-forward to the Dev 7b commits (`7be1a4f` to `39d2335`, then the docs commit). Then `pip install -e .` (adds `cryptography`).
2. Make a key and put it on the clipboard: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | Set-Clipboard`. Nothing prints; the key is on the clipboard.
3. Render dashboard, your Mini Me service, **Environment**, **Add Environment Variable**: name `DATA_ENCRYPTION_KEY`, value = paste, **Save, rebuild, and deploy**. You should see a new deploy go to "Live".
4. In the repo, `notepad .env`, add a line `DATA_ENCRYPTION_KEY=` and paste the same key, save. (Keep it somewhere safe too: a lost key means tag labels cannot be read.)
5. `python -m app.migrate`: you should see `migrations: {'sender_tags': {'migrated': 0, 'dropped_plaintext_column': False}, ...}` if Render already migrated (or `migrated: N` if yours ran first). Then `python -m scripts.data_controls summary`: **tag labels readable with this key: N of N** and **dedicated DATA_ENCRYPTION_KEY set: yes**.
6. Tests: `python -m pytest`. You should see `421 passed` (no need to clear `OPENAI_COMPAT_REASONING_EFFORT` any more).
7. Optional: `python -m scripts.data_controls export --out $HOME\mini-me-export.json` (you should see counts and the privacy line). **Do not run `delete-activity` or `delete-self-model`** unless you mean to wipe that data.
8. The cron-job.org job needs no change.

### 13.7 Phone checks (made-up messages only; Mini Me app on the phone)
0. Wake and refresh: open the app (or `https://mini-me-bm81.onrender.com/healthz`, you should see `{"ok":true...}`), then close the app fully and reopen it. If the From: row below has no "New group", the deploy has not finished: wait a minute, close and reopen.
1. WhatsApp "Message yourself": send `Is the club meet on Friday at 5?`, long-press, Copy, open Mini Me, **Paste message**. You should see the draft screen, a **From:** card with the hint "Short labels like \u201cClub senior\u201d work best." and chips **Someone new**, **New group**, **+ Add**.
2. Tap **New group**: the chip shows a tick and the note "Tagged. I looked again; this draft stays as it is." Tap it again: "Tag cleared."
3. Tap **+ Add**, type `Club senior`, leave **Group** selected, **Save tag**. You should see a chip `✓ Club senior`.
4. Do **not** tap Copy. Go back to the app home. Under **Waiting for you** you should see, below a quiet **Not copied** heading, a card with the draft and two buttons **Replied my own way** and **Not replying**. The chip at the top should not count it ("Nothing waiting" or "All caught up").
5. Tap **Replied my own way**: a screen "You replied your own way" with an optional box. Paste a made-up reply (or leave it empty), tap **Save**. You should see `✓ Saved: you replied your own way · Club senior` with **Undo**. Tap Undo within 10 s: the card comes back. Do it again and wait 10 s (or tap Done).
6. Make another draft, leave it uncopied, tap **Not replying** on its card: `– Marked not replying`.
7. Make a third draft and tap **Copy draft**: it appears in the main list as **Copied**, not under Not copied.
8. Telegram `/events`: for step 5 you should see `draft_acted` (discarded) and `draft_discarded`; with a pasted reply, maybe an `edit_classified` line. `/stats`: **Discarded** goes up by one.
9. After 8 pm IST with one copied and one not-copied draft waiting, the nudge should say "1 copied draft waiting", not 2.
10. After 24 h, an untouched Not copied card disappears and `/events` shows `outcome_unknown`.
(The 30-day tag deletion cannot be waited for; it is tested.)

### 13.8 Choices and flags
**Phone-verified (15 to 16 Nov, Braj):** New group and label hint, Not copied list, Replied my own way (#71: `draft_acted` discarded, `draft_discarded`, `edit_classified`; Discarded 9 to 10), Not replying, copied draft stays in the main list, Undo, 24 h `outcome_unknown` for a not-copied draft (#73) and a copied one (#72), `nudge_sent` logged at 20:01 on 16 Nov. The nudge text ("1 copied draft waiting") was not seen in a screenshot yet.
**Build-level:** label encryption with a fallback key plus automatic re-wrap; migration drops the plaintext column; `/tasks/sweep` runs the purge; New group sends `new_group: true` (no saved tag, no label); `replied_own` is one extra confirm action; Export / Delete are functions + CLI; export includes events and logs `data_exported` / `data_deleted` with counts only; classifier reads discards that carry a reply; "Not copied" has no grace period (a draft you are still looking at shows there once you leave the screen).
**Flags (product-level):**
1. **Classifier on discards: DECIDED, keep (Braj, 17 Nov).** It classifies an unused draft against the user's own reply (a discard with a pasted reply); the score stays a discard. Hub: treat these labels as a different kind of signal from edits of a sent draft, and update Dev 4's "discards have no edit class". Seen live: #71 got `edit_classified class=decision`.
2. **5.16 encrypted pseudonym mapping is not built**; this doc's helper is the first encryption in the app. Tag labels are encrypted at rest only (the key sits in Render's env, the database in Neon).
3. **What "Delete everything" covers: DECIDED, two commands (Braj, 17 Nov).** Activity and self-model are deleted separately (13.3). Hub: a future Settings screen (U1 to U3) should offer the same two actions, and say what "Delete everything" means if it is ever one button (the function `delete_everything` does both).
4. **"Known" survives a tag purge** through the pseudonym kept in old message records (not reversible, but the same label re-typed becomes known again).
5. Export contains decrypted labels (the user's own export).
