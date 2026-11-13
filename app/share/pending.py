"""The waiting list (Dev 7, decisions 5.20 pt 3, 5.21 pt 3, 5.23): the home for confirming what happened to copied
drafts, for the shortcut (B) and the web app (C) alike.

Lists open items only (inside their 24 h window):
  - copied drafts: shown, with at least one `draft_copied` event;
  - held drafts: a web-share blind sample waiting for the user's reply (no draft text, no confidence, no why).
Drafts that were never copied are not listed (nothing was done with them); they close as outcome unknown when the
window ends, as does everything unconfirmed. Soonest to expire first.

Test mode (5.23 pt 3): the incoming text is not kept, so every card says "message not kept in test mode".
"""
import math
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from app import db
from app.config import Settings
from app.events.queries import events_for_message
from app.share.service import SHARE_CHANNELS, _chat_id
from app.telegram.format import NOT_KEPT
from app.telegram.store import ThreadStore, aware


def _hours_left(deadline: datetime, now: datetime) -> int:
    return max(1, math.ceil((aware(deadline) - now).total_seconds() / 3600))


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return ""
    mins = max(0, int((now - aware(then)).total_seconds() // 60))
    return "just now" if mins < 2 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago"


def _tag_labels(engine: Engine, message_ids: list[str]) -> dict[str, dict]:
    if not message_ids:
        return {}
    it, st = db.item_tags, db.sender_tags
    with engine.connect() as conn:
        rows = conn.execute(select(it.c.message_id, st.c.label, st.c.kind)
                            .join(st, st.c.tag_id == it.c.tag_id).where(it.c.message_id.in_(message_ids))).all()
    return {r[0]: {"label": r[1], "kind": r[2]} for r in rows}


def list_pending(engine: Engine, settings: Settings, *,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    t_now = now()
    threads = ThreadStore(engine).list_open(_chat_id(settings))
    m = db.message_records
    ids = [t["message_id"] for t in threads if t["message_id"].startswith("sh-")]
    if not ids:
        return {"items": [], "copied": 0, "held": 0, "test_mode": settings.test_mode}
    with engine.connect() as conn:
        recs = {r["message_id"]: dict(r) for r in conn.execute(
            select(m).where(and_(m.c.message_id.in_(ids), m.c.channel.in_(SHARE_CHANNELS)))).mappings().all()}
    tags = _tag_labels(engine, ids)
    items = []
    for t in threads:
        rec = recs.get(t["message_id"])
        if not rec:
            continue
        held = t["state"] == "blind_held"
        copies = [e for e in events_for_message(engine, t["message_id"]) if e["event_name"] == "draft_copied"]
        if not held and not (rec.get("draft_text") and copies):
            continue
        tag = tags.get(t["message_id"])
        card = {
            "message_id": t["message_id"], "kind": "held" if held else "copied",
            "group": tag["label"] if tag else None, "channel": rec.get("channel"),
            "incoming": rec.get("incoming_text") or None,           # None in test mode
            "incoming_note": None if rec.get("incoming_text") else NOT_KEPT,
            "when": _ago(copies[-1]["occurred_at"] if copies else t.get("created_at"), t_now),
            "confirm_within_hours": _hours_left(t["deadline_at"], t_now),
            "deadline_at": aware(t["deadline_at"]).isoformat(),
        }
        if not held:   # a held draft never shows its text, confidence or why
            card.update(draft=rec["draft_text"], draft_masked=True)
        items.append(card)
    items.sort(key=lambda c: c["deadline_at"])
    return {"items": items, "copied": sum(1 for i in items if i["kind"] == "copied"),
            "held": sum(1 for i in items if i["kind"] == "held"), "test_mode": settings.test_mode}
