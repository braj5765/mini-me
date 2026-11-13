"""Telegram check-in nudge (decisions 5.20 pt 3, 5.21 pt 5-6; design section 7).

Once a day, at the check-in time (default 8:00 pm, 5.12 pt 2), if copied drafts are waiting:
    "N copied drafts waiting · Confirm within N h"   [Review]  (a link button to the web app)
- Never any message text, group or contact name, or draft text (it would sit on Telegram's servers).
- Counts copied drafts only: held (blind) drafts are excluded (design F5).
- Nothing is sent at zero. Never extends the 24 h window. Wording: "copied", never "sent"; no urgency words.
- Logged as `nudge_sent` (own event, not `digest_sent`; payload: number of pending items, channel).
- Called from the existing /tasks/sweep (the outside scheduler already runs every 10 minutes), so no new schedule.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import db
from app.events import EventName
from app.events.writer import make_key
from app.share.pending import list_pending
from app.telegram.bot import BotDeps

log = logging.getLogger("minime.nudge")


def nudge_text(copied: int, hours: int) -> str:
    noun = "copied draft" if copied == 1 else "copied drafts"
    return f"{copied} {noun} waiting · Confirm within {hours} h"


def _checkin_today(settings, now_utc: datetime) -> tuple[datetime, str]:
    tz = ZoneInfo(settings.checkin_timezone)
    local = now_utc.astimezone(tz)
    try:
        hh, mm = (int(x) for x in settings.checkin_time.split(":"))
    except ValueError:
        hh, mm = 20, 0
    return local.replace(hour=hh, minute=mm, second=0, microsecond=0), local.date().isoformat()


def maybe_send_nudge(deps: BotDeps) -> bool:
    """True if a nudge was sent now. Safe to call every few minutes: at most one per day."""
    s = deps.settings
    chat = (s.telegram_allowed_user_id or "").strip()
    if not chat.lstrip("-").isdigit():
        return False
    now = deps.now()
    start, day = _checkin_today(s, now)
    if not (start <= now.astimezone(start.tzinfo) < start + timedelta(hours=s.nudge_window_hours)):
        return False
    key = make_key("nudge_sent", day)
    with deps.engine.connect() as conn:
        if conn.execute(select(db.events.c.event_id).where(db.events.c.idempotency_key == key)).first():
            return False
    pend = list_pending(deps.engine, s, now=lambda: now)
    copied = [i for i in pend["items"] if i["kind"] == "copied"]       # held items are excluded
    if not copied:
        return False
    hours = min(i["confirm_within_hours"] for i in copied)
    markup = None
    if s.base_url.startswith("https://"):
        markup = {"inline_keyboard": [[{"text": "Review", "url": s.base_url + "/app/"}]]}
    try:
        deps.client.send_message(int(chat), nudge_text(len(copied), hours), reply_markup=markup)
    except Exception:  # noqa: BLE001
        log.exception("could not send the nudge")
        return False
    deps.writer.log(EventName.nudge_sent, idempotency_key=key,
                    payload={"pending_count": len(copied), "channel": "telegram", "confirm_within_hours": hours},
                    channel="telegram", mode="shadow")
    return True
