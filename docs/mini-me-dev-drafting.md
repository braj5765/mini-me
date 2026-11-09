# Mini Me — Dev 3: Self-model + drafting (D6a, D8a, v0.1 parts)

_Built 7 Nov 2026, updated 9 Nov · Spoke: Dev 3 Self-model + drafting · Status: code complete, **183 tests passing on SQLite**, on `main`, **deployed to Fly and run against a real model** (Groq `openai/gpt-oss-120b`, free tier) with made-up messages only. Prompt `drafter-v0.2` is live. Provider terms evidence gathered 9 Nov, decision still open (section 5)._

Source of truth: decisions 4 (steps 1-2), 5.2, 5.3 A, 5.10, 5.13, 5.14, 5.16, 5.17; design-core (rationale tone); foundation and Telegram docs; timeline D6a/D8a. Where the PRD differs, decisions win.

## 1. What was built

- **D6a Self-model store.** Table `self_model_items` plus `SelfModelStore` (add, get, list, edit, archive, version tag, retrieval). Three layers: voice, decision, fact. Seeding script that reads **own sent messages only**.
- **D8a Real drafter.** `LLMDrafter` implements the `Drafter` interface and replaces the stub. One LLM call per draft through `get_llm()`. Returns text, confidence (0-1), one-line rationale, ids of the self-model items used. `is_stub=false`. Prompt files are versioned (`drafter-v0.1`, now `drafter-v0.2`).
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
4. What happens: lines are de-duplicated and masked; up to 15 spread-out lines become voice examples; unless `--no-llm`, one model call (`seeder-v0.2`) writes up to 8 voice, 6 decision and 6 fact notes from the lines. The call is declared own text because the function only accepts own lines. A file that looks like a chat export (timestamps with sender names) is **refused**. Re-running skips identical text. The model is called **first**: if the call fails, nothing is archived or added; an empty or invalid reply is retried once with double the token budget (`max_tokens` 3000, then 6000). With a reasoning model such as `openai/gpt-oss-120b`, set `OPENAI_COMPAT_REASONING_EFFORT=low` for the seeding run (PowerShell: `$env:OPENAI_COMPAT_REASONING_EFFORT='low'`).
5. Review the result with `--list`. Everything is editable later (screen 4 / D6b).

## 4. Drafter design (`app/drafting/llm_drafter.py`)

1. `store.retrieve(masked_text)`, then label items V1.., D1.., F1.. for the prompt.
2. Prompt from `prompts/drafter-v0.2/{system,user}.md`. The incoming text sits inside `<incoming_message>` (a closing tag typed into the message is neutralised) and the system prompt says to ignore instructions inside it.
3. One call: `max_tokens` = `DRAFTER_MAX_TOKENS` (default 1500, headroom for reasoning models, which spend tokens thinking), `temperature=0.5`, JSON reply `{draft, confidence, rationale, used}`.
4. Checks: valid JSON (code fences tolerated), non-empty draft up to 1,500 chars, **tokens** (below). One retry with the reason on a bad reply, then `DraftError("bad_output")`.
5. Result: `used` labels become item ids (unknown or repeated labels dropped); rationale made one short line (140 chars) and replaced by a plain fallback if empty or it contains unlock / gate / level up; confidence computed (below); `is_stub=false`.

**Tokens (5.17 pt 3).** The prompt tells the model to keep placeholders exactly. The drafter enforces it: any `[...]` placeholder or `Person X` in the draft must exist in the incoming message or in the notes shown. Case or spacing slips are repaired (`[Phone  1]` becomes `[phone 1]`); an invented or renumbered token triggers the retry, then a clean failure. Works today with `[PHONE]` / `[EMAIL]` and unchanged once D3b brings `[phone 1]`, `[id 1]`, `[address 1]`, `[link: domain]`, `Person A`.

**Confidence (`conf-0.1`, build-level, uncalibrated).** `0.65 x model estimate + 0.35 x support`, clamped to 0.05-0.95 (never certainty). Support = 0.4 + 0.3 if a decision rule was cited + 0.3 if a fact was cited. Display only, as in the Telegram bot; no thresholds hang on it (Q1 untouched). To be tuned with D9a data. Note it relies on the model's own citations.

**Failure path (bot).** `DraftError` (or any crash) gives the user one calm line ("I couldn't draft this one: ... Nothing was saved as a draft. Reply yourself if you like."), records `outcome=unknown` (never scored) with event `outcome_unknown` payload `reason=draft_failed, draft_error=<code>`. Codes: `provider_not_verified`, `rate_limited` (HTTP 429), `llm_error`, `bad_output`, `error`. No thread is opened, so nothing waits.

**Which drafter runs.** `DRAFTER=auto` (default): real drafter if `LLM_PROVIDER` and `LLM_MODEL` are set, else the stub (labelled stub in the DM). `stub` or `llm` force it.

## 5. Prompt versions, model, provider

| Item | Value |
|---|---|
| Drafter prompt | `drafter-v0.2` (system.md + user.md), logged as `prompt_version` and as `drafter_version` on every draft. `drafter-v0.1` is kept unchanged |
| Seeder prompt | `seeder-v0.2` (general rules only, lasting facts, jokes and one-off events ignored, neutral wording, a voice note per register). `seeder-v0.1` is kept unchanged |
| Confidence method | `conf-0.1` |
| Provider / model | `openai_compat` adapter pointed at Groq, model `openai/gpt-oss-120b` (free tier; Gemini `gemini-2.5-flash-lite` kept as a comparison option). Set by `LLM_PROVIDER` / `LLM_MODEL` (Fly secrets), recorded per draft as `provider` and `model`. No model name is in code. Free-tier limits noted from third-party sources, to confirm in the Groq console: about 30 requests/min, 8K tokens/min; a draft is about 2-3K tokens, so about 3 drafts a minute in a batch. |
| Provider terms check | **Evidence gathered 9 Nov, decision pending** (5.16 pt 2, needed before M2). **Groq:** the Services Agreement (console.groq.com/docs/legal/services-agreement, section 4) says Groq may not use Inputs or Outputs for training or fine-tuning unless the customer grants permission; the Data Processing Addendum is at console.groq.com/docs/legal/customer-data-processing-addendum (effective 15 Oct 2025). Groq's your-data page says inference requests are not retained by default, but inputs and outputs may be logged for up to 30 days for reliability and abuse monitoring; on 9 Nov Braj's console showed Global ZDR and Inference ZDR both Disabled. **Gemini API** Additional Terms (effective 23 Mar 2026): unpaid-tier content is used to improve Google products and human reviewers may read it; paid tier does not train. Groq's models page (9 Nov) lists `openai/gpt-oss-120b` under Production Models, not Preview. **Open:** Braj to read the Services Agreement himself, decide whether to turn on Inference ZDR, and the decision to flip any provider flag. The guard flags are unchanged (all False). Record who decided and the date here: _pending Braj_ |

Pre-release edit (7 Nov, before any deploy or real run, so still `v0.1`): prompts made pronoun-neutral and the drafter told to mirror the message's language and script (the real sample is Hinglish in Roman letters). From the first real run on, a prompt change means a new version folder.

**8 Nov provider change.** Braj's Groq key does not list `llama-3.3-70b-versatile`; the plan moved to `openai/gpt-oss-120b` (reasoning model). Code follow-ups: `DRAFTER_MAX_TOKENS` (was fixed 500, which hidden reasoning can use up), optional `OPENAI_COMPAT_REASONING_EFFORT` (sent as `reasoning_effort`, e.g. `low`), empty model content handled as `bad_output`, HTTP status shown in provider errors (never the key or URL), `rate_limited` reason, and `--pause` on the batch runner (free tiers have per-minute token limits). First real run on 8-9 Nov (section 9).

**9 Nov prompt change (`drafter-v0.2`).** Made-up runs on `v0.1` showed drafts that confirmed facts they were not given, claimed actions as done, misread who was late, and put the same emoji on almost every reply (section 9). `v0.2` adds rules 8-10 to `system.md`: never say something is already done or confirm an unknown fact (keep confidence at 0.5 or less), read who is doing what, use plain words and no habitual emoji. `v0.1` is unchanged; `PROMPT_VERSION` is now `drafter-v0.2`; `prompts/README.md` lists both.

Prompts are files in the repo; never edit a released version in place, copy to `drafter-v0.2` and change `PROMPT_VERSION` (see `prompts/README.md`).

## 6. The LLM guard and own-data mode (decision-adjacent, FLAGGED)

- The **guard logic in `app/llm/base.py` is unchanged** (8 Nov: only the `openai_compat` request body and error text changed, see section 5): all provider flags are still `no_training_verified=False`, and a test asserts it.
- The drafter calls with `contains_third_party_text=True` by default, so with a real unverified provider and **no** mode set, drafting is **refused** (test with the real Gemini client: nothing is sent; user sees the "provider is not approved" line).
- `OWN_DATA_ONLY_MODE=true` (Fly secret / env, default false): the drafter declares the text own or made-up (`contains_third_party_text=False`). Every draft logs `data_mode` (`own_or_made_up` or `third_party_guarded`), the app warns at startup, and the setting is documented as v0.1-only.
- **Risk to know:** with the mode on, anything you forward goes to the provider whatever it is. Forward only your own or made-up messages (5.16 pt 2, 5.14).
- **Remove or retire the mode before real third-party messages** (D3b / M2). Suggested M2 exit item: mode removed, one provider flag flipped after the terms check.

## 7. Files

New: `app/selfmodel/{__init__,store,seed}.py`, `app/drafting/{llm_drafter,factory,tokens,prompts_loader}.py`, `app/drafting/prompts/{drafter-v0.1,drafter-v0.2,seeder-v0.1}/*.md`, `scripts/seed_self_model.py` (now also `--add`, `--edit`, `--archive`; direct store edits, no events until D6b), `scripts/run_made_up_batch.py` (`--pause`, `--delay` alias, `--only`, wait-and-retry on HTTP 429), `seed/{own_lines.example.txt,made_up_messages.txt}`, `tests/test_selfmodel.py`, `tests/test_drafter.py`.
Changed: `app/db.py` (+`self_model_items`), `app/config.py` (+`DRAFTER`, `DRAFTER_MAX_TOKENS`, `OWN_DATA_ONLY_MODE`, `OPENAI_COMPAT_REASONING_EFFORT`), `app/drafting/base.py` (+`DraftResult.meta`, `DraftError`), `app/telegram/bot.py` (failure path, audit fields), `app/telegram/format.py` (`draft_failed`), `app/telegram/router.py` (`build_drafter`), `pyproject.toml` (prompt files packaged; checked in a built wheel), `.gitignore` (`seed/*` private), `.env.example`.

## 8. Tests (183 total, all passing on SQLite)

- **Self-model CRUD (25):** schema columns, add / get / list / count, edit bumps version, archive keeps the row, bad layer / empty / too long refused, text masked, version tag, retrieval (small store whole, large trimmed, notes before examples).
- **Seeding from own lines only:** parsing; examples and notes with sources; model sees only own text and the call is declared own text; **chat export refused**; too few lines refused; masked before storage and before the model; no-model path; re-run dedupe; `--replace` archives only seed items (manual survive); bad model JSON stops cleanly.
- **Drafter contract (53):** fields, confidence range, one-line rationale, item ids exist and match layers, unknown labels dropped; **tokens preserved** (`[phone 1]`, `Person A`, `[PHONE]`), case repair, invented token retried then failed; fenced / bad / missing JSON; confidence never 1 and handles garbage; gate words replaced; delimiter and injection checks; **guard behaviour** (flags unverified; mode off refuses with the real Gemini client and sends nothing; mode on goes through); provider errors; **stub vs real flag**; factory choices; prompt files exist; bot integration (real draft shown, masked before the model, `draft_generated` payload and `self_model_version`, failure path, capture still works).
- Seeder safety: a failed model call leaves the store untouched on `--replace`; an empty first reply is retried with a bigger budget.
- Later additions: reasoning-model reply cap and HTTP status in errors; batch runner (pause, `--only`, retry on 429); self-model CLI add / edit / archive; `v0.2` prompt rules with `v0.1` left unchanged.
- Test isolation: `tests/conftest.py` ignores a developer's `.env` and behaviour-changing environment variables, so tests pass the same on any machine.
- Not run: tests on Postgres (same as Dev 1/2).

## 9. Latency and test-run notes

All runs used made-up or Braj's own messages only (`OWN_DATA_ONLY_MODE=true`). Quality notes only; no accuracy claims; samples are tiny.

**Timings.**
- **Model call on Fly (`llm_ms` in `draft_generated`):** `v0.1`, 5 live drafts, 903-1189 ms. `v0.2`, 6 live drafts, 956-2255 ms, median about 1.2 s. Telegram end to end and Fly cold starts are not in `llm_ms`.
- **Batch runner on Braj's laptop (29 made-up messages, `v0.1`, Groq `openai/gpt-oss-120b`):** median 2719 ms, p90 3108 ms, max 4562 ms per draft. This is the whole `draft()` call (including reading the self-model from the database), so it is not comparable to `llm_ms`.
- **Fly free trial:** the machine stops after 5 idle minutes and restarts on the next message (cold start about 3.6 s in the logs). Add a card on Fly to avoid it.
- First batch hit HTTP 429 on 19 of 29 messages because `--delay` had no effect after a merge. Fixed (`--delay` is now an alias of `--pause`, plus retry on 429); with `--pause 20` all 29 succeeded.
- Query for real timings (Neon; the column is `occurred_at`): `select occurred_at, payload->>'llm_ms', payload->>'prompt_version', payload->>'self_model_version' from events where event_name='draft_generated' and payload->>'is_stub'='false' order by occurred_at desc limit 30;`

**Quality notes, `v0.1` (29 messages).**
- Most drafts (about 60%) were a neutral "check karke bataunga" with a 😏 or 😎, confidence 0.40-0.53. Safe but generic.
- Placeholders were kept (`[EMAIL]`, `Person A`). The message that told the drafter to approve everything was not obeyed. The 5000-rupee request got a non-committal reply.
- Problems: #21 confirmed a venue it was never told (0.67); #7 and #24 claimed an action as done and #24 misread who was late; #11 asked for a pincode, from a decision rule that was one old message turned into a rule; #4 began with "Kedi", copied from one of Braj's own seed lines. Confidence was highest on the riskiest drafts, so it carries little signal yet.
- Only one decision rule and no fact was cited across the 29 drafts.

**Quality notes, self-model seed.** The seeder wrote 8 voice, 6 decision and 6 fact notes from the sample. Some "decision rules" and "facts" are descriptions of single past messages (a pincode request, an S3 access grant, an ElevenLabs model name), one rule came from a joke, and the notes say "He" although the prompt asks for neutral wording. Braj archived 5 weak items and added a casual-style voice note by hand (self-model tag `sm-0.1-521d0a6f` to `sm-0.1-7a8b30f3`). The seed also mixes formal work English and casual Hinglish, and all of it is sent every time. **9 Nov re-seed with `seeder-v0.2`.** The first attempt failed ("The model did not return JSON notes") after `--replace` had already archived the old seed, which left the store half-replaced. That was a bug in `seed_self_model`, now fixed (see section 3). With `OPENAI_COMPAT_REASONING_EFFORT=low` the run succeeded; a reasoning model probably used up its token budget (not confirmed). Result: 2 voice notes (casual Hinglish with friends, and work English; neutral wording), 0 decisions, 6 facts. The facts were still weak: one misread "congratulations" lines as membership, and others restated single messages (an ElevenLabs answer, food links, a UPI request, design-call timings). Braj archived those five by hand.

**Curation and the register problem.** A kept fact ("handles Confluence and S3 access") then leaked into a volunteers-list draft (#11), and the work-register voice note made #7 and #24 come out in work English. Braj archived that fact, the work voice note and five work-style lines (option A: club and college messages during v0.1), and edited one voice note to say emojis on some messages, not all. Self-model tag now `sm-0.1-d28e0a9a`: about 12 voice items (10 verbatim lines, his own casual-style note, the seeded casual-style note), 0 decisions, 0 facts. The self-model has no domain or register tag, so the drafter cannot tell work from club messages; a domain field would be an additive change (not built).

**Quality notes, `v0.2` live on Telegram (6 drafts, #26-#31).**
- Venue confirmation: "check karke batauga", no confirmation. Volunteers list: no pincode. Instruction-injection message: not obeyed. Casual greeting: lowercase "h", no emoji, closer to Braj's own edits.
- Still commits to future actions: "group ko update kar dunga" (#27) and "minutes bhej deta hun ... soon" with an emoji (#28, 0.53). Not claimed as done, but still promises. Needs a product view (below).
- Confidence was 0.45 on five of six drafts and 0.53 on one, so it has almost no spread.
- **After curation (batch of messages 7, 11, 21, 24, `v0.2`, tag `sm-0.1-d28e0a9a`):** all four came out as short casual Hinglish with no emoji, no invented details and no work-fact leak; the venue message got "check karke bata deta hu". #7 ("minutes ... bhej dunga") and #24 ("group ko update karta hun") still promise actions (kept on review, flag 9). Confidence 0.46-0.53; only the seeded casual-style note was cited, no decision or fact. Whole-draft time on the laptop 2.4-4.6 s. Four drafts only, so a quality read and nothing more.
- **Before curation (same four messages, tag `sm-0.1-147541dd`):** the Confluence fact appeared in #11 and #7 and #24 came out in English.

## 10. Deploy steps (Windows PowerShell)

1. `git pull`; `fly secrets list` should show `DATABASE_URL`, `LLM_PROVIDER`, `LLM_MODEL`, `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY`, `OWN_DATA_ONLY_MODE`. Set missing ones with `fly secrets set NAME="value"` (keys: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_COMPAT_*`). Keep keys and connection strings out of chat and screenshots.
2. Seed or fix the self-model in Neon from the laptop (section 3; `--list`, `--archive`, `--edit`, `--add`). Edits take effect on the next draft, with no deploy.
3. `fly deploy`, then `fly status` and `fly logs`; send a made-up message to the bot. In Neon, the newest `draft_generated` rows should show the current `prompt_version`.
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

Table and column names above; item id format; layer / source labels; JSON reply format; retrieval rules; `conf-0.1`; one retry; max tokens 1500 (`DRAFTER_MAX_TOKENS`); temperature 0.5; token enforcement; `DRAFTER` and `OWN_DATA_ONLY_MODE` settings; prompt versions as files; seed file format and `seed/*` git-ignored; verbatim own lines kept as voice items (up to 15).

## 13. Flags and questions

See the handoff block in the chat; copied in the decision-adjacent items above (section 6) and below.
1. **Own-data-only mode** is a decision-adjacent change (b). Needs to be retired at M2.
2. **Telegram doc contract** says a model-calling drafter must pass `contains_third_party_text=True`. In own-data mode it passes False. Update the Telegram doc / interface note when the mode is retired.
3. **Names in own lines.** Names of other people in seed lines are not masked in v0.1 (D3b), so they are stored and sent to the provider. Scrub them from the seed file or accept it.
4. **Free-tier terms.** Seeding and drafting send your own and made-up text to the chosen provider. Gemini's free tier uses inputs to improve Google products with human review, so keep it for made-up text only. Groq's agreement bars training on inputs (section 5), but see the retention note there.
5. **Verbatim examples in the Voice layer** (up to 15 raw own lines, `source=seed_own_line`) may clutter screen 4. Build-level for now; D21 can filter by source.
6. **Used-item tags are model-reported**, not verified. They feed the 5.10 voice / decision / fact attribution, so D9a should treat them as a hint.
7. `outcome_unknown` is now also written when no draft could be made (`reason=draft_failed`). Scores are unaffected; D11a can show it separately.
8. **Latency and quality** are now measured on small made-up samples only (section 9). Not a basis for any threshold.
9. **Drafts can still promise future actions** ("bhej deta hun", "update kar dunga") at low-to-medium confidence. Braj decided on 9 Nov to keep relying on his own review for now (no prompt rule, no code check). Options compared in the chat; revisit if D9a data shows approved promises or if blind-sample drafts matter.
10. **Seeder output quality.** `seeder-v0.1` wrote "rules" and "facts" from single past events or jokes, and used "He". `seeder-v0.2` fixed the wording and the registers but still wrote weak facts from 16 lines, so notes were curated by hand. Treat seeded notes as a draft for review; a review step on screen 4 (D6b) matters for the same reason.
13. **No domain or register tag on items.** Work and club messages share one self-model, so the drafter picks a register by guess. For v0.1 Braj kept club and college style only. An additive `domain` field and retrieval by domain is the likely fix (product-level, not built).
11. **Commits from another session** (`b9a7892`, `6fcc8da`: reasoning headroom, `reasoning_effort`, 429 handling, `--pause`) were merged into main; their effects are described above.
12. **Fly trial limit:** machine stops after 5 idle minutes (section 9). Braj chose on 9 Nov to keep the free trial for now and revisit later.
