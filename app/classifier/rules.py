"""Deterministic parts of the edit classifier (D9a). No model call in here.

Decisions followed: 5.4 (four classes; 'none' = sent as-is), 5.13 pt 4 (decision and fact edits are never
light), 5.17 pt 3 (a changed numbered token is a fact edit), 5.12 (decision *reversed* is a signal, not a class).

Build-level choices (recorded in the dev doc):
- Class precedence when several apply: decision > fact > voice > cosmetic. The heaviest becomes `edit_class`;
  the full list goes in the event payload.
- Cosmetic-only differences (case, spacing, punctuation, emoji) are detected here, without a model call.
- The light/heavy flag is a placeholder (None) until Q1a; `BOUNDARY_SET` flips when D9b wires it.
"""
import re
import unicodedata

from app.drafting.tokens import extract_tokens

CLASSES = ("decision", "fact", "voice", "cosmetic")          # heaviest first
PRECEDENCE = {c: i for i, c in enumerate(CLASSES)}
NEVER_LIGHT = frozenset({"decision", "fact"})                # 5.13 pt 4
BOUNDARY_SET = False                                          # light/heavy boundary: placeholder until Q1a

_JOINERS = {"‍", "‌", "︎", "️"}           # emoji joiners / variation selectors


def order_classes(classes) -> list[str]:
    """Unique classes, heaviest first. Unknown names are dropped."""
    seen = {c for c in classes if c in PRECEDENCE}
    return sorted(seen, key=PRECEDENCE.__getitem__)


def heaviest(classes) -> str:
    ordered = order_classes(classes)
    return ordered[0] if ordered else "none"


def never_light(edit_class: str) -> bool:
    return edit_class in NEVER_LIGHT


def light_or_heavy(edit_class: str) -> str | None:
    """Placeholder (None) until Q1a sets the boundary. Decision / fact edits are never light, but the flag
    still stays None for them too: 'not light' is carried by `never_light`, so nothing is invented."""
    return None


def _masked_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r"\[[A-Za-z][^\[\]\n]{0,40}\]|\bPerson [A-Z]\b", text)]


def normalise(text: str) -> str:
    """Comparison form: case-folded, whitespace collapsed, punctuation / symbols / emoji removed. Letters, digits
    and combining marks (Devanagari etc.) are kept. A '.' ',' ':' '/' '-' between two digits is kept, so
    '3.5' and '35' stay different. Placeholders such as [phone 1] and 'Person A' are kept whole."""
    s = text or ""
    spans = _masked_spans(s)
    out: list[str] = []
    i = 0
    while i < len(s):
        span = next((sp for sp in spans if sp[0] == i), None)
        if span:
            out.append(" " + " ".join(s[span[0]:span[1]].split()).casefold() + " ")
            i = span[1]
            continue
        ch = s[i]
        cat = unicodedata.category(ch)
        if ch in _JOINERS or cat[0] in ("P", "S", "C"):
            keep = (ch in ".,:/-" and i > 0 and i + 1 < len(s) and s[i - 1].isdigit() and s[i + 1].isdigit())
            out.append(ch if keep else " ")
        else:
            out.append(ch.casefold())
        i += 1
    return " ".join("".join(out).split())


def same_text(a: str, b: str) -> bool:
    """Identical apart from whitespace (this is the bot's own 'sent as is' test)."""
    return " ".join((a or "").split()) == " ".join((b or "").split())


def cosmetic_only(draft: str, sent: str) -> bool:
    """True when the two differ only in case, spacing, punctuation or emoji."""
    return normalise(draft) == normalise(sent)


def _token_key(tok: str) -> str:
    return " ".join(tok.split()).casefold()


def token_changes(draft: str, sent: str) -> dict[str, list[str]]:
    """Placeholders that appear in only one of the two texts (5.17 pt 3). `removed` = in the draft, not in
    what was sent; `added` = in what was sent, not in the draft. Compared as sets, case and spacing ignored.
    With today's unnumbered [PHONE] / [EMAIL] two different values look the same; numbered tokens (D3b) fix that."""
    d = {_token_key(t): t for t in extract_tokens(draft)}
    s = {_token_key(t): t for t in extract_tokens(sent)}
    return {"removed": sorted(d[k] for k in d.keys() - s.keys()),
            "added": sorted(s[k] for k in s.keys() - d.keys())}


def has_token_change(changes: dict[str, list[str]]) -> bool:
    return bool(changes["removed"] or changes["added"])
