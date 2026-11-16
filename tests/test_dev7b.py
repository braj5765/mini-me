"""Dev 7b (decisions 5.24): sender-tag labels (encrypted, never sent to a model, ids only in events, export / delete,
30-day unused deletion, safe migration), the "New group" choice, and the "Not copied" waiting-list section."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app import db, migrations
from app.classifier.classifier import EditClassifier
from app.config import Settings, get_settings
from app.events import EventWriter
from app.privacy import crypto, data_controls
from app.privacy.crypto import UNREADABLE
from app.results.stats import compute_stats
from app.share import nudge, pending, tags
from app.telegram.bot import BotDeps, sweep_expired
from test_share import events, record, rows, everything_stored
from test_telegram import FakeTelegram
from test_web import (PHONE, Clock, confirm, copy, draft, mk, nudge_deps, tag, web)   # noqa: F401  (web = fixture)

LABEL = "Zorblax Collective"          # a made-up label that cannot appear by accident
DAY = timedelta(days=1)


def every_table_text(engine) -> str:
    """All user tables except sender_tags, as one string (proves where a label is NOT)."""
    return json.dumps({n: rows(engine, t) for n, t in db.metadata.tables.items() if n != "sender_tags"}, default=str)


def tagged(web, label=LABEL, kind="group", text="Is the club meet on Friday?"):
    mid = draft(web, text).json()["message_id"]
    r = tag(web, mid, label=label, kind=kind).json()
    return mid, r["tag"]["tag_id"]


# ======================================================================================= crypto helper
def test_encrypt_decrypt_roundtrip_and_ciphertext_is_not_plaintext():
    s = Settings(app_secret="a-secret")
    tok = crypto.encrypt_text("Club senior", s)
    assert "Club" not in tok and crypto.decrypt_text(tok, s) == "Club senior"
    assert crypto.encrypt_text("Club senior", s) != tok                      # fresh IV each time


def test_wrong_key_and_tampering_give_none_not_an_exception():
    tok = crypto.encrypt_text("Club senior", Settings(app_secret="one"))
    assert crypto.decrypt_text(tok, Settings(app_secret="two")) is None
    assert crypto.decrypt_text(tok[:-4] + "AAAA", Settings(app_secret="one")) is None
    assert crypto.decrypt_text(None) is None and crypto.decrypt_text("") is None


def test_dedicated_key_encrypts_and_old_fallback_tokens_still_read_and_rewrap():
    from cryptography.fernet import Fernet
    old = Settings(app_secret="s")
    tok_old = crypto.encrypt_text("Fest team", old)                           # made with the APP_SECRET fallback
    new = Settings(app_secret="s", data_encryption_key=Fernet.generate_key().decode())
    assert crypto.has_dedicated_key(new) and not crypto.has_dedicated_key(old)
    assert crypto.decrypt_text(tok_old, new) == "Fest team"                  # fallback is always the last key
    tok_new = crypto.encrypt_text("Fest team", new)
    assert crypto.decrypt_text(tok_new, old) is None                          # new key is not the fallback
    wrapped = crypto.rewrap(tok_old, new)
    assert wrapped and crypto.decrypt_text(wrapped, new) == "Fest team"
    assert crypto.decrypt_text(wrapped, old) is None                          # now only the dedicated key opens it


def test_setting_a_dedicated_key_rewraps_existing_labels_at_the_next_start(web, monkeypatch):
    from cryptography.fernet import Fernet
    mid, tid = tagged(web)                                                  # encrypted under the APP_SECRET fallback
    before = rows(web.engine, db.sender_tags)[0]["label_enc"]
    assert not crypto.has_dedicated_key()
    assert db.init_db(web.engine)["sender_tag_key"] == {"rewrapped": 0, "unreadable": 0}   # no dedicated key: nothing to do
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    assert not crypto.is_current(before)
    assert db.init_db(web.engine)["sender_tag_key"] == {"rewrapped": 1, "unreadable": 0}
    after = rows(web.engine, db.sender_tags)[0]["label_enc"]
    assert after != before and crypto.is_current(after) and crypto.decrypt_text(after) == LABEL
    assert crypto.decrypt_text(after, Settings(app_secret="test-secret", data_encryption_key="")) is None     # the fallback no longer opens it
    assert db.init_db(web.engine)["sender_tag_key"] == {"rewrapped": 0, "unreadable": 0}   # second start: no change
    assert web.client.get("/app/api/tags").json()["tags"][0]["label"] == LABEL


# ======================================================================================= labels: encrypted, id-only
def test_label_is_stored_encrypted_and_read_back_everywhere_it_is_shown(web):
    mid, tid = tagged(web)
    raw = json.dumps(rows(web.engine, db.sender_tags), default=str)
    assert LABEL not in raw and "Zorblax" not in raw                          # no plaintext in the table
    row = rows(web.engine, db.sender_tags)[0]
    assert "label" not in row and crypto.decrypt_text(row["label_enc"]) == LABEL
    assert web.client.get("/app/api/tags").json()["tags"][0]["label"] == LABEL
    copy(web, mid)
    assert web.client.get("/app/api/pending").json()["items"][0]["group"] == LABEL


def test_label_is_nowhere_else_not_in_records_events_threads_item_tags_or_logs(web, caplog):
    caplog.set_level(logging.DEBUG)
    mid, tid = tagged(web)
    copy(web, mid)
    confirm(web, mid, "sent_as_is")
    web.client.get("/app/api/tags")
    web.client.get("/app/api/pending")
    blob = every_table_text(web.engine)
    assert "Zorblax" not in blob and LABEL not in caplog.text
    assert tid in json.dumps(rows(web.engine, db.item_tags), default=str)     # a tag is referred to by id
    # events carry a contact type and the message's own pseudonym, never the label
    assert not any("Zorblax" in json.dumps(e["payload"], default=str) for e in events(web.engine))


def test_label_never_reaches_the_model(web):
    mid, _ = tagged(web)
    for _i in range(2):                                                       # later drafts for the same tag too
        draft(web, "Another question about Friday?")
    assert web.drafter.requests and "Zorblax" not in repr(web.drafter.requests)
    assert "Zorblax" not in repr([vars(r) for r in web.drafter.requests])


def test_unreadable_label_is_a_placeholder_not_a_crash(web):
    tagged(web)
    other = Settings(app_secret="a-different-secret")
    shown = tags.list_tags(web.engine, settings=other)
    assert shown[0]["label"] == UNREADABLE


def test_label_is_still_masked_before_it_is_encrypted(web):
    mid = draft(web, "Third question?").json()["message_id"]
    tag(web, mid, label="Call 9876543210", kind="individual")
    label = tags.list_tags(web.engine)[0]["label"]
    assert PHONE not in label and "[PHONE]" in label


# ======================================================================================= migration (plaintext -> encrypted)
def legacy_db(tmp_path):
    """A database as it was before Dev 7b: sender_tags with a plaintext, NOT NULL `label` column."""
    eng = create_engine(f"sqlite+pysqlite:///{tmp_path}/legacy.db", future=True)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE sender_tags (tag_id VARCHAR(16) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, "
                       "label VARCHAR(60) NOT NULL, kind VARCHAR(12) NOT NULL, domain VARCHAR(64) NOT NULL, "
                       "pseudonym VARCHAR(32) NOT NULL UNIQUE, created_at DATETIME NOT NULL, last_used_at DATETIME)"))
        for i, name in enumerate(["Riya", "Club core team"]):
            c.execute(text("INSERT INTO sender_tags VALUES (:i,'braj',:l,'group','club',:p,'2026-11-01 10:00:00',NULL)"),
                      {"i": f"st_{i}", "l": name, "p": f"contact-0000000{i}"})
    return eng, tmp_path / "legacy.db"


def test_migration_encrypts_existing_labels_and_drops_the_plaintext(tmp_path):
    eng, _path = legacy_db(tmp_path)
    result = db.init_db(eng, raise_migration_errors=True)
    assert result["sender_tags"] == {"migrated": 2, "dropped_plaintext_column": True}
    cols = {r[1] for r in eng.connect().execute(text("PRAGMA table_info(sender_tags)")).all()}
    assert "label" not in cols and "label_enc" in cols
    got = {r["tag_id"]: crypto.decrypt_text(r["label_enc"]) for r in rows(eng, db.sender_tags)}
    assert got == {"st_0": "Riya", "st_1": "Club core team"}
    assert "Riya" not in json.dumps(rows(eng, db.sender_tags), default=str)           # no plaintext left in any column


def test_migration_is_idempotent_and_keeps_pseudonyms_and_dates(tmp_path):
    eng, _ = legacy_db(tmp_path)
    db.init_db(eng, raise_migration_errors=True)
    before = rows(eng, db.sender_tags)
    assert db.init_db(eng, raise_migration_errors=True)["sender_tags"] == {"migrated": 0, "dropped_plaintext_column": False}
    assert rows(eng, db.sender_tags) == before
    assert {r["pseudonym"] for r in before} == {"contact-00000000", "contact-00000001"}


def test_migration_failure_keeps_every_plaintext_label_and_a_rerun_finishes(tmp_path, monkeypatch):
    eng, _ = legacy_db(tmp_path)
    monkeypatch.setattr(migrations, "decrypt_text", lambda token, settings=None: "not the label")   # read-back fails
    with pytest.raises(RuntimeError):
        db.init_db(eng, raise_migration_errors=True)
    cols = {r[1] for r in eng.connect().execute(text("PRAGMA table_info(sender_tags)")).all()}
    assert "label" in cols                                                   # the plaintext column was NOT dropped
    labels = [r[0] for r in eng.connect().execute(text("SELECT label FROM sender_tags ORDER BY tag_id")).all()]
    assert labels == ["Riya", "Club core team"]                              # and nothing was overwritten
    assert db.init_db(eng) == {"error": True}                                # startup (not raising) survives it
    monkeypatch.undo()
    assert db.init_db(eng, raise_migration_errors=True)["sender_tags"]["migrated"] == 2   # the rerun finishes the job


def test_new_database_has_no_plaintext_column_and_migration_is_a_noop(engine):
    assert migrations.migrate_sender_tag_labels(engine) == {"migrated": 0, "dropped_plaintext_column": False}


def test_a_migrated_tag_still_works_in_the_app(web):
    """Tag made before the change (here: a row written the old way and migrated) is usable after the migration."""
    mid, tid = tagged(web)
    other = draft(web, "Second question?").json()["message_id"]
    assert tag(web, other, tag_id=tid).json()["contact_type"] == "new_group"


# ======================================================================================= 30-day unused deletion
def test_unused_tag_is_deleted_after_30_days_and_its_item_link_goes_too(web):
    mid, tid = tagged(web)
    later = datetime.now(timezone.utc) + 31 * DAY
    assert tags.purge_unused_tags(web.engine, get_settings(), now=later) == 1
    assert rows(web.engine, db.sender_tags) == [] and rows(web.engine, db.item_tags) == []


def test_a_tag_in_use_is_kept_and_using_it_again_moves_the_clock(web):
    mid, tid = tagged(web)
    s = get_settings()
    assert tags.purge_unused_tags(web.engine, s, now=datetime.now(timezone.utc) + 29 * DAY) == 0
    # applied again on day 25: unused for 30 days only counts from then
    t = db.sender_tags
    with web.engine.begin() as c:
        c.execute(t.update().values(last_used_at=datetime.now(timezone.utc) - 25 * DAY))
    other = draft(web, "Another?").json()["message_id"]
    tag(web, other, tag_id=tid)
    assert tags.purge_unused_tags(web.engine, s, now=datetime.now(timezone.utc) + 29 * DAY) == 0
    assert len(rows(web.engine, db.sender_tags)) == 1


def test_a_never_applied_tag_counts_from_its_creation_and_the_limit_is_configurable(web, monkeypatch):
    tagged(web)
    with web.engine.begin() as c:
        c.execute(db.sender_tags.update().values(last_used_at=None, created_at=datetime.now(timezone.utc) - 10 * DAY))
    assert tags.purge_unused_tags(web.engine, get_settings(), now=datetime.now(timezone.utc)) == 0
    monkeypatch.setenv("SENDER_TAG_RETENTION_DAYS", "7")
    get_settings.cache_clear()
    assert get_settings().sender_tag_retention_days == 7
    assert tags.purge_unused_tags(web.engine, get_settings(), now=datetime.now(timezone.utc)) == 1


def test_only_the_old_tag_goes_and_known_status_data_in_messages_is_not_touched(web):
    mid, tid = tagged(web, label="Old club")
    mk2, tid2 = tagged(web, label="Fresh club", text="Second question?")
    with web.engine.begin() as c:
        c.execute(db.sender_tags.update().where(db.sender_tags.c.tag_id == tid)
                  .values(last_used_at=datetime.now(timezone.utc) - 40 * DAY))
    assert tags.purge_unused_tags(web.engine, get_settings()) == 1
    assert [r["tag_id"] for r in rows(web.engine, db.sender_tags)] == [tid2]
    assert len(rows(web.engine, db.message_records)) == 2


def test_the_protected_sweep_runs_the_purge(web, monkeypatch):
    monkeypatch.setenv("SWEEP_SECRET", "s")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "w")
    get_settings.cache_clear()
    from app.telegram.router import get_bot_deps
    tagged(web)
    clock = Clock(datetime.now(timezone.utc) + 31 * DAY)
    deps = BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=FakeTelegram(),
                   drafter=web.drafter, now=clock)
    web.app.dependency_overrides[get_bot_deps] = lambda: deps
    try:
        r = web.client.post("/tasks/sweep", headers={"X-Sweep-Secret": "s"}).json()
        assert r["tags_deleted"] == 1 and rows(web.engine, db.sender_tags) == []
    finally:
        web.app.dependency_overrides.pop(get_bot_deps, None)


# ======================================================================================= export / delete everything
def test_export_includes_tags_readable_and_no_secrets(web):
    mid, tid = tagged(web)
    copy(web, mid)
    ex = data_controls.export_my_data(web.engine)
    assert ex["sender_tags"][0]["label"] == LABEL and "label_enc" not in ex["sender_tags"][0]
    assert ex["counts"]["sender_tags"] == 1 and ex["counts"]["message_records"] == 1 and ex["counts"]["events"] > 3
    assert ex["item_tags"][0]["tag_id"] == tid
    dump = json.dumps(ex, default=str)
    assert web.token not in dump and "token_hash" not in dump and ex["api_tokens"][0]["name"] == "phone"


def test_export_is_logged_with_counts_only(web):
    tagged(web)
    data_controls.export_my_data(web.engine)
    ev = events(web.engine, "data_exported")
    assert len(ev) == 1 and ev[0]["payload"]["counts"]["sender_tags"] == 1
    assert "Zorblax" not in json.dumps(ev[0]["payload"])


def test_delete_everything_needs_the_phrase(web):
    tagged(web)
    with pytest.raises(ValueError):
        data_controls.delete_everything(web.engine, "yes")
    assert len(rows(web.engine, db.sender_tags)) == 1


def test_delete_everything_removes_tags_and_all_user_data_but_keeps_tokens_and_logs_the_wipe(web):
    mid, tid = tagged(web)
    copy(web, mid)
    out = data_controls.delete_everything(web.engine, "Delete Everything")
    assert out["sender_tags"] == 1 and out["message_records"] == 1
    for t in (db.sender_tags, db.item_tags, db.message_records, db.telegram_threads, db.self_model_items):
        assert rows(web.engine, t) == []
    left = events(web.engine)
    assert [e["event_name"] for e in left] == ["data_deleted"] and left[0]["payload"]["deleted"]["sender_tags"] == 1
    assert left[0]["payload"]["scope"] == "everything"
    assert len(rows(web.engine, db.api_tokens)) == 1                          # settings, not data
    assert web.client.get("/app/api/tags").json()["tags"] == []


def test_cli_summary_export_delete_and_rewrap(web, tmp_path, capsys):
    from scripts import data_controls as cli
    tagged(web)
    assert cli.main(["summary"]) == 0
    out = capsys.readouterr().out
    assert "sender_tags" in out and "readable with this key: 1 of 1" in out and "APP_SECRET fallback" in out
    f = tmp_path / "mine.json"
    assert cli.main(["export", "--out", str(f)]) == 0
    assert json.loads(f.read_text(encoding="utf-8"))["sender_tags"][0]["label"] == LABEL
    assert cli.main(["rewrap-tags"]) == 0 and "rewrapped" in capsys.readouterr().out
    seed_self_model(web)
    assert cli.main(["delete-activity", "--confirm", "nope"]) == 2 and len(rows(web.engine, db.sender_tags)) == 1
    assert cli.main(["delete-activity", "--confirm", "delete everything"]) == 2     # the old phrase is not accepted here
    assert cli.main(["delete-activity", "--confirm", "delete my activity"]) == 0
    assert rows(web.engine, db.sender_tags) == [] and len(rows(web.engine, db.self_model_items)) == 1
    assert cli.main(["delete-self-model", "--confirm", "delete my activity"]) == 2
    assert cli.main(["delete-self-model", "--confirm", "delete my self-model"]) == 0
    assert rows(web.engine, db.self_model_items) == []


def seed_self_model(web):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with web.engine.begin() as c:
        c.execute(db.self_model_items.insert().values(
            item_id="sm_test000001", user_id="u", layer="voice", text="short and casual", source="manual",
            status="active", version=1, created_at=now, updated_at=now))


def test_delete_activity_keeps_self_model_and_tokens_and_logs_scope(web):
    mid, _ = tagged(web)
    copy(web, mid)
    seed_self_model(web)
    with pytest.raises(ValueError):
        data_controls.delete_activity(web.engine, "delete everything")
    out = data_controls.delete_activity(web.engine, "Delete My Activity")
    assert out["sender_tags"] == 1 and out["message_records"] == 1 and "self_model_items" not in out
    assert len(rows(web.engine, db.self_model_items)) == 1 and len(rows(web.engine, db.api_tokens)) == 1
    left = events(web.engine)
    assert [e["event_name"] for e in left] == ["data_deleted"] and left[0]["payload"]["scope"] == "activity"


def test_delete_self_model_touches_only_the_self_model(web):
    mid, _ = tagged(web)
    seed_self_model(web)
    with pytest.raises(ValueError):
        data_controls.delete_self_model(web.engine, "delete my activity")
    out = data_controls.delete_self_model(web.engine, "delete my self-model")
    assert out == {"self_model_items": 1} and rows(web.engine, db.self_model_items) == []
    assert len(rows(web.engine, db.sender_tags)) == 1 and len(rows(web.engine, db.message_records)) == 1
    assert [e["payload"]["scope"] for e in events(web.engine, "data_deleted")] == ["self_model"]


def test_reasoning_effort_variable_is_cleared_for_tests():
    import os
    assert "OPENAI_COMPAT_REASONING_EFFORT" not in os.environ


# ======================================================================================= New group (5.24 pt 2)
def test_new_group_is_new_group_and_someone_new_stays_new_individual(web):
    a = draft(web).json()["message_id"]
    assert tag(web, a, new_group=True).json()["contact_type"] == "new_group"
    assert web.writer.get_message(a)["contact_type"] == "new_group"
    assert tag(web, a, someone_new=True).json()["contact_type"] == "new_individual"
    assert tag(web, a, clear=True).json()["contact_type"] == "unknown"        # untagged stays unknown
    assert draft(web, "Fresh one?").json()["message_id"] and record(web.engine)["contact_type"] == "unknown"


def test_new_group_logs_both_decisions_never_eligible_and_is_not_a_saved_tag(web):
    mid = draft(web).json()["message_id"]
    r = tag(web, mid, new_group=True).json()
    assert r["rerun"] and r["action"] == "draft_notify" and "new_sender_draft_only" in r["reasons"]
    d = [e for e in events(web.engine, "autonomy_decision") if e["message_id"] == mid]
    assert len(d) == 2 and d[1]["contact_type"] == "new_group" and d[1]["payload"]["autonomy_eligible"] is False
    assert d[0]["payload"]["trigger"] == "initial" and d[1]["payload"]["rerun_of"] == d[0]["event_id"]
    assert rows(web.engine, db.sender_tags) == [] and r["tag"] is None       # nothing to encrypt: no label was typed


def test_new_group_never_becomes_known_even_after_replies(web):
    a = mk(web, "First question?")
    tag(web, a, new_group=True)
    confirm(web, a, "sent_as_is")
    b = draft(web, "Second question?").json()["message_id"]
    assert tag(web, b, new_group=True).json()["contact_type"] == "new_group"
    assert web.writer.get_message(a)["contact_pseudonym"] != web.writer.get_message(b)["contact_pseudonym"]


def test_new_group_keeps_a_content_limit(web):
    mid = draft(web, "Please pay Rs 500 for the hall").json()["message_id"]
    r = tag(web, mid, new_group=True).json()
    assert r["action"] == "ask_first" and r["changed"] is False


def test_from_screen_offers_new_group_and_the_short_label_hint(web):
    js = web.client.get("/app/static/app.js").text
    assert "New group" in js and "new_group: true" in js and "Club senior" in js


# ======================================================================================= Not copied (5.24 pt 3)
def listed(web):
    return {i["message_id"]: i for i in web.client.get("/app/api/pending").json()["items"]}


def test_shown_but_never_copied_web_draft_is_listed_as_not_copied(web):
    nc = mk(web, "Will you bring the banner?", copied=False)
    cp = mk(web, "Is the hall free?", copied=True)
    items = listed(web)
    assert items[nc]["kind"] == "not_copied" and items[nc]["draft"] and items[nc]["draft_masked"] is True
    assert items[cp]["kind"] == "copied"
    assert items[nc]["confirm_within_hours"] in (23, 24)
    p = web.client.get("/app/api/pending").json()
    assert p["not_copied"] == 1 and p["copied"] == 1 and p["held"] == 0
    assert PHONE not in json.dumps(p)


def test_copying_moves_a_not_copied_item_to_copied(web):
    nc = mk(web, copied=False)
    assert listed(web)[nc]["kind"] == "not_copied"
    copy(web, nc)
    assert listed(web)[nc]["kind"] == "copied"


def test_held_ask_first_failed_and_shortcut_drafts_are_not_in_not_copied(web, monkeypatch):
    # shortcut draft (always copied by its own flow): never listed as not copied, even before the copy call
    h = {"Authorization": f"Bearer {web.token}", "Content-Type": "text/plain"}
    sc = web.client.post("/api/draft?format=json", content=b"Will you come on Saturday?", headers=h).json()
    assert sc["message_id"] not in listed(web)
    ask = draft(web, "Please pay Rs 500 for the hall").json()["message_id"]       # ask first: no draft
    assert ask not in listed(web)
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    held = draft(web, "A held one?").json()["message_id"]
    assert listed(web)[held]["kind"] == "held"                                    # held stays its own kind
    web.drafter.fail = __import__("app.drafting.base", fromlist=["DraftError"]).DraftError("llm_error", "boom")
    monkeypatch.setenv("WEB_BLIND_RATIO", "0")
    get_settings.cache_clear()
    failed = draft(web, "This one fails?").json()
    assert failed["action"] == "failed" and (failed["message_id"] or "x") not in listed(web)


def test_replied_my_own_way_without_a_reply_is_a_discard(web):
    mid = mk(web, copied=False)
    r = confirm(web, mid, "replied_own")
    assert r["ok"] and r["outcome"] == "discarded" and r["reply_saved"] is False
    rec = web.writer.get_message(mid)
    assert rec["outcome"] == "discarded" and rec["scorable"] is True and rec["sent_text"] is None
    acted = [e for e in events(web.engine, "draft_acted") if e["message_id"] == mid][0]["payload"]
    assert acted["action"] == "discarded" and acted["capture"] == "button" and acted["reply_shared"] is False
    assert acted["was_copied"] is False and acted["shown_state"] == "shown"
    assert len(events(web.engine, "draft_discarded")) == 1
    assert events(web.engine, "draft_sent") == [] and web.hook.calls == []        # no reply, nothing to learn from
    assert mid not in listed(web)
    assert confirm(web, mid, "replied_own")["status"] == 409                       # one close per item


def test_replied_my_own_way_with_a_reply_saves_it_masked_and_hands_it_to_the_classifier(web):
    mid = mk(web, copied=False)
    r = confirm(web, mid, "replied_own", f"Haan Friday 5 baje, call {PHONE}")
    assert r["outcome"] == "discarded" and r["reply_saved"] is True
    rec = web.writer.get_message(mid)
    assert rec["outcome"] == "discarded" and rec["scorable"] is True              # still a discard in the scores
    assert rec["sent_text"] == "Haan Friday 5 baje, call [PHONE]" and PHONE not in everything_stored(web.engine)
    acted = [e for e in events(web.engine, "draft_acted") if e["message_id"] == mid][0]["payload"]
    assert acted["capture"] == "paste" and acted["reply_shared"] is True and acted["action"] == "discarded"
    assert web.hook.calls == [(mid, "paste")]
    assert events(web.engine, "draft_sent") == []                                 # a discard is not a send


def test_a_copied_draft_can_also_be_closed_as_replied_my_own_way(web):
    mid = mk(web, copied=True)
    assert confirm(web, mid, "replied_own")["outcome"] == "discarded"
    assert [e for e in events(web.engine, "draft_acted") if e["message_id"] == mid][0]["payload"]["was_copied"] is True


def test_not_replying_on_a_not_copied_item_is_not_a_discard_and_is_never_scored(web):
    mid = mk(web, copied=False)
    assert confirm(web, mid, "not_replying")["outcome"] == "not_replying"
    rec = web.writer.get_message(mid)
    assert rec["outcome"] == "not_replying" and rec["scorable"] is False
    assert len(events(web.engine, "not_replying")) == 1 and events(web.engine, "draft_discarded") == []


def test_untouched_not_copied_item_becomes_outcome_unknown_after_24_hours(web):
    mid = mk(web, copied=False)
    assert mid in listed(web)
    deps = BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=FakeTelegram(),
                   drafter=web.drafter, now=Clock(datetime.now(timezone.utc) + timedelta(hours=25)))
    assert sweep_expired(deps) == 1
    rec = web.writer.get_message(mid)
    assert rec["outcome"] == "unknown" and rec["scorable"] is False
    assert events(web.engine, "outcome_unknown")[0]["payload"]["reason"] == "capture_window_expired"
    assert mid not in listed(web)


def test_a_discard_counts_against_the_numbers_where_a_forgotten_draft_would_not(web):
    """The point of 5.24 pt 3: a draft that was used for nothing is a discard, not a silent drop."""
    from sqlalchemy import select  # noqa: F401
    mid = mk(web, copied=False)
    confirm(web, mid, "replied_own", "my own reply")
    st = compute_stats(web.engine)
    block = next(iter(st.blocks.values()))
    assert block.discarded == 1 and block.scored == 1


def test_the_batch_confirm_handles_a_mix_including_replied_own(web):
    a, b, c = mk(web, "One?", copied=False), mk(web, "Two?", copied=False), mk(web, "Three?", copied=True)
    r = web.client.post("/app/api/confirm", json={"items": [
        {"message_id": a, "action": "replied_own", "text": "my own"},
        {"message_id": b, "action": "not_replying"},
        {"message_id": c, "action": "sent_as_is"}]}).json()["results"]
    assert [x["outcome"] for x in r] == ["discarded", "not_replying", "sent_as_is"]


def test_replied_own_is_refused_for_held_and_for_ask_first_items(web, monkeypatch):
    ask = draft(web, "Please pay Rs 500 for the hall").json()["message_id"]
    assert confirm(web, ask, "replied_own")["status"] == 409                      # no draft to discard
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    held = draft(web, "A held one?").json()["message_id"]
    assert confirm(web, held, "replied_own")["status"] == 409
    assert confirm(web, held, "nonsense")["status"] == 422


def test_the_nudge_counts_copied_drafts_only(web, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mini-me.example")
    get_settings.cache_clear()
    mk(web, "One?", copied=True)
    mk(web, "Two?", copied=False)
    mk(web, "Three?", copied=False)
    tg, deps = nudge_deps(web, 20)
    assert nudge.maybe_send_nudge(deps) is True
    sent = json.dumps(tg.sent, default=str)
    assert "1 copied draft waiting" in sent and "not copied" not in sent.lower()
    assert events(web.engine, "nudge_sent")[0]["payload"]["pending_count"] == 1


def test_nudge_stays_quiet_when_only_not_copied_items_wait(web):
    mk(web, copied=False)
    tg, deps = nudge_deps(web, 20)
    assert nudge.maybe_send_nudge(deps) is False and tg.sent == []


def test_the_page_script_has_the_not_copied_section_without_urgency_words(web):
    js = web.client.get("/app/static/app.js").text
    assert "Not copied" in js and "Replied my own way" in js and "replied_own" in js
    section = js[js.index("const quiet"):js.index("if (S.queue.size) kids.push")].lower()
    for bad in ("overdue", "runs out", "urgent", "hurry", "unlock", "level up", "gate", "sent"):
        assert bad not in section


# ======================================================================================= classifier learns from a reply
def test_classifier_reads_a_discard_that_carries_a_reply_and_still_skips_a_bare_discard(web):
    clf = EditClassifier(web.engine, web.writer, settings=get_settings(), notify=False)
    bare = mk(web, "One?", copied=False)
    confirm(web, bare, "replied_own")
    assert clf.classify(bare).status == "skipped"
    mid = mk(web, "Two?", copied=False)
    draft_text = web.writer.get_message(mid)["draft_text"]
    confirm(web, mid, "replied_own", draft_text.upper())                                # differs only in case: rules, no model
    res = clf.classify(mid)
    assert res.status == "classified" and res.edit_class == "cosmetic"
    rec = web.writer.get_message(mid)
    assert rec["outcome"] == "discarded" and rec["edit_class"] == "cosmetic"
    assert next(iter(compute_stats(web.engine).blocks.values())).discarded == 2          # still counted as discards
    assert events(web.engine, "edit_classified")[0]["payload"]["outcome"] == "discarded"
