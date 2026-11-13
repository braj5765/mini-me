"""Sender tags for the "From:" row (decisions 5.17 pt 1, 5.18 pt 2, 5.21 pt 1).

A tag is a short label the user types ("Cultural committee", a group or a person). The label is the user's own text,
kept in its own table (masked for phone numbers and emails). Messages and events never carry it: they get the tag's
pseudonym, so the same tag is the same contact every time and can become "known" (the user has replied at least
once, 5.13 pt 6). A tag remembers its domain (default `club`).

"Someone new" is `new_individual` with the item's own unique pseudonym (it can never become known).
Clearing a tag goes back to `unknown`.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from app import db
from app.config import Settings
from app.events import EventWriter
from app.privacy.masking import mask_text, pseudonymise
from app.share import service
from app.share.service import ShareError
from app.telegram.store import ThreadStore

MAX_LABEL = 40
KINDS = ("individual", "group")


def list_tags(engine: Engine, user_id: str = "braj", limit: int = 8) -> list[dict]:
    t = db.sender_tags
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(t.c.user_id == user_id)
                            .order_by(t.c.last_used_at.desc().nullslast(), t.c.created_at.desc()).limit(limit)
                            ).mappings().all()
    return [{"tag_id": r["tag_id"], "label": r["label"], "kind": r["kind"], "domain": r["domain"]} for r in rows]


def _clean_label(label: str) -> str:
    label = " ".join(mask_text((label or "").strip()).split())
    if not label:
        raise ShareError(422, "Type a short label for who it is from.")
    return label[:MAX_LABEL]


def _create(engine: Engine, settings: Settings, label: str, kind: str, user_id: str) -> dict:
    if kind not in KINDS:
        raise ShareError(422, "Choose group or person.")
    label = _clean_label(label)
    pseud = pseudonymise(f"tag-{label.lower()}@web.invalid")
    now = datetime.now(timezone.utc)
    t = db.sender_tags
    ins = (pg_insert if engine.dialect.name == "postgresql" else sqlite_insert)(t).values(
        tag_id="st_" + uuid.uuid4().hex[:10], user_id=user_id, label=label, kind=kind,
        domain=settings.share_default_domain, pseudonym=pseud, created_at=now, last_used_at=now
    ).on_conflict_do_nothing(index_elements=["pseudonym"])
    with engine.begin() as conn:
        conn.execute(ins)
        row = conn.execute(select(t).where(t.c.pseudonym == pseud)).mappings().one()
    return dict(row)


def apply_tag(engine: Engine, writer: EventWriter, settings: Settings, message_id: str, *,
              tag_id: str | None = None, label: str | None = None, kind: str | None = None,
              someone_new: bool = False, clear: bool = False, user_id: str = "braj") -> dict:
    """Tag the sender of an open shared item and run the autonomy decision again (both decisions stay logged)."""
    rec = writer.get_message(message_id)
    if not rec or rec.get("channel") not in service.SHARE_CHANNELS:
        raise ShareError(404, "No such shared item.")
    own_pseud = pseudonymise(f"shared-{message_id}@share.invalid")   # the item's own, unique and never "known"
    chosen: dict | None = None
    if clear:
        contact_type, pseud, domain = "unknown", own_pseud, settings.share_default_domain
    elif someone_new:
        contact_type, pseud, domain = "new_individual", own_pseud, settings.share_default_domain
    else:
        if tag_id:
            t = db.sender_tags
            with engine.connect() as conn:
                row = conn.execute(select(t).where(t.c.tag_id == tag_id, t.c.user_id == user_id)).mappings().first()
            if not row:
                raise ShareError(404, "No such tag.")
            chosen = dict(row)
        else:
            chosen = _create(engine, settings, label or "", kind or "", user_id)
        known = ThreadStore(engine).is_known_contact(chosen["pseudonym"])
        contact_type = f"{'known' if known else 'new'}_{chosen['kind']}"
        pseud, domain = chosen["pseudonym"], chosen["domain"]
    result = service.rerun_decision(engine, writer, message_id, contact_type=contact_type,
                                    contact_pseudonym=pseud, domain=domain)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(delete(db.item_tags).where(db.item_tags.c.message_id == message_id))
        if chosen:
            conn.execute(db.item_tags.insert().values(message_id=message_id, tag_id=chosen["tag_id"], tagged_at=now))
            conn.execute(db.sender_tags.update().where(db.sender_tags.c.tag_id == chosen["tag_id"])
                         .values(last_used_at=now))
    result["tag"] = {"tag_id": chosen["tag_id"], "label": chosen["label"], "kind": chosen["kind"]} if chosen else None
    return result
