"""Small one-off data migrations, run by `db.init_db()` on every start (and by `python -m app.migrate`). Each one is
idempotent: a second run changes nothing. Until Alembic arrives (D1b) this is where an existing database is brought
in line with a schema change that `create_all` cannot make on its own.

sender_tags: plaintext `label` -> encrypted `label_enc` (decisions 5.24 pt 1). One transaction (Postgres DDL is
transactional, so a failure changes nothing; SQLite, used only for local dev and tests, may keep the empty new column,
which the next run simply continues from). The plaintext column is only dropped after every label was encrypted AND
read back correctly.
Limit (say it plainly): a database may keep an overwritten value in unused storage until it is cleaned up (Postgres
autovacuum; `VACUUM FULL sender_tags` forces it) and Neon keeps short-term history, so old plaintext can linger for a
while. The rows are made-up or own labels today; nothing here can reach back into backups or history.
"""
import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from app import db
from app.config import Settings, get_settings
from app.privacy.crypto import decrypt_text, encrypt_text, has_dedicated_key, is_current, rewrap

log = logging.getLogger("minime.migrations")


def migrate_sender_tag_labels(engine: Engine, settings: Settings | None = None) -> dict:
    """Encrypt existing plaintext tag labels and drop the plaintext column. Returns what it did."""
    s = settings or get_settings()
    insp = inspect(engine)
    if "sender_tags" not in insp.get_table_names():
        return {"migrated": 0, "dropped_plaintext_column": False}
    cols = {c["name"] for c in insp.get_columns("sender_tags")}
    if "label" not in cols:
        return {"migrated": 0, "dropped_plaintext_column": False}          # already migrated (or created new)
    pg = engine.dialect.name == "postgresql"
    migrated = 0
    with engine.begin() as conn:                                            # all or nothing
        if "label_enc" not in cols:
            conn.execute(text("ALTER TABLE sender_tags ADD COLUMN label_enc TEXT"))
        rows = conn.execute(text("SELECT tag_id, label FROM sender_tags WHERE label_enc IS NULL")).all()
        for tag_id, label in rows:
            token = encrypt_text(label or "", s)
            if decrypt_text(token, s) != (label or ""):                      # read it back before trusting it
                raise RuntimeError(f"tag {tag_id}: encrypted label did not read back; nothing was changed")
            conn.execute(text("UPDATE sender_tags SET label_enc = :t WHERE tag_id = :i"), {"t": token, "i": tag_id})
            migrated += 1
        left = conn.execute(text("SELECT count(*) FROM sender_tags WHERE label_enc IS NULL")).scalar_one()
        if left:
            raise RuntimeError(f"{left} tag label(s) are still not encrypted; nothing was changed")
        conn.execute(text("UPDATE sender_tags SET label = ''"))             # overwrite first, then drop the column
        conn.execute(text("ALTER TABLE sender_tags DROP COLUMN label"))     # the plaintext is gone
        if pg:
            conn.execute(text("ALTER TABLE sender_tags ALTER COLUMN label_enc SET NOT NULL"))
    log.info("sender_tags: %d label(s) encrypted, plaintext column dropped", migrated)
    return {"migrated": migrated, "dropped_plaintext_column": True}


def rewrap_sender_tag_labels(engine: Engine, settings: Settings | None = None) -> dict:
    """If a dedicated DATA_ENCRYPTION_KEY is set, re-encrypt any label still under an older key (for example the
    APP_SECRET fallback it was first encrypted with) under the current one. Idempotent; labels that cannot be read
    are left as they are and counted."""
    s = settings or get_settings()
    if not has_dedicated_key(s):
        return {"rewrapped": 0, "unreadable": 0}
    insp = inspect(engine)
    if "sender_tags" not in insp.get_table_names() or "label_enc" not in {c["name"] for c in insp.get_columns("sender_tags")}:
        return {"rewrapped": 0, "unreadable": 0}
    done = bad = 0
    with engine.begin() as conn:
        for tag_id, enc in conn.execute(text("SELECT tag_id, label_enc FROM sender_tags WHERE label_enc IS NOT NULL")).all():
            if is_current(enc, s):
                continue
            new = rewrap(enc, s)
            if new is None:
                bad += 1
                continue
            conn.execute(text("UPDATE sender_tags SET label_enc = :t WHERE tag_id = :i"), {"t": new, "i": tag_id})
            done += 1
    return {"rewrapped": done, "unreadable": bad}


def run_all(engine: Engine) -> dict:
    return {"sender_tags": migrate_sender_tag_labels(engine), "sender_tag_key": rewrap_sender_tag_labels(engine)}
