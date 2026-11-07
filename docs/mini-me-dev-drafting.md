# Mini Me — Dev 3: Self-model + drafting (D6a, D8a, v0.1 parts)

_Built 7 Nov 2026 · Spoke: Dev 3 Self-model + drafting · Status: code complete, **174 tests passing on SQLite** (93 earlier + 81 new). pushed to `main` (latest `b9a7892`). **Not yet deployed to Fly and not yet run against a real model** (no Fly, Telegram or provider access from this session; see sections 9 and 10)._

Source of truth: decisions 4 (steps 1-2), 5.2, 5.3 A, 5.10, 5.13, 5.14, 5.16, 5.17; design-core (rationale tone); foundation and Telegram docs; timeline D6a/D8a. Where the PRD differs, decisions win.

## 1. What was built

- **D6a Self-model store.** Table `self_model_items` plus `SelfModelStore` (add, get, list, edit, archive, version tag, retrieval). Three layers: voice, decision, fact. Seeding script that reads **own sent messages only**.
- **D8a Real drafter.** `LLMDrafter` implements the `Drafter` interface and replaces the stub. One LLM call per draft through `get_llm()`. Returns text, confidence (0-1), one-line rationale, ids of the self-model items used. `is_stub=false`. Prompt files are versioned (`drafter-v0.1`).
- **Own/made-up-data mode** (decision of 7 Nov, option b) so drafting can run before a provider is verified. The guard itself is untouched (section 6).
- **Bot changes (small, flagged):** drafter errors are handled (user told, outcome unknown), and `draft_generated` carries the audit fields.

## 2. Self-model schema (`self_model_items`)

| Column | Notes |
|---|---|
| item_id (PK) | `sm_` + 10 hex |
| user_id | `braj` |
| layer | `voice` / `decision` / `fact` |
| text | **masked** on every write (max 400 chars) |
| source | `seed_own_line` (verbatim own line kept as a voice example), `seed_own_lines` (note a model wrote from own lines), `manual` (typed by hand) |
| source_ref | e.g. the seed file name; never message text |
| status | `active` / `archived` (nothing is hard-deleted) |
| version | starts 1, +1 on every text change or archive |
| created_at, updated_at | timestamptz |

**Why D6b is additive.** D6b adds a `self_model_item_versions` table (item_id, version, previous text, who, when, reason), a snapshot table, and writes the `selfmodel_item_edited` / `selfmodel_version_*` events. `status` already makes removal reversible and `version` is already bumped, so no column here changes. `SelfModelStore.update_text` and `archive` are the two places D6b hooks in (write the old row to the versions table before updating; log the event).

`version_tag()` = `sm-0.1-<8 hex>` over the active items and their versions. It is logged as `self_model_version` on every `draft_generated`. D6b replaces it with real version numbers.

**Retrieval (build-level).** Voice: style notes first, then own-line examples ranked by word overlap, up to 10. Decision and fact: if a layer has 12 items or fewer, all are shown (the model decides what applies); otherwise the top 6 by word overlap (overlap above 0 only). Overlap = shared meaningful words (stop words removed). Simple on purpose for v0.1; revisit when the store grows.

## 3. Seeding steps (own lines only)

1. Put your own sent messages in `seed/own_lines.txt` (git-ignored): one message per block, blocks separated by a blank line, `#` lines ignored. At least 5. Example format: `seed/own_lines.example.txt` (made-up).
2. Keep other people's names and details out for now: masking of names is D3b (5.16), so a name in your own line is stored and sent to the model as written.
3. Point `.env` at the database to seed (Neon `DATABASE_URL` for production) and set `LLM_PROVIDER`, `LLM_MODEL` and the provider key. Then:
   `python -m scripts.seed_self_model seed/own_lines.txt` (add `--no-llm` for verbatim examples only, `--replace` to archive the earlier seed first). `python -m scripts.seed_self_model --list` shows what is stored.
4. What happens: lines are de-duplicated and masked; up to 15 spread-out lines become voice examples; unless `--no-llm`, one model call (`seeder-v0.1`) writes up to 8 voice, 6 decision and 6 fact notes from the lines. The call is declared own text because the function only accepts own lines. A file that looks like a chat export (timestamps with sender names) is **refused**. Re-running skips identical text.
5. Review the result with `--list`. Everything is editable later (screen 4 / D6b).

## 4. Drafter design (`app/drafting/llm_drafter.py`)

1. `store.retrieve(masked_text)`, then label items V1.., D1.., F1.. for the prompt.
2. Prompt from `prompts/drafter-v0.1/{system,user}.md`. The incoming text sits inside `<incoming_message>` (a closing tag typed into the message is neutralised) and the system prompt says to ignore instructions inside it.
3. One call: `max_tokens` = `DRAFTER_MAX_TOKENS` (default 1000, headroom for reasoning models), `temperature=0.5`, JSON reply `{draft, confidence, rationale, used}`.
4. Checks: valid JSON (code fences tolerated), non-empty draft up to 1,500 chars, **tokens** (below). One retry with the reason on a bad reply, then `DraftError("bad_output")`.
5. Result: `used` labels become item ids (unknown or repeated labels dropped); rationale made one short line (140 chars) and replaced by a plain fallback if empty or it contains unlock / gate / level up; confidence computed (below); `is_stub=false`.

**Tokens (5.17 pt 3).** The prompt tells the model to keep placeholders exactly. The drafter enforces it: any `[...]` placeholder or `Person X` in the draft must exist in the incoming message or in the notes shown. Case or spacing slips are repaired (`[Phone  1]` becomes `[phone 1]`); an invented or renumbered token triggers the retry, then a clean failure. Works today with `[PHONE]` / `[EMAIL]` and unchanged once D3b brings `[phone 1]`, `[id 1]`, `[address 1]`, `[link: domain]`, `Person A`.

**Confidence (`conf-0.1`, build-level, uncalibrated).** `0.65 x model estimate + 0.35 x support`, clamped to 0.05-0.95 (never certainty). Support = 0.4 + 0.3 if a decision rule was cited + 0.3 if a fact was cited. Display only, as in the Telegram bot; no thresholds hang on it (Q1 untouched). To be tuned with D9a data. Note it relies on the model's own citations.

**Failure path (bot).** `DraftError` (or any crash) gives the user one calm line ("I couldn't draft this one: ... Nothing was saved as a draft. Reply yourself if you like."), records `outcome=unknown` (never scored) with event `outcome_unknown` payload `reason=draft_failed, draft_error=<code>`. Codes: `provider_not_verified`, `rate_limited` (HTTP 429), `llm_error`, `bad_output`, `error`. No thread is opened, so nothing waits.

**Which drafter runs.** `DRAFTER=auto` (default): real drafter if `LLM_PROVIDER` and `LLM_MODEL` are set, else the stub (labelled stub in the DM). `stub` or `llm` force it.

## 5. Prompt versions, model, provider

| Item | Value |
|---|---|
| Drafter prompt | `drafter-v0.1` (system.md + user.md), logged as `prompt_version` and as `drafter_version` on every draft |
| Seeder prompt | `seeder-v0.1` |
| Confidence method | `conf-0.1` |
| Provider / model | **Not chosen yet.** Set by `LLM_PROVIDER` / `LLM_MODEL` (Fly secrets), recorded per draft as `provider` and `model`. No model name is in code. |
| Provider terms check | **Still open** (5.16 pt 2, needed before M2). Record provider, plan, date here when done: _pending Braj_ |

Pre-release edit (7 Nov, before any deploy or real run, so still `v0.1`): prompts made pronoun-neutral and the drafter told to mirror the message's language and script (the real sample is Hinglish in Roman letters). From the first real run on, a prompt change means a new version folder.

**8 Nov provider change.** Braj's Groq key does not list `llama-3.3-70b-versatile`; the plan moved to `openai/gpt-oss-120b` (reasoning model). Code follow-ups: `DRAFTER_MAX_TOKENS` (was fixed 500, which hidden reasoning can use up), optional `OPENAI_COMPAT_REASONING_EFFORT` (sent as `reasoning_effort`, e.g. `low`), empty model content handled as `bad_output`, HTTP status shown in provider errors (never the key or URL), `rate_limited` reason, and `--pause` on the batch runner (free tiers have per-minute token limits). Not yet run against the real provider.

Prompts are files in the repo; never edit a released version in place, copy to `drafter-v0.2` and change `PROMPT_VERSION` (see `prompts/README.md`).

## 6. The LLM guard and own-data mode (decision-adjacent, FLAGGED)

- The **guard logic in `app/llm/base.py` is unchanged** (8 Nov: only the `openai_compat` request body and error text changed, see section 5): all provider flags are still `no_training_verified=False`, and a test asserts it.
- The drafter calls with `contains_third_party_text=True` by default, so with a real unverified provider and **no** mode set, drafting is **refused** (test with the real Gemini client: nothing is sent; user sees the "provider is not approved" line).
- `OWN_DATA_ONLY_MODE=true` (Fly secret / env, default false): the drafter declares the text own or made-up (`contains_third_party_text=False`). Every draft logs `data_mode` (`own_or_made_up` or `third_party_guarded`), the app warns at startup, and the setting is documented as v0.1-only.
- **Risk to know:** with the mode on, anything you forward goes to the provider whatever it is. Forward only your own or made-up messages (5.16 pt 2, 5.14).
- **Remove or retire the mode before real third-party messages** (D3b / M2). Suggested M2 exit item: mode removed, one provider flag flipped after the terms check.

## 7. Files

New: `app/selfmodel/{__init__,store,seed}.py`, `app/drafting/{llm_drafter,factory,tokens,prompts_loader}.py`, `app/drafting/prompts/{drafter-v0.1,seeder-v0.1}/*.md`, `scripts/seed_self_model.py`, `scripts/run_made_up_batch.py`, `seed/{own_lines.example.txt,made_up_messages.txt}`, `tests/test_selfmodel.py`, `tests/test_drafter.py`.
Changed: `app/db.py` (+`self_model_items`), `app/config.py` (+`DRAFTER`, `OWN_DATA_ONLY_MODE`), `app/drafting/base.py` (+`DraftResult.meta`, `DraftError`), `app/telegram/bot.py` (failure path, audit fields), `app/telegram/format.py` (`draft_failed`), `app/telegram/router.py` (`build_drafter`), `pyproject.toml` (prompt files packaged; checked in a built wheel), `.gitignore` (`seed/*` private), `.env.example`.

## 8. Tests (81 new, 174 total, all passing on SQLite)

- **Self-model CRUD (25):** schema columns, add / get / list / count, edit bumps version, archive keeps the row, bad layer / empty / too long refused, text masked, version tag, retrieval (small store whole, large trimmed, notes before examples).
- **Seeding from own lines only:** parsing; examples and notes with sources; model sees only own text and the call is declared own text; **chat export refused**; too few lines refused; masked before storage and before the model; no-model path; re-run dedupe; `--replace` archives only seed items (manual survive); bad model JSON stops cleanly.
- **Drafter contract (53):** fields, confidence range, one-line rationale, item ids exist and match layers, unknown labels dropped; **tokens preserved** (`[phone 1]`, `Person A`, `[PHONE]`), case repair, invented token retried then failed; fenced / bad / missing JSON; confidence never 1 and handles garbage; gate words replaced; delimiter and injection checks; **guard behaviour** (flags unverified; mode off refuses with the real Gemini client and sends nothing; mode on goes through); provider errors; **stub vs real flag**; factory choices; prompt files exist; bot integration (real draft shown, masked before the model, `draft_generated` payload and `self_model_version`, failure path, capture still works).
- Not run: tests on Postgres (same as Dev 1/2).

## 9. Latency and test-run notes

**Not measured with a real model.** This session has no provider key and no route to Telegram or Fly, so no real timings or quality notes exist yet. What was run:
- **Local pipeline smoke test (29 made-up messages, own seed file):** bot webhook logic, masking, self-model retrieval, the real `openai_compat` HTTP adapter and the real drafter against a **local fake model server** (answers after 50 ms). Median 86 ms, max 127 ms per message including the fake 50 ms, so the app's own overhead is about 35 ms. 29 of 29 drafted with `is_stub=false`, tokens kept, 6 held as blind (about 1 in 5), 23 shown. This says nothing about model quality or real model speed.
- **Target:** a draft in a few seconds. Expected cost drivers: one model call (1000 max output tokens) plus the Telegram round trip; a Fly cold start adds a few seconds on the first message after idle. Every draft logs `llm_ms` (model time) and `gen_latency_ms` (whole drafter step) in `draft_generated`, so real timings are read from the event log after the first real run.
- **To do (Braj):** (1) seed; (2) set secrets, deploy; (3) forward the made-up messages in `seed/made_up_messages.txt` (29 messages, includes a phone, an email, a pseudonym, a money request, a message that tries to give the drafter instructions) to the bot; or run `python -m scripts.run_made_up_batch seed/made_up_messages.txt` for model-only timings and a table of drafts. Then note timings and quality here (quality notes only, no accuracy claims).

## 10. Deploy steps (for Braj; no Fly access from this session)

1. `git pull`, then set secrets (PowerShell, single quotes): `fly secrets set LLM_PROVIDER='gemini' LLM_MODEL='<model>' GEMINI_API_KEY='...' OWN_DATA_ONLY_MODE='true'` (use the provider you choose; keys: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_COMPAT_*`).
2. Seed the self-model into Neon from your laptop (section 3). The release command creates the new table on deploy (`create_all`, additive).
3. `fly deploy`. Check `/healthz`, then forward a made-up message to the bot: the draft should show no "stub" tag.
4. To go back to stubs at any time: `fly secrets set DRAFTER=stub`.

## 11. Interfaces for later tasks

**D9a (edit classifier).**
- Read `message_records` (`draft_text`, `sent_text`) in the existing `on_captured(message_id, capture)` hook. Real drafts have `draft_generated.payload.is_stub=false`; **exclude `is_stub=true` rows** (stub rows from 6 Nov are still in the database).
- Useful on the draft event: `items_used` (`layer`, `item_id`, `label`), `prompt_version`, `provider`, `model`, `confidence`, `confidence_method`, `data_mode`, `retries`. Join draft to edit via `message_id` to attribute misses to items (voice / decision / fact scores, 5.10).
- Messages whose drafter failed have `outcome=unknown` and no `draft_text`; they never reach the hook. Edits to token-bearing values: tokens are kept intact in drafts (5.17), so a changed `[phone n]` is visible as a fact edit.
- A classifier prompt should expect numbered tokens (`[phone 1]`, `Person A`) and today's `[PHONE]` / `[EMAIL]`.

**D7 / D8b (cold start, pre-generation).**
- Cold start A: call `seed_self_model(store, own_lines, llm=..., source_ref=..., replace=...)`; a WhatsApp export importer must reduce the file to the user's own lines first (the seeder refuses export-like text). Cold start B (12 sample replies, Me / Not me): add items via `store.add_item(layer, text, source="calibration")` and log `calibration_*` events (not done here).
- D6b: wrap `update_text` / `archive` as described in section 2.
- D8b pre-generation: `LLMDrafter.draft(req)` is a pure function of the request and the store, safe to call ahead of time; cache by `(message_id, store.version_tag())`. "Rationale detail on tap" can use `items_used` ids to show the notes behind a draft. The blind-sample logic stays in the bot.
- Prompt changes: new folder `drafter-v0.2`, bump `PROMPT_VERSION`.

## 12. Build-level choices (to record)

Table and column names above; item id format; layer / source labels; JSON reply format; retrieval rules; `conf-0.1`; one retry; max tokens 1000 (`DRAFTER_MAX_TOKENS`); temperature 0.5; token enforcement; `DRAFTER` and `OWN_DATA_ONLY_MODE` settings; prompt versions as files; seed file format and `seed/*` git-ignored; verbatim own lines kept as voice items (up to 15).

## 13. Flags and questions

See the handoff block in the chat; copied in the decision-adjacent items above (section 6) and below.
1. **Own-data-only mode** is a decision-adjacent change (b). Needs to be retired at M2.
2. **Telegram doc contract** says a model-calling drafter must pass `contains_third_party_text=True`. In own-data mode it passes False. Update the Telegram doc / interface note when the mode is retired.
3. **Names in own lines.** Names of other people in seed lines are not masked in v0.1 (D3b), so they are stored and sent to the provider. Scrub them from the seed file or accept it.
4. **Free-tier terms.** Seeding and drafting send your own and made-up text to the chosen provider. Own text is allowed by the guard, but a free tier may use inputs for training; pick the provider with that in mind.
5. **Verbatim examples in the Voice layer** (up to 15 raw own lines, `source=seed_own_line`) may clutter screen 4. Build-level for now; D21 can filter by source.
6. **Used-item tags are model-reported**, not verified. They feed the 5.10 voice / decision / fact attribution, so D9a should treat them as a hint.
7. `outcome_unknown` is now also written when no draft could be made (`reason=draft_failed`). Scores are unaffected; D11a can show it separately.
8. **Latency and quality are unmeasured** until a real run (section 9).
