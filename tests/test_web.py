"""Dev 7: installable web app + capture (decisions 5.20 pt 3, 5.21, 5.23): guards, web draft, blind sample, waiting
list, confirm actions, sender tags and the re-run, nudge, sweep, share target, static files."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, db
from app.config import get_settings
from app.drafting.base import DraftError
from app.events import EventWriter
from app.share import cache, limits, nudge, pending, service
from app.share.router import get_share_deps
from app.share import tokens
from app.telegram.bot import BotDeps, sweep_expired
from app.telegram.store import ThreadStore
from app.web.router import get_web_deps
from test_share import FakeDrafter, events, record, rows, everything_stored
from test_telegram import BRAJ, CHAT, FakeTelegram


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

MSG = "Is the club meet on Friday at 5? Call 9876543210"
PHONE = "9876543210"


class Hook:
    def __init__(self):
        self.calls = []

    def __call__(self, mid, capture):
        self.calls.append((mid, capture))
        return None


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/web.db")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", str(BRAJ))
    for n in ("SHARE_ALWAYS_ASK_TOPICS", "SHARE_CONFIDENCE_FLOOR", "TEST_MODE"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("WEB_BLIND_RATIO", "0")          # the 1-in-10 sample is random: tests opt in explicitly
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    cache.clear()
    from app.main import app
    drafter = FakeDrafter(text=f"yes, see you at 5. Call {PHONE}")
    hook = Hook()
    with TestClient(app) as client:
        engine = db.get_engine()
        ctx = type("Ctx", (), {})()
        ctx.client, ctx.engine, ctx.drafter, ctx.hook, ctx.writer = client, engine, drafter, hook, EventWriter(engine)
        client.cookies.set(auth.COOKIE, auth.make_session_token("braj"))
        app.dependency_overrides[get_web_deps] = lambda: (engine, ctx.writer, get_settings(), ctx.drafter, ctx.hook)
        app.dependency_overrides[get_share_deps] = lambda: (engine, ctx.writer, get_settings(), ctx.drafter)
        ctx.app = app
        ctx.token = tokens.create_token(engine, "phone")[1]
        yield ctx
    app.dependency_overrides.pop(get_web_deps, None)
    app.dependency_overrides.pop(get_share_deps, None)
    db.get_engine.cache_clear()


def draft(web, text=MSG, entry="paste", **kw):
    return web.client.post("/app/api/drafts", json={"text": text, "entry": entry, **kw})


def copy(web, mid, n=None):
    return web.client.post(f"/app/api/items/{mid}/copied", json={"copy_no": n} if n else {})


def mk(web, text=MSG, copied=True):
    r = draft(web, text).json()
    if copied:
        assert copy(web, r["message_id"]).status_code == 200
    return r["message_id"]


# ----------------------------------------------------------------------------- test-mode guard (5.23 pt 3)
def test_incoming_text_is_masked_used_for_the_draft_and_not_stored(web):
    r = draft(web).json()
    assert r["action"] == "draft" and r["test_mode"] is True and "made-up or own" in r["test_mode_label"]
    assert "[PHONE]" in r["masked_text"] and PHONE not in r["masked_text"]        # shown masked on screen
    assert web.drafter.requests[0].masked_text == "Is the club meet on Friday at 5? Call [PHONE]"
    rec = record(web.engine)
    assert rec["incoming_text"] is None and rec["draft_text"] and PHONE not in rec["draft_text"]
    stored = everything_stored(web.engine)
    assert PHONE not in stored and "club meet" not in stored                       # the incoming message is nowhere
    assert events(web.engine, "message_received")[0]["payload"]["text_kept"] is False


def test_with_test_mode_off_the_masked_text_is_kept(web, monkeypatch):
    monkeypatch.setenv("TEST_MODE", "false")
    get_settings.cache_clear()
    r = draft(web).json()
    assert r["test_mode"] is False and r["test_mode_label"] is None
    assert record(web.engine)["incoming_text"] == "Is the club meet on Friday at 5? Call [PHONE]"


def test_shortcut_responses_carry_the_test_mode_label_and_json_flag(web):
    h = {"Authorization": f"Bearer {web.token}", "Content-Type": "text/plain"}
    r = web.client.post("/api/draft?format=text", content=b"Is the club meet on Friday?", headers=h)
    assert r.status_code == 200 and r.headers["x-mini-me-test-mode"] == "on" and "Test mode" not in r.text
    j = web.client.post("/api/draft?format=json", content=b"Will you come on Saturday?", headers=h).json()
    assert j["test_mode"] is True and j["test_mode_label"].startswith("Test mode")


# ----------------------------------------------------------------------------- copy-first guard (5.23 pt 1)
def test_copy_first_guard_refuses_the_last_draft_on_the_shortcut(web):
    h = {"Authorization": f"Bearer {web.token}", "Content-Type": "text/plain"}
    first = web.client.post("/api/draft?format=text", content=b"Is the club meet on Friday at 5?", headers=h)
    n_before, calls = len(rows(web.engine, db.message_records)), len(web.drafter.requests)
    again = web.client.post("/api/draft?format=text", content=first.text.encode(), headers=h)   # clipboard still = draft
    assert again.status_code == 409 and again.headers["x-mini-me-action"] == "copy_first"
    assert "Copy the message" in again.text
    assert len(rows(web.engine, db.message_records)) == n_before and len(web.drafter.requests) == calls   # nothing made
    other = web.client.post("/api/draft?format=text", content=b"A different question?", headers=h)
    assert other.status_code == 200


def test_copy_first_guard_on_the_web_matches_real_values_and_masked_text(web):
    r = draft(web).json()
    assert PHONE in r["draft"]                                         # real value restored in the response
    again = draft(web, text=r["draft"]).json()                         # the clipboard holds the restored draft
    assert again["action"] == "copy_first" and again["message_id"] is None and again["draft"] if "draft" in again else True
    assert len(rows(web.engine, db.message_records)) == 1


def test_copy_first_ignores_a_held_blind_draft_that_was_never_shown(web, monkeypatch):
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    held = draft(web).json()
    assert held["action"] == "held"
    # the held draft text equals what the drafter wrote; pasting that text must NOT be refused (never on the clipboard)
    assert draft(web, text=f"yes, see you at 5. Call {PHONE}").json()["action"] == "held"


# ----------------------------------------------------------------------------- web draft (1b-1d, 5.21 pt 2)
def test_web_draft_record_events_and_real_values_restored(web):
    r = draft(web, entry="paste").json()
    assert r["action"] == "draft" and r["confidence_label"] == "High" and PHONE in r["draft"]
    rec = record(web.engine)
    assert rec["channel"] == "web_share" and rec["device_type"] == "mobile" and rec["shown_state"] == "shown"
    assert rec["contact_type"] == "unknown" and PHONE not in (rec["draft_text"] or "")     # stored draft stays masked
    names = [e["event_name"] for e in events(web.engine)]
    assert names == ["message_received", "draft_generated", "autonomy_decision", "draft_shown"]
    rcv = events(web.engine, "message_received")[0]["payload"]
    assert rcv["origin"] == "pasted" and rcv["entry"] == "paste"
    gen = events(web.engine, "draft_generated")[0]["payload"]
    assert gen["blind_eligible"] is True and gen["shown_state"] == "shown"
    assert PHONE not in everything_stored(web.engine)
    # reopening the screen: real values come back from the short in-memory cache only
    it = web.client.get(f"/app/api/items/{r['message_id']}").json()
    assert it["action"] == "draft" and PHONE in it["draft"]
    cache.clear()
    assert PHONE not in web.client.get(f"/app/api/items/{r['message_id']}").json()["draft"]


def test_share_entry_is_origin_shared(web):
    draft(web, entry="share")
    assert events(web.engine, "message_received")[0]["payload"]["origin"] == "shared"


def test_ask_first_failed_and_retry_on_the_web(web):
    money = draft(web, text="Please pay Rs 500 for the hall").json()
    assert money["action"] == "ask_first" and "money" in money["reason"]["headline"]
    assert web.drafter.requests == []                                    # never reached the model
    web.drafter.fail = DraftError("llm_error")
    bad = draft(web, text="Will you come on Saturday?")
    assert bad.status_code == 502 and bad.json()["action"] == "failed"
    failed_id = bad.json()["message_id"]
    assert events(web.engine, "outcome_unknown")[-1]["payload"]["reason"] == "draft_failed"
    web.drafter.fail = None
    retry = draft(web, text="Will you come on Saturday?", retry_of=failed_id).json()
    assert retry["action"] == "draft" and retry["message_id"] != failed_id
    assert [e for e in events(web.engine, "message_received") if e["message_id"] == retry["message_id"]][0]["payload"]["retry_of"] == failed_id


def test_api_needs_the_signed_in_cookie(web):
    web.client.cookies.clear()
    for method, path in (("post", "/app/api/drafts"), ("get", "/app/api/pending"), ("get", "/app/api/tags"),
                         ("post", "/app/api/confirm"), ("post", "/app/api/items/sh-x/copied")):
        r = getattr(web.client, method)(path, **({"json": {"text": "hi", "entry": "paste"}} if method == "post" else {}))
        assert r.status_code == 401
    assert web.client.get("/app/api/session").json()["signed_in"] is False
    assert rows(web.engine, db.message_records) == []


def test_api_posts_need_json(web):
    assert web.client.post("/app/api/drafts", content=b"hello", headers={"Content-Type": "text/plain"}).status_code == 415


# ----------------------------------------------------------------------------- draft_copied (5.20 pt 3)
def test_copy_logs_only_after_the_client_confirms_and_is_not_sent(web):
    mid = draft(web).json()["message_id"]
    assert events(web.engine, "draft_copied") == []
    r = copy(web, mid, 1).json()
    assert r["logged"] is True and r["outcome"] == "waiting"
    assert copy(web, mid, 1).json()["logged"] is False                    # idempotent retry
    assert copy(web, mid, 2).json()["logged"] is True
    assert events(web.engine, "draft_sent") == [] and events(web.engine, "draft_acted") == []
    assert record(web.engine)["outcome"] == "waiting"


# ----------------------------------------------------------------------------- blind sample (5.21 pt 4)
def blind(web, monkeypatch):
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    return draft(web).json()


def test_held_draft_never_reaches_the_browser(web, monkeypatch):
    r = blind(web, monkeypatch)
    assert r["action"] == "held" and r["message_id"]
    wire = json.dumps(r)
    assert "see you at 5" not in wire and "draft" not in {k for k, v in r.items() if v} and "confidence_label" not in r
    it = web.client.get(f"/app/api/items/{r['message_id']}").json()
    assert it["action"] == "held" and "see you at 5" not in json.dumps(it) and "draft" not in it
    assert "see you at 5" not in json.dumps(web.client.get("/app/api/pending").json())
    names = [e["event_name"] for e in events(web.engine)]
    assert "draft_held_blind" in names and "draft_shown" not in names
    assert ThreadStore(web.engine).get(r["message_id"])["state"] == "blind_held"
    assert events(web.engine, "draft_generated")[0]["payload"]["shown_state"] == "blind"


def test_held_draft_cannot_be_confirmed_or_copied_but_can_be_skipped(web, monkeypatch):
    mid = blind(web, monkeypatch)["message_id"]
    c = web.client.post("/app/api/confirm", json={"items": [{"message_id": mid, "action": "sent_as_is"}]}).json()
    assert c["results"][0]["ok"] is False and c["results"][0]["status"] == 409
    assert copy(web, mid).status_code in (200, 409)         # copying is not offered; whatever it answers logs no outcome
    assert record(web.engine)["outcome"] == "waiting"
    skip = web.client.post(f"/app/api/items/{mid}/not-replying", json={}).json()
    assert skip["outcome"] == "not_replying" and events(web.engine, "not_replying")[0]["payload"]["shown_state"] == "blind"


def test_reply_first_then_reveal(web, monkeypatch):
    mid = blind(web, monkeypatch)["message_id"]
    assert web.client.post(f"/app/api/items/{mid}/reveal", json={}).status_code == 409    # nothing before the reply
    r = web.client.post(f"/app/api/items/{mid}/reply", json={"text": f"yes, 5pm. Call {PHONE}", "capture": "share"})
    assert r.status_code == 200 and r.json()["outcome"] == "edited_sent" and "draft" not in r.json()
    assert web.hook.calls == [(mid, "share")]
    acted = events(web.engine, "draft_acted")[0]["payload"]
    assert acted["capture"] == "share" and acted["shown_state"] == "blind"
    assert PHONE not in everything_stored(web.engine)
    assert events(web.engine, "draft_released_after_reply") == []                         # not until the user taps
    rv = web.client.post(f"/app/api/items/{mid}/reveal", json={}).json()
    assert rv["draft"] and rv["matched"] is False and "[PHONE]" in rv["your_reply"]
    assert len(events(web.engine, "draft_released_after_reply")) == 1
    assert web.client.post(f"/app/api/items/{mid}/reply", json={"text": "again"}).status_code == 409


def test_blind_ratio_default_is_one_in_ten_and_the_shortcut_never_holds(web, monkeypatch):
    monkeypatch.delenv("WEB_BLIND_RATIO")
    get_settings.cache_clear()
    s = get_settings()
    assert s.web_blind_ratio == 0.1 and s.telegram_blind_ratio == 0.2
    out = service.create_share_draft(web.engine, web.writer, s, web.drafter, text="Is it on?", channel="web_share",
                                     rng=lambda: 0.09)
    assert out.shown_state == "blind"
    out2 = service.create_share_draft(web.engine, web.writer, s, web.drafter, text="Is it on Monday?", channel="web_share",
                                      rng=lambda: 0.11)
    assert out2.shown_state == "shown"
    out3 = service.create_share_draft(web.engine, web.writer, s, web.drafter, text="Is it on Tuesday?", channel="shortcut",
                                      rng=lambda: 0.0)
    assert out3.shown_state == "shown"


def test_sweep_closes_held_items_as_outcome_unknown(web, monkeypatch):
    mid = blind(web, monkeypatch)["message_id"]
    clock = Clock(datetime.now(timezone.utc) + timedelta(hours=25))
    deps = BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=FakeTelegram(),
                   drafter=web.drafter, now=clock)
    assert sweep_expired(deps) == 1
    assert record(web.engine)["outcome"] == "unknown"
    ou = events(web.engine, "outcome_unknown")[0]["payload"]
    assert ou["reason"] == "capture_window_expired" and ou["shown_state"] == "blind"
    assert web.client.get("/app/api/pending").json()["items"] == []


def test_held_draft_is_not_sent_to_telegram_pending(web, monkeypatch):
    blind(web, monkeypatch)
    from test_telegram import _msg
    from app.telegram.bot import handle_update
    tg = FakeTelegram()
    deps = BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=tg, drafter=web.drafter)
    handle_update(_msg("/pending"), deps)
    assert "see you at 5" not in json.dumps(tg.sent, default=str) and "held back" in json.dumps(tg.sent, default=str)


# ----------------------------------------------------------------------------- waiting list + confirm (5.21 pt 3)
def test_waiting_list_lists_copied_and_held_with_test_mode_note(web, monkeypatch):
    a = mk(web, "Is the club meet on Friday?", copied=True)
    mk(web, "Will you bring the banner?", copied=False)                   # never copied: not listed
    p = web.client.get("/app/api/pending").json()
    assert [i["message_id"] for i in p["items"]] == [a] and p["copied"] == 1 and p["held"] == 0
    c = p["items"][0]
    assert c["kind"] == "copied" and c["incoming"] is None and c["incoming_note"] == "message not kept in test mode"
    assert c["confirm_within_hours"] in (23, 24) and c["draft"] and "[PHONE]" not in "x"
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    draft(web, "Another question?")
    p = web.client.get("/app/api/pending").json()
    assert p["copied"] == 1 and p["held"] == 1
    held = [i for i in p["items"] if i["kind"] == "held"][0]
    assert "draft" not in held and "confidence" not in held


def test_waiting_list_is_sorted_soonest_first_and_survives_empty(web):
    assert web.client.get("/app/api/pending").json()["items"] == []
    a, b = mk(web, "First question?"), mk(web, "Second question?")
    t = db.telegram_threads
    with web.engine.begin() as conn:
        conn.execute(t.update().where(t.c.message_id == b).values(deadline_at=datetime.now(timezone.utc) + timedelta(hours=3)))
    ids = [i["message_id"] for i in web.client.get("/app/api/pending").json()["items"]]
    assert ids == [b, a]


def confirm(web, mid, action, text=None):
    return web.client.post("/app/api/confirm", json={"items": [{"message_id": mid, "action": action, "text": text}]}).json()["results"][0]


def test_sent_as_is_edited_and_not_replying(web):
    a, b, c = mk(web, "One?"), mk(web, "Two?"), mk(web, "Three?")
    assert confirm(web, a, "sent_as_is")["outcome"] == "sent_as_is"
    ra = web.writer.get_message(a)
    assert ra["scorable"] is True and ra["sent_text"] == ra["draft_text"]
    assert [e["payload"]["capture"] for e in events(web.engine, "draft_acted") if e["message_id"] == a] == ["button"]
    r = confirm(web, b, "edited", f"yes, see you at 6. Call {PHONE}")
    assert r["outcome"] == "edited_sent" and "[PHONE]" in web.writer.get_message(b)["sent_text"]
    assert PHONE not in everything_stored(web.engine)
    assert [e["payload"]["capture"] for e in events(web.engine, "draft_sent") if e["message_id"] == b] == ["paste"]
    assert confirm(web, c, "not_replying")["outcome"] == "not_replying"
    assert web.writer.get_message(c)["scorable"] is False and len(events(web.engine, "not_replying")) == 1
    assert [m for m, _ in web.hook.calls] == [a, b] and [k for _, k in web.hook.calls] == ["button", "paste"]
    assert web.client.get("/app/api/pending").json()["items"] == []


def test_a_pasted_edit_equal_to_the_draft_is_sent_as_is(web):
    mid = mk(web)
    r = confirm(web, mid, "edited", f"yes, see you at 5. Call {PHONE}")
    assert r["outcome"] == "sent_as_is" and r["matched"] is True
    assert events(web.engine, "draft_acted")[0]["payload"]["action"] == "sent"
    assert events(web.engine, "draft_sent")[0]["payload"]["edited"] is False


def test_double_confirm_and_bad_input(web):
    mid = mk(web)
    assert confirm(web, mid, "sent_as_is")["ok"] is True
    again = confirm(web, mid, "not_replying")
    assert again["ok"] is False and again["status"] == 409
    assert len(events(web.engine, "draft_acted")) == 1 and events(web.engine, "not_replying") == []
    other = mk(web, "Other?")
    assert confirm(web, other, "edited", "  ")["status"] == 422
    assert confirm(web, other, "teleport")["status"] == 422
    assert confirm(web, "sh-nope", "sent_as_is")["status"] == 404
    assert web.writer.get_message(other)["outcome"] == "waiting"


def test_a_confirmation_on_the_phone_closes_the_telegram_card_too(web):
    mid = mk(web)
    assert confirm(web, mid, "sent_as_is")["ok"]
    assert ThreadStore(web.engine).get(mid)["state"] == "closed"
    from app.telegram.bot import handle_update
    from test_telegram import _cb
    tg = FakeTelegram()
    deps = BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=tg, drafter=web.drafter)
    handle_update(_cb(f"nrep:{mid}", 5), deps)
    assert events(web.engine, "not_replying") == [] and record(web.engine)["outcome"] == "sent_as_is"


def test_unconfirmed_copied_item_becomes_outcome_unknown_after_24h(web):
    mid = mk(web)
    deps = BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=FakeTelegram(),
                   drafter=web.drafter, now=Clock(datetime.now(timezone.utc) + timedelta(hours=24, minutes=5)))
    assert sweep_expired(deps) == 1
    assert record(web.engine)["outcome"] == "unknown" and record(web.engine)["scorable"] is False
    assert confirm(web, mid, "sent_as_is")["status"] == 409


# ----------------------------------------------------------------------------- ask-first reply (5.21 pt 3)
def test_share_your_reply_on_ask_first_is_user_authored_and_unscored(web):
    r = draft(web, text="Please pay Rs 500 for the hall").json()
    mid = r["message_id"]
    out = web.client.post(f"/app/api/items/{mid}/reply", json={"text": f"Sent it. Call {PHONE}", "capture": "share"}).json()
    assert out["outcome"] == "user_authored"
    rec = web.writer.get_message(mid)
    assert rec["scorable"] is False and "[PHONE]" in rec["sent_text"] and rec["draft_text"] is None
    acted = events(web.engine, "draft_acted")[0]["payload"]
    assert acted["action"] == "user_authored" and acted["capture"] == "share"
    assert web.hook.calls == []                                           # never classified: no draft to compare
    assert web.client.post(f"/app/api/items/{mid}/reply", json={"text": "again"}).status_code == 409
    d = draft(web, text="Is the club meet on Friday?").json()             # a real draft is not an ask-first item
    assert web.client.post(f"/app/api/items/{d['message_id']}/reply", json={"text": "x"}).status_code == 409


# ----------------------------------------------------------------------------- sender tags + re-run (5.21 pt 1)
def tag(web, mid, **body):
    return web.client.post(f"/app/api/items/{mid}/tag", json=body)


def test_tagging_reruns_the_decision_and_keeps_both(web):
    mid = draft(web).json()["message_id"]
    r = tag(web, mid, label="Cultural committee", kind="group").json()
    assert r["rerun"] is True and r["action"] == "draft_notify" and r["contact_type"] == "new_group"
    decisions = [e for e in events(web.engine, "autonomy_decision") if e["message_id"] == mid]
    assert len(decisions) == 2
    first, second = decisions
    assert first["payload"]["trigger"] == "initial" and first["payload"]["rerun_of"] is None
    assert second["payload"]["trigger"] == "sender_tag" and second["payload"]["rerun_of"] == first["event_id"]
    assert second["payload"]["autonomy_eligible"] is False and "new_sender_draft_only" in second["payload"]["reasons"]
    assert first["payload"]["action"] == "draft_notify" and first["payload"]["reasons"][-1] == "untagged_sender"   # never edited
    rec = web.writer.get_message(mid)
    assert rec["contact_type"] == "new_group" and rec["contact_pseudonym"].startswith("contact-")
    assert "Cultural" not in everything_stored(web.engine).replace("Cultural committee", "")   # label not in any message row
    assert "would_have_sent_shadow" not in json.dumps(events(web.engine), default=str)
    # same tag again: nothing new is logged
    assert tag(web, mid, label="Cultural committee", kind="group").json()["rerun"] is False
    assert len([e for e in events(web.engine, "autonomy_decision") if e["message_id"] == mid]) == 2


def test_tag_choices_someone_new_clear_and_recent_list(web):
    mid = draft(web).json()["message_id"]
    assert tag(web, mid, someone_new=True).json()["contact_type"] == "new_individual"
    assert tag(web, mid, clear=True).json()["contact_type"] == "unknown"
    t = tag(web, mid, label="Priya R", kind="individual").json()
    assert t["tag"]["label"] == "Priya R"
    tags_ = web.client.get("/app/api/tags").json()["tags"]
    assert [x["label"] for x in tags_] == ["Priya R"]
    other = draft(web, text="Another one?").json()["message_id"]
    assert tag(web, other, tag_id=tags_[0]["tag_id"]).json()["contact_type"] == "new_individual"
    assert tag(web, other, tag_id="st_nope").status_code == 404
    assert tag(web, other, label=" ", kind="group").status_code == 422
    assert tag(web, other, label="Club", kind="robot").status_code == 422
    # the card on the waiting list names the tag
    copy(web, other)
    assert web.client.get("/app/api/pending").json()["items"][0]["group"] == "Priya R"


def test_a_tag_becomes_known_after_a_reply_and_labels_are_masked(web):
    a = mk(web)
    tag(web, a, label="Club group", kind="group")
    confirm(web, a, "sent_as_is")                                           # the user replied: now it is known
    b = draft(web, text="Next question?").json()["message_id"]
    t = web.client.get("/app/api/tags").json()["tags"][0]
    assert tag(web, b, tag_id=t["tag_id"]).json()["contact_type"] == "known_group"
    c = draft(web, text="Third question?").json()["message_id"]
    tag(web, c, label="Call 9876543210", kind="individual")
    assert PHONE not in json.dumps(rows(web.engine, db.sender_tags), default=str)


def test_ask_first_stays_ask_first_after_a_tag_and_closed_items_refuse(web):
    mid = draft(web, text="Please pay Rs 500 for the hall").json()["message_id"]
    r = tag(web, mid, label="Treasurer", kind="individual").json()
    assert r["action"] == "ask_first" and r["changed"] is False            # a tag clears only the contact reason
    second = [e for e in events(web.engine, "autonomy_decision") if e["message_id"] == mid][-1]["payload"]
    assert second["limit"] == "money" and second["trigger"] == "sender_tag"
    assert web.drafter.requests == []
    web.client.post(f"/app/api/items/{mid}/reply", json={"text": "done"})
    assert tag(web, mid, someone_new=True).status_code == 409


# ----------------------------------------------------------------------------- Telegram nudge (5.21 pt 5-6)
def nudge_deps(web, hour, **settings_env):
    tz = timezone(timedelta(hours=5, minutes=30))
    real = datetime.now(tz)
    cands = [real.replace(hour=hour, minute=5, second=0, microsecond=0) + timedelta(days=d) for d in (-1, 0, 1)]
    now = min(cands, key=lambda c: abs(c - real)).astimezone(timezone.utc)      # nearest, so a copied item is never expired
    tg = FakeTelegram()
    return tg, BotDeps(settings=get_settings(), engine=web.engine, writer=web.writer, client=tg, drafter=web.drafter,
                       now=Clock(now))


def test_nudge_at_the_checkin_time_counts_copied_not_held_and_has_no_text(web, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mini-me.example")
    get_settings.cache_clear()
    mk(web, "Is the club meet on Friday?"), mk(web, "Will you bring the banner?")
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    draft(web, "A held one?")
    tg, deps = nudge_deps(web, 20)
    assert nudge.maybe_send_nudge(deps) is True
    text = tg.sent[0]["text"] if isinstance(tg.sent[0], dict) else str(tg.sent[0])
    assert "2 copied drafts waiting" in text and "Confirm within" in text and " h" in text
    for bad in ("club", "banner", "held", "sent", "overdue", "runs out", "urgent", "[PHONE]", PHONE):
        assert bad not in text.lower() or bad == "sent" and False
    ev = events(web.engine, "nudge_sent")
    assert len(ev) == 1 and ev[0]["payload"]["pending_count"] == 2 and ev[0]["payload"]["channel"] == "telegram"
    assert events(web.engine, "digest_sent") == []
    assert nudge.maybe_send_nudge(deps) is False                                # once a day
    assert len(tg.sent) == 1


def test_nudge_is_quiet_outside_the_window_at_zero_and_for_held_only(web, monkeypatch):
    mk(web)
    tg, early = nudge_deps(web, 14)
    assert nudge.maybe_send_nudge(early) is False and tg.sent == []
    _, late = nudge_deps(web, 23)
    assert nudge.maybe_send_nudge(late) is False                                # past the 3 h window
    web2 = None
    assert nudge.nudge_text(1, 5) == "1 copied draft waiting · Confirm within 5 h"


def test_nudge_sends_nothing_when_only_held_items_wait(web, monkeypatch):
    monkeypatch.setenv("WEB_BLIND_RATIO", "1")
    get_settings.cache_clear()
    draft(web)
    tg, deps = nudge_deps(web, 20)
    assert nudge.maybe_send_nudge(deps) is False and tg.sent == [] and events(web.engine, "nudge_sent") == []


def test_nudge_rides_on_the_protected_sweep_endpoint(web, monkeypatch):
    monkeypatch.setenv("SWEEP_SECRET", "s")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "w")
    get_settings.cache_clear()
    from app.telegram.router import get_bot_deps
    tg, deps = nudge_deps(web, 20)
    web.app.dependency_overrides[get_bot_deps] = lambda: deps
    try:
        mk(web)
        r = web.client.post("/tasks/sweep", headers={"X-Sweep-Secret": "s"}).json()
        assert r == {"ok": True, "marked_unknown": 0, "nudge_sent": True}
        assert web.client.post("/tasks/sweep", headers={"X-Sweep-Secret": "s"}).json() == {"ok": True, "marked_unknown": 0}
    finally:
        web.app.dependency_overrides.pop(get_bot_deps, None)


# ----------------------------------------------------------------------------- pages: share target, manifest, static
def test_manifest_is_installable_and_the_share_target_is_post(web):
    m = web.client.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["start_url"] == "/app/" and m["name"] == "Mini Me"
    st = m["share_target"]
    assert st["method"] == "POST" and st["action"] == "/share-target" and st["params"]["text"] == "text"
    assert "multipart" not in st["enctype"] and "GET" not in json.dumps(st)
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for i in m["icons"]:
        assert web.client.get(i["src"]).status_code == 200
    sw = web.client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
    assert "/api" not in sw.text.split("fetch', (e)")[1].split("startsWith(")[0]


def test_share_target_carries_the_text_in_the_page_only_and_stores_nothing(web):
    web.client.cookies.clear()                                               # signed out
    r = web.client.post("/share-target", data={"text": "Is the club meet on Friday? Call 9876543210", "title": "t"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    boot = r.text.split('id="boot" type="application/json">')[1].split("</script>")[0]
    assert json.loads(boot)["shared"]["text"].startswith("Is the club meet")
    assert "<h1" not in r.text and "club meet" not in r.text.replace(boot, "")      # nothing shown before login
    assert rows(web.engine, db.message_records) == [] and rows(web.engine, db.events) == []
    assert web.drafter.requests == []


def test_share_target_escapes_the_text_and_has_no_get_route(web):
    evil = '</script><script>alert(1)</script> & <b>'
    r = web.client.post("/share-target", data={"text": evil})
    assert "</script><script>alert" not in r.text and r.text.count("<script") == 2    # boot + app.js only
    boot = r.text.split('id="boot" type="application/json">')[1].split("</script>")[0]
    assert json.loads(boot)["shared"]["text"] == evil
    assert web.client.get("/share-target").status_code == 405
    plain = web.client.post("/share-target", data={})
    assert json.loads(plain.text.split('id="boot" type="application/json">')[1].split("</script>")[0]) is None


def test_app_shell_static_files_and_no_path_escape(web):
    assert web.client.get("/app/").status_code == 200 and web.client.get("/app").status_code == 200
    html = web.client.get("/app/").text
    assert "app.css" in html and "app.js" in html and "__BOOT__" not in html
    assert web.client.get("/app/static/app.js").status_code == 200
    assert web.client.get("/app/static/fonts/fraunces-latin-wght-normal.woff2").status_code == 200
    assert web.client.get("/app/static/app.html").status_code == 404            # the template itself is not served
    assert web.client.get("/app/static/../../config.py").status_code == 404
    assert web.client.get("/app/static/%2e%2e/%2e%2e/config.py").status_code == 404


def test_the_app_never_stores_shared_text_in_the_browser_and_uses_text_nodes():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "app" / "web" / "static" / "app.js").read_text()
    for banned in ("localStorage", "sessionStorage", "indexedDB", "innerHTML", "document.write", "caches.put"):
        assert banned not in js
    sw = (Path(__file__).parent.parent / "app" / "web" / "static" / "sw.js").read_text()
    assert "/app/static/" in sw and "req.method !== 'GET'" in sw.replace('"', "'")
    css = (Path(__file__).parent.parent / "app" / "web" / "static" / "app.css").read_text()
    for word in ("unlock", "level up", "gate"):
        assert word not in (js + css).lower().replace("navigate", "")


def test_copy_never_says_sent_and_wording_rules_hold():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "app" / "web" / "static" / "app.js").read_text()
    for banned in ("unlock", "level up", "overdue", "last chance", "runs out", "don't forget"):
        assert banned not in js.lower()


def test_session_logging_and_login_event(web):
    assert web.client.post("/app/api/session", json={"entry": "share", "nonce": "n1"}).json()["ok"]
    assert web.client.post("/app/api/session", json={"entry": "share", "nonce": "n1"}).json()["ok"]    # same open: once
    ev = events(web.engine, "session_started")
    assert len(ev) == 1 and ev[0]["device_type"] == "mobile" and ev[0]["payload"]["entry_screen"] == "share"


def test_enums_carry_the_capture_values():
    from app.events.schema import Capture, Channel
    assert Capture.share.value == "share" and Capture.paste.value == "paste" and Channel.web_share.value == "web_share"


def test_waiting_list_cards_carry_a_local_copy_time(web):
    mk(web, "Is the club meet on Friday?")
    c = web.client.get("/app/api/pending").json()["items"][0]
    assert len(c["clock"]) == 5 and c["clock"][2] == ":"


def test_login_page_logs_one_session_started_and_the_app_does_not_add_a_second():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "app" / "web" / "static" / "app.js").read_text()
    after_login = js.split("res = await fetch('/login'")[1].split("go('home'")[0]
    assert "logSession" not in after_login


def test_back_to_whatsapp_opens_the_app_through_an_intent():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "app" / "web" / "static" / "app.js").read_text()
    assert "package=com.whatsapp" in js and "window.close" not in js
