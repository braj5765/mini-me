"""Real drafter (D8a, v0.1). Implements the `Drafter` interface; replaces the stub.

Flow for one message (decisions 4 step 2, 5.2 confidence, 5.16, 5.17 pt 3):
  1. Pick self-model items for the message (SelfModelStore.retrieve).
  2. Build the prompt from versioned files (prompts/drafter-v0.1), label items V1.., D1.., F1..
  3. One LLM call through app.llm.get_llm() (provider / model from LLM_PROVIDER / LLM_MODEL).
  4. Parse JSON {draft, confidence, rationale, used}; check tokens; retry ONCE on bad output.
  5. Return text, confidence, one-line rationale, ids of the self-model items used.

LLM guard: incoming text is third-party text, so by default the call is flagged
`contains_third_party_text=True` and the guard in app/llm/base.py refuses it for any provider not marked
no-training-verified. v0.1 (own / made-up messages only, decision given 7 Nov 2026) can switch on an explicit
OWN_DATA_ONLY_MODE: the drafter then declares the text own / made-up. The guard itself is not changed.
Every draft logs `data_mode` so this stays auditable. REMOVE the mode before real third-party messages (D3b/M2).
"""
import json
import logging
import re
import time

from app.config import Settings, get_settings
from app.drafting.base import DraftError, DraftRequest, DraftResult, UsedItem
from app.drafting.prompts_loader import load_prompt, render
from app.drafting.tokens import enforce_tokens
from app.llm.base import LLMClient, LLMError, Message
from app.selfmodel.store import SelfModelStore

log = logging.getLogger("minime.drafter")

PROMPT_VERSION = "drafter-v0.1"
CONFIDENCE_METHOD = "conf-0.1"
DRAFTER_NAME = "llm"
TEMPERATURE = 0.5
MAX_DRAFT_CHARS = 1500
MAX_RATIONALE_CHARS = 140
MAX_CONFIDENCE, MIN_CONFIDENCE = 0.95, 0.05

_GATE_WORDS = re.compile(r"\b(unlock\w*|gate\w*|level[- ]?up)\b", re.I)
_LABEL_PREFIX = {"voice": "V", "decision": "D", "fact": "F"}
_LAYER_EMPTY = "(nothing yet)"


class _BadOutput(Exception):
    pass


def _label_items(retrieved: dict[str, list[dict]]) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Return (prompt lines per layer, label -> item)."""
    lines: dict[str, list[str]] = {}
    by_label: dict[str, dict] = {}
    for layer, items in retrieved.items():
        lines[layer] = []
        for n, it in enumerate(items, 1):
            label = f"{_LABEL_PREFIX[layer]}{n}"
            by_label[label] = it
            lines[layer].append(f"- {label}: {it['text']}")
    return lines, by_label


def _parse(raw: str) -> dict:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
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


def _clean_rationale(text: object) -> str:
    r = " ".join(str(text or "").split())
    if not r or _GATE_WORDS.search(r):
        return ""
    return r if len(r) <= MAX_RATIONALE_CHARS else r[: MAX_RATIONALE_CHARS - 3].rstrip(" ,;:") + "..."


def _fallback_rationale(used: list[dict]) -> str:
    for layer, lead in (("decision", "Followed your habit:"), ("fact", "Used a fact about you:"),
                        ("voice", "Matched your usual style:")):
        for it in used:
            if it["layer"] == layer:
                t = it["text"].rstrip(".")
                return f"{lead} {t[:90]}" if len(t) <= 90 else f"{lead} {t[:87]}..."
    return "Written in your usual style; no specific habit or fact applied."


def _confidence(model_conf: object, used: list[dict]) -> float:
    """conf-0.1: blend of the model's own estimate (65%) and how much of the self-model backs the draft (35%).
    Support = 0.4 base (voice only), +0.3 if a decision rule was cited, +0.3 if a fact was cited. The model
    decides what is relevant, so this is only as honest as its citations. Uncalibrated (display only, no
    thresholds hang on it); D9a data will be used to tune it."""
    try:
        m = float(model_conf)
    except (TypeError, ValueError):
        m = 0.4
    m = min(1.0, max(0.0, m))
    support = 0.4
    if any(i["layer"] == "decision" for i in used):
        support += 0.3
    if any(i["layer"] == "fact" for i in used):
        support += 0.3
    return round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, 0.65 * m + 0.35 * support)), 2)


def _short(text: str, n: int = 60) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


class LLMDrafter:
    name = DRAFTER_NAME
    version = PROMPT_VERSION

    def __init__(self, llm: LLMClient, store: SelfModelStore, *, settings: Settings | None = None):
        self.llm, self.store = llm, store
        self.settings = settings or get_settings()

    # -------------------------------------------------------------- public
    def draft(self, req: DraftRequest) -> DraftResult:
        retrieved = self.store.retrieve(req.masked_text)
        lines, by_label = _label_items(retrieved)
        system = load_prompt(PROMPT_VERSION, "system")
        user = render(
            load_prompt(PROMPT_VERSION, "user"),
            voice="\n".join(lines["voice"]) or _LAYER_EMPTY,
            decision="\n".join(lines["decision"]) or _LAYER_EMPTY,
            fact="\n".join(lines["fact"]) or _LAYER_EMPTY,
            channel=req.channel, contact_type=req.contact_type,
            masked_text=req.masked_text.replace("</incoming_message>", "< /incoming_message>"),
        )
        allowed = [req.masked_text, *[i["text"] for items in retrieved.values() for i in items]]
        own_mode = bool(self.settings.own_data_only_mode)

        messages = [Message("system", system), Message("user", user)]
        t0 = time.monotonic()
        retries = 0
        last_problem = ""
        while True:
            try:
                raw = self.llm.complete(messages, max_tokens=self.settings.drafter_max_tokens, temperature=TEMPERATURE,
                                        contains_third_party_text=not own_mode)
            except LLMError as e:
                msg = str(e)
                reason = ("provider_not_verified" if "no-training" in msg
                          else "rate_limited" if "429" in msg else "llm_error")
                raise DraftError(reason, str(e)) from None
            try:
                text, data = self._validate(raw, allowed)
                break
            except _BadOutput as bad:
                last_problem = str(bad)
                if retries >= 1:
                    raise DraftError("bad_output", f"Model output rejected twice: {last_problem}") from None
                retries += 1
                messages = [*messages, Message("assistant", (raw or "")[:1500]),
                            Message("user", f"That output was rejected: {last_problem}. "
                                            "Return only the corrected JSON object.")]
        llm_ms = int((time.monotonic() - t0) * 1000)

        used_items: list[dict] = []
        for label in data.get("used") or []:
            it = by_label.get(str(label).strip().upper())
            if it is not None and it not in used_items:
                used_items.append(it)

        rationale = _clean_rationale(data.get("rationale")) or _fallback_rationale(used_items)
        return DraftResult(
            text=text,
            confidence=_confidence(data.get("confidence"), used_items),
            rationale=rationale,
            items_used=[UsedItem(layer=i["layer"], item_id=i["item_id"], label=_short(i["text"])) for i in used_items],
            drafter=DRAFTER_NAME,
            drafter_version=PROMPT_VERSION,
            is_stub=False,
            meta={
                "prompt_version": PROMPT_VERSION, "confidence_method": CONFIDENCE_METHOD,
                "provider": self.llm.provider, "model": self.llm.model, "llm_ms": llm_ms, "retries": retries,
                "data_mode": "own_or_made_up" if own_mode else "third_party_guarded",
                "self_model_version": self.store.version_tag(),
                "retrieved": {k: len(v) for k, v in retrieved.items()},
            },
        )

    # -------------------------------------------------------------- checks
    def _validate(self, raw: str, allowed: list[str]) -> tuple[str, dict]:
        data = _parse(raw)
        text = data.get("draft")
        if not isinstance(text, str) or not text.strip():
            raise _BadOutput("'draft' is missing or empty")
        text = text.strip()
        if len(text) > MAX_DRAFT_CHARS:
            raise _BadOutput(f"'draft' is longer than {MAX_DRAFT_CHARS} characters")
        text, bad = enforce_tokens(text, allowed)
        if bad:
            raise _BadOutput("the draft contains placeholders that are not in the message: " + ", ".join(bad[:3]))
        return text, data
