# Classifier prompts (versioned files)

One folder per prompt version, like the drafter's. Placeholders are `{{name}}`. Never edit a released version
in place: copy to a new folder (`classifier-v0.2/`), change `PROMPT_VERSION` in `app/classifier/classifier.py`, bump
`CLASSIFIER_VERSION` and note it in the dev doc. `CLASSIFIER_VERSION` (logged as `classifier_version` on the
record and on `edit_classified`) names prompt + rules together, so a rules change bumps it even if the prompt
file does not change.

## Versions
- `classifier-v0.1` (`clf-0.1`): first release. Four classes from decisions 5.4; the model lists every class it
  sees; code adds a fact edit whenever a numbered token changed (5.17 pt 3) and picks the heaviest class
  (decision > fact > voice > cosmetic). Written without a real-model run in the build session: re-check on the
  first live run and on the D9b spot-check set.
