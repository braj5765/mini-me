# Prompts (versioned files)

One folder per prompt version: `drafter-v0.1/` (system.md + user.md) and `seeder-v0.1/`. Placeholders are
`{{name}}`. Never edit a released version in place: copy to the next folder (e.g. `drafter-v0.4/`), change `PROMPT_VERSION` in
`app/drafting/llm_drafter.py`, and note the change in the dev doc. The version is logged on every draft
(`draft_generated.payload.prompt_version`).

## Versions
- `drafter-v0.1`: first release. Found in made-up runs: confirmed unknown facts ("confirm kar diya"), claimed actions as done, misread who was late, an invented word, the same emoji on nearly every reply.
- `drafter-v0.2`: adds rules 8-10 (no claiming actions done or confirming unknown facts, read who is doing what, plain words and no habitual emoji). Not yet re-measured; re-run messages 7, 11, 21, 24 and compare.
- `drafter-v0.3`: changes rule 5 only. The language and script of the draft now come from the VOICE notes (how Braj really writes, e.g. short Hinglish in Roman letters), not from the incoming message, so an English question gets a Hinglish draft when the voice items are Hinglish. Falls back to mirroring the message only when the VOICE lines show no clear language. Reason: drafts came out mostly English while Braj's own replies are Hinglish (9 Nov). Compare with `python -m scripts.run_made_up_batch seed/made_up_messages.txt --only 1,2,6,7,9,11,13,21,24,29 --prompt drafter-v0.2` (and `--prompt drafter-v0.3`).
- `seeder-v0.1`: first release. On Braj's real sample it wrote "decisions" and "facts" that restated single messages or a joke, and used "He".
- `seeder-v0.2`: notes must be general, facts lasting, jokes and one-off events ignored, neutral wording, separate voice notes per register. Not yet run on the real sample.
