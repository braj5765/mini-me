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

Dev 7 additions (decisions 5.21, 5.23):
- Test-mode guard: with TEST_MODE on (until M2) the incoming text is masked, used for the draft, and NOT stored.
- Copy-first guard: text that equals the last draft is refused (the user forgot to copy the message first).
- Web share (`web_share`) draws a blind sample (~1 in 10): the draft is stored masked but never returned until
  the user's reply has been captured (5.15 pt 3).
- Sender tag: `rerun_decision` runs the autonomy decision again after a tag, both decisions stay in the log.
"""
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Engine

from app import db
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

TEST_MODE_LABEL = "Test mode \u2014 made-up or own messages only"
COPY_FIRST_TEXT = ("That is the draft I just made. Copy the message you want a reply to first, "
                   "then try again.")
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
    shown_state: str = "shown"               # shown | blind (blind: no draft, confidence or why is returned)
    test_mode: bool = False                  # the "Test mode" label goes on every draft response (5.23 pt 3)
    message: str | None = None               # copy_first: what to tell the user


def compose_text(text: str | None, title: str | None = None, url: str | None = None) -> str:
    """Design §8 note 1: a shared URL is appended to the text. A title is used only when there is no text."""
    t = (text or "").strip()
    title, url = (title or "").strip(), (url or "").strip()
    if not t and title:
        t = title
    if url and url not in t:
        t = f"{t}\n{url}".strip()
    return t


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def matches_last_draft(engine: Engine, raw_text: str) -> bool:
    """Copy-first guard (5.23 pt 1): True when the text is the last draft this app made (shortcut / web share), so
    the clipboard still holds the draft and the user forgot to copy the message. Compared MASKED against the
    stored masked draft, so a draft with real phone numbers restored still matches. A held blind draft was never
    shown, so it cannot be on the clipboard and is left out."""
    m = db.message_records
    with engine.connect() as conn:
        row = conn.execute(select(m.c.draft_text).where(and_(
            m.c.channel.in_(SHARE_CHANNELS), m.c.draft_text.is_not(None),
            or_(m.c.shown_state.is_(None), m.c.shown_state != "blind")))
            .order_by(m.c.created_at.desc()).limit(1)).first()
    return bool(row and _norm(row[0]) and _norm(row[0]) == _norm(mask_text(raw_text)))


def _chat_id(settings: Settings) -> str:
    raw = (settings.telegram_allowed_user_id or "").strip()
    return raw if raw.lstrip("-").isdigit() else "share"   # Telegram private chat id = the user's id


def _common(channel: str, domain: str) -> dict:
    return {"channel": channel, "domain": domain, "mode": MODE, "device_type": DEVICE_TYPE,
            "contact_type": CONTACT_TYPE}


def create_share_draft(
    engine: Engine, writer: EventWriter, settings: Settings, drafter: Drafter, *,
    text: str, title: str | None = None, url: str | None = None, channel: str = "shortcut",
    origin: str = "shared", entry: str | None = None, retry_of: str | None = None,
    rng: Callable[[], float] = random.random,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ShareOutcome:
    if channel not in SHARE_CHANNELS:
        raise ShareError(422, f"Unknown channel '{channel}'.")
    raw = compose_text(text, title, url)
    if not raw:
        raise ShareError(422, "Nothing to draft: the shared text is empty.")
    if len(raw) > settings.share_max_chars:
        raise ShareError(413, f"The shared text is longer than {settings.share_max_chars} characters.")
    if matches_last_draft(engine, raw):      # nothing is stored, logged or sent to a model
        return ShareOutcome(message_id="", channel=channel, domain=settings.share_default_domain,
                            action="copy_first", message=COPY_FIRST_TEXT, test_mode=settings.test_mode)

    # 1. Mask locally, before storage and before any model call. `found` (real values) lives in this function only.
    masked, found = mask_with_values(raw)
    mid = "sh-" + uuid.uuid4().hex[:16]
    pseud = pseudonymise(f"shared-{mid}@share.invalid")   # no sender on a share: unique pseudonym, never "known"
    domain = settings.share_default_domain
    common = _common(channel, domain)
    window = settings.telegram_capture_window_hours      # one capture window shared with Telegram (design §8 note 8)
    out = ShareOutcome(message_id=mid, channel=channel, domain=domain, action="draft", masked_text=masked,
                       masked_count=len(found), window_hours=window, test_mode=settings.test_mode)
    entry = entry or channel

    # 2. Store masked (test-mode guard, 5.23 pt 3: the incoming text is NOT stored while TEST_MODE is on);
    #    log the arrival.
    kept = {} if settings.test_mode else {"incoming_text": masked}
    writer.upsert_message(
        mid, channel=channel, domain=domain, contact_type=CONTACT_TYPE, contact_pseudonym=pseud,
        device_type=DEVICE_TYPE, mode=MODE, outcome="waiting", **kept)
    received = {"sender_type": "unknown", "origin": origin, "masked_count": len(found), "entry": entry,
                "text_kept": not settings.test_mode}
    if retry_of:
        received["retry_of"] = retry_of
    writer.log(EventName.message_received, message_id=mid, payload=received, **common)

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

    # Blind sample (5.21 pt 4): web share only, ~1 in 10. The shortcut never holds drafts (5.20 pt 4).
    blind_eligible = channel == "web_share"
    blind = blind_eligible and rng() < settings.web_blind_ratio
    shown_state = "blind" if blind else "shown"
    draft_masked = mask_text(result.text)
    writer.log(
        EventName.draft_generated, message_id=mid,
        payload={**result.meta, "items_used": [i.__dict__ for i in result.items_used], "drafter": result.drafter,
                 "drafter_version": result.drafter_version, "is_stub": result.is_stub,
                 "confidence": result.confidence, "gen_latency_ms": gen_ms, "shown_state": shown_state,
                 "blind_eligible": blind_eligible, "entry": entry},
        **{**common, "self_model_version": result.meta.get("self_model_version")})

    # 5. Confidence floor (hard limit; unset until Q1): the draft is generated but never stored or returned.
    low = limits.check_confidence(result.confidence, settings)
    if low:
        writer.upsert_message(mid, confidence=result.confidence)
        return _ask_first(engine, writer, settings, out, low, common, now)

    # 6. Draft & notify. Never autonomy-eligible here (5.21 pt 1): no shadow "would have sent" is written.
    writer.upsert_message(
        mid, draft_text=draft_masked, confidence=result.confidence, rationale=result.rationale,
        autonomy_decision="draft_notify", shown_state=shown_state, mode=MODE)
    writer.log(EventName.autonomy_decision, message_id=mid,
               payload={"action": "draft_notify", "autonomy_eligible": False,
                        "reasons": ["draft_only_channel", "untagged_sender"],
                        "trigger": "initial", "rerun_of": None}, **common)
    ThreadStore(engine).create(mid, _chat_id(settings), None, "blind_held" if blind else "shown", now(), window)
    if blind:
        # Held: the draft text, confidence and why never leave the server until the reply is captured.
        writer.log(EventName.draft_held_blind, message_id=mid,
                   payload={"ratio": settings.web_blind_ratio}, **common)
        out.shown_state = "blind"
        return out
    writer.log(EventName.draft_shown, message_id=mid, idempotency_key=make_key("draft_shown", mid, 1),
               payload={"view_no": 1, "surface": "api_response" if channel == "shortcut" else "web_app"}, **common)

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


# ------------------------------------------------------------------------------------------ re-run after a tag
def first_decision(engine: Engine, message_id: str) -> dict | None:
    """The first `autonomy_decision` event of a message (never edited; a re-run points back to it)."""
    for e in events_for_message(engine, message_id):
        if e["event_name"] == "autonomy_decision":
            return e
    return None


def rerun_decision(engine: Engine, writer: EventWriter, message_id: str, *, contact_type: str,
                   contact_pseudonym: str, domain: str | None = None) -> dict:
    """Decisions 5.21 pt 1: tagging the sender runs the autonomy decision again for the same message, server-side.
    The first `autonomy_decision` is never edited; the second carries `rerun_of` (the first event) and
    `trigger = sender_tag`. A tag can only clear the CONTACT-based reason, and on draft-only channels (shortcut,
    web share) an untagged or new sender never stopped the draft in the first place (5.21 pt 1), so the answer
    can only differ if a content limit applies, and then it does not change. Content limits are carried from the
    first decision (the incoming text is not kept in test mode, so it cannot be read again). Always draft-only:
    never autonomy-eligible, no shadow "would have sent"."""
    rec = writer.get_message(message_id)
    if not rec or rec.get("channel") not in SHARE_CHANNELS:
        raise ShareError(404, "No such shared item.")
    if rec.get("outcome") != "waiting":
        raise ShareError(409, "That item is already closed, so the tag cannot change anything.")
    first = first_decision(engine, message_id)
    unchanged = (rec.get("contact_type") == contact_type and rec.get("contact_pseudonym") == contact_pseudonym)
    limit = rec.get("hard_limit") if rec.get("hard_limit") not in (None, "none") else None
    action = "ask_first" if (limit or rec.get("topic_triggered")) else "draft_notify"
    reasons = ["draft_only_channel"]
    if contact_type == "unknown":
        reasons.append("untagged_sender")
    elif contact_type.startswith("new"):
        reasons.append("new_sender_draft_only")
    payload = {"action": action, "autonomy_eligible": False, "reasons": reasons, "trigger": "sender_tag",
               "rerun_of": first["event_id"] if first else None, "contact_type": contact_type,
               "changed": bool(first and (first["payload"] or {}).get("action") != action)}
    if limit:
        payload["limit"] = limit
    n = 0
    if not unchanged:
        n = 1 + sum(1 for e in events_for_message(engine, message_id)
                    if e["event_name"] == "autonomy_decision" and (e["payload"] or {}).get("trigger") == "sender_tag")
        writer.upsert_message(message_id, contact_type=contact_type, contact_pseudonym=contact_pseudonym,
                              **({"domain": domain} if domain else {}))
        writer.log(EventName.autonomy_decision, message_id=message_id,
                   idempotency_key=make_key("autonomy_decision", message_id, "sender_tag", n),
                   payload=payload, channel=rec["channel"], domain=domain or rec.get("domain"), mode=MODE,
                   device_type=DEVICE_TYPE, contact_type=contact_type)
    return {"ok": True, "rerun": not unchanged, "action": action, "changed": payload["changed"],
            "contact_type": contact_type, "reasons": reasons}
