"""Confirming what happened to a shared draft, from the web app's waiting list (Dev 7, decisions 5.20 pt 3, 5.13 pt 2,
5.15 pt 3, 5.17 pt 4, 5.21 pt 3).

Same rules and the same events as the Telegram capture, so scoring and the classifier do not care where a
confirmation came from:
- Copy is not sent. Nothing here counts until the user taps one of the outcomes.
- One close per item (`ThreadStore.claim`), so a double tap or a Telegram tap on the same item loses.
- The user's own words are masked before storage. Unconfirmed after the window = outcome unknown (the sweep).
- "Sent as is" is self-reported (design F6); an edit pasted back that equals the draft is logged as sent as is (C-8).
- Held (blind) draft: the user's reply comes first; the draft is released only after, by an explicit tap.
"""
import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.engine import Engine

from app.config import Settings
from app.events import EventName, EventWriter
from app.events.writer import make_key
from app.privacy.masking import mask_text
from app.share import cache
from app.share.service import DEVICE_TYPE, MODE, SHARE_CHANNELS, ShareError, _norm
from app.telegram.store import ThreadStore, aware

log = logging.getLogger("minime.share")
Hook = Callable[[str, str], "str | None"]


def _common(rec: dict) -> dict:
    out = {"channel": rec.get("channel"), "domain": rec.get("domain"), "mode": MODE, "device_type": DEVICE_TYPE}
    if rec.get("contact_type"):
        out["contact_type"] = rec["contact_type"]
    return out


def _tta(thread: dict, now: datetime) -> int | None:
    shown = aware(thread.get("shown_at"))
    return max(0, int((now - shown).total_seconds() * 1000)) if shown else None


def _run_hook(hook: Hook | None, message_id: str, capture: str) -> None:
    if hook is None:
        return
    try:
        hook(message_id, capture)           # the edit classifier; its note is for Telegram, not for this screen
    except Exception:  # noqa: BLE001
        log.exception("on_captured hook failed")   # logging must not block the user (NFR-6)


def _open_item(engine: Engine, writer: EventWriter, message_id: str) -> tuple[dict, dict]:
    rec = writer.get_message(message_id)
    if not rec or rec.get("channel") not in SHARE_CHANNELS:
        raise ShareError(404, "No such shared item.")
    thread = ThreadStore(engine).get(message_id)
    if not thread:
        raise ShareError(404, "No such shared item.")
    return rec, thread


def _claim(engine: Engine, message_id: str) -> None:
    if not ThreadStore(engine).claim(message_id):
        raise ShareError(409, "That one is already recorded.")


def confirm(engine: Engine, writer: EventWriter, settings: Settings, message_id: str, action: str,
            text: str | None = None, *, hook: Hook | None = None,
            now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    """action: sent_as_is | edited (text = what was sent) | not_replying. For a copied (shown) draft or, for
    not_replying only, a held one."""
    rec, thread = _open_item(engine, writer, message_id)
    state = thread["state"]
    if action not in ("sent_as_is", "edited", "not_replying"):
        raise ShareError(422, "Unknown action.")
    if state not in ("shown", "blind_held"):
        raise ShareError(409, "That one is already recorded.")
    if state == "blind_held" and action != "not_replying":
        raise ShareError(409, "That draft is held until you have replied. Share your reply first.")
    if action in ("sent_as_is", "edited") and not rec.get("draft_text"):
        raise ShareError(409, "That item has no draft to confirm.")
    if action == "edited" and not (text or "").strip():
        raise ShareError(422, "Paste what you sent.")
    _claim(engine, message_id)

    common, tta = _common(rec), _tta(thread, now())
    shown_state = "blind" if state == "blind_held" else "shown"
    cache.drop(message_id)

    if action == "not_replying":
        # 5.17 pt 4: chose not to answer. Not a discard; never scored; counted separately.
        writer.upsert_message(message_id, outcome="not_replying", time_to_action_ms=tta)
        writer.log(EventName.not_replying, message_id=message_id,
                   payload={"capture": "button", "time_to_action_ms": tta, "shown_state": shown_state}, **common)
        return {"ok": True, "outcome": "not_replying"}

    draft = rec["draft_text"]
    if action == "sent_as_is":
        sent, matched, capture = draft, True, "button"
    else:
        sent = mask_text(text.strip())                       # the user's own words, masked before storage
        matched, capture = _norm(sent) == _norm(draft), "paste"   # identical to the draft = sent as is (C-8)
    outcome = "sent_as_is" if matched else "edited_sent"
    writer.upsert_message(message_id, sent_text=sent, outcome=outcome, time_to_action_ms=tta)
    writer.log(EventName.draft_acted, message_id=message_id, idempotency_key=make_key("draft_acted", message_id),
               payload={"action": "sent" if matched else "edited_sent", "capture": capture,
                        "time_to_action_ms": tta, "shown_state": "shown"}, **common)
    writer.log(EventName.draft_sent, message_id=message_id,
               payload={"sent_text": sent, "sent_by": "user", "edited": not matched, "capture": capture}, **common)
    _run_hook(hook, message_id, capture)
    return {"ok": True, "outcome": outcome, "matched": matched}


def capture_held_reply(engine: Engine, writer: EventWriter, settings: Settings, message_id: str, text: str,
                       capture: str = "paste", *, hook: Hook | None = None,
                       now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    """The user's own reply for a HELD (blind) draft (5.15 pt 3). Saves it; the draft is NOT returned here (the
    user taps "Show my draft" next). capture = `share` (arrived through the share target) or `paste`."""
    if capture not in ("share", "paste"):
        raise ShareError(422, "Unknown capture.")
    if not (text or "").strip():
        raise ShareError(422, "Paste what you sent.")
    rec, thread = _open_item(engine, writer, message_id)
    if thread["state"] != "blind_held" or rec.get("shown_state") != "blind":
        raise ShareError(409, "That one is not waiting for your reply.")
    _claim(engine, message_id)
    common, tta = _common(rec), _tta(thread, now())
    sent = mask_text(text.strip())
    matched = _norm(sent) == _norm(rec.get("draft_text"))
    outcome = "sent_as_is" if matched else "edited_sent"
    writer.upsert_message(message_id, sent_text=sent, outcome=outcome, time_to_action_ms=tta)
    writer.log(EventName.draft_acted, message_id=message_id, idempotency_key=make_key("draft_acted", message_id),
               payload={"action": "sent" if matched else "edited_sent", "capture": capture,
                        "time_to_action_ms": tta, "shown_state": "blind"}, **common)
    writer.log(EventName.draft_sent, message_id=message_id,
               payload={"sent_text": sent, "sent_by": "user", "edited": not matched, "capture": capture}, **common)
    _run_hook(hook, message_id, capture)
    return {"ok": True, "outcome": outcome, "matched": matched}


def reveal_held(engine: Engine, writer: EventWriter, message_id: str) -> dict:
    """The user tapped "Show my draft": only now does the held draft leave the server (masked: its real values
    were never kept). Only after a reply was captured."""
    rec, _thread = _open_item(engine, writer, message_id)
    if rec.get("shown_state") != "blind" or rec.get("outcome") not in ("sent_as_is", "edited_sent"):
        raise ShareError(409, "Your reply has not been saved yet, so the draft stays held.")
    matched = rec["outcome"] == "sent_as_is"
    writer.log(EventName.draft_released_after_reply, message_id=message_id,
               payload={"matched": matched}, **_common(rec))
    return {"ok": True, "draft": rec.get("draft_text"), "confidence_label": _label(rec.get("confidence")),
            "why": rec.get("rationale"), "your_reply": rec.get("sent_text"), "matched": matched}


def _label(c: float | None) -> str | None:
    from app.telegram.format import confidence_label
    return confidence_label(c) if c is not None else None


def reply_ask_first(engine: Engine, writer: EventWriter, settings: Settings, message_id: str, text: str,
                    capture: str = "paste", *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    """Optional "share your reply" on an ask-first item (5.21 pt 3; FIRST TO CUT). The user wrote the reply with no
    draft: `draft_acted` with outcome `user_authored`, no score, excluded from the north star (5.13 pt 5). The text is
    saved masked as an example for learning (cold start C); it is never classified as an edit."""
    if capture not in ("share", "paste"):
        raise ShareError(422, "Unknown capture.")
    if not (text or "").strip():
        raise ShareError(422, "Paste what you sent.")
    rec, thread = _open_item(engine, writer, message_id)
    if thread["state"] != "shown" or rec.get("draft_text") or rec.get("autonomy_decision") != "ask_first":
        raise ShareError(409, "That one is not an ask-first item waiting for your reply.")
    _claim(engine, message_id)
    tta = _tta(thread, now())
    sent = mask_text(text.strip())
    writer.upsert_message(message_id, sent_text=sent, outcome="user_authored", time_to_action_ms=tta)
    writer.log(EventName.draft_acted, message_id=message_id, idempotency_key=make_key("draft_acted", message_id),
               payload={"action": "user_authored", "capture": capture, "time_to_action_ms": tta,
                        "shown_state": "shown"}, **_common(rec))
    return {"ok": True, "outcome": "user_authored"}
