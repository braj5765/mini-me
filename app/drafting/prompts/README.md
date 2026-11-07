# Prompts (versioned files)

One folder per prompt version: `drafter-v0.1/` (system.md + user.md) and `seeder-v0.1/`. Placeholders are
`{{name}}`. Never edit a released version in place: copy to `drafter-v0.2/`, change `PROMPT_VERSION` in
`app/drafting/llm_drafter.py`, and note the change in the dev doc. The version is logged on every draft
(`draft_generated.payload.prompt_version`).
