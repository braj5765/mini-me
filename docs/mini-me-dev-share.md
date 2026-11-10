# Mini Me — Dev 6: Share-to-draft backend + Android shortcut (§5.20 pt 7 steps a and b)

_Built 10 Nov 2026, host moved 11 Nov 2026 · Spoke: Dev 6 · Status: code on `main` (commits `63c7243` … see section 11), **329 tests passing on SQLite** (274 before + 46 in `tests/test_share.py` + 9 in `tests/test_hosting.py`), run end to end locally against a real HTTP server with the stub drafter. **Hosting is now Render free (decision §5.22; see `mini-me-dev-hosting.md`); Fly.io is no longer used.** Not yet deployed or run on a phone: this session cannot reach Render or an Android phone. Setup, live check and the phone steps are for Braj (sections 7 and 8)._

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
You should see three variables in the list.

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
3. Long-press that message → three-dot menu → **Share** *(if you cannot find Share, see Troubleshooting)* → **Mini Me**.
   You should see HTTP Shortcuts flash briefly, then a notification "Draft copied (… confidence)" with the draft text. First call after a long idle can take up to a minute; the 10-minute scheduler normally keeps the app awake.
4. In the same chat, tap the message box, long-press → **Paste**. You should see the draft (with the phone number as the real one, if the draft used it). Delete it or send it to yourself; nothing goes to anyone else.
5. In Telegram, send the bot `/events`: you should see `draft_copied`. Send `/pending`: the draft shows as a card. Tap **✓ Sent as is** or **Not replying**; you should see "✓ Recorded: sent as is" or "Marked: not replying".
6. Ask-first check: send yourself `Please pay Rs 500 for the hall` and share it the same way. You should see a dialog "Mini Me asks you first: this looks like it involves money." and **no** draft on the clipboard.

**F. Lost phone or leaked token.** On your laptop (PowerShell, repo folder, env active): `python -m scripts.api_token list` → find the row (name and `last used`) → `python -m scripts.api_token revoke tk_<id>` (or `python -m scripts.api_token revoke --all`). You should see "Revoked tk_… It stops working on the next request." Test: share again from the phone; you should see "The token was refused…". Then `python -m scripts.api_token create "phone shortcut"` and paste the new token into the `mini_me_token` variable (HTTP Shortcuts → menu → Global Variables → `mini_me_token`).  Free Render instances have no shell, so token commands run from your laptop (they talk to Neon directly).

**Troubleshooting.**
- *No "Mini Me" in the share sheet:* confirm **Direct Share** is ticked (Android 11+). Otherwise share to **HTTP Shortcuts** itself and pick the shortcut from its list.
- *WhatsApp has no Share item:* select the message, tap **Copy**, then use a shortcut that reads the clipboard instead (HTTP Shortcuts has a "Clipboard Content" variable type; use it in place of `share_text`). Tell me and I will give exact steps.
- *"could not copy" dialog:* Android blocks the clipboard when an app runs in the background. Make sure the shortcut is not set to run in the background.
- *Nothing happens / timeout:* the app may be waking from idle. Share again after 10 s. Check the Logs tab of the Render service.
- *Draft in English:* the self-model voice note controls this (see the drafting doc section 14), not this feature.

## 9. Tests (46 new here + 9 for hosting; 329 in total, all passing on SQLite)

Tokens: only the hash is stored, verify, revoke, revoke-all, name required, CLI create / list / revoke (the secret is never listed). Auth: wrong, missing, non-bearer, revoked all 401 with **nothing stored** (no records, events, threads; the text and phone number are in no table; the drafter is never reached); 401 before a bad body; copied call needs a token. Draft path: plain-text response is just the draft, record fields and event order, origin `shared`, contact `unknown`, no contact-based limit, **no shadow would-have-sent**, **never blind over 25 shares** and tagged `blind_eligible: false`, **masked before storage and before the model** (phone and email in no table), **real values only in the response**, ambiguous token left unrestored, JSON contract fields, Accept header, JSON / form / url / title bodies, empty / too long / reserved channel / bad JSON, text not read from the URL. Ask first: money (three phrasings), emotional, custom topic (names itself), ordinary group queries not blocked, confidence floor off then on, nothing to copy, drafter never called. Guard: the **real** Gemini client refuses with the mode off and nothing is sent; own-data mode declares the text own and the provider flags stay False; drafter crash and `rate_limited` are clean failures with `outcome_unknown`. `draft_copied`: not logged by the draft call, logged by the copied call only, outcome stays waiting, no `draft_sent` / `draft_acted`, copy again, idempotent retry, 404 / 409. Sweep: 23 h nothing, 25 h → `outcome_unknown` (never scored) for draft and ask-first, no-Telegram-chat share survives the sweep. `/pending`: card and buttons, no resend, Sent as is, Not replying (and a second tap), `/sent #N`, plain `/sent` untouched, ask-first listed without a card, no nudge. Stats: shortcut kept out of shown-vs-blind; ask-first-without-draft counted. Enums and the web-share service path.
**Not run:** tests on Postgres (same as Dev 1–5); the real model through `/api/draft`; any phone. A local end-to-end run with the stub drafter on a real uvicorn server passed the smoke script (all seven checks).

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
10. **HTTP Shortcuts UI labels and the `getClipboardContent` check are unverified on a device.** The scripts follow the app's documentation (response object, `copyToClipboard`, `getClipboardContent`, `executeShortcut`, `showNotification`, `showDialog`, `setVariable`).
11. **`conftest.py` untouched.** The pending `OPENAI_COMPAT_REASONING_EFFORT` reset still needs Braj's OK; new tests clear their own `SHARE_*` variables. Run `pytest` with that variable unset.
12. **Dev 7 needs:** re-run after tagging (§5.21 pt 1), `nudge_sent` sender, web login route calling `create_share_draft`, restore after D3b, `share` / `paste` capture handling, blind 1 in 10 on `web_share`.

## 11. Files

New: `app/share/{__init__,tokens,limits,restore,service,router}.py`, `scripts/api_token.py`, `scripts/share_smoke.py`, `tests/test_share.py`, this doc.
Changed: `app/events/schema.py` (enums), `app/events/writer.py` (`not_replying` once per message), `app/privacy/masking.py` (+`mask_with_values`; `mask_text` unchanged), `app/db.py` (+`api_tokens`), `app/config.py` (+`SHARE_*`), `app/main.py` (router), `app/telegram/{bot,store,format}.py` (section 6), `app/results/stats.py` (never-blind line, ask-first-without-draft).
Commits on `main`: `63c7243` (enums), `134ecec` (tokens + CLI), `129d77f` (endpoint), `c411692` (Telegram capture), `58838b6` (stats), `e11ccb0` (tests), `eb616fa` (smoke script), `83f146b` (this doc), `21bc5a7` (Render move: `/tasks/sweep`, `PUBLIC_BASE_URL`, `$PORT`, `tests/test_hosting.py`).

Config (Render environment variables; all optional): `SHARE_DEFAULT_DOMAIN` (club), `SHARE_MAX_CHARS` (4000), `SHARE_ALWAYS_ASK_TOPICS` (empty), `SHARE_CONFIDENCE_FLOOR` (unset = off). Set in the Render service's Environment tab (for example `SHARE_ALWAYS_ASK_TOPICS` = `fees`), then it redeploys.
