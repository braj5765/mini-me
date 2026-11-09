"""Read-only helpers over the event log and message records, shared by the classifier (D9a) and the results
view (D11a). Nothing here writes. Payloads are read in Python so the same code runs on Postgres and SQLite."""
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import db


def draft_payloads(engine: Engine, message_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """message_id -> payload of its `draft_generated` event (is_stub, data_mode, items_used, prompt_version ...)."""
    e = db.events
    q = select(e.c.message_id, e.c.payload).where(e.c.event_name == "draft_generated")
    if message_ids is not None:
        q = q.where(e.c.message_id.in_(message_ids))
    with engine.connect() as conn:
        return {r[0]: dict(r[1] or {}) for r in conn.execute(q).all()}


def draft_payload(engine: Engine, message_id: str) -> dict[str, Any] | None:
    return draft_payloads(engine, [message_id]).get(message_id)


def all_records(engine: Engine, user_id: str = "braj") -> list[dict[str, Any]]:
    m = db.message_records
    with engine.connect() as conn:
        rows = conn.execute(select(m).where(m.c.user_id == user_id).order_by(m.c.created_at)).mappings().all()
    return [dict(r) for r in rows]


def unknown_reasons(engine: Engine) -> dict[str, str]:
    """message_id -> reason of its `outcome_unknown` event (capture_window_expired | draft_failed ...)."""
    e = db.events
    with engine.connect() as conn:
        rows = conn.execute(select(e.c.message_id, e.c.payload).where(e.c.event_name == "outcome_unknown")).all()
    return {r[0]: str((r[1] or {}).get("reason") or "unspecified") for r in rows}


def events_for_message(engine: Engine, message_id: str) -> list[dict[str, Any]]:
    e = db.events
    with engine.connect() as conn:
        rows = conn.execute(select(e).where(e.c.message_id == message_id)
                            .order_by(e.c.occurred_at, e.c.recorded_at)).mappings().all()
    return [dict(r) for r in rows]


def recent_events(engine: Engine, limit: int = 12) -> list[dict[str, Any]]:
    e = db.events
    with engine.connect() as conn:
        rows = conn.execute(select(e).order_by(e.c.recorded_at.desc(), e.c.occurred_at.desc())
                            .limit(limit)).mappings().all()
    return [dict(r) for r in reversed(rows)]
