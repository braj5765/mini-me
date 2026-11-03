import pytest
from sqlalchemy import func, select

from app import db
from app.events import EventName, EventWriter
from app.events.writer import make_key


def count(engine, table=db.events):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar_one()


def test_writes_common_fields(writer, engine):
    eid, inserted = writer.log(
        EventName.draft_generated, message_id="m1", channel="telegram", domain="group_queries",
        contact_type="known_group", device_type="mobile", classifier_version="clf-0",
        mode="shadow", payload={"confidence": 0.82},
    )
    assert inserted
    with engine.connect() as c:
        r = c.execute(select(db.events)).mappings().one()
    assert r["event_id"] == eid and r["user_id"] == "braj" and r["app_version"]
    assert (r["channel"], r["contact_type"], r["device_type"], r["mode"]) == (
        "telegram", "known_group", "mobile", "shadow")
    assert r["payload"]["confidence"] == 0.82 and r["classifier_version"] == "clf-0"


def test_once_per_message_is_idempotent(writer, engine):
    a = writer.log(EventName.draft_generated, message_id="m1")
    b = writer.log(EventName.draft_generated, message_id="m1", payload={"x": 1})
    assert a[0] == b[0] and a[1] is True and b[1] is False
    assert count(engine) == 1


def test_repeatable_event_needs_explicit_key(writer, engine):
    with pytest.raises(ValueError):
        writer.log(EventName.draft_shown, message_id="m1")
    k = make_key("draft_shown", "m1", "view-1")
    assert writer.log(EventName.draft_shown, message_id="m1", idempotency_key=k)[1]
    assert not writer.log(EventName.draft_shown, message_id="m1", idempotency_key=k)[1]
    k2 = make_key("draft_shown", "m1", "view-2")
    assert writer.log(EventName.draft_shown, message_id="m1", idempotency_key=k2)[1]
    assert count(engine) == 2


@pytest.mark.parametrize("field,value", [
    ("channel", "whatsapp"), ("device_type", "tablet"), ("contact_type", "stranger"), ("mode", "test"),
])
def test_rejects_bad_enum_values(writer, field, value):
    with pytest.raises(ValueError):
        writer.log(EventName.message_received, message_id="m1", **{field: value})


def test_rejects_unknown_event_and_field(writer):
    with pytest.raises(ValueError):
        writer.log("made_up_event", message_id="m1")
    with pytest.raises(ValueError):
        writer.log(EventName.message_received, message_id="m1", colour="red")


def test_settings_events_exist(writer):
    for name, key in [
        (EventName.topic_added, "t1"), (EventName.topic_removed, "t2"),
        (EventName.topic_triggered, "t3"), (EventName.checkin_time_changed, "c1"),
    ]:
        assert writer.log(name, idempotency_key=make_key(name, key), message_id=None,
                          payload={"topic": "fees"})[1]


def test_incoming_text_in_payload_is_masked(writer, engine):
    writer.log(EventName.message_received, message_id="m9",
               payload={"incoming_text": "call 9876543210 or mail a@b.com"})
    with engine.connect() as c:
        p = c.execute(select(db.events.c.payload)).scalar_one()
    assert p["incoming_text"] == "call [PHONE] or mail [EMAIL]"


def test_log_safe_never_raises(writer):
    assert writer.log_safe("not_an_event") is None


def test_message_record_projection_and_blind_state(writer):
    writer.upsert_message(
        "m1", channel="telegram", domain="group_queries", contact_type="new_individual",
        contact_pseudonym="contact-ab12cd34", draft_text="Yes, 6pm works.", confidence=0.7,
        autonomy_decision="would_have_sent_shadow", shown_state="blind", device_type="mobile",
        classifier_version="clf-0", mode="shadow",
    )
    r = writer.get_message("m1")
    assert r["shown_state"] == "blind" and r["autonomy_decision"] == "would_have_sent_shadow"
    assert r["outcome"] == "waiting" and r["scorable"] is False

    writer.upsert_message("m1", sent_text="Yes, 6 pm works!", outcome="edited_sent",
                          edit_class="cosmetic", time_to_action_ms=4200)
    r = writer.get_message("m1")
    assert r["draft_text"] == "Yes, 6pm works." and r["sent_text"] == "Yes, 6 pm works!"
    assert r["scorable"] is True and r["edit_class"] == "cosmetic"


def test_outcome_unknown_is_never_scorable(writer):
    writer.upsert_message("m2", draft_text="ok")
    writer.mark_outcome_unknown("m2", channel="telegram")
    r = writer.get_message("m2")
    assert r["outcome"] == "unknown" and r["scorable"] is False
    writer.mark_outcome_unknown("m2")  # retry-safe
    for o in ("user_authored", "waiting"):
        writer.upsert_message("m2", outcome=o)
        assert writer.get_message("m2")["scorable"] is False


def test_signal_values_and_reversed_is_not_an_edit_class(writer):
    writer.upsert_message("m3", signal="decision_reversed", edit_class="decision")
    with pytest.raises(ValueError):
        writer.upsert_message("m3", edit_class="decision_reversed")
    with pytest.raises(ValueError):
        writer.upsert_message("m3", signal="sent_as_is")


def test_incoming_text_masked_and_expiry_set(writer):
    writer.upsert_message("m4", incoming_text="reach me 9876543210")
    r = writer.get_message("m4")
    assert r["incoming_text"] == "reach me [PHONE]" and r["incoming_text_expires_at"]


def test_upsert_rejects_unknown_field(writer):
    with pytest.raises(ValueError):
        writer.upsert_message("m5", phone_number="123")
