"""Dev 6: share-to-draft backend (POST /api/draft, tokens, draft_copied, ask-first, sweep, /pending capture)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db
from app.config import get_settings
from app.drafting.base import DraftError, DraftRequest, DraftResult
from app.drafting.llm_drafter import LLMDrafter
from app.events import EventWriter
from app.events.schema import Capture, Channel, ContactType, EventName, Origin, Outcome
from app.events.writer import make_key
from app.llm import base as llm_base
from app.llm.base import GeminiClient
from app.privacy.masking import mask_text, mask_with_values
from app.results.stats import stats_text
from app.selfmodel.store import SelfModelStore
from app.share import limits, service, tokens
from app.share.restore import restore_values
from app.share.router import get_share_deps
from app.telegram import format as fmt
from app.telegram.bot import BotDeps, handle_update, sweep_expired
from app.telegram.store import ThreadStore
from test_telegram import BRAJ, CHAT, Clock, FakeTelegram, _cb, _msg

PHONE = "9876543210"
EMAIL = "riya.sharma@example.com"
MSG = "Is the club meet on Friday at 5?"


class FakeDrafter:
    def __init__(self, text="yes, see you at 5", confidence=0.8, rationale="Short yes, like your quick replies."):
        self.text, self.confidence, self.rationale = text, confidence, rationale
        self.requests: list[DraftRequest] = []
        self.fail: Exception | None = None

    def draft(self, req):
        self.requests.append(req)
        if self.fail:
            raise self.fail
        return DraftResult(
            text=self.text, confidence=self.confidence, rationale=self.rationale, items_used=[], drafter="llm",
            drafter_version="drafter-v0.2", is_stub=False,
            meta={"data_mode": "own_or_made_up", "prompt_version": "drafter-v0.2", "provider": "fake",
                  "model": "fake-1", "llm_ms": 5, "self_model_version": "sm-test"})


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/share.db")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", str(BRAJ))
    for n in ("SHARE_ALWAYS_ASK_TOPICS", "SHARE_CONFIDENCE_FLOOR", "SHARE_DEFAULT_DOMAIN", "SHARE_MAX_CHARS"):
        monkeypatch.delenv(n, raising=False)
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    from app.main import app
    drafter = FakeDrafter()
    with TestClient(app) as client:
        engine = db.get_engine()
        _, token = tokens.create_token(engine, "test phone")
        ctx = type("Ctx", (), {})()
        ctx.client, ctx.engine, ctx.drafter, ctx.token = client, engine, drafter, token
        ctx.writer = EventWriter(engine)
        ctx.auth = {"Authorization": f"Bearer {token}"}
        ctx.use = lambda: app.dependency_overrides.__setitem__(
            get_share_deps, lambda: (engine, ctx.writer, get_settings(), ctx.drafter))
        ctx.use()
        yield ctx
    app.dependency_overrides.pop(get_share_deps, None)
    db.get_engine.cache_clear()


def post(api, text=MSG, *, fmt_="text", headers=None, **kw):
    return api.client.post(f"/api/draft?format={fmt_}", content=text.encode(),
                           headers={"Content-Type": "text/plain; charset=utf-8", **(api.auth if headers is None else headers)}, **kw)


def rows(engine, table):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(select(table)).mappings().all()]


def events(engine, name=None):
    ev = rows(engine, db.events)
    return [e for e in ev if name is None or e["event_name"] == name]


def record(engine):
    return rows(engine, db.message_records)[-1]


def everything_stored(engine) -> str:
    """Every row of every table that holds message data, as one string (to prove what is NOT in it)."""
    return json.dumps({t: rows(engine, tbl) for t, tbl in
                       (("records", db.message_records), ("events", db.events), ("threads", db.telegram_threads))},
                      default=str)


# ------------------------------------------------------------------------------------------- tokens
def test_only_a_hash_is_stored_and_the_token_works_once_created(api):
    tid, tok = tokens.create_token(api.engine, "second")
    stored = json.dumps(rows(api.engine, db.api_tokens), default=str)
    assert tok not in stored and tokens.hash_token(tok) in stored
    assert tok.startswith("mm_") and len(tok) > 40
    assert tokens.verify_token(api.engine, tok)["token_id"] == tid
    assert tokens.verify_token(api.engine, tok + "x") is None
    assert tokens.verify_token(api.engine, "") is None and tokens.verify_token(api.engine, None) is None
    assert all("token_hash" not in t for t in tokens.list_tokens(api.engine))
    assert tokens.list_tokens(api.engine)[-1]["last_used_at"] is not None


def test_revoked_token_stops_working_and_revoke_all(api):
    tid, tok = tokens.create_token(api.engine, "lost phone")
    assert tokens.revoke_token(api.engine, tid) is True
    assert tokens.revoke_token(api.engine, tid) is False          # already revoked
    assert tokens.verify_token(api.engine, tok) is None
    assert post(api, headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    assert post(api).status_code == 200                           # the other token still works
    assert tokens.revoke_all(api.engine) >= 1
    assert post(api).status_code == 401


def test_a_name_is_required():
    with pytest.raises(ValueError):
        tokens.create_token(db.get_engine(), "  ")


def test_cli_create_list_revoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/cli.db")
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    from scripts import api_token
    assert api_token.main(["create", "my phone"]) == 0
    out = capsys.readouterr().out
    token = next(l for l in out.splitlines() if l.startswith("mm_"))
    tid = next(t["token_id"] for t in tokens.list_tokens(db.get_engine()))
    assert api_token.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert tid in listing and "active" in listing and token not in listing      # the secret is never listed
    assert api_token.main(["revoke", tid]) == 0
    assert api_token.main(["revoke", tid]) == 1                                  # already revoked
    assert tokens.verify_token(db.get_engine(), token) is None
    assert api_token.main(["revoke"]) == 2
    db.get_engine.cache_clear()


# ------------------------------------------------------------------------------------------- auth: 401, nothing stored
@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer mm_wrong"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"},
    {"Authorization": "mm_nope"}])
def test_wrong_or_missing_token_gets_401_and_stores_nothing(api, headers):
    secret = f"SECRET-CLUB-TEXT call {PHONE}"
    r = post(api, secret, headers=headers)
    assert r.status_code == 401
    assert rows(api.engine, db.message_records) == [] and events(api.engine) == []
    assert rows(api.engine, db.telegram_threads) == []
    assert "SECRET-CLUB-TEXT" not in everything_stored(api.engine) and PHONE not in everything_stored(api.engine)
    assert api.drafter.requests == []                                      # the drafter was never reached
    assert "SECRET" not in r.text


def test_401_even_for_a_broken_body_and_the_copied_call_needs_a_token(api):
    r = api.client.post("/api/draft", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    assert api.client.post("/api/draft/sh-abc/copied").status_code == 401


# ------------------------------------------------------------------------------------------- the draft path
def test_plain_text_response_is_just_the_draft_and_the_record_is_right(api):
    r = post(api)
    assert r.status_code == 200 and r.text == "yes, see you at 5"
    assert r.headers["content-type"].startswith("text/plain")
    mid = r.headers["x-mini-me-message-id"]
    assert mid.startswith("sh-") and r.headers["x-mini-me-action"] == "draft"
    assert r.headers["x-mini-me-confidence"] == "High"

    rec = record(api.engine)
    assert rec["message_id"] == mid and rec["channel"] == "shortcut" and rec["domain"] == "club"
    assert rec["contact_type"] == "unknown" and rec["device_type"] == "mobile" and rec["mode"] == "shadow"
    assert rec["autonomy_decision"] == "draft_notify" and rec["shown_state"] == "shown"
    assert rec["outcome"] == "waiting" and rec["scorable"] is False
    assert rec["incoming_text"] is None and rec["draft_text"] == "yes, see you at 5"   # test-mode guard (5.23 pt 3)

    names = [e["event_name"] for e in events(api.engine)]
    assert names == ["message_received", "draft_generated", "autonomy_decision", "draft_shown"]
    received = events(api.engine, "message_received")[0]
    assert received["payload"]["origin"] == Origin.shared.value and received["channel"] == "shortcut"
    assert received["device_type"] == "mobile" and received["contact_type"] == "unknown"


def test_untagged_sender_gets_a_draft_and_is_never_autonomy_eligible(api):
    post(api)
    dec = events(api.engine, "autonomy_decision")[0]["payload"]
    assert dec["action"] == "draft_notify" and dec["autonomy_eligible"] is False and dec["rerun_of"] is None
    assert record(api.engine)["autonomy_decision"] != "would_have_sent_shadow"
    assert "would_have_sent_shadow" not in everything_stored(api.engine)
    assert events(api.engine, "hard_limit_triggered") == []                 # no contact-based limit on draft-only channels


def test_shortcut_drafts_are_never_blind_and_are_tagged(api):
    for _ in range(25):
        assert post(api).status_code == 200
    assert events(api.engine, "draft_held_blind") == [] and events(api.engine, "draft_released_after_reply") == []
    assert {r["shown_state"] for r in rows(api.engine, db.message_records)} == {"shown"}
    for e in events(api.engine, "draft_generated"):
        assert e["payload"]["blind_eligible"] is False and e["payload"]["entry"] == "shortcut"


def test_masked_before_storage_and_before_the_model(api):
    text = f"Please call {PHONE} or mail {EMAIL} about Friday"
    assert post(api, text).status_code == 200
    seen = api.drafter.requests[0]
    assert PHONE not in seen.masked_text and EMAIL not in seen.masked_text
    assert "[PHONE]" in seen.masked_text and "[EMAIL]" in seen.masked_text
    stored = everything_stored(api.engine)
    assert PHONE not in stored and EMAIL not in stored
    assert events(api.engine, "message_received")[0]["payload"]["masked_count"] == 2


def test_real_values_come_back_in_the_response_only(api):
    api.drafter.text = "Sure, ring [PHONE] and I will confirm"
    r = post(api, f"Call me on {PHONE}")
    assert r.status_code == 200 and PHONE in r.text and "[PHONE]" not in r.text
    assert record(api.engine)["draft_text"] == "Sure, ring [PHONE] and I will confirm"   # stored draft stays masked
    assert PHONE not in everything_stored(api.engine)
    j = post(api, f"Call me on {PHONE}", fmt_="json").json()
    assert PHONE in j["draft"] and PHONE not in j["masked_text"] and j["unrestored_tokens"] == []


def test_a_token_standing_for_two_different_values_is_not_restored(api):
    api.drafter.text = "Ring [PHONE]"
    j = post(api, "Numbers: 9876543210 and 9123456780", fmt_="json").json()
    assert j["draft"] == "Ring [PHONE]" and j["unrestored_tokens"] == ["[PHONE]"]


def test_restore_unit():
    masked, found = mask_with_values(f"a {PHONE} b {EMAIL} c {PHONE}")
    assert masked == mask_text(f"a {PHONE} b {EMAIL} c {PHONE}") and len(found) == 3
    assert restore_values("[EMAIL] / [PHONE]", found) == (f"{EMAIL} / {PHONE}", [])
    assert restore_values("nothing here", found) == ("nothing here", [])
    assert mask_with_values("") == ("", [])


def test_json_response_has_what_dev7_needs(api):
    j = post(api, fmt_="json").json()
    assert set(j) >= {"message_id", "action", "draft", "confidence_label", "confidence", "why", "masked_text",
                      "masked_count", "contact_type", "domain", "channel", "shown_state", "copied_path",
                      "confirm_within_hours", "unrestored_tokens"}
    assert j["action"] == "draft" and j["confidence_label"] == "High" and j["why"].startswith("Short yes")
    assert j["channel"] == "shortcut" and j["contact_type"] == "unknown" and j["domain"] == "club"
    assert j["copied_path"] == f"/api/draft/{j['message_id']}/copied" and j["confirm_within_hours"] == 24.0


def test_accept_header_chooses_text_and_json_is_the_default(api):
    r = api.client.post("/api/draft", content=MSG.encode(),
                        headers={**api.auth, "Content-Type": "text/plain", "Accept": "text/plain"})
    assert r.text == "yes, see you at 5"
    r = api.client.post("/api/draft", content=MSG.encode(), headers={**api.auth, "Content-Type": "text/plain"})
    assert r.json()["action"] == "draft"


def test_json_and_form_bodies_and_url_and_title(api):
    r = api.client.post("/api/draft?format=json", headers=api.auth,
                        json={"text": "Meet at the hall?", "url": "https://example.org/x", "title": "ignored"})
    assert r.status_code == 200
    seen = api.drafter.requests[-1].masked_text
    assert seen == "Meet at the hall?\nhttps://example.org/x"
    r = api.client.post("/api/draft?format=json", headers=api.auth, data={"text": "From a form", "channel": "shortcut"})
    assert r.status_code == 200 and api.drafter.requests[-1].masked_text == "From a form"
    r = api.client.post("/api/draft?format=json", headers=api.auth, json={"title": "Only a title"})
    assert api.drafter.requests[-1].masked_text == "Only a title"


def test_compose_text_rules():
    assert service.compose_text("hi", None, "https://a.b") == "hi\nhttps://a.b"
    assert service.compose_text("see https://a.b", None, "https://a.b") == "see https://a.b"
    assert service.compose_text("", "Title", None) == "Title" and service.compose_text("  ", None, None) == ""


def test_empty_too_long_and_reserved_channel(api, monkeypatch):
    assert post(api, "   ").status_code == 422
    assert post(api, "x" * 4001).status_code == 413
    assert post(api, "x" * 70000).status_code == 413
    r = api.client.post("/api/draft?channel=web_share", content=b"hi", headers={**api.auth, "Content-Type": "text/plain"})
    assert r.status_code == 422 and "reserved" in r.json()["detail"]
    r = api.client.post("/api/draft?channel=bogus", content=b"hi", headers={**api.auth, "Content-Type": "text/plain"})
    assert r.status_code == 422
    assert api.client.post("/api/draft", content=b"{oops", headers={**api.auth, "Content-Type": "application/json"}
                           ).status_code == 400
    assert rows(api.engine, db.message_records) == []            # none of those created an item


def test_the_text_is_not_taken_from_the_url(api):
    r = api.client.post("/api/draft?text=Is+the+meet+on+Friday", headers={**api.auth, "Content-Type": "text/plain"})
    assert r.status_code == 422


# ------------------------------------------------------------------------------------------- ask first
@pytest.mark.parametrize("text,code", [
    ("Please pay ₹500 for the hall by tonight", "money"),
    ("what is the UPI id for the registration?", "money"),
    ("bhai paisa kab bhejna hai", "money"),
    ("I am feeling really sad and anxious today", "emotional"),
])
def test_content_hard_limits_mean_ask_first_with_no_draft(api, text, code):
    r = post(api, text)
    assert r.status_code == 409 and r.headers["x-mini-me-action"] == "ask_first"
    assert r.text.startswith("Asked you first:")
    assert api.drafter.requests == []                                   # never reached the drafter or a model
    rec = record(api.engine)
    assert rec["draft_text"] is None and rec["autonomy_decision"] == "ask_first" and rec["hard_limit"] == code
    names = [e["event_name"] for e in events(api.engine)]
    assert names == ["message_received", "hard_limit_triggered", "autonomy_decision"]
    assert events(api.engine, "hard_limit_triggered")[0]["payload"]["limit"] == code
    j = post(api, text, fmt_="json")
    assert j.status_code == 200 and j.json()["action"] == "ask_first" and j.json()["draft"] is None
    assert j.json()["reason"]["code"] == code and j.json()["reason"]["headline"].startswith("Asked you first:")


def test_your_own_topic_names_itself_in_the_reason(api, monkeypatch):
    monkeypatch.setenv("SHARE_ALWAYS_ASK_TOPICS", "fees, attendance waiver")
    get_settings.cache_clear()
    j = post(api, "Is the Attendance Waiver form out?", fmt_="json").json()
    assert j["action"] == "ask_first" and j["reason"]["code"] == "custom_topic" and j["reason"]["topic"] == "attendance waiver"
    assert "matches your topic 'attendance waiver'" in j["reason"]["headline"]
    assert len(events(api.engine, "topic_triggered")) == 1 and record(api.engine)["topic_triggered"] == "attendance waiver"
    assert post(api, "Is the meet on Friday?", fmt_="json").json()["action"] == "draft"


def test_ordinary_group_queries_are_not_blocked(api):
    for t in ["Is the club meet on Friday at 5?", "Can you share the volunteers list?", "Hey, are you coming to the fest?",
              "Venue for the workshop?", "Who is handling registrations?"]:
        assert limits.check_text(t, get_settings()) is None, t


def test_confidence_floor_is_wired_but_off_until_set(api, monkeypatch):
    api.drafter.confidence = 0.3
    assert post(api, fmt_="json").json()["action"] == "draft"              # floor unset: a low-confidence draft is still shown
    monkeypatch.setenv("SHARE_CONFIDENCE_FLOOR", "0.5")
    get_settings.cache_clear()
    j = post(api, fmt_="json").json()
    assert j["action"] == "ask_first" and j["reason"]["code"] == "low_confidence" and j["draft"] is None
    rec = record(api.engine)
    assert rec["draft_text"] is None and rec["autonomy_decision"] == "ask_first" and rec["hard_limit"] == "low_confidence"


def test_an_ask_first_item_has_nothing_to_copy(api):
    mid = post(api, "Please pay ₹500", fmt_="json").json()["message_id"]
    r = api.client.post(f"/api/draft/{mid}/copied", headers=api.auth)
    assert r.status_code == 409 and events(api.engine, "draft_copied") == []


# ------------------------------------------------------------------------------------------- guard + failure
def test_real_guard_refuses_unverified_provider_and_nothing_is_sent(api, monkeypatch):
    monkeypatch.delenv("OWN_DATA_ONLY_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    get_settings.cache_clear()

    def boom(*a, **k):
        raise AssertionError("nothing may be sent to the provider")

    monkeypatch.setattr(llm_base.httpx, "post", boom)
    api.drafter = LLMDrafter(GeminiClient("some-model"), SelfModelStore(api.engine), settings=get_settings())
    r = post(api)
    assert r.status_code == 502 and "not approved" in r.text
    rec = record(api.engine)
    assert rec["outcome"] == "unknown" and rec["scorable"] is False and rec["draft_text"] is None
    unk = events(api.engine, "outcome_unknown")[0]["payload"]
    assert unk["reason"] == "draft_failed" and unk["draft_error"] == "provider_not_verified"
    assert rows(api.engine, db.telegram_threads) == []                     # nothing waits


def test_own_data_mode_declares_the_text_own_and_the_guard_is_unchanged(api, monkeypatch):
    monkeypatch.setenv("OWN_DATA_ONLY_MODE", "true")
    get_settings.cache_clear()
    seen = {}

    class Llm:
        provider, model = "fake", "fake-1"

        def complete(self, messages, *, max_tokens, temperature, contains_third_party_text=False):
            seen["third_party"] = contains_third_party_text
            seen["prompt"] = " ".join(m.content for m in messages)
            return json.dumps({"draft": "yes", "confidence": 0.7, "rationale": "ok", "used": []})

    api.drafter = LLMDrafter(Llm(), SelfModelStore(api.engine), settings=get_settings())
    assert post(api, f"call {PHONE}").status_code == 200
    assert seen["third_party"] is False and PHONE not in seen["prompt"] and "[PHONE]" in seen["prompt"]
    assert all(not p.no_training_verified for p in llm_base.PROVIDERS.values())


def test_a_drafter_crash_is_a_clean_failure(api):
    api.drafter.fail = RuntimeError("boom")
    r = post(api)
    assert r.status_code == 502 and "couldn't draft" in r.text
    assert events(api.engine, "outcome_unknown")[0]["payload"]["draft_error"] == "error"
    api.drafter.fail = DraftError("rate_limited", "429")
    j = post(api, fmt_="json")
    assert j.status_code == 502 and j.json()["action"] == "failed" and j.json()["error"] == "rate_limited"


# ------------------------------------------------------------------------------------------- draft_copied
def test_copy_is_logged_only_by_the_second_call_and_is_never_sent(api):
    r = post(api)
    mid = r.headers["x-mini-me-message-id"]
    assert events(api.engine, "draft_copied") == []                          # the draft call alone logs no copy

    c = api.client.post(f"/api/draft/{mid}/copied", headers=api.auth)
    assert c.status_code == 200 and c.json()["copy_no"] == 1 and c.json()["outcome"] == "waiting"
    e = events(api.engine, "draft_copied")
    assert len(e) == 1 and e[0]["channel"] == "shortcut" and e[0]["device_type"] == "mobile"
    assert e[0]["payload"]["shown_state"] == "shown" and e[0]["payload"]["copy_no"] == 1

    rec = record(api.engine)
    assert rec["outcome"] == "waiting" and rec["scorable"] is False and rec["sent_text"] is None
    assert events(api.engine, "draft_sent") == [] and events(api.engine, "draft_acted") == []

    assert api.client.post(f"/api/draft/{mid}/copied", headers=api.auth).json()["copy_no"] == 2   # copy again
    assert len(events(api.engine, "draft_copied")) == 2
    a = api.client.post(f"/api/draft/{mid}/copied?copy_no=7", headers=api.auth).json()
    b = api.client.post(f"/api/draft/{mid}/copied?copy_no=7", headers=api.auth).json()               # a retry
    assert a["logged"] is True and b["logged"] is False and len(events(api.engine, "draft_copied")) == 3


def test_copied_for_an_unknown_or_telegram_item_is_404(api):
    assert api.client.post("/api/draft/sh-nope/copied", headers=api.auth).status_code == 404
    EventWriter(api.engine).upsert_message("tg-1", channel="telegram", draft_text="x")
    assert api.client.post("/api/draft/tg-1/copied", headers=api.auth).status_code == 404


# ------------------------------------------------------------------------------------------- 24 h sweep
def _bot(api, clock):
    fake = FakeTelegram()
    return BotDeps(settings=get_settings(), engine=api.engine, writer=api.writer, client=fake,
                   drafter=api.drafter, on_captured=lambda m, h: None, rng=lambda: 0.99, now=clock), fake


def test_no_capture_in_24h_becomes_outcome_unknown_and_is_never_scored(api):
    clock = Clock()
    mid = post(api, fmt_="json").json()["message_id"]
    ask = post(api, "Please pay ₹500", fmt_="json").json()["message_id"]
    deps, _ = _bot(api, clock)
    clock.t = datetime.now(timezone.utc) + timedelta(hours=23)
    assert sweep_expired(deps) == 0
    clock.t = datetime.now(timezone.utc) + timedelta(hours=25)
    assert sweep_expired(deps) == 2
    for m in (mid, ask):
        rec = api.writer.get_message(m)
        assert rec["outcome"] == "unknown" and rec["scorable"] is False
    unk = [e for e in events(api.engine, "outcome_unknown") if e["message_id"] == mid][0]
    assert unk["payload"]["reason"] == "capture_window_expired" and unk["payload"]["window_hours"] == 24.0
    assert unk["channel"] == "shortcut" and unk["device_type"] == "mobile" and unk["contact_type"] == "unknown"
    text = stats_text(api.engine)
    assert "outcome unknown 1" in text and "asked you first, no draft 1" in text


def test_the_sweep_survives_a_share_with_no_telegram_chat(api, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    get_settings.cache_clear()
    mid = post(api, fmt_="json").json()["message_id"]
    assert ThreadStore(api.engine).get(mid)["chat_id"] == "share"
    clock = Clock()
    clock.t = datetime.now(timezone.utc) + timedelta(hours=30)
    deps, _ = _bot(api, clock)
    assert sweep_expired(deps) == 1 and api.writer.get_message(mid)["outcome"] == "unknown"


# ------------------------------------------------------------------------------------------- /pending capture
def test_pending_shows_a_card_with_the_usual_buttons_and_sent_as_is_works(api):
    mid = post(api, fmt_="json").json()["message_id"]
    deps, fake = _bot(api, Clock())
    handle_update(_msg("/pending"), deps)
    assert "shared from your phone" in fake.sent[0]["text"]
    card = fake.sent[1]
    assert "yes, see you at 5" in card["text"] and "Draft #1" in card["text"]
    labels = [b["text"] for b in card["markup"]["inline_keyboard"][0]]
    assert labels == [fmt.BTN_SENT, fmt.BTN_DISCARD, fmt.BTN_NOT_REPLYING]

    handle_update(_msg("/pending"), deps)                                   # a second look does not resend the card
    assert len(fake.sent) == 3 and "shared from your phone" not in fake.sent[2]["text"]

    handle_update(_cb(f"sent:{mid}", card["id"], text=card["text"]), deps)
    rec = api.writer.get_message(mid)
    assert rec["outcome"] == "sent_as_is" and rec["scorable"] is True and rec["sent_text"] == "yes, see you at 5"
    acted = [e for e in events(api.engine, "draft_acted") if e["message_id"] == mid][0]
    assert acted["channel"] == "shortcut" and acted["domain"] == "club" and acted["device_type"] == "mobile"
    assert acted["payload"]["capture"] == Capture.button.value


def test_pending_not_replying_is_excluded_from_scores(api):
    mid = post(api, fmt_="json").json()["message_id"]
    deps, fake = _bot(api, Clock())
    handle_update(_msg("/pending"), deps)
    card = fake.sent[1]
    handle_update(_cb(f"nrep:{mid}", card["id"], text=card["text"]), deps)
    rec = api.writer.get_message(mid)
    assert rec["outcome"] == "not_replying" and rec["scorable"] is False
    nr = events(api.engine, "not_replying")
    assert len(nr) == 1 and nr[0]["channel"] == "shortcut"
    assert events(api.engine, "draft_sent") == [] and events(api.engine, "draft_discarded") == []
    assert "not replying 1" in stats_text(api.engine)
    handle_update(_cb(f"nrep:{mid}", card["id"], text=card["text"]), deps)   # a second tap is a no-op
    assert len(events(api.engine, "not_replying")) == 1


def test_sent_hash_n_captures_an_edit_and_plain_sent_never_touches_a_shared_draft(api):
    mid = post(api, fmt_="json").json()["message_id"]
    deps, fake = _bot(api, Clock())
    handle_update(_msg("/sent Friday works"), deps)                         # plain /sent: only Telegram-native items
    assert fake.sent[-1]["text"] == fmt.NOTHING_WAITING and api.writer.get_message(mid)["outcome"] == "waiting"
    handle_update(_msg("/sent #9 hi"), deps)
    assert "no open draft #9" in fake.sent[-1]["text"]
    handle_update(_msg("/sent #1 Yes, Friday at 5 works"), deps)
    rec = api.writer.get_message(mid)
    assert rec["outcome"] == "edited_sent" and rec["sent_text"] == "Yes, Friday at 5 works"
    assert fake.sent[-1]["text"] == fmt.saved(1, False)


def test_an_ask_first_item_is_listed_without_a_card_and_cannot_be_confirmed(api):
    mid = post(api, "Please pay ₹500", fmt_="json").json()["message_id"]
    deps, fake = _bot(api, Clock())
    handle_update(_msg("/pending"), deps)
    assert "I asked you first, no draft" in fake.sent[0]["text"] and len(fake.sent) == 1
    handle_update(_msg("/sent #1 ok"), deps)
    assert fake.sent[-1]["text"] == fmt.SHARED_NO_DRAFT and api.writer.get_message(mid)["outcome"] == "waiting"


def test_a_plain_paste_in_telegram_does_not_nudge_about_a_shared_draft(api):
    post(api)
    deps, fake = _bot(api, Clock())
    handle_update(_msg("hello can you come on friday"), deps)
    assert not any("I treated that as a new message" in m["text"] for m in fake.sent)


# ------------------------------------------------------------------------------------------- stats: anchoring
def test_shortcut_drafts_are_left_out_of_the_shown_vs_blind_comparison(api):
    mid = post(api, fmt_="json").json()["message_id"]
    deps, fake = _bot(api, Clock())
    handle_update(_msg("/pending"), deps)
    handle_update(_cb(f"sent:{mid}", fake.sent[1]["id"], text=fake.sent[1]["text"]), deps)
    text = stats_text(api.engine)
    assert "Scored: 1 (shown 0 · blind 0 · shared from phone, never blind 1)" in text
    assert "Shown vs blind: shown 0 scored, 0 as is · blind 0 scored, 0 as is" in text
    assert "Sent as is: 1" in text


# ------------------------------------------------------------------------------------------- enums
def test_new_enum_values_exist_and_the_writer_accepts_them(api):
    assert {Channel.shortcut.value, Channel.web_share.value} == {"shortcut", "web_share"}
    assert ContactType.unknown.value == "unknown" and Outcome.not_replying.value == "not_replying"
    assert {Capture.share.value, Capture.paste.value} <= {c.value for c in Capture}
    assert {o.value for o in Origin} == {"forwarded", "pasted", "shared"}
    w = api.writer
    w.upsert_message("web-1", channel="web_share", contact_type="unknown", outcome="not_replying")
    assert w.get_message("web-1")["scorable"] is False
    _, ok = w.log(EventName.nudge_sent, idempotency_key=make_key("nudge_sent", "2026-11-10"),
                  payload={"pending": 3, "channel": "telegram"}, channel="telegram")
    assert ok
    w.log(EventName.draft_copied, message_id="web-1", idempotency_key=make_key("draft_copied", "web-1", 1),
          channel="web_share", device_type="mobile")


def test_web_share_goes_through_the_same_service(api):
    out = service.create_share_draft(api.engine, api.writer, get_settings(), api.drafter, text=MSG,
                                     channel="web_share")
    assert out.action == "draft" and out.channel == "web_share"
    assert record(api.engine)["channel"] == "web_share"
    with pytest.raises(service.ShareError):
        service.create_share_draft(api.engine, api.writer, get_settings(), api.drafter, text=MSG, channel="email")
