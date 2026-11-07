"""Message wording for the bot. Calm tone; no gate words ("unlock", "gate", "level up"); no shouty badges.

Confidence labels here are DISPLAY ONLY (High / Medium / Low plus a 3-segment bar, as in the mockup).
They are not autonomy thresholds and set nothing; real values are decided after testing (Q1).
"""
from app.drafting.base import DraftResult, snippet

BTN_SENT = "✓ Sent as is"
BTN_DISCARD = "Discard"

HELP = (
    "Mini Me (private). Forward or paste a message here and I'll draft a reply.\n\n"
    "• Under a draft: tap “Sent as is”, or “Discard”.\n"
    "• Changed it before sending? Reply to my draft with what you actually sent. "
    "To reply: swipe the draft to the left on a phone, or right-click it and choose Reply on a computer. "
    "(Or type /sent followed by the text; it applies to the newest waiting message.)\n"
    "• Now and then I draft quietly and hold it back until you've replied first. "
    "Reply to my note with what you sent, and I'll show you mine after.\n"
    "• A plain message is always treated as a new message, not as your final text.\n\n"
    "/pending – what is waiting\n/stats – numbers (coming soon)\n/help – this message\n\n"
    "Nothing is ever sent for you from here: you always press send."
)

STATS_STUB = "Stats are coming soon. For now, everything is recorded in the event log."
BLIND_NOTICE = "I've drafted this one; reply first and I'll show you mine."
NO_TEXT = "I can only read text for now. Paste or forward the message as text."
NOTHING_WAITING = "Nothing is waiting for a reply from you."
ALREADY_RECORDED = "That one is already recorded."

REPLY_HINT = "Edited it? Reply to this message with what you sent."
STATUS_SENT = "✓ Recorded: sent as is"
STATUS_DISCARDED = "Discarded: not sent"


_DRAFT_FAILED_REASONS = {
    "provider_not_verified": "the model provider is not approved for this kind of text yet",
    "llm_error": "the model could not be reached",
    "bad_output": "the model's answer was not usable",
}


def draft_failed(reason: str) -> str:
    why = _DRAFT_FAILED_REASONS.get(reason, "something went wrong")
    return f"I couldn't draft this one: {why}. Nothing was saved as a draft. Reply yourself if you like."


def confidence_label(c: float) -> str:
    return "High" if c >= 0.75 else "Medium" if c >= 0.5 else "Low"


def confidence_bar(c: float) -> str:
    n = 3 if c >= 0.75 else 2 if c >= 0.5 else 1
    return "▰" * n + "▱" * (3 - n)


def ref(masked_incoming: str | None) -> str:
    """Short quote of the (already masked) message a draft belongs to."""
    return f'"{snippet(masked_incoming or "", 40)}"'


def draft_message(d: DraftResult, number: int | None = None, masked_incoming: str | None = None) -> str:
    tag = " (stub: placeholder until real drafting is built)" if d.is_stub else ""
    head = f"Draft #{number}{tag}" if number else f"Draft{tag}"
    re_line = f"Re: {ref(masked_incoming)}\n" if masked_incoming else ""
    return (
        f"{head}\n{re_line}"
        f"Confidence: {confidence_label(d.confidence)} {confidence_bar(d.confidence)} ({d.confidence:.2f})\n"
        f"Why: {d.rationale}\n\n"
        f"{d.text}\n\n"
        f"{REPLY_HINT}"
    )


def with_status(original: str, status: str) -> str:
    """The same draft message, with the 'Edited it?' hint swapped for what happened."""
    body = original[: -len(REPLY_HINT)].rstrip() if original.endswith(REPLY_HINT) else original.rstrip()
    return f"{body}\n\n{status}"


def saved(number: int, matched: bool) -> str:
    return f"Saved #{number}: " + ("sent as written." if matched else "edited before sending.")


def pending_list(items: list[tuple[int, str, str]]) -> str:
    """items: (number, masked incoming text, state); state is 'shown' or 'blind_held'."""
    if not items:
        return NOTHING_WAITING
    lines = [f"{len(items)} waiting for you:"]
    for n, text, state in items[:10]:
        lines.append(f"#{n} · {ref(text)}" + (" (my draft is held back)" if state == "blind_held" else ""))
    if len(items) > 10:
        lines.append(f"...and {len(items) - 10} more.")
    return "\n".join(lines)


def nudge(number: int) -> str:
    return (f"I treated that as a new message. If it was your final reply to #{number}, "
            f"reply to my message for #{number} instead (swipe it left, or right-click and choose Reply).")


def keyboard(message_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": BTN_SENT, "callback_data": f"sent:{message_id}"},
        {"text": BTN_DISCARD, "callback_data": f"disc:{message_id}"},
    ]]}


def released_message(draft_text: str, matched: bool, number: int | None = None) -> str:
    note = "Same as what you sent." if matched else "Different from what you sent."
    head = f"Here's the draft I held back for #{number}:" if number else "Here's the draft I held back:"
    return f"{head}\n\n{draft_text}\n\n{note} Recorded."
