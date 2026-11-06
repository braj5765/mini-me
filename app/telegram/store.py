"""Bot plumbing storage: thread rows (Telegram ids + timers, no text) and the 'known contact' lookup."""
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Engine

from app import db

OPEN_STATES = ("shown", "blind_held")


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything is stored as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class ThreadStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, message_id: str, chat_id: int | str, incoming_tg_msg_id: int | None,
               state: str, now: datetime, window_hours: float) -> None:
        with self.engine.begin() as conn:
            conn.execute(db.telegram_threads.insert().values(
                message_id=message_id, chat_id=str(chat_id), incoming_tg_msg_id=incoming_tg_msg_id,
                prompt_tg_msg_id=None, state=state, shown_at=now,
                deadline_at=now + timedelta(hours=window_hours), created_at=now,
            ))

    def set_prompt(self, message_id: str, prompt_tg_msg_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(db.telegram_threads)
                         .where(db.telegram_threads.c.message_id == message_id)
                         .values(prompt_tg_msg_id=prompt_tg_msg_id))

    def get(self, message_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(db.telegram_threads)
                             .where(db.telegram_threads.c.message_id == message_id)).mappings().first()
        return self._fix(r)

    def by_prompt(self, chat_id: int | str, prompt_tg_msg_id: int) -> dict[str, Any] | None:
        t = db.telegram_threads
        with self.engine.connect() as conn:
            r = conn.execute(select(t).where(and_(t.c.chat_id == str(chat_id),
                                                  t.c.prompt_tg_msg_id == prompt_tg_msg_id))).mappings().first()
        return self._fix(r)

    def latest_open(self, chat_id: int | str) -> dict[str, Any] | None:
        t = db.telegram_threads
        with self.engine.connect() as conn:
            r = conn.execute(select(t).where(and_(t.c.chat_id == str(chat_id), t.c.state.in_(OPEN_STATES)))
                             .order_by(t.c.created_at.desc()).limit(1)).mappings().first()
        return self._fix(r)

    def count_open(self, chat_id: int | str) -> int:
        t = db.telegram_threads
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.c.message_id).where(
                and_(t.c.chat_id == str(chat_id), t.c.state.in_(OPEN_STATES)))).all()
        return len(rows)

    def _order(self, chat_id: int | str) -> list[str]:
        t = db.telegram_threads
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.c.message_id).where(t.c.chat_id == str(chat_id))
                                .order_by(t.c.created_at, t.c.message_id)).all()
        return [r[0] for r in rows]

    def number_of(self, chat_id: int | str, message_id: str) -> int:
        """Draft number shown to the user (#1, #2, ...): position of the thread in creation order.
        Derived, not stored (no schema change); stable because threads are never deleted."""
        order = self._order(chat_id)
        return order.index(message_id) + 1 if message_id in order else 0

    def list_open(self, chat_id: int | str) -> list[dict[str, Any]]:
        t = db.telegram_threads
        order = {m: i + 1 for i, m in enumerate(self._order(chat_id))}
        with self.engine.connect() as conn:
            rows = conn.execute(select(t).where(and_(t.c.chat_id == str(chat_id), t.c.state.in_(OPEN_STATES)))
                                .order_by(t.c.created_at, t.c.message_id)).mappings().all()
        out = []
        for r in rows:
            d = self._fix(r)
            d["number"] = order.get(d["message_id"], 0)
            out.append(d)
        return out

    def claim(self, message_id: str) -> bool:
        """Atomically close an open thread. True only for the one caller that closed it (retries lose)."""
        t = db.telegram_threads
        with self.engine.begin() as conn:
            res = conn.execute(update(t).where(and_(t.c.message_id == message_id, t.c.state.in_(OPEN_STATES)))
                               .values(state="closed"))
        return res.rowcount == 1

    def expired(self, now: datetime) -> list[dict[str, Any]]:
        t = db.telegram_threads
        with self.engine.connect() as conn:
            rows = conn.execute(select(t).where(and_(t.c.state.in_(OPEN_STATES), t.c.deadline_at <= now))
                                ).mappings().all()
        return [self._fix(r) for r in rows]

    def is_known_contact(self, contact_pseudonym: str) -> bool:
        """Known = the user has replied to this contact at least once before (5.13 pt 6)."""
        m = db.message_records
        with self.engine.connect() as conn:
            r = conn.execute(select(m.c.message_id).where(and_(
                m.c.contact_pseudonym == contact_pseudonym,
                m.c.outcome.in_(("sent_as_is", "edited_sent", "user_authored")))).limit(1)).first()
        return r is not None

    @staticmethod
    def _fix(r) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("shown_at", "deadline_at", "created_at"):
            d[k] = aware(d.get(k))
        return d
