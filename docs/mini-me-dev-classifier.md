# Mini Me — Dev 4: Edit classifier + results view (D9a, D11a, v0.1 parts)

_Built 9 Nov 2026 · Spoke: Dev 4 Classifier + results view · Status: **code complete, 261 tests passing on SQLite, pushed to `main`, deployed to Fly, checked by Braj on the real bot on 9 Nov.** Checks run: offline loop (29 made-up messages, all plumbing checks OK), live loop on the real model (all OK), Telegram `/help`, Sent as is, blind reply with edit (note shown), Discard, `/stats`, `/events`, `/events N`, and the Neon count query. **No open items: the 4 older edits were backfilled and `/stats` re-checked** (section 15). **M1 complete.** Checklist in section 10; test-run notes in section 15._

Source of truth: decisions 4 step 3, 5.4, 5.7, 5.10 mapping, 5.12, 5.13 pts 2/4/5, 5.15, 5.17, 5.18; foundation, Telegram and drafting docs; timeline section 3 (v0.1 rules, cut order, M1) and tasks D9a / D11a. Where the PRD differs, decisions win. No decision was changed; items that need a view are in section 14.

## 1. What was built

- **D9a Edit classifier** (`app/classifier/`). Plugged into the existing `on_captured(message_id, capture)` hook. Compares the masked draft with the masked sent text and records one of `none` (sent as is), `cosmetic`, `voice`, `fact`, `decision`, plus the full list of classes seen. Logs `edit_classified` with `classifier_version` and updates the message record.
- **D11a Results view** (`app/results/`). Telegram `/stats` (counts, class mix, shown vs blind, north-star readout, data-mode labels) and a read-only event table as Telegram `/events` (latest steps) and `/events 7` (every step of draft #7).
- **Tools:** `scripts/run_m1_loop.py` (the whole M1 loop on the made-up messages; offline or `--live`), `scripts/classify_pending.py` (classify edits captured while the model was down).
- **Small bot changes (flagged):** the `on_captured` hook may now return a one-line note that the bot sends; new `/events` command and `events_provider` hook; `/help` wording. Nothing else in the bot changed.

## 2. Code map

| File | Role |
|---|---|
| `app/classifier/rules.py` | No-model part: class precedence, never-light, text normalisation, cosmetic-only test, placeholder (token) diff |
| `app/classifier/classifier.py` | `EditClassifier` (the hook; `.classify()` is the pure core), the model call, retry, guard handling, `classify_pending()` |
| `app/classifier/prompts/classifier-v0.1/{system,user}.md` | Versioned prompt files; `prompts/README.md` lists versions |
| `app/results/stats.py` | `compute_stats()`, `format_stats()`, `make_stats_provider()` |
| `app/results/event_view.py` | `/events` text, `make_events_provider()` |
| `app/events/queries.py` | Read-only helpers (draft payloads, records, events) shared by both |
| `app/telegram/{bot,router,format,store}.py` | Hook note, `/events`, wiring, help text, `ThreadStore.numbers()` |
| `app/config.py` | `CLASSIFIER_*` settings (section 8) |
| `tests/test_classifier.py` (60), `tests/test_results.py` (18) | Section 9 |

## 3. Classifier flow (one captured message)

1. Read the record. **Skip** if it must not be classified or scored (table below).
2. **Same text** as the draft (whitespace aside) → `none`, no model call.
3. **Only case, spacing, punctuation or emoji differ** → `cosmetic` from rules, no model call.
4. Otherwise **one model call** (temperature 0) returns every class it sees, e.g. `["voice","cosmetic"]` and a one-line reason. Junk output gets one retry, then a clean failure.
5. **Code adds `fact`** whenever a placeholder differs between the two texts (`[phone 1]` vs `[phone 2]`, `Person A` vs `Person B`, present in only one). So a changed token is a fact edit even if the model says "voice" (5.17 pt 3).
6. **`edit_class` = heaviest class**: decision > fact > voice > cosmetic. The full list stays in the payload.
7. Log `edit_classified` (once per message), then write `edit_class` + `classifier_version` on the record. The outcome is not touched.
8. The bot sends a one-line note ("Edit read as a wording change (also cosmetic)."), unless `CLASSIFIER_NOTIFY=false` or the edit was sent as is.

**Class rules (5.4, as given to the model):** *decision* = stance changed (yes/no, accept/decline, promise made or withdrawn; also non-committal ↔ firm answer, see flag 1 in section 14). *fact* = a date, time, name, number, amount, place, link or placeholder differs, was added or removed. *voice* = same stance and facts, different wording, tone, length or language mix. *cosmetic* = greeting, sign-off, punctuation, capitals, emoji, spacing, typo fix.

**Light / heavy.** The flag stays a placeholder: `light_or_heavy: null` on every event, `rules.BOUNDARY_SET = False`. Decision and fact edits carry `never_light: true` (5.13 pt 4). Nothing is inferred beyond that.

**Skip / exclusion table** (no model call, no event, `edit_class` stays empty; reason is logged without text):

| Case | Reason code |
|---|---|
| No record, or no draft / sent text | `no_record`, `no_text` |
| Outcome unknown | `outcome_unknown` |
| Outcome `not_replying` (when D4b builds it) | `outcome_not_replying` |
| Waiting, including a **blind draft whose reply has not come** | `blind_before_reply` / `outcome_waiting` |
| **Discarded** (an outcome, not an edit: the bot never calls the hook for discards, and a direct call is skipped) | `outcome_discarded` |
| `user_authored` (ask-first reply), `not_me` | `outcome_user_authored`, `outcome_not_me` |
| **Stub draft** (`draft_generated.is_stub`) | `stub` |
| No `draft_generated` event for the message | `no_draft_event` |
| Already classified (retries, redelivery) | `already_classified` |

A blind message **is** classified, once the user's reply is in (the bot fires the hook after the reply, and the held draft is released).

**Model-reported "items used"** from the draft event are copied into the payload as `items_used_hint` (layer + item id only). Nothing here uses them for a decision.

## 4. Event and record

`edit_classified` (once per message, key `edit_classified:<message_id>`). Common fields: `classifier_version` (`clf-0.1`), channel, domain, contact_type, mode, `self_model_version` (from the draft). Payload:

`edit_class`, `classes` (heaviest first), `light_or_heavy` (null), `never_light`, `method` (`exact` | `rules` | `llm` | `llm+rules`), `capture`, `token_changes` ({removed, added} or null), `outcome`, `shown_state`, `draft_confidence`, `drafter_version`, `data_mode`, `items_used_hint`, and for model calls `prompt_version`, `provider`, `model`, `llm_ms`, `model_classes`, `model_reason` (one line), `declared`, and `retries` / `fallback` / `truncated` when relevant. No message text is stored in this event.

Record: `edit_class` (`none` for sent as is) and `classifier_version` are set. `outcome` and `scorable` are left as the bot set them (scorable still follows the outcome, `unknown` never scorable). **A model failure leaves `edit_class` empty and logs no event**; /stats shows these as "Edited but not classified yet" and does not count them; `python -m scripts.classify_pending` (or a later run) fills them in.

## 5. Prompt and version

| Item | Value |
|---|---|
| `classifier_version` (prompt + rules, logged) | `clf-0.1` |
| Prompt folder | `classifier-v0.1` (`system.md`, `user.md`); a rules change bumps `clf-` even if the file does not change |
| Model | The same layer as the drafter: `CLASSIFIER_PROVIDER` / `CLASSIFIER_MODEL` if set, else `LLM_PROVIDER` / `LLM_MODEL` (today Groq `openai/gpt-oss-120b`). Temperature 0. `CLASSIFIER_MAX_TOKENS=1500` (reasoning headroom) |
| Texts sent | The masked draft and the masked sent text only. **The incoming message is not sent** (less third-party text; not needed for 5.4). Each text is cut at 2,000 characters (`truncated` is logged). Closing tags typed into the texts are neutralised and the prompt says to ignore instructions inside them |

**First real-model runs done (9 Nov, small samples, plumbing and behaviour only, no accuracy claim):** the live M1 loop and one real edit on Fly (section 15). Treat `clf-0.1` as lightly tried until the D9b spot-check.

## 6. LLM guard and own-data mode (5.18)

The guard in `app/llm/base.py` is unchanged. The call is declared own / made-up **only when `OWN_DATA_ONLY_MODE` is on and that draft was itself made under it** (`data_mode = own_or_made_up`). A row without that label (older rows, or real text later) is sent as third-party text and the guard refuses an unverified provider (`provider_not_verified`; the edit stays unclassified). At M2, when the mode is removed, delete the `own = ...` line in `EditClassifier._ask_model`; the call then goes through the guard like any real text.

## 7. /stats and /events

**/stats definitions** (`app/results/stats.py`; all-time, v0.1 only; weekly windows are D15):

- **Scored** = sent as is + edited **and classified** + discarded + flagged not-me, for real (non-stub) drafts. Shown vs blind counts are over scored rows.
- **Never in a denominator** (counted separately): outcome unknown (with reason), not replying, waiting (blind-held counted), ask-first / user-authored, edits not classified yet, stub drafts, messages with no draft (draft failed).
- **Edit-class mix**: counted by the heaviest class per message.
- **Light / heavy**: both print "not set yet". What is known is shown: decision + fact edits ("never light") and voice + cosmetic edits ("could be light or heavy").
- **North star** (5.7, 5.13): share handled with no or light edits, **as a range** because the boundary is unset: low = sent as is only; high = sent as is + every voice and cosmetic edit. Discards stay in the denominator. With nothing scored it says so and prints no percentage.
- **Data mode**: rows are grouped by the draft's `data_mode`. Own / made-up rows sit under "OWN / MADE-UP DATA - a demo of the loop, not results"; third-party rows under their own heading; older rows with no label under "no data mode". Every output starts "These are a demo of the loop, not results: no accuracy is claimed."

Example (offline run: 29 made-up messages, **scripted stand-in model, so the numbers show plumbing, not results**):
```
OWN / MADE-UP DATA - a demo of the loop, not results (29 messages)
Scored: 28 (shown 22 · blind 6)
• Sent as is: 6
• Light edits: not set yet
• Heavy edits: not set yet
  Edited in total: 19 - decision 15 · fact 0 · voice 4 · cosmetic 0
  (heaviest class per message; decision and fact are never light: 15; voice and cosmetic could be light or heavy: 4)
• Discarded: 3
No-or-light-edit share (north star): 21% to 36% (6 to 10 of 28). A range because light vs heavy is not set yet (Q1a).
Shown vs blind: shown 22 scored, 6 as is · blind 6 scored, 0 as is
Not scored: outcome unknown 1 (window expired 1) · not replying 0 · waiting 0
```

**/events** (the read-only event table; Telegram only because v0.1 has no web screens, and the web log is D11b / D18): `/events` = latest 12 events, `/events 7` = every step of draft #7. Only whitelisted fields are shown (versions, counts, labels, classes). No message text, no model reason. Times are UTC.

## 8. Config (Fly secrets / env)

| Setting | Default | Notes |
|---|---|---|
| `CLASSIFIER_PROVIDER`, `CLASSIFIER_MODEL` | blank | Blank = same model as the drafter |
| `CLASSIFIER_MAX_TOKENS` | 1500 | Raise if a reasoning model returns empty content (shows as `bad_output`) |
| `CLASSIFIER_NOTIFY` | true | One-line "Edit read as ..." note after an edit; `false` = quiet |

No new secrets. `OWN_DATA_ONLY_MODE`, `LLM_*` and the provider key already on Fly are reused.

## 9. Tests (261 in total, all passing on SQLite; 78 new)

Rules: precedence (heaviest-class rule), never-light, normalisation (case, punctuation, emoji, Devanagari, `3.5` ≠ `35`), token diff (numbered tokens, `Person A/B`, case ignored, added / dropped). Classifier: sent as is (no call), cosmetic by rules (no call), each class from the model, **token-change fact edit even when the model says voice**, heaviest class and full list kept, empty reply fallback, junk retry, two bad replies leave it unclassified, provider error then catch-up, **exclusions** (unknown, discarded, not_replying, waiting, **blind before reply**, user_authored, **stub**, no draft event, empty texts), blind classified after the reply, idempotent (one event, one call), items-used hint changes nothing, **guard** (refuses unverified provider with own-data mode off; own-data mode only for rows made under it), prompt wrapping, event fields and key. In the bot: button as is, reply with edit (+ note), discard not classified, blind reply, model failure does not block capture, notify off. Results: **/stats arithmetic on a hand-built set** (counts, shown vs blind, north-star range, exclusions of stub / no-draft / unknown / not-replying / waiting / ask-first / user-authored / unclassified), data-mode blocks and order, labels, no-data and nothing-scored states, message length, wording; `/events` (latest, per draft, no text leaked, bad arguments); and a smoke test of `run_m1_loop`. Not run on Postgres (same as Dev 1-3).

## 10. M1 exit criteria, one by one (timeline section 5)

| Criterion | Status |
|---|---|
| Forward a message to the Telegram bot | Done earlier (Dev 2), tested on real Telegram 6 Nov |
| Get a draft in your voice with confidence and a one-line rationale | Done earlier (Dev 3), run live on Fly 8-9 Nov |
| Tap "Sent as is" or paste the final text | Done earlier (Dev 2: button, Reply, `/sent`) |
| **See the edit classified** | **Done, checked live on Fly 9 Nov:** draft #33 (blind) got the note "Edit read as a decision change (also voice, cosmetic)" 2 s after the reply, and `edit_classified` shows in `/events` (method llm, gpt-oss-120b). See the open item in section 15 |
| **See every step in the event log** | **Done, checked live:** `/events` and `/events 7` show every step from message_received to edit_classified; a discard stops at draft_discarded; stub draft #2 has no classification, as designed |
| **/stats shows the counts** | **Done, checked live:** the numbers add up (section 15) |
| Masking is on | Done (D3a): phones and emails only; extended masking is D3b |
| It runs on hosting, not only on a laptop | **Done:** this build is deployed on Fly and answered `/help`, `/stats`, `/events` and classified an edit |
| Never cut: event logging, masking, own / made-up data, classifier version logging | All kept |
| Cut order (event table first) | Not needed; the event table is in |
| ~30 made-up messages end to end | **Done:** 29 messages offline (scripted model) and live on the real model, all six plumbing checks OK both times |

## 11. Steps for Braj (Windows PowerShell)

1. Open PowerShell, go to the repo and activate the Python 3.12 environment you used before:
   `cd <your path>\mini-me` then `conda activate <your env name>`
2. Get the code and check it: `git pull origin main`, then `pip install -e ".[dev]"`, then `python -m pytest`. Expect `261 passed`.
3. Plumbing run (no model, no network): `python -m scripts.run_m1_loop --out m1_offline.md`. All "Plumbing checks" must say OK.
4. Live run (real model; made-up messages only; about 10 minutes at 20 s per message). Your `.env` must have `LLM_PROVIDER`, `LLM_MODEL` and the key as for the batch runner. Optional first: `$env:OPENAI_COMPAT_REASONING_EFFORT='low'`. Then:
   `python -m scripts.run_m1_loop --live --pause 20 --out m1_live.md`
   It uses a throw-away SQLite file, never Neon. Open `m1_live.md`: read each draft / sent / class row and note where the class looks wrong. If rows end `bad_output` or unclassified, run again with `$env:CLASSIFIER_MAX_TOKENS='2500'`.
5. Deploy: `fly deploy`, then `fly status` and `fly logs`. No new secrets. (Optional: `fly secrets set CLASSIFIER_NOTIFY=false` to keep the chat quiet.)
6. In Telegram, with made-up messages: `/help`; forward a message, tap **Sent as is**; forward another and Reply with a changed text (you should get "Edit read as ..."); try a Discard; then `/stats` and `/events` (and `/events 3`).
7. Optional Neon check (SQL editor): `select edit_class, outcome, count(*) from message_records group by 1,2 order by 3 desc;`. The 19 stub rows from 6 Nov are left out of `/stats` automatically.
8. If a message shows "not classified yet" (free-tier limit), run `python -m scripts.classify_pending` from the laptop with `DATABASE_URL` pointing at Neon.

## 12. Build-level choices (to record)

- Class precedence **decision > fact > voice > cosmetic**; `edit_class` = heaviest, full list in the payload.
- `classifier_version` = `clf-0.1` names prompt + rules; prompt folder `classifier-v0.1`.
- Sent as is → `edit_class none` **and** an `edit_classified` event (so every captured message has one); discards have no edit class (the schema has none; they are an outcome).
- Cosmetic-only edits decided by rules, not by the model; a changed placeholder always adds `fact` in code.
- Model sees only the two masked texts, not the incoming message; temperature 0; one retry; failure leaves the edit unclassified (no event, no guess).
- Empty model reply on a real text difference → `cosmetic`, flagged `fallback: empty_model_reply`.
- Stub rows and rows with no draft event are skipped entirely (not classified, never scored; /stats only lists them under "Left out of everything above").
- North star shown as a **range** while light / heavy is unset; light / heavy rows read "not set yet"; stats grouped by data mode; all-time.
- `/events` in Telegram as the v0.1 event table; hook note; `CLASSIFIER_NOTIFY`; classifier model defaults to the drafter's.
- `EditClassifier` runs inside the webhook request (like the drafter); the "Sent as is" and Saved messages go out before the model call.

## 13. Interfaces for later tasks

**D9b (finish classifier).** `rules.light_or_heavy()` and `rules.BOUNDARY_SET` are the one place the boundary plugs in; the stats code reads `BOUNDARY_SET` through the placeholder text only, so it needs a small change then (one number instead of a range). The decision-vs-reversed rule is a signal (5.12) and needs the incoming message and the sent history, which the classifier does not read today. A spot-check set can call `EditClassifier(engine, writer, llm=...).classify(mid)` or the pure pieces in `rules.py`. **Re-classifying under a new `classifier_version` is not possible with the current once-per-message event key** (`edit_classified:<message_id>`): D9b needs a versioned key (and to decide what a re-run does to the record).
**D10 (shadow mode).** Uses `shown_state` (shown / blind) and the same hook. `outcome_unknown` and `not_replying` are already excluded everywhere here. `autonomy_decision` events are still not logged anywhere.
**D15 (scoring windows).** Per-row data is all in `message_records` + `edit_classified` payloads (`classes`, `items_used_hint` for voice / decision / fact attribution, `draft_confidence`, `data_mode`). `compute_stats()` is all-time; windows would filter `records` by time. Use `data_mode` to keep own / made-up rows out of real scores (5.18).
**D2b / D4b.** `not_replying` is **not yet in the `Outcome` enum**, so the writer rejects it; classifier and stats already treat the string `not_replying` as "excluded, counted separately". Add it to the enum when D2b / D4b land.
**M2.** Retire own-data mode (section 6); clear or exclude own / made-up rows (the data-mode grouping already separates them); consider clearing the 6 Nov stub rows (already excluded).
**Bot hook contract (supersedes the Telegram doc for these points):** `on_captured(message_id, capture) -> str | None` (a returned string is sent as a follow-up note); new `events_provider(deps, arg, chat_id) -> str` and `/events`; `ThreadStore.numbers(chat_id)`.

## 14. Flags and questions

**Product-level or needs a view**
1. **Decision edits will dominate while the drafter plays safe.** `drafter-v0.2` answers "check karke bataunga" whenever no rule covers a message, so most real replies ("haan", "nahi") change the stance from non-committal to firm. 5.4 says decision edit = "stance changed (yes→no)". I counted non-committal ↔ firm as a decision change (it tells us a decision rule is missing). That will push the north star down and decision fidelity down until the self-model has decision rules. Is that the intended reading, or should "no stance → stance" be its own thing (or a fact / voice-like "gap" that is not a penalty)? Needs the Ideation chat before D9b and D15.
2. **North star is a range.** While Q1a is open I show "x% to y%" instead of one number. Design-core §1 says one weekly number. OK for v0.1; confirm.
3. **The hook now returns a note and there is a new `/events` command.** Small additions to the Dev 2 contract. Update the Telegram doc when convenient.

**Contradictions or gaps in earlier work (not changed here)**
4. **Numbered tokens do not exist yet.** The masker still writes `[PHONE]` / `[EMAIL]`, so two different phone numbers look the same and a changed number is invisible to the classifier until D3b (5.17 pt 3). The token-change rule is built and tested with numbered tokens and `Person A/B`, ready for D3b. Names in free text are still unmasked.
5. **`not_replying` is not in the `Outcome` enum** (see D2b / D4b above). The decisions say it exists (5.17 pt 4).
6. **`app/config.py` defines `drafter_max_tokens` twice** (1500, then 1000), so the second wins and the documented 1500 is not what runs. Dev 3 follow-up; not changed here.
7. **`mini_me.egg-info/` is committed to git**, so `pip install -e` shows it as modified. Suggest untracking it.

**Limits of this session**
8. This session had no route to Fly, Groq or Telegram; Braj ran the deploy, live loop and Telegram checks (section 15). `clf-0.1` quality is still unmeasured; the offline model's classes are heuristics and say nothing about quality.
9. With `DRAFTER=stub` nothing gets classified (stub rows are skipped by design).
10. Event times in `/events` are UTC; Braj is in IST. A display time zone can be added if it bothers you.

## 15. Test-run notes (9 Nov 2026, small samples: plumbing and behaviour, not accuracy)

**Local:** 261 tests passed (1 warning, text not seen). Offline loop and live loop (real model, throw-away SQLite) both passed all six plumbing checks; no failed or unclassified rows; about 1.2 s per draft.

**Live loop, how the classes looked (19 edited rows; made-up replies were written without seeing the drafts, so draft / reply mismatch is built in):**
- Greeting + emoji additions came out cosmetic; same-meaning rewordings came out voice; an added detail came out fact; sent as is came out `none` with no model call.
- **Decision was 10 of 19**, mostly deferring drafts ("let me check") meeting firm replies. This is flag 1 (section 14). One of them (draft "will confirm by tonight" vs reply "14 ko booked hai") is arguably a fact edit.
- Placeholder rule at work: a draft with `[EMAIL]` and a reply without it became fact; a reply that added `[PHONE]` came out decision (heaviest class).
- Blind rows can never be "sent as is" (no draft is seen before the reply).
- Drafter side, not the classifier: drafts are mostly English while replies are Hinglish.

**Real Telegram on Fly (9 Nov, about 11:00 to 11:05 PM IST):**
- `/help` lists `/stats` and `/events`. Draft #32: Sent as is, recorded. Draft #33: blind (held); the reply was the incoming text itself ("can you come this Friday?"), so "decision (also voice, cosmetic)" is a test artefact, not a quality signal. The classification came 2 s after the reply (llm method, `openai/gpt-oss-120b`), so the Fly model settings work for the classifier. Draft #34: Discard, no classification, as designed.
- `/stats` arithmetic checks out: own / made-up block 14 messages = scored 9 (sent as is 3 + edited and classified 1 + discarded 5) + edited not classified 4 + outcome unknown 1. Shown 8 + blind 1 = 9. North star 3 of 9 = 33% to 33% (equal ends because the only classified edit is a decision, which is never light). The 20 stub drafts are listed under "Left out of everything above".
- `/events` shows the latest steps; `/events 2` (a 6 Nov stub draft) shows its trail with no classification, as designed.
- Neon count query: 34 rows = 14 own / made-up + 20 stub. Classified: 1 `none` (draft #32), 1 `decision` (#33). Null `edit_class` with outcome edited_sent: 9, of which 4 are in the own / made-up block (the rest are stub rows).

**Open item, closed (9 Nov, 11:13 PM IST):** the 4 own / made-up edited messages with no class were edits from before the deploy. `python -m scripts.classify_pending` against Neon printed `{'skipped:stub': 5, 'classified': 4}`. `/stats` afterwards: scored 13 (shown 10 · blind 3) = sent as is 3 + edited 5 + discarded 5; edited 5 = decision 1 · fact 2 · voice 2 · cosmetic 0; never light 3, could be light or heavy 2; north star 23% to 38% (3 to 5 of 13); no edits left unclassified; stub drafts 20 left out. Arithmetic checked. Sent-as-is rows from before the deploy still have a null class; that does not affect `/stats` (it counts by outcome).

## 16. v0.1 fix batch (Dev 5, 9 Nov 2026)

Five small fixes from the tracker, before the freeze. Nothing in D1b / D2b / D3b / D4b / D6b / D9b was touched; no decision was changed; `OWN_DATA_ONLY_MODE` and the LLM guard are unchanged; own or made-up data only. Tests: **274 passing on SQLite** (261 before + 13 new in `tests/test_v01_fix_batch.py`).

| # | Change | Result |
|---|---|---|
| 1 | `app/config.py` defined `drafter_max_tokens` twice (1500, then 1000). The second one won, so the documented 1500 never ran. Removed the 1000 line; one definition = **1500** | New tests: no field is defined twice in `Settings` (checks the source, because Python allows the repeat silently), the default is 1500, an env value still overrides it and reaches the model call. One older test had the bug baked in (`max_tokens == 1000`); it now expects 1500 |
| 2 | Drafts were mostly English while Braj's replies are Hinglish. New prompt **`drafter-v0.3`** (`drafter-v0.2` and `v0.1` unchanged). Only rule 5 differs: language and script come from the VOICE notes, not from the incoming message; English only if the VOICE lines are English; mirrors the message only when VOICE shows no clear language. `PROMPT_VERSION` is now `drafter-v0.3`, so every `draft_generated` logs it | Tests: v0.3 differs from v0.2 in rule 5 only, v0.2 still mirrors the message language, safety rules carried over, the model receives the v0.3 text. Quality check on made-up messages: see the handoff for its status |
| 3 | `mini_me.egg-info/` untracked (`git rm --cached`) and `*.egg-info/` added to `.gitignore` | `pip install -e` no longer shows modified files |
| 4 | `/events` and `/events N` show **IST (Asia/Kolkata)**; heading says `(IST)`. Stored times stay UTC. `tzdata` added as a dependency (the slim Docker image has no time zone data); fixed +05:30 fallback if the zone is missing | Tests: UTC to IST conversion (aware and naive, across midnight), both `/events` forms show IST and never "UTC", the stored event time is still the UTC instant |
| 5 | `mini-me-dev-telegram.md` updated (Project and `docs/`): the hook's one-line note, `CLASSIFIER_NOTIFY`, `/events`, `/stats`, new section 12; stale "stub" lines corrected | This note and that doc |

**Build-level choices.**
- `LLMDrafter` takes an optional `prompt_version` (default = the released one) and `scripts/run_made_up_batch.py` takes `--prompt`, so v0.2 and v0.3 can be compared on the same messages without a deploy. The version actually used is what gets logged. Production never sets it.
- IST is a display setting in `app/results/event_view.py` (a constant), not a user setting; nothing else in the app converts times.
- The 1500 default is kept (the documented value, headroom for reasoning models). Raise with `DRAFTER_MAX_TOKENS` if a reasoning model returns empty content.
- A one-line comment in `app/drafting/prompts/README.md` lists v0.3 and the compare command.

**Q40 / Q41 (decisions 5.19), checked against the code, not built.**
- **Q41 (north star as a range): already matches.** `/stats` shows the range, says it is a range because light vs heavy is not set (Q1a), and groups rows by data mode. Only the label wording differs slightly from 5.19 ("range until the light/heavy boundary is set"). Not changed; a one-line wording edit in `app/results/stats.py` if you want the exact phrase.
- **Q40 (hedge to firm): not a one-line change.** It needs the `hedge_to_firm` / `flip` sub-type on decision edits, the `missing_fact` flag, a classifier prompt update (`clf-0.2`), and a separate `hedge_to_firm` line in `/stats`. That is the D9b work already planned (~2 h). The current prompt already counts non-committal to firm as a decision edit, which agrees with 5.19.
- **Drafter answers firmly when the self-model supports it (the optional item in 5.19):** not built. It is a drafter prompt change (D8b) and was outside these five items.
