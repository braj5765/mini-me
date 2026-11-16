"""Export my data / Delete everything (decisions 5.14 pt 7, 5.24 pt 1).

The Settings screen that will hold these two controls is U1-U3 and is not built, so for now they are plain functions
with a command-line wrapper (`python -m scripts.data_controls`). Sender-tag labels are included in the export
(decrypted: it is the user's own export) and removed by delete.

Export: every table that holds the user's data, as one JSON-able dict: message records, events, self-model items,
sender tags (with readable labels), item-tag links, Telegram thread ids and timers, and API-token METADATA (name, prefix,
dates; never a token or a hash). Logs a `data_exported` event with counts only.

Delete (decision from Braj, 17 Nov): two separate commands, because the self-model is the hardest part to rebuild.
`delete_activity` removes messages, events, tags and thread records and KEEPS the self-model; `delete_self_model`
removes only the self-model. `delete_everything` (both) stays as a function for a future button. None of them touch the
API tokens or the login, which are settings, not data. Each needs its own exact confirmation phrase. A single `data_deleted` event with counts only is written afterwards, so the log shows that
a wipe happened without keeping anything that was wiped.
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from app import db
from app.config import Settings, get_settings
from app.events import EventName, EventWriter
from app.events.writer import make_key
from app.privacy.crypto import UNREADABLE, decrypt_text, rewrap
from app.share import cache

CONFIRM_PHRASE = "delete everything"
CONFIRM_ACTIVITY = "delete my activity"
CONFIRM_SELF_MODEL = "delete my self-model"

# table label -> table. Order matters for delete only in that nothing here has foreign keys.
_DATA_TABLES = {
    "message_records": db.message_records,
    "events": db.events,
    "self_model_items": db.self_model_items,
    "sender_tags": db.sender_tags,
    "item_tags": db.item_tags,
    "telegram_threads": db.telegram_threads,
}


def _rows(engine: Engine, table) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings().all()]


def counts(engine: Engine) -> dict[str, int]:
    out = {}
    with engine.connect() as conn:
        for name, table in _DATA_TABLES.items():
            out[name] = conn.execute(select(func.count()).select_from(table)).scalar_one()
    return out


def export_my_data(engine: Engine, settings: Settings | None = None, *, log_event: bool = True) -> dict:
    """Everything stored about the user, as plain data. Sender-tag labels are decrypted."""
    s = settings or get_settings()
    data: dict[str, Any] = {}
    for name, table in _DATA_TABLES.items():
        if name == "sender_tags":
            tags = []
            for r in _rows(engine, table):
                enc = r.pop("label_enc", None)
                r["label"] = decrypt_text(enc, s) or UNREADABLE
                tags.append(r)
            data[name] = tags
        else:
            data[name] = _rows(engine, table)
    data["api_tokens"] = [{k: v for k, v in r.items() if k != "token_hash"} for r in _rows(engine, db.api_tokens)]
    n = {k: len(v) for k, v in data.items()}
    out = {"exported_at": datetime.now(timezone.utc).isoformat(), "app_version": s.app_version,
           "note": "Message text is stored masked. Sender-tag labels are shown readable here.", "counts": n, **data}
    if log_event:
        EventWriter(engine).log_safe(EventName.data_exported, idempotency_key=make_key("data_exported", out["exported_at"]),
                                     payload={"counts": n})
    return out


_ACTIVITY_TABLES = [k for k in _DATA_TABLES if k != "self_model_items"]


def _wipe(engine: Engine, names: list[str], scope: str) -> dict[str, int]:
    deleted = {n: v for n, v in counts(engine).items() if n in names}
    with engine.begin() as conn:                      # all or nothing
        for n in names:
            conn.execute(delete(_DATA_TABLES[n]))
    cache.clear()                                     # in-memory restored values (this process only)
    EventWriter(engine).log_safe(EventName.data_deleted,
                                 idempotency_key=make_key("data_deleted", scope, datetime.now(timezone.utc).isoformat()),
                                 payload={"scope": scope, "deleted": deleted})
    return deleted


def _check(confirm: str, phrase: str) -> None:
    if (confirm or "").strip().lower() != phrase:
        raise ValueError(f'To go ahead, confirm with the exact phrase "{phrase}".')


def delete_activity(engine: Engine, confirm: str, settings: Settings | None = None) -> dict[str, int]:
    """Delete messages, events, sender tags, tag links and Telegram thread records. KEEPS the self-model, the API
    tokens and the login. `confirm` must be "delete my activity"."""
    _check(confirm, CONFIRM_ACTIVITY)
    return _wipe(engine, _ACTIVITY_TABLES, "activity")


def delete_self_model(engine: Engine, confirm: str, settings: Settings | None = None) -> dict[str, int]:
    """Delete only the self-model items (voice, decision and fact items). `confirm` must be "delete my self-model"."""
    _check(confirm, CONFIRM_SELF_MODEL)
    return _wipe(engine, ["self_model_items"], "self_model")


def delete_everything(engine: Engine, confirm: str, settings: Settings | None = None) -> dict[str, int]:
    """Both of the above in one step (what a future 'Delete everything' button would call). `confirm` must be
    "delete everything". Not exposed on the command line: use the two commands there."""
    _check(confirm, CONFIRM_PHRASE)
    return _wipe(engine, list(_DATA_TABLES), "everything")


def rewrap_tags(engine: Engine, settings: Settings | None = None) -> dict[str, int]:
    """Re-encrypt every tag label under the CURRENT first key (after DATA_ENCRYPTION_KEY was set or rotated)."""
    s = settings or get_settings()
    t = db.sender_tags
    done = failed = 0
    with engine.begin() as conn:
        for tag_id, enc in conn.execute(select(t.c.tag_id, t.c.label_enc)).all():
            new = rewrap(enc, s)
            if new is None:
                failed += 1
                continue
            conn.execute(update(t).where(t.c.tag_id == tag_id).values(label_enc=new))
            done += 1
    return {"rewrapped": done, "unreadable": failed}
