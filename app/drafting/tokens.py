"""Masked-token handling for drafts (decisions 5.17 pt 3).

Incoming text carries placeholders: [phone 1], [email 1], [id 1], [address 1], [link: domain] (and today's
[PHONE] / [EMAIL]), and pseudonyms like `Person A`. A draft may reuse them but must keep them EXACTLY, and
must not invent new ones. Keeping them intact is what lets the classifier see a changed value as a fact edit.
"""
import re

TOKEN_RE = re.compile(r"\[[A-Za-z][^\[\]\n]{0,40}\]|\bPerson [A-Z]\b")


def extract_tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def _key(tok: str) -> str:
    return " ".join(tok.split()).casefold()


def enforce_tokens(draft: str, allowed_sources: list[str]) -> tuple[str, list[str]]:
    """Return (draft with harmless case / spacing slips repaired, tokens that are still not allowed).

    Allowed tokens = every token in `allowed_sources` (the incoming message and the notes shown to the model).
    A token that matches an allowed one apart from case or spacing is silently repaired; anything else
    (a new or altered token) is reported so the drafter can retry or fail."""
    allowed: dict[str, str] = {}
    for src in allowed_sources:
        for t in extract_tokens(src):
            allowed.setdefault(_key(t), t)
    bad: list[str] = []

    def fix(m: re.Match) -> str:
        t = m.group(0)
        canon = allowed.get(_key(t))
        if canon is None:
            bad.append(t)
            return t
        return canon

    return TOKEN_RE.sub(fix, draft or ""), bad
