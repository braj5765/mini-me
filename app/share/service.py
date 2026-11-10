"""Share-to-draft service (Dev 6): the one front door for Android share entry points (decisions 5.20, 5.21).

Pipeline (the existing one, unchanged): mask locally -> store masked -> content limits -> LLM guard -> drafter ->
event log. Called by the token route (`app/share/router.py`, channel `shortcut`) and, later, by Dev 7's web share
target (channel `web_share`, cookie login) directly.

Rules applied here:
- Masked text only is stored, logged or sent to a model. Real values are restored into the RETURN value only
  (5.21 pt 2); nothing real is ever written anywhere.
- Contact type is `unknown` (no sender on a share). On these draft-only channels that never stops a draft and is
  never autonomy-eligible: no shadow "would have sent" is logged (5.21 pt 1).
- Content-based hard limits (money, emotional, your topics, confidence below the floor) mean ask first: no draft.
- Shortcut drafts are never blind (5.20 pt 4). They are tagged `blind_eligible: false` so the anchoring gap and the
  shown-vs-blind comparison leave them out.
- Each item opens the same capture window (24 h) as Telegram; no capture = outcome unknown (never scored).
- Copy is not sent: `log_copied` writes only a `draft_copied` event and changes no outcome (5.20 pt 3).
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.engine import Engine

from app.config import Settings
from app.drafting.base import DraftError, DraftRequest, Drafter
from app.events import EventName, EventWriter
from app.events.queries import events_for_message
from app.events.writer import make_key
from app.privacy.masking import mask_text, mask_with_values, pseudonymise
from app.share import limits
from app.share.restore import restore_values
from app.telegram.format import confidence_label
from app.telegram.store import ThreadStore

log = logging.getLogger("minime.share")

SHARE_CHANNELS = ("shortcut", "web_share")
TOKEN_ROUTE_CHANNELS = ("shortcut",)   # `web_share` is reserved for Dev 7's web app (cookie login)
DEVICE_TYPE = "mobile"
MODE = "shadow"                        # draft-only channels: live autonomy is Gmail only (5.13 pt 1)
CONTACT_TYPE = "unknown"               # 5.17 pt 1


class ShareError(Exception):
    """A request the service refuses. `status` is the HTTP status the route should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class ShareOutcome:
    message_id: str
    channel: str
    domain: str
    action: str                              # draft | ask_first | failed
    masked_text: str = ""                    # what the model (and the database) saw
    masked_count: int = 0
    draft: str | None = None                 # real values restored; for THIS response only, never stored
    unrestored: list[str] = field(default_factory=list)
    confidence: float | None = None
    confidence_label: str | None = None
    why: str | None = None
    reason: dict | None = None               # ask_first: {code, headline, detail, topic}
    error: str | None = None                 # failed: short code (provider_not_verified, rate_limited, ...)
    window_hours: float = 24.0


def compose_text(text: str | None, title: str | None = None, url: str | None = None) -> str:
    """Design §8 note 1: a shared URL is appended to the text. A title is used only when there is no text."""
    t = (text or "").strip()
    title, url = (title or "").strip(), (url or "").strip()
    if not t and title:
        t = title
    if url and url not in t:
        t = f"{t}\n{url}".strip()
    return t


def _chat_id(settings: Settings) -> str:
    raw = (settings.telegram_allowed_user_id or "").strip()
    return raw if raw.lstrip("-").isdigit() else "share"   # Telegram private chat id = the user's id


def _common(channel: str, domain: str) -> dict:
    return {"channel": channel, "domain": domain, "mode": MODE, "device_type": DEVICE_TYPE,
            "contact_type": CONTACT_TYPE}


def create_share_draft(
    engine: Engine, writer: EventWriter, settings: Settings, drafter: Drafter, *,
    text: str, title: str | None = None, url: str | None = None, channel: str = "shortcut",
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ShareOutcome:
    if channel not in SHARE_CHANNELS:
        raise ShareError(422, f"Unknown channel '{channel}'.")
    raw = compose_text(text, title, url)
    if not raw:
        raise ShareError(422, "Nothing to draft: the shared text is empty.")
    if len(raw) > settings.share_max_chars:
        raise ShareError(413, f"The shared text is longer than {settings.share_max_chars} characters.")

    # 1. Mask locally, before storage and before any model call. `found` (real values) lives in this function only.
    masked, found = mask_with_values(raw)
    mid = "sh-" + uuid.uuid4().hex[:16]
    pseud = pseudonymise(f"shared-{mid}@share.invalid")   # no sender on a share: unique pseudonym, never "known"
    domain = settings.share_default_domain
    common = _common(channel, domain)
    window = settings.telegram_capture_window_hours      # one capture window shared with Telegram (design §8 note 8)
    out = ShareOutcome(message_id=mid, channel=channel, domain=domain, action="draft", masked_text=masked,
                       masked_count=len(found), window_hours=window)

    # 2. Store masked; log the arrival.
    writer.upsert_message(
        mid, channel=channel, domain=domain, contact_type=CONTACT_TYPE, contact_pseudonym=pseud,
        incoming_text=masked, device_type=DEVICE_TYPE, mode=MODE, outcome="waiting")
    writer.log(EventName.message_received, message_id=mid,
               payload={"sender_type": "unknown", "origin": "shared", "masked_count": len(found)}, **common)

    # 3. Content-based hard limits, before any model call (an ask-first message never reaches the provider).
    hit = limits.check_text(masked, settings)
    if hit:
        return _ask_first(engine, writer, settings, out, hit, common, now)

    # 4. Draft (LLM guard lives inside the drafter / app.llm, unchanged).
    t0 = time.monotonic()
    try:
        result = drafter.draft(DraftRequest(
            message_id=mid, masked_text=masked, channel=channel, domain=domain,
            contact_type=CONTACT_TYPE, contact_pseudonym=pseud))
    except Exception as e:  # noqa: BLE001  (DraftError or anything unexpected: never a draft, never scored)
        reason = e.reason if isinstance(e, DraftError) else "error"
        log.warning("drafter failed: %s", reason)
        writer.upsert_message(mid, outcome="unknown")
        writer.log(EventName.outcome_unknown, message_id=mid,
                   payload={"reason": "draft_failed", "draft_error": reason}, **common)
        out.action, out.error = "failed", reason
        return out
    gen_ms = int((time.monotonic() - t0) * 1000)

    draft_masked = mask_text(result.text)
    writer.log(
        EventName.draft_generated, message_id=mid,
        payload={**result.meta, "items_used": [i.__dict__ for i in result.items_used], "drafter": result.drafter,
                 "drafter_version": result.drafter_version, "is_stub": result.is_stub,
                 "confidence": result.confidence, "gen_latency_ms": gen_ms, "shown_state": "shown",
                 "blind_eligible": False,          # 5.20 pt 4: the shortcut never holds drafts
                 "entry": channel},
        **{**common, "self_model_version": result.meta.get("self_model_version")})

    # 5. Confidence floor (hard limit; unset until Q1): the draft is generated but never stored or returned.
    low = limits.check_confidence(result.confidence, settings)
    if low:
        writer.upsert_message(mid, confidence=result.confidence)
        return _ask_first(engine, writer, settings, out, low, common, now)

    # 6. Draft & notify. Never autonomy-eligible here (5.21 pt 1): no shadow "would have sent" is written.
    writer.upsert_message(
        mid, draft_text=draft_masked, confidence=result.confidence, rationale=result.rationale,
        autonomy_decision="draft_notify", shown_state="shown", mode=MODE)
    writer.log(EventName.autonomy_decision, message_id=mid,
               payload={"action": "draft_notify", "autonomy_eligible": False,
                        "reasons": ["draft_only_channel", "untagged_sender"],
                        "trigger": "initial", "rerun_of": None}, **common)
    ThreadStore(engine).create(mid, _chat_id(settings), None, "shown", now(), window)   # capture window + sweep
    writer.log(EventName.draft_shown, message_id=mid, idempotency_key=make_key("draft_shown", mid, 1),
               payload={"view_no": 1, "surface": "api_response"}, **common)

    # 7. Real values go back ONLY in this return value (5.21 pt 2).
    out.draft, out.unrestored = restore_values(result.text, found)
    out.confidence, out.confidence_label, out.why = result.confidence, confidence_label(result.confidence), result.rationale
    return out


def _ask_first(engine: Engine, writer: EventWriter, settings: Settings, out: ShareOutcome,
               hit: limits.LimitHit, common: dict, now: Callable[[], datetime]) -> ShareOutcome:
    """No draft. Log the decision and the trigger; keep the item open so the 24 h sweep can close it."""
    mid = out.message_id
    writer.upsert_message(mid, autonomy_decision="ask_first", hard_limit=hit.kind, topic_triggered=hit.topic)
    if hit.kind == limits.CUSTOM_TOPIC:
        writer.log(EventName.topic_triggered, message_id=mid,
                   idempotency_key=make_key("topic_triggered", mid, hit.topic), payload={"topic": hit.topic}, **common)
    else:
        writer.log(EventName.hard_limit_triggered, message_id=mid,
                   idempotency_key=make_key("hard_limit_triggered", mid, hit.kind),
                   payload={"limit": hit.kind, "source": "content"}, **common)
    writer.log(EventName.autonomy_decision, message_id=mid,
               payload={"action": "ask_first", "autonomy_eligible": False, "limit": hit.kind,
                        "trigger": "initial", "rerun_of": None}, **common)
    ThreadStore(engine).create(mid, _chat_id(settings), None, "shown", now(), out.window_hours)
    out.action = "ask_first"
    out.reason = {"code": hit.kind, "headline": hit.headline, "detail": hit.detail, "topic": hit.topic}
    return out


def log_copied(engine: Engine, writer: EventWriter, message_id: str, copy_no: int | None = None) -> dict:
    """Log `draft_copied` AFTER the client has put the draft on the clipboard. Copy is not sent: no outcome,
    no draft_sent, no score changes (5.20 pt 3). `copy_no` is counted here unless the client passes its own
    (then a retry of the same call is a no-op)."""
    rec = writer.get_message(message_id)
    if not rec or rec.get("channel") not in SHARE_CHANNELS:
        raise ShareError(404, "No such shared draft.")
    if not rec.get("draft_text"):
        raise ShareError(409, "That item has no draft to copy (it was asked first, or drafting failed).")
    if copy_no is None:
        copy_no = 1 + sum(1 for e in events_for_message(engine, message_id) if e["event_name"] == "draft_copied")
    key = make_key("draft_copied", message_id, copy_no)
    common = {"channel": rec["channel"], "domain": rec.get("domain"), "mode": rec.get("mode"),
              "device_type": DEVICE_TYPE, "contact_type": rec.get("contact_type")}
    _, inserted = writer.log(
        EventName.draft_copied, message_id=message_id, idempotency_key=key,
        payload={"shown_state": rec.get("shown_state") or "shown", "copy_no": copy_no,
                 "after_window": rec.get("outcome") == "unknown"}, **common)
    return {"ok": True, "message_id": message_id, "copy_no": copy_no, "logged": inserted,
            "outcome": rec.get("outcome")}   # still "waiting": a copy never counts as sent
