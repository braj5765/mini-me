"""Edit classifier (D9a, v0.1 part). Called from the Telegram bot's `on_captured(message_id, capture)` hook.

What it does (decisions 4 step 3, 5.4, 5.10 mapping, 5.12, 5.13 pt 4, 5.17 pt 3):
  1. Read the message record (masked draft vs masked sent text). Skip anything that must not be scored.
  2. Sent as is        -> edit_class 'none'; no model call.
     Cosmetic only     -> 'cosmetic' from rules (case, spacing, punctuation, emoji); no model call.
     Anything else     -> one model call that lists every class it sees (decision / fact / voice / cosmetic).
  3. Code adds 'fact' whenever a placeholder changed (a changed [phone 1] is a fact edit; the model cannot miss it).
  4. The heaviest class (decision > fact > voice > cosmetic) becomes `edit_class`; the full list stays in the payload.
  5. Log `edit_classified` (once per message, with `classifier_version`), then update the record.
The light/heavy flag stays None until Q1a; decision and fact edits are marked `never_light`.

Never scored / never classified: outcome unknown, not_replying, waiting (incl. a blind draft whose reply has not
come yet), discarded (an outcome, not an edit), user_authored, not_me, and any row whose draft is a stub.
Model-reported "items used" are copied into the payload as a hint only; nothing here relies on them.

LLM guard: the call goes through app.llm.get_llm(); the guard is not touched. Text is declared own / made-up
only when OWN_DATA_ONLY_MODE is on AND the draft itself was made under that mode (`data_mode`); otherwise the
call is flagged third-party and the guard decides.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Engine

from app.classifier import rules
from app.config import Settings, get_settings
from app.events import EventName, EventWriter
from app.events.queries import all_records, draft_payload
from app.llm.base import LLMClient, LLMError, Message, get_llm

log = logging.getLogger("minime.classifier")

CLASSIFIER_VERSION = "clf-0.1"          # prompt + rules; logged as `classifier_version`
PROMPT_VERSION = "classifier-v0.1"      # the prompt folder
TEMPERATURE = 0.0
MAX_TEXT_CHARS = 2000
MAX_REASON_CHARS = 140

_PROMPTS = Path(__file__).parent / "prompts"
_CAPTURED = ("sent_as_is", "edited_sent")

_LABELS = {"decision": "a decision change", "fact": "a fact change",
           "voice": "a wording change", "cosmetic": "a cosmetic change"}
_FAIL_WHY = {
    "provider_not_verified": "the model provider is not approved for this kind of text yet",
    "rate_limited": "the model's free limit was reached",
    "llm_error": "the model could not be reached",
    "bad_output": "the model's answer was not usable",
}


class _BadOutput(Exception):
    pass


@dataclass
class ClassifyResult:
    status: str                       # classified | skipped | failed
    reason: str = ""                  # why skipped / failed
    edit_class: str | None = None
    classes: list[str] = field(default_factory=list)
    note: str = ""                    # short line for the user (empty = say nothing)


def _load(name: str) -> str:
    return (_PROMPTS / PROMPT_VERSION / f"{name}.md").read_text(encoding="utf-8")


def _clip(text: str) -> tuple[str, bool]:
    t = text or ""
    return (t, False) if len(t) <= MAX_TEXT_CHARS else (t[:MAX_TEXT_CHARS], True)


def _neutralise(text: str) -> str:
    """The texts sit inside <draft> / <sent>; a closing tag typed into them must not end the block."""
    return re.sub(r"</(draft|sent)>", r"< /\1>", text, flags=re.I)


def _parse(raw: str) -> dict:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise _BadOutput("not JSON")
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        raise _BadOutput("not valid JSON") from None
    if not isinstance(data, dict):
        raise _BadOutput("not an object")
    return data


def _model_classes(data: dict) -> list[str]:
    raw = data.get("classes")
    if not isinstance(raw, list):
        raise _BadOutput("'classes' must be a list")
    wanted = [str(c).strip().lower() for c in raw]
    valid = [c for c in wanted if c in rules.PRECEDENCE]
    if wanted and not valid:
        raise _BadOutput("'classes' has no valid class (use decision, fact, voice, cosmetic)")
    return rules.order_classes(valid)


def _one_line(text: object) -> str:
    r = " ".join(str(text or "").split())
    return r if len(r) <= MAX_REASON_CHARS else r[: MAX_REASON_CHARS - 3].rstrip(" ,;:") + "..."


def _note(edit_class: str, classes: list[str]) -> str:
    if edit_class == "none":
        return ""
    extra = [c for c in classes if c != edit_class]
    more = f" (also {', '.join(extra)})" if extra else ""
    return f"Edit read as {_LABELS[edit_class]}{more}."


# --------------------------------------------------------------------------------------------- core
class EditClassifier:
    """Callable hook: `EditClassifier(engine, writer)(message_id, capture)`. Returns a short note for the user
    (or None); the bot sends it as a follow-up line. Never raises into the bot."""

    def __init__(self, engine: Engine, writer: EventWriter, *, settings: Settings | None = None,
                 llm: LLMClient | None = None, notify: bool | None = None):
        self.engine, self.writer = engine, writer
        self.settings = settings or get_settings()
        self._llm = llm
        self.notify = self.settings.classifier_notify if notify is None else notify

    # bot hook -------------------------------------------------------------------------------
    def __call__(self, message_id: str, capture: str) -> str | None:
        try:
            res = self.classify(message_id, capture)
        except Exception:  # noqa: BLE001  (logging must not block the user, NFR-6)
            log.exception("classifier crashed for a message")
            return None
        if not self.notify:
            return None
        if res.status == "classified":
            return res.note or None
        if res.status == "failed":
            return (f"Saved. I couldn't read this edit yet ({_FAIL_WHY.get(res.reason, 'something went wrong')}); "
                    "it will show as \"not classified yet\" in /stats.")
        return None

    # ----------------------------------------------------------------------------------------
    def classify(self, message_id: str, capture: str = "") -> ClassifyResult:
        rec = self.writer.get_message(message_id)
        skip = self._skip_reason(rec)
        if skip:
            log.info("classifier skipped a message: %s", skip)
            return ClassifyResult("skipped", skip)
        draft_meta = draft_payload(self.engine, message_id)
        if draft_meta is None:
            return ClassifyResult("skipped", "no_draft_event")
        if draft_meta.get("is_stub"):
            return ClassifyResult("skipped", "stub")

        draft, sent = rec["draft_text"] or "", rec["sent_text"] or ""
        changes = rules.token_changes(draft, sent)
        info: dict = {"model_reason": None, "fallback": None, "retries": 0}

        if rules.same_text(draft, sent):
            classes, method = [], "exact"
        elif rules.cosmetic_only(draft, sent):
            classes, method = ["cosmetic"], "rules"
        else:
            try:
                model_classes, info = self._ask_model(draft, sent, draft_meta)
            except _ClassifierFailed as e:
                log.warning("classification failed: %s %s", e.reason, e.detail[:160])
                return ClassifyResult("failed", e.reason)
            classes = list(model_classes)
            method = "llm"
            if rules.has_token_change(changes) and "fact" not in classes:
                classes.append("fact")
                method = "llm+rules"
            if not classes:   # texts differ beyond cosmetics but the model saw nothing: smallest class, flagged
                classes, info["fallback"] = ["cosmetic"], "empty_model_reply"
            classes = rules.order_classes(classes)

        edit_class = rules.heaviest(classes)
        payload = {
            "edit_class": edit_class, "classes": classes,
            "light_or_heavy": rules.light_or_heavy(edit_class),     # placeholder until Q1a
            "never_light": rules.never_light(edit_class),
            "method": method, "capture": capture or None,
            "token_changes": changes if rules.has_token_change(changes) else None,
            "outcome": rec["outcome"], "shown_state": rec.get("shown_state"),
            "draft_confidence": rec.get("confidence"),
            "drafter_version": draft_meta.get("drafter_version"),
            "data_mode": draft_meta.get("data_mode"),
            # model-reported by the drafter: a hint only, never used for a decision here (Dev 3 note)
            "items_used_hint": [{"layer": i.get("layer"), "item_id": i.get("item_id")}
                                for i in (draft_meta.get("items_used") or []) if isinstance(i, dict)],
            **{k: v for k, v in info.items() if v not in (None, 0)},
        }
        self.writer.log(
            EventName.edit_classified, message_id=message_id, classifier_version=CLASSIFIER_VERSION,
            channel=rec.get("channel"), domain=rec.get("domain"), contact_type=rec.get("contact_type"),
            mode=rec.get("mode"), self_model_version=draft_meta.get("self_model_version"), payload=payload)
        self.writer.upsert_message(message_id, edit_class=edit_class, classifier_version=CLASSIFIER_VERSION)
        return ClassifyResult("classified", method, edit_class, classes, _note(edit_class, classes))

    # ----------------------------------------------------------------------------------------
    @staticmethod
    def _skip_reason(rec: dict | None) -> str:
        if rec is None:
            return "no_record"
        if rec.get("edit_class") is not None:
            return "already_classified"
        outcome = rec.get("outcome")
        if outcome not in _CAPTURED:
            if outcome == "waiting" and rec.get("shown_state") == "blind":
                return "blind_before_reply"
            return f"outcome_{outcome}"           # discarded, unknown, not_replying, user_authored, not_me, waiting
        if not rec.get("scorable"):
            return "not_scorable"
        if not (rec.get("draft_text") or "").strip() or not (rec.get("sent_text") or "").strip():
            return "no_text"
        return ""

    def _llm_client(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_llm(self.settings.classifier_provider or None, self.settings.classifier_model or None)
        return self._llm

    def _ask_model(self, draft: str, sent: str, draft_meta: dict) -> tuple[list[str], dict]:
        d, d_cut = _clip(draft)
        s, s_cut = _clip(sent)
        # Own / made-up only when the switch is on AND this draft was made under it; anything else is guarded.
        own = bool(self.settings.own_data_only_mode) and draft_meta.get("data_mode") == "own_or_made_up"
        messages = [
            Message("system", _load("system")),
            Message("user", _load("user").replace("{{draft}}", _neutralise(d)).replace("{{sent}}", _neutralise(s))),
        ]
        info: dict = {"prompt_version": PROMPT_VERSION, "retries": 0, "truncated": (d_cut or s_cut) or None}
        t0 = time.monotonic()
        llm = None
        while True:
            try:
                llm = self._llm_client()
                raw = llm.complete(messages, max_tokens=self.settings.classifier_max_tokens,
                                   temperature=TEMPERATURE, contains_third_party_text=not own)
            except LLMError as e:
                msg = str(e)
                reason = ("provider_not_verified" if "no-training" in msg
                          else "rate_limited" if "429" in msg else "llm_error")
                raise _ClassifierFailed(reason, msg) from None
            try:
                data = _parse(raw)
                classes = _model_classes(data)
                break
            except _BadOutput as bad:
                if info["retries"] >= 1:
                    raise _ClassifierFailed("bad_output", str(bad)) from None
                info["retries"] += 1
                messages = [*messages, Message("assistant", (raw or "")[:1000]),
                            Message("user", f"That output was rejected: {bad}. Return only the corrected JSON object.")]
        info.update({"llm_ms": int((time.monotonic() - t0) * 1000), "provider": llm.provider, "model": llm.model,
                     "model_reason": _one_line(data.get("reason")) or None,
                     "model_classes": classes, "declared": "own_or_made_up" if own else "third_party_guarded"})
        return classes, info


class _ClassifierFailed(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


# ------------------------------------------------------------------------------------------ catch-up
def classify_pending(engine: Engine, writer: EventWriter, *, settings: Settings | None = None,
                     llm: LLMClient | None = None, limit: int = 50) -> dict[str, int]:
    """Classify edited messages that were captured but not classified (model was down, rate limit ...).
    Used by scripts/classify_pending.py. Returns counts by result."""
    clf = EditClassifier(engine, writer, settings=settings, llm=llm, notify=False)
    counts: dict[str, int] = {}
    todo = [r for r in all_records(engine, writer.user_id)
            if r["outcome"] == "edited_sent" and r.get("edit_class") is None][:limit]
    for r in todo:
        res = clf.classify(r["message_id"], "catch_up")
        key = res.status if res.status != "skipped" else f"skipped:{res.reason}"
        counts[key] = counts.get(key, 0) + 1
    return counts
