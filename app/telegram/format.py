"""Message wording for the bot. Calm tone; no gate words ("unlock", "gate", "level up"); no shouty badges.

Confidence labels here are DISPLAY ONLY (High / Medium / Low plus a 3-segment bar, as in the mockup).
They are not autonomy thresholds and set nothing; real values are decided after testing (Q1).
"""
from app.drafting.base import DraftResult

BTN_SENT = "✓ Sent as is"
BTN_DISCARD = "Discard"

HELP = (
    "Mini Me (private). Forward or paste a message here and I'll draft a reply.\n\n"
    "• Under a draft: tap “Sent as is”, or “Discard”.\n"
    "• Changed it before sending? Reply to my draft with what you actually sent "
    "(or type /sent followed by the text).\n"
    "• Now and then I draft quietly and hold it back until you've replied first. "
    "Reply to my note with what you sent, and I'll show you mine after.\n\n"
    "/pending – how many are waiting\n/stats – numbers (coming soon)\n/help – this message\n\n"
    "Nothing is ever sent for you from here: you always press send."
)

STATS_STUB = "Stats are coming soon. For now, everything is recorded in the event log."
BLIND_NOTICE = "I've drafted this one; reply first and I'll show you mine."
NO_TEXT = "I can only read text for now. Paste or forward the message as text."
NOTHING_WAITING = "Nothing is waiting for a reply from you."
ALREADY_RECORDED = "That one is already recorded."


def confidence_label(c: float) -> str:
    return "High" if c >= 0.75 else "Medium" if c >= 0.5 else "Low"


def confidence_bar(c: float) -> str:
    n = 3 if c >= 0.75 else 2 if c >= 0.5 else 1
    return "▰" * n + "▱" * (3 - n)


def draft_message(d: DraftResult) -> str:
    tag = " (stub: placeholder until real drafting is built)" if d.is_stub else ""
    return (
        f"Draft{tag}\n"
        f"Confidence: {confidence_label(d.confidence)} {confidence_bar(d.confidence)} ({d.confidence:.2f})\n"
        f"Why: {d.rationale}\n\n"
        f"{d.text}\n\n"
        "Edited it? Reply to this message with what you sent."
    )


def keyboard(message_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": BTN_SENT, "callback_data": f"sent:{message_id}"},
        {"text": BTN_DISCARD, "callback_data": f"disc:{message_id}"},
    ]]}


def released_message(draft_text: str, matched: bool) -> str:
    note = "Same as what you sent." if matched else "Different from what you sent."
    return f"Here's the draft I held back:\n\n{draft_text}\n\n{note} Recorded."
