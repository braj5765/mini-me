"""Results view (D11a, v0.1 part): the numbers behind Telegram `/stats`.

Definitions (decisions 5.7, 5.13 pts 2/4/5, 5.15, 5.18; all build-level layout choices are in the dev doc):

- Scored message = captured outcome that is a real accept / edit / reject of a REAL draft:
    sent as is, edited and classified, discarded, flagged not-me.
- Never scored (counted separately, never in a denominator): stub drafts (`draft_generated.is_stub`), outcome
  unknown, not_replying (when built), waiting (incl. a blind draft whose reply has not come), ask-first /
  user-authored replies, edits not classified yet. A message with no draft at all (draft failed) is counted
  once under "no draft made".
- Edited messages are counted by their heaviest class: decision > fact > voice > cosmetic.
- Light vs heavy: the boundary is a placeholder until Q1a, so both read "not set yet". What is known now is shown
  instead: decision / fact edits are never light (5.13 pt 4); voice / cosmetic edits are "undecided".
- North star (5.7): share of scored messages handled with no or light edits. Because light vs heavy is unset it is
  a RANGE: low = sent as is only; high = sent as is + every voice / cosmetic edit. Discarded counts in the
  denominator. All-time for v0.1 (weekly windows are D15).
- Rows are grouped by the draft's `data_mode` (own_or_made_up | third_party_guarded | unlabelled for older rows) so
  v0.1 numbers can never be mistaken for real results.
No accuracy claim is made anywhere in this view.
"""
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from app.classifier import rules
from app.events.queries import all_records, draft_payloads, unknown_reasons

NEVER_BLIND_CHANNELS = ("shortcut",)   # 5.20 pt 4: no blind sample, so excluded from the anchoring comparison
OWN = "own_or_made_up"
GUARDED = "third_party_guarded"
UNLABELLED = "unlabelled"

_MODE_TITLE = {
    OWN: "OWN / MADE-UP DATA - a demo of the loop, not results",
    GUARDED: "THIRD-PARTY DATA (guarded path)",
    UNLABELLED: "OLDER ROWS WITH NO DATA MODE - treat as demo",
}


@dataclass
class Group:
    scored: int = 0
    as_is: int = 0


@dataclass
class Block:
    mode: str
    total: int = 0
    as_is: int = 0
    edited: dict[str, int] = field(default_factory=lambda: {c: 0 for c in rules.CLASSES})
    unclassified: int = 0
    discarded: int = 0
    not_me: int = 0
    unknown: int = 0
    unknown_reasons: dict[str, int] = field(default_factory=dict)
    not_replying: int = 0
    waiting: int = 0
    waiting_blind: int = 0
    user_authored: int = 0
    ask_first: int = 0
    other: int = 0
    shown: Group = field(default_factory=Group)
    blind: Group = field(default_factory=Group)
    # Drafts from channels that never hold a draft (the share shortcut, 5.20 pt 4). Scored like any other, but kept
    # out of the shown-vs-blind comparison so the anchoring gap is only computed where blind items exist.
    unblinded: Group = field(default_factory=Group)

    @property
    def edited_total(self) -> int:
        return sum(self.edited.values())

    @property
    def scored(self) -> int:
        return self.as_is + self.edited_total + self.discarded + self.not_me

    @property
    def never_light(self) -> int:
        return sum(self.edited[c] for c in rules.NEVER_LIGHT)

    @property
    def undecided(self) -> int:
        """Voice + cosmetic edits: light or heavy depending on the boundary Q1a will set."""
        return self.edited["voice"] + self.edited["cosmetic"]

    def north_star(self) -> tuple[int, int, int] | None:
        """(low, high, scored) or None with nothing scored. Counts, not percentages."""
        if self.scored == 0:
            return None
        return self.as_is, self.as_is + self.undecided, self.scored


@dataclass
class Stats:
    blocks: dict[str, Block] = field(default_factory=dict)
    stub_excluded: int = 0
    no_draft: int = 0
    ask_first_no_draft: int = 0     # shared items I asked you first about: no draft was ever made
    total_rows: int = 0


def compute_stats(engine: Engine, user_id: str = "braj") -> Stats:
    records = all_records(engine, user_id)
    drafts = draft_payloads(engine)
    reasons = unknown_reasons(engine)
    st = Stats(total_rows=len(records))
    for r in records:
        meta = drafts.get(r["message_id"])
        if meta is None:
            if r.get("autonomy_decision") == "ask_first":
                st.ask_first_no_draft += 1   # asked first, no draft made (5.13 pt 5): never scored
            else:
                st.no_draft += 1             # draft failed: never a draft, never scored
            continue
        if meta.get("is_stub"):
            st.stub_excluded += 1
            continue
        mode = meta.get("data_mode") or UNLABELLED
        b = st.blocks.setdefault(mode, Block(mode))
        b.total += 1
        _count(b, r, reasons.get(r["message_id"]))
    return st


def _count(b: Block, r: dict, unknown_reason: str | None) -> None:
    outcome, blind = r.get("outcome"), r.get("shown_state") == "blind"
    captured = outcome in ("sent_as_is", "edited_sent", "discarded", "not_me")
    if captured and r.get("autonomy_decision") == "ask_first":
        b.ask_first += 1                     # 5.13 pt 5: out of the north star denominator
        return
    if captured and not r.get("scorable"):
        b.other += 1
        return

    scored_as_is = False
    if outcome == "sent_as_is":
        b.as_is += 1
        scored_as_is = True
    elif outcome == "edited_sent":
        ec = r.get("edit_class")
        if ec in b.edited:
            b.edited[ec] += 1
        else:
            b.unclassified += 1              # captured, model has not read it yet: not scored
            return
    elif outcome == "discarded":
        b.discarded += 1
    elif outcome == "not_me":
        b.not_me += 1
    elif outcome == "unknown":
        b.unknown += 1
        key = unknown_reason or "unspecified"
        b.unknown_reasons[key] = b.unknown_reasons.get(key, 0) + 1
        return
    elif outcome == "not_replying":          # 5.17 pt 4, when D4b builds it
        b.not_replying += 1
        return
    elif outcome == "user_authored":
        b.user_authored += 1
        return
    elif outcome == "waiting":
        b.waiting += 1
        b.waiting_blind += 1 if blind else 0
        return
    else:
        b.other += 1
        return

    if r.get("channel") in NEVER_BLIND_CHANNELS:
        g = b.unblinded
    else:
        g = b.blind if blind else b.shown
    g.scored += 1
    g.as_is += 1 if scored_as_is else 0


# ------------------------------------------------------------------------------------------ text
def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d)}%"


_REASON_WORDS = {"capture_window_expired": "window expired", "draft_failed": "draft failed"}


def _block_text(b: Block) -> list[str]:
    out = [f"{_MODE_TITLE.get(b.mode, b.mode)} ({b.total} message{'s' if b.total != 1 else ''})"]
    extra = f" · shared from phone, never blind {b.unblinded.scored}" if b.unblinded.scored else ""
    out.append(f"Scored: {b.scored} (shown {b.shown.scored} · blind {b.blind.scored}{extra})")
    out.append(f"• Sent as is: {b.as_is}")
    out.append("• Light edits: not set yet")
    out.append("• Heavy edits: not set yet")
    e = b.edited
    out.append(f"  Edited in total: {b.edited_total} - decision {e['decision']} · fact {e['fact']} · "
               f"voice {e['voice']} · cosmetic {e['cosmetic']}")
    out.append(f"  (heaviest class per message; decision and fact are never light: {b.never_light}; "
               f"voice and cosmetic could be light or heavy: {b.undecided})")
    if b.unclassified:
        out.append(f"  Edited but not classified yet: {b.unclassified} (not counted)")
    out.append(f"• Discarded: {b.discarded}")
    if b.not_me:
        out.append(f"• Flagged as not me: {b.not_me}")

    ns = b.north_star()
    if ns is None:
        out.append("No-or-light-edit share (north star): not enough scored messages yet")
    else:
        lo, hi, n = ns
        out.append(f"No-or-light-edit share (north star): {_pct(lo, n)} to {_pct(hi, n)} "
                   f"({lo} to {hi} of {n}). A range because light vs heavy is not set yet (Q1a).")
    out.append(f"Shown vs blind: shown {b.shown.scored} scored, {b.shown.as_is} as is · "
               f"blind {b.blind.scored} scored, {b.blind.as_is} as is")

    skipped = [f"outcome unknown {b.unknown}"]
    if b.unknown_reasons:
        skipped[0] += " (" + ", ".join(f"{_REASON_WORDS.get(k, k)} {v}"
                                       for k, v in sorted(b.unknown_reasons.items())) + ")"
    skipped.append(f"not replying {b.not_replying}")
    skipped.append(f"waiting {b.waiting}" + (f" ({b.waiting_blind} blind, draft held)" if b.waiting_blind else ""))
    if b.user_authored or b.ask_first:
        skipped.append(f"ask-first / your own reply {b.user_authored + b.ask_first}")
    if b.other:
        skipped.append(f"other {b.other}")
    out.append("Not scored: " + " · ".join(skipped))
    return out


def format_stats(st: Stats) -> str:
    lines = ["Mini Me stats", "These are a demo of the loop, not results: no accuracy is claimed.", ""]
    order = [m for m in (OWN, UNLABELLED, GUARDED) if m in st.blocks] + \
            [m for m in st.blocks if m not in (OWN, UNLABELLED, GUARDED)]
    if not order:
        lines.append("No real drafts yet. Forward or paste a message and I'll start counting.")
    for m in order:
        lines += _block_text(st.blocks[m]) + [""]
    foot = []
    if st.stub_excluded:
        foot.append(f"stub drafts {st.stub_excluded}")
    if st.no_draft:
        foot.append(f"no draft made {st.no_draft}")
    if st.ask_first_no_draft:
        foot.append(f"asked you first, no draft {st.ask_first_no_draft}")
    if foot:
        lines.append("Left out of everything above: " + " · ".join(foot))
    return "\n".join(lines).rstrip()


def stats_text(engine: Engine, user_id: str = "braj") -> str:
    return format_stats(compute_stats(engine, user_id))


def make_stats_provider():
    """For BotDeps.stats_provider: `/stats` reads the database fresh on every call."""
    def provider(deps) -> str:
        return stats_text(deps.engine, deps.writer.user_id)
    return provider
