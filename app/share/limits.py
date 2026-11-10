"""Content-based hard limits for share-to-draft (decisions 5.2, 5.10 pt 2, 5.12 pt 2, 5.21 pt 1).

INTERIM STAND-IN for the autonomy engine (D22, out of scope here). The brief needs the ask-first path now, so this
is a small, conservative keyword check on the MASKED text. It runs BEFORE any model call, so an ask-first message
never reaches the model provider. It only adds caution. Replace with D22 (meaning-based matching) later.

Content limits only: money, emotional, the user's own topics, and confidence below the floor (value deferred to
post-testing, Q1: unset = off). The contact-based limit is NOT applied here: on draft-only channels an untagged
or new sender still gets a draft (5.21 pt 1).
"""
import re
from dataclasses import dataclass

from app.config import Settings

MONEY = "money"
EMOTIONAL = "emotional"
CUSTOM_TOPIC = "custom_topic"
LOW_CONFIDENCE = "low_confidence"

_MONEY = re.compile(
    r"(?:₹|\brs\.?|\binr\b|\$|€|£|\busd\b|\beur\b)\s*\d"                     # a currency sign / code with an amount
    r"|\d[\d,]*(?:\.\d+)?\s*(?:₹|rs\b|rupees?|rupaye|rupay|inr\b|usd\b|dollars?|bucks)"
    r"|\b(?:pay|paid|payment|payments|refund|reimburse\w*|invoice|upi|loan|salary|emi|cheque|paisa|paise|paisay"
    r"|rupees?|rupaye|rupay)\b"
    r"|\btransfer(?:red)?\s+(?:the\s+)?(?:money|amount|funds)\b",
    re.IGNORECASE)

_EMOTIONAL = re.compile(
    r"\b(?:depress\w*|anxiety|anxious|panic|suicid\w*|self[- ]harm|breakup|break up|heartbr\w*|crying|grief|grieving"
    r"|passed away|funeral|miscarriage|abus\w+|harass\w*|lonely|hopeless|devastated|miss you|love you|mental health"
    r"|therap\w+|upset|stressed|scared|sad|died|death|udaas|dukhi|rona|mar gaya|guzar gaye|nidhan)\b",
    re.IGNORECASE)

_REASONS = {
    MONEY: ("Asked you first: this looks like it involves money.",
            "Money questions always come to you. This does not change with practice."),
    EMOTIONAL: ("Asked you first: this looks like an emotional message.",
                "Emotional messages always come to you. This does not change with practice."),
    LOW_CONFIDENCE: ("Asked you first: I am not sure enough about this one.",
                     "My confidence was below the floor you set, so I held the draft back."),
}


@dataclass(frozen=True)
class LimitHit:
    kind: str                    # money | emotional | custom_topic | low_confidence
    headline: str                # "Asked you first: ..."  (names the trigger, 5.12 pt 2)
    detail: str                  # one more line of why
    topic: str | None = None     # the matched custom topic


def _topics(settings: Settings) -> list[str]:
    return [t.strip() for t in (settings.share_always_ask_topics or "").split(",") if t.strip()]


def check_text(masked_text: str, settings: Settings) -> LimitHit | None:
    """Content limits that need only the (masked) incoming text. First match wins: topics, money, emotional."""
    text = masked_text or ""
    for topic in _topics(settings):
        if re.search(r"(?<!\w)" + re.escape(topic) + r"(?!\w)", text, re.IGNORECASE):
            return LimitHit(
                CUSTOM_TOPIC, f"Asked you first: this matches your topic '{topic}'.",
                f"You told me to always check with you about {topic}. Topics you add only ever make me more "
                "careful, so this does not change with practice.", topic)
    if _MONEY.search(text):
        return LimitHit(MONEY, *_REASONS[MONEY])
    if _EMOTIONAL.search(text):
        return LimitHit(EMOTIONAL, *_REASONS[EMOTIONAL])
    return None


def check_confidence(confidence: float, settings: Settings) -> LimitHit | None:
    """Below the floor = hard limit (5.10 pt 2). The floor is unset (off) until Q1 is decided after testing."""
    floor = settings.share_confidence_floor
    if floor is not None and confidence < floor:
        return LimitHit(LOW_CONFIDENCE, *_REASONS[LOW_CONFIDENCE])
    return None
