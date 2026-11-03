"""D4a Telegram bot tests. A fake Telegram client records what the bot would send; time and the blind-sample
dice are injected, so every flow is deterministic."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.config import get_settings
from app.drafting.base import DraftRequest, DraftResult
from app.drafting.stub import StubDrafter
from app.events import EventWriter
from app.telegram import format as fmt
from app.telegram.bot import BotDeps, handle_update, sweep_expired
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.runtime import ensure_webhook

BRAJ = 4242
CHAT = 4242
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
RAW_PHONE = "9876543210"
RAW_EMAIL = "riya.sharma@example.com"


class FakeTelegram:
    def __init__(self):
        self.sent: list[dict] = []        # every message the bot sends
        self.answers: list[tuple] = []
        self.cleared: list[tuple] = []
        self._next = 1000
        self.webhook = ""

    def send_message(self, chat_id, text, *, reply_markup=None, reply_to_message_id=None):
        self._next += 1
        self.sent.append({"id": self._next, "chat_id": chat_id, "text": text, "markup": reply_markup,
                          "reply_to": reply_to_message_id})
        return self._next

    def answer_callback(self, callback_query_id, text=""):
        self.answers.append((callback_query_id, text))

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append((chat_id, message_id))

    def get_webhook_url(self):
        return self.webhook

    def set_webhook(self, url, secret_token):
        self.webhook = url


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class SpyDrafter(StubDrafter):
    def __init__(self):
        self.requests: list[DraftRequest] = []

    def draft(self, req):
        self.requests.append(req)
        return super().draft(req)


@pytest.fixture
def env(engine, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", str(BRAJ))
    monkeypatch.setenv("TELEGRAM_BLIND_RATIO", "0.2")
    get_settings.cache_clear()
    fake, clock, drafter = FakeTelegram(), Clock(), SpyDrafter()
    hook_calls: list[tuple] = []
    deps = BotDeps(settings=get_settings(), engine=engine, writer=EventWriter(engine), client=fake,
                   drafter=drafter, on_captured=lambda mid, how: hook_calls.append((mid, how)),
                   rng=lambda: 0.99, now=clock)  # 0.99 -> never blind unless a test says so
    deps.fake, deps.clock, deps.spy, deps.hooks = fake, clock, drafter, hook_calls
    return deps


_n = [100]


def _msg(text, *, user=BRAJ, chat=CHAT, chat_type="private", forward=None, reply_to=None):
    _n[0] += 1
    m = {"message_id": _n[0], "from": {"id": user}, "chat": {"id": chat, "type": chat_type}, "text": text}
    if forward:
        m["forward_origin"] = forward
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": _n[0], "message": m}


def _cb(data, msg_id, *, user=BRAJ, chat=CHAT):
    _n[0] += 1
    return {"update_id": _n[0], "callback_query": {
        "id": f"cb{_n[0]}", "from": {"id": user}, "data": data,
        "message": {"message_id": msg_id, "chat": {"id": chat, "type": "private"}}}}


FWD_USER = {"type": "user", "sender_user": {"id": 777000111}}
FWD_CHAT = {"type": "chat", "sender_chat": {"id": -1001234567890}}

INCOMING = f"Hi! Is the club meet on Friday? Call me on {RAW_PHONE} or mail {RAW_EMAIL}"


def _record(deps, mid=None):
    with deps.engine.connect() as c:
        rows = c.execute(select(db.message_records)).mappings().all()
    return dict(rows[-1] if mid is None else next(r for r in rows if r["message_id"] == mid))


def _events(deps, name=None):
    with deps.engine.connect() as c:
        rows = c.execute(select(db.events).order_by(db.events.c.recorded_at)).mappings().all()
    return [dict(r) for r in rows if name is None or r["event_name"] == name]


def _forward_and_get_draft(deps, text=INCOMING, forward=FWD_USER):
    handle_update(_msg(text, forward=forward), deps)
    return deps.fake.sent[-1]


# ---------------------------------------------------------------- ingestion -> masking -> events
def test_ingest_masks_pseudonymises_drafts_and_logs(env):
    draft = _forward_and_get_draft(env)

    rec = _record(env)
    assert rec["channel"] == "telegram" and rec["domain"] == "group_queries"
    assert rec["contact_type"] == "new_individual"
    assert rec["contact_pseudonym"].startswith("contact-")
    assert "[PHONE]" in rec["incoming_text"] and "[EMAIL]" in rec["incoming_text"]
    assert rec["incoming_text_expires_at"] is not None
    assert rec["outcome"] == "waiting" and rec["scorable"] is False
    assert rec["shown_state"] == "shown" and rec["autonomy_decision"] == "draft_notify"
    assert rec["mode"] == "shadow" and rec["device_type"] is None
    assert rec["confidence"] == 0.5 and "Stub" in rec["rationale"]

    names = [e["event_name"] for e in _events(env)]
    assert names == ["message_received", "draft_generated", "draft_shown"]
    received = _events(env, "message_received")[0]
    assert received["payload"]["masked_count"] == 2 and received["payload"]["origin"] == "forwarded"
    assert received["channel"] == "telegram" and received["contact_type"] == "new_individual"
    gen = _events(env, "draft_generated")[0]["payload"]
    assert gen["is_stub"] is True and gen["shown_state"] == "shown" and gen["items_used"] == []

    # draft goes back in the DM with the two buttons, labelled as a stub, with confidence + one-line why
    assert "stub" in draft["text"].lower() and "Confidence: Medium" in draft["text"]
    labels = [b["text"] for b in draft["markup"]["inline_keyboard"][0]]
    assert labels == [fmt.BTN_SENT, fmt.BTN_DISCARD]
    assert draft["reply_to"] is not None


def test_raw_text_is_never_stored_anywhere(env):
    _forward_and_get_draft(env)
    handle_update(_cb(f"sent:{_record(env)['message_id']}", env.fake.sent[-1]["id"]), env)
    handle_update(_msg(f"Yes, friday. Ring {RAW_PHONE}", reply_to=None), env)  # a second message (pasted)
    dump = json.dumps([_record(env)] + _events(env), default=str)
    with env.engine.connect() as c:
        dump += json.dumps([dict(r) for r in c.execute(select(db.message_records)).mappings()], default=str)
        dump += json.dumps([dict(r) for r in c.execute(select(db.telegram_threads)).mappings()], default=str)
    assert RAW_PHONE not in dump and RAW_EMAIL not in dump and "777000111" not in dump


def test_drafter_only_ever_sees_masked_text_and_no_llm_is_called(env, monkeypatch):
    import app.llm.base as llm

    def boom(*a, **k):
        raise AssertionError("the stub drafter must not touch any LLM provider")

    monkeypatch.setattr(llm, "get_llm", boom)
    _forward_and_get_draft(env)
    req = env.spy.requests[0]
    assert RAW_PHONE not in req.masked_text and RAW_EMAIL not in req.masked_text
    assert "[PHONE]" in req.masked_text
    assert req.contact_pseudonym.startswith("contact-")


@pytest.mark.parametrize("forward,pasted,expected", [
    (FWD_USER, False, "new_individual"),
    (FWD_CHAT, False, "new_group"),
    (None, True, "new_group"),
])
def test_contact_type_defaults(env, forward, pasted, expected):
    handle_update(_msg("Is the hall booked?", forward=forward), env)
    assert _record(env)["contact_type"] == expected
    assert _events(env, "message_received")[0]["payload"]["origin"] == ("pasted" if pasted else "forwarded")


def test_known_contact_after_a_reply(env):
    handle_update(_msg("First question", forward=FWD_USER), env)
    handle_update(_cb(f"sent:{_record(env)['message_id']}", env.fake.sent[-1]["id"]), env)
    handle_update(_msg("Second question", forward=FWD_USER), env)
    assert _record(env)["contact_type"] == "known_individual"
    # a discard is not a reply: a different person who was only discarded stays new
    other = {"type": "user", "sender_user": {"id": 5}}
    handle_update(_msg("Q", forward=other), env)
    handle_update(_cb(f"disc:{_record(env)['message_id']}", env.fake.sent[-1]["id"]), env)
    handle_update(_msg("Q again", forward=other), env)
    assert _record(env)["contact_type"] == "new_individual"


def test_pasted_messages_never_become_known_contacts(env):
    handle_update(_msg("pasted one"), env)
    handle_update(_cb(f"sent:{_record(env)['message_id']}", env.fake.sent[-1]["id"]), env)
    handle_update(_msg("pasted two"), env)
    assert _record(env)["contact_type"] == "new_group"


def test_redelivered_update_is_handled_once(env):
    upd = _msg("Is the hall booked?", forward=FWD_USER)
    handle_update(upd, env)
    handle_update(upd, env)
    assert len(env.fake.sent) == 1
    assert len(_events(env, "message_received")) == 1


# ---------------------------------------------------------------- capture paths
def test_sent_as_is_button(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    env.clock.advance(seconds=30)
    handle_update(_cb(f"sent:{mid}", draft["id"]), env)

    rec = _record(env, mid)
    assert rec["outcome"] == "sent_as_is" and rec["scorable"] is True
    assert rec["sent_text"] == rec["draft_text"] and rec["time_to_action_ms"] == 30_000
    acted = _events(env, "draft_acted")[0]["payload"]
    assert acted["action"] == "sent" and acted["capture"] == "button"
    sent = _events(env, "draft_sent")[0]["payload"]
    assert sent["edited"] is False and sent["sent_by"] == "user"
    assert (CHAT, draft["id"]) in env.fake.cleared
    assert env.hooks == [(mid, "button")]  # D9a hook point fired once


def test_second_button_press_is_ignored(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_cb(f"sent:{mid}", draft["id"]), env)
    handle_update(_cb(f"sent:{mid}", draft["id"]), env)
    handle_update(_cb(f"disc:{mid}", draft["id"]), env)
    assert len(_events(env, "draft_sent")) == 1 and len(_events(env, "draft_discarded")) == 0
    assert len(env.hooks) == 1
    assert env.fake.answers[-1][1] == fmt.ALREADY_RECORDED


def test_discard_button(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_cb(f"disc:{mid}", draft["id"]), env)
    rec = _record(env, mid)
    assert rec["outcome"] == "discarded" and rec["scorable"] is True and rec["sent_text"] is None
    assert _events(env, "draft_discarded") and _events(env, "draft_acted")[0]["payload"]["action"] == "discarded"
    assert env.hooks == []  # nothing was sent, so nothing to classify


def test_reply_with_edited_text_is_captured_masked(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_msg(f"Yes, Friday 5pm. Call me on {RAW_PHONE}.", reply_to=draft["id"]), env)

    rec = _record(env, mid)
    assert rec["outcome"] == "edited_sent" and rec["scorable"] is True
    assert "Friday 5pm" in rec["sent_text"] and RAW_PHONE not in rec["sent_text"] and "[PHONE]" in rec["sent_text"]
    sent = _events(env, "draft_sent")[0]["payload"]
    assert sent["edited"] is True and sent["capture"] == "reply_text"
    assert _events(env, "draft_acted")[0]["payload"]["action"] == "edited_sent"
    assert (CHAT, draft["id"]) in env.fake.cleared and env.hooks == [(mid, "reply_text")]


def test_reply_identical_to_draft_counts_as_sent_as_is(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    body = _record(env, mid)["draft_text"]
    handle_update(_msg("  " + body.replace(" ", "  ") + " ", reply_to=draft["id"]), env)  # whitespace only
    assert _record(env, mid)["outcome"] == "sent_as_is"


def test_slash_sent_command_captures_for_latest_waiting(env):
    _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_msg("/sent Yes, see you Friday"), env)
    assert _record(env, mid)["sent_text"] == "Yes, see you Friday"
    handle_update(_msg("/sent nothing is waiting now"), env)
    assert env.fake.sent[-1]["text"] == fmt.NOTHING_WAITING


def test_capture_twice_is_recorded_once(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_msg("First answer", reply_to=draft["id"]), env)
    handle_update(_msg("Second answer", reply_to=draft["id"]), env)
    assert _record(env, mid)["sent_text"] == "First answer"
    assert len(_events(env, "draft_sent")) == 1
    assert env.fake.sent[-1]["text"] == fmt.ALREADY_RECORDED


def test_forwarding_own_message_is_a_new_message_not_a_capture(env):
    _forward_and_get_draft(env)
    handle_update(_msg("my own test message", forward={"type": "user", "sender_user": {"id": BRAJ}}), env)
    assert len(_events(env, "message_received")) == 2


# ---------------------------------------------------------------- outcome unknown
def test_no_capture_in_window_becomes_outcome_unknown(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    env.clock.advance(hours=23)
    assert sweep_expired(env) == 0
    env.clock.advance(hours=1, minutes=1)
    assert sweep_expired(env) == 1

    rec = _record(env, mid)
    assert rec["outcome"] == "unknown" and rec["scorable"] is False and rec["sent_text"] is None
    ev = _events(env, "outcome_unknown")
    assert len(ev) == 1 and ev[0]["payload"]["reason"] == "capture_window_expired"
    assert ev[0]["payload"]["window_hours"] == 24.0
    assert (CHAT, draft["id"]) in env.fake.cleared
    assert sweep_expired(env) == 0                     # never twice
    handle_update(_cb(f"sent:{mid}", draft["id"]), env)  # a late button press cannot make it a clean accept
    assert _record(env, mid)["outcome"] == "unknown" and not _events(env, "draft_sent")


def test_sweep_also_runs_on_the_next_incoming_update(env):
    _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    env.clock.advance(hours=30)
    handle_update(_msg("/help"), env)
    assert _record(env, mid)["outcome"] == "unknown"


def test_capture_window_is_configurable(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CAPTURE_WINDOW_HOURS", "2")
    get_settings.cache_clear()
    env.settings = get_settings()
    _forward_and_get_draft(env)
    env.clock.advance(hours=2, minutes=1)
    assert sweep_expired(env) == 1


# ---------------------------------------------------------------- blind sample (5.15 pt 3)
def test_blind_sample_holds_the_draft_until_the_user_replies(env):
    env.rng = lambda: 0.0  # always blind
    handle_update(_msg(INCOMING, forward=FWD_USER), env)
    mid = _record(env)["message_id"]

    notice = env.fake.sent[-1]
    assert notice["text"] == fmt.BLIND_NOTICE == "I've drafted this one; reply first and I'll show you mine."
    assert notice["markup"] is None                      # no "Sent as is" for a blind sample
    held = _record(env, mid)["draft_text"]
    assert held and not any(held in s["text"] for s in env.fake.sent)  # draft not leaked
    assert _record(env, mid)["shown_state"] == "blind"
    names = [e["event_name"] for e in _events(env)]
    assert names == ["message_received", "draft_generated", "draft_held_blind"]  # no draft_shown
    assert _events(env, "draft_generated")[0]["payload"]["shown_state"] == "blind"

    env.clock.advance(minutes=5)
    handle_update(_msg("Yes, Friday works.", reply_to=notice["id"]), env)

    rec = _record(env, mid)
    assert rec["sent_text"] == "Yes, Friday works." and rec["outcome"] == "edited_sent" and rec["scorable"]
    assert rec["time_to_action_ms"] == 300_000
    released = env.fake.sent[-1]["text"]
    assert held in released and "Different from what you sent" in released
    assert _events(env, "draft_released_after_reply")[0]["payload"]["matched"] is False
    assert [e["event_name"] for e in _events(env)][-3:] == ["draft_acted", "draft_sent", "draft_released_after_reply"]
    assert env.hooks == [(mid, "reply_text")]
    assert not any(s["markup"] for s in env.fake.sent)    # no buttons anywhere in the blind flow


def test_blind_reply_matching_the_held_draft(env):
    env.rng = lambda: 0.0
    handle_update(_msg("Is the hall booked?", forward=FWD_USER), env)
    mid = _record(env)["message_id"]
    handle_update(_msg(_record(env, mid)["draft_text"], reply_to=env.fake.sent[-1]["id"]), env)
    assert _record(env, mid)["outcome"] == "sent_as_is"
    assert _events(env, "draft_released_after_reply")[0]["payload"]["matched"] is True


def test_blind_with_no_reply_becomes_unknown_and_the_draft_is_never_shown(env):
    env.rng = lambda: 0.0
    handle_update(_msg("Is the hall booked?", forward=FWD_USER), env)
    mid = _record(env)["message_id"]
    held = _record(env, mid)["draft_text"]
    env.clock.advance(hours=25)
    assert sweep_expired(env) == 1
    assert _record(env, mid)["outcome"] == "unknown"
    assert not _events(env, "draft_released_after_reply")
    assert not any(held in s["text"] for s in env.fake.sent)


def test_callback_on_a_blind_message_is_refused(env):
    env.rng = lambda: 0.0
    handle_update(_msg("Is the hall booked?", forward=FWD_USER), env)
    mid = _record(env)["message_id"]
    handle_update(_cb(f"sent:{mid}", env.fake.sent[-1]["id"]), env)
    assert _record(env, mid)["outcome"] == "waiting" and not _events(env, "draft_sent")


def test_blind_ratio_is_a_config_value(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BLIND_RATIO", "0.5")
    get_settings.cache_clear()
    env.settings = get_settings()
    env.rng = lambda: 0.49
    handle_update(_msg("one", forward=FWD_USER), env)
    assert _record(env)["shown_state"] == "blind"
    env.rng = lambda: 0.51
    handle_update(_msg("two", forward=FWD_USER), env)
    assert _record(env)["shown_state"] == "shown"
    assert env.settings.telegram_blind_ratio == 0.5


# ---------------------------------------------------------------- authorisation
def test_other_telegram_users_are_rejected_silently(env):
    handle_update(_msg(INCOMING, user=999, chat=999, forward=FWD_USER), env)
    handle_update(_msg("/start", user=999, chat=999), env)
    handle_update(_cb("sent:tg-abc", 5, user=999, chat=999), env)
    assert env.fake.sent == [] and env.fake.answers == []
    assert _events(env) == []
    with env.engine.connect() as c:
        assert c.execute(select(db.message_records)).first() is None


def test_group_chats_are_rejected_even_from_braj(env):
    handle_update(_msg(INCOMING, chat=-100555, chat_type="supergroup"), env)
    assert env.fake.sent == [] and _events(env) == []


def test_no_allowed_user_configured_fails_closed(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "")
    get_settings.cache_clear()
    env.settings = get_settings()
    handle_update(_msg("/start"), env)
    assert env.fake.sent == [] and _events(env) == []


def test_another_user_cannot_capture_for_braj(env):
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_cb(f"sent:{mid}", draft["id"], user=999, chat=999), env)
    assert _record(env, mid)["outcome"] == "waiting"


# ---------------------------------------------------------------- commands + hooks
def test_commands(env):
    handle_update(_msg("/start"), env)
    assert "Nothing is ever sent for you" in env.fake.sent[-1]["text"]
    handle_update(_msg("/pending"), env)
    assert env.fake.sent[-1]["text"] == fmt.NOTHING_WAITING
    _forward_and_get_draft(env)
    handle_update(_msg("/pending"), env)
    assert env.fake.sent[-1]["text"] == "1 waiting for you."
    handle_update(_msg("/stats"), env)
    assert env.fake.sent[-1]["text"] == fmt.STATS_STUB


def test_stats_provider_hook_is_swappable(env):
    env.stats_provider = lambda d: "8 sent as is"
    handle_update(_msg("/stats"), env)
    assert env.fake.sent[-1]["text"] == "8 sent as is"


def test_classifier_hook_failure_never_blocks_recording(env):
    def broken(mid, how):
        raise RuntimeError("classifier down")

    env.on_captured = broken
    draft = _forward_and_get_draft(env)
    mid = _record(env)["message_id"]
    handle_update(_cb(f"sent:{mid}", draft["id"]), env)
    assert _record(env, mid)["outcome"] == "sent_as_is"


def test_non_text_message_gets_a_calm_nudge(env):
    upd = _msg("x")
    del upd["message"]["text"]
    handle_update(upd, env)
    assert env.fake.sent[-1]["text"] == fmt.NO_TEXT and _events(env) == []


def test_drafts_sent_to_the_dm_are_never_the_users_message_sent_out(env):
    """Telegram is draft-only: the bot only ever messages Braj's own chat."""
    _forward_and_get_draft(env)
    assert {s["chat_id"] for s in env.fake.sent} == {CHAT}


# ---------------------------------------------------------------- wording, token safety, webhook
def test_no_gate_wording_in_bot_copy():
    blob = " ".join([fmt.HELP, fmt.STATS_STUB, fmt.BLIND_NOTICE, fmt.NO_TEXT, fmt.NOTHING_WAITING,
                     fmt.ALREADY_RECORDED, fmt.BTN_SENT, fmt.BTN_DISCARD,
                     fmt.draft_message(StubDrafter().draft(DraftRequest("m", "hi", "telegram", "d", "x", "p"))),
                     fmt.released_message("d", True), fmt.released_message("d", False)]).lower()
    for banned in ("unlock", "gate", "level up"):
        assert banned not in blob


def test_telegram_errors_never_contain_the_token(monkeypatch):
    c = TelegramClient("123456:SECRET-TOKEN", "http://127.0.0.1:1")  # nothing listens here
    with pytest.raises(TelegramError) as ei:
        c.send_message(1, "hi")
    assert "SECRET-TOKEN" not in str(ei.value) and "127.0.0.1" not in str(ei.value)
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_ensure_webhook_is_idempotent(env):
    env.settings.telegram_public_base_url = "https://mini-me.fly.dev/"
    env.settings.telegram_webhook_secret = "s"
    assert ensure_webhook(env.settings, env.fake) is True
    assert env.fake.webhook == "https://mini-me.fly.dev/telegram/webhook"
    assert ensure_webhook(env.settings, env.fake) is False


@pytest.fixture
def api(tmp_path, monkeypatch, env):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    from app.main import app
    from app.telegram.router import get_bot_deps
    deps = env
    deps.settings = get_settings()
    deps.engine = db.get_engine()
    deps.writer = EventWriter(deps.engine)
    deps.store.engine = deps.engine
    app.dependency_overrides[get_bot_deps] = lambda: deps
    with TestClient(app) as c:
        c.deps = deps
        yield c
    app.dependency_overrides.clear()
    db.get_engine.cache_clear()


def test_webhook_needs_the_secret(api):
    body = _msg("/help")
    assert api.post("/telegram/webhook", json=body).status_code == 403
    assert api.post("/telegram/webhook", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "no"}).status_code == 403
    assert api.deps.fake.sent == []
    r = api.post("/telegram/webhook", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
    assert r.status_code == 200 and "Mini Me" in api.deps.fake.sent[-1]["text"]


def test_webhook_end_to_end_and_sweep_endpoint(api):
    h = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}
    api.post("/telegram/webhook", json=_msg(INCOMING, forward=FWD_USER), headers=h)
    assert api.deps.fake.sent[-1]["markup"] is not None
    api.deps.clock.advance(hours=25)
    assert api.post("/telegram/sweep", headers={"X-Telegram-Bot-Api-Secret-Token": "x"}).status_code == 403
    assert api.post("/telegram/sweep", headers=h).json() == {"ok": True, "marked_unknown": 1}


def test_webhook_answers_200_even_if_handling_blows_up(api, monkeypatch):
    import app.telegram.router as r
    monkeypatch.setattr(r, "handle_update", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    h = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}
    assert api.post("/telegram/webhook", json=_msg("hi"), headers=h).status_code == 200


def test_bot_is_off_until_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/off.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/healthz").json()["ok"] is True      # the rest of the app still works
        assert c.post("/telegram/webhook", json={}).status_code == 503
        assert c.post("/telegram/sweep").status_code == 503
    db.get_engine.cache_clear()
