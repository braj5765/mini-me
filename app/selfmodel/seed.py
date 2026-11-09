"""Seed the self-model from Braj's OWN sent messages only (decisions 4 step 1, 5.3 A, 5.13 pt 3).

Input is a list of own lines. Nothing else is accepted: `seed_self_model` takes `own_lines` and has no way to
receive anyone else's text, and a file that looks like a chat export (timestamps / "Name:" prefixes) is
refused outright, so a pasted WhatsApp export cannot be seeded by mistake. (Importing an export properly,
keeping only the user's own lines, is cold start D7.)

Two kinds of item come out:
  - voice examples: up to `max_exemplars` verbatim own lines (source `seed_own_line`), masked.
  - notes: voice / decision / fact notes written by a model from the own lines (source `seed_own_lines`).
    The model sees only own text, so the call is declared own text (no third-party text is involved).
Everything is editable afterwards. Re-running is safe: identical text is skipped; `replace=True` archives the
earlier seed items first.
"""
import json
import logging
import re

from app.drafting.prompts_loader import load_prompt, render
from app.llm.base import LLMClient, Message
from app.privacy.masking import mask_text
from app.selfmodel.store import (
    MAX_ITEM_CHARS, SRC_EXEMPLAR, SRC_EXTRACTED, Layer, SelfModelError, SelfModelStore,
)

log = logging.getLogger("minime.seed")

SEEDER_VERSION = "seeder-v0.2"
MIN_LINES = 5
MAX_LINES_TO_MODEL = 120
MAX_CHARS_TO_MODEL = 8000
MAX_NOTES = {"voice": 8, "decisions": 6, "facts": 6}
_LAYER_FOR_KEY = {"voice": Layer.voice, "decisions": Layer.decision, "facts": Layer.fact}

# "12/03/2026, 9:41 pm - Riya: ..." / "[12/03/26, 21:41:05] Riya: ..." / "2026-03-12 21:41 Riya: ..."
_EXPORT_LINE = re.compile(
    r"^\s*\[?\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4},?\s+\d{1,2}:\d{2}(:\d{2})?\s*([ap]m)?\]?\s*[-–]?\s*[^:\n]{1,40}:",
    re.I,
)


def parse_own_lines(text: str) -> list[str]:
    """Messages separated by blank lines; lines starting with # are comments. A single message may span lines."""
    blocks, cur = [], []
    for raw in (text or "").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        if raw.strip():
            cur.append(raw.strip())
        elif cur:
            blocks.append(" ".join(cur))
            cur = []
    if cur:
        blocks.append(" ".join(cur))
    return blocks


def refuse_chat_exports(lines: list[str]) -> None:
    hits = sum(1 for ln in lines if _EXPORT_LINE.match(ln))
    if hits and hits >= max(1, len(lines) // 5):
        raise SelfModelError(
            "This looks like a chat export (timestamps with sender names). Seed accepts only your own sent "
            "messages, one per block. Remove other people's lines, or use the import step planned for D7."
        )


def _dedupe(lines: list[str]) -> list[str]:
    seen, out = set(), []
    for ln in lines:
        k = " ".join(ln.lower().split())
        if k and k not in seen:
            seen.add(k)
            out.append(ln)
    return out


def _spread(items: list[str], n: int) -> list[str]:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _parse_notes(raw: str) -> dict:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise SelfModelError("The model did not return JSON notes.")
    try:
        data = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        raise SelfModelError("The model returned invalid JSON notes.") from None
    return data if isinstance(data, dict) else {}


def seed_self_model(store: SelfModelStore, own_lines: list[str], *, llm: LLMClient | None = None,
                    source_ref: str = "own_lines", max_exemplars: int = 15, replace: bool = False) -> dict:
    """Seed the store. Returns counts. `llm=None` stores only the verbatim examples."""
    lines = _dedupe([ln.strip() for ln in own_lines if ln and ln.strip()])
    if len(lines) < MIN_LINES:
        raise SelfModelError(f"Need at least {MIN_LINES} of your own messages to seed; got {len(lines)}.")
    refuse_chat_exports(lines)
    masked = [mask_text(ln) for ln in lines]

    archived = 0
    if replace:
        archived = store.archive_source("seed_")

    added = {"voice_examples": 0, "voice": 0, "decision": 0, "fact": 0, "skipped_duplicates": 0}
    for ln in _spread([m for m in masked if len(m) <= MAX_ITEM_CHARS], max_exemplars):
        if store.has_text(Layer.voice, ln):
            added["skipped_duplicates"] += 1
            continue
        store.add_item(Layer.voice, ln, SRC_EXEMPLAR, source_ref)
        added["voice_examples"] += 1

    if llm is not None:
        sample, size = [], 0
        for ln in _spread(masked, MAX_LINES_TO_MODEL):
            if size + len(ln) > MAX_CHARS_TO_MODEL:
                break
            sample.append(f"- {ln}")
            size += len(ln)
        raw = llm.complete(
            [Message("system", load_prompt(SEEDER_VERSION, "system")),
             Message("user", render(load_prompt(SEEDER_VERSION, "user"), lines="\n".join(sample)))],
            max_tokens=3000, temperature=0.2,
            contains_third_party_text=False,  # own lines only: enforced above, never other people's text
        )
        notes = _parse_notes(raw)
        for key, layer in _LAYER_FOR_KEY.items():
            for text in (notes.get(key) or [])[: MAX_NOTES[key]]:
                if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_ITEM_CHARS:
                    continue
                if store.has_text(layer, text):
                    added["skipped_duplicates"] += 1
                    continue
                store.add_item(layer, text, SRC_EXTRACTED, source_ref)
                added[layer.value] += 1
    return {"own_lines_read": len(lines), "archived_earlier_seed_items": archived, **added,
            "seeder_version": SEEDER_VERSION if llm is not None else None}
