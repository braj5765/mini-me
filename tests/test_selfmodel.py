"""D6a self-model store and seeding tests."""
import json

import pytest
from sqlalchemy import inspect

from app.selfmodel.seed import parse_own_lines, seed_self_model
from app.selfmodel.store import SRC_EXEMPLAR, SRC_EXTRACTED, SRC_MANUAL, Layer, SelfModelError, SelfModelStore

OWN = [
    "yes bro i'll be there at 5, bring the banner",
    "can't make it this friday, exams. send me the minutes later?",
    "Hi Riya, the auditorium is booked for the 14th. Will confirm the time by tonight.",
    "haha sure, count me in",
    "not sure about the budget yet, let me check with the treasurer and get back",
    "registrations close on sunday night, please share the form in the group",
]


class FakeLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, messages, *, max_tokens=600, temperature=0.4, contains_third_party_text=False):
        self.calls.append({"messages": messages, "third_party": contains_third_party_text})
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


NOTES = {"voice": ["Writes short, casual replies in lowercase."],
         "decisions": ["Says yes to quick favours and gives a time."],
         "facts": ["Is on the club committee."]}


@pytest.fixture
def store(engine):
    return SelfModelStore(engine)


# ---------------------------------------------------------------- schema
def test_table_has_the_columns_d6b_builds_on(engine):
    cols = {c["name"] for c in inspect(engine).get_columns("self_model_items")}
    assert {"item_id", "layer", "text", "source", "source_ref", "status", "version",
            "created_at", "updated_at"} <= cols


# ---------------------------------------------------------------- CRUD
def test_add_and_read_item(store):
    it = store.add_item("voice", "Short and casual.", SRC_MANUAL)
    got = store.get(it["item_id"])
    assert got["item_id"].startswith("sm_") and got["layer"] == "voice" and got["source"] == SRC_MANUAL
    assert got["status"] == "active" and got["version"] == 1
    assert got["created_at"] is not None and got["updated_at"] is not None


def test_list_by_layer_and_count(store):
    store.add_item(Layer.voice, "v", SRC_MANUAL)
    store.add_item(Layer.decision, "d", SRC_MANUAL)
    store.add_item(Layer.fact, "f", SRC_MANUAL)
    assert [i["text"] for i in store.list_items("decision")] == ["d"]
    assert store.count() == 3 and store.count("fact") == 1


def test_update_bumps_version_and_time(store):
    it = store.add_item("fact", "Is treasurer.", SRC_MANUAL)
    new = store.update_text(it["item_id"], "Is secretary.")
    assert new["text"] == "Is secretary." and new["version"] == 2 and new["updated_at"] is not None


def test_archive_hides_but_keeps_the_row(store):
    it = store.add_item("fact", "x fact", SRC_MANUAL)
    store.archive(it["item_id"])
    assert store.list_items() == []
    assert [i["item_id"] for i in store.list_items(include_archived=True)] == [it["item_id"]]
    with pytest.raises(SelfModelError):
        store.update_text(it["item_id"], "changed")


@pytest.mark.parametrize("layer,text", [("mood", "x"), ("voice", "  "), ("voice", "a" * 401)])
def test_bad_items_are_refused(store, layer, text):
    with pytest.raises(SelfModelError):
        store.add_item(layer, text, SRC_MANUAL)


def test_every_item_needs_a_source(store):
    with pytest.raises(SelfModelError):
        store.add_item("voice", "x", "")


def test_stored_text_is_masked(store):
    it = store.add_item("fact", "Call me on 9876543210 or a.b@example.com", SRC_MANUAL)
    assert "9876543210" not in it["text"] and "example.com" not in it["text"]
    up = store.update_text(it["item_id"], "New number 9123456789")
    assert "9123456789" not in up["text"]


def test_version_tag_changes_when_an_item_changes(store):
    a = store.add_item("voice", "one", SRC_MANUAL)
    t1 = store.version_tag()
    store.update_text(a["item_id"], "two")
    assert store.version_tag() != t1 and t1.startswith("sm-0.1-")


# ---------------------------------------------------------------- retrieval
def test_small_store_is_sent_whole(store):
    store.add_item("decision", "Declines anything on exam days.", SRC_MANUAL)
    store.add_item("fact", "Is on the club committee.", SRC_MANUAL)
    r = store.retrieve("Is the poster ready?")
    assert len(r["decision"]) == 1 and len(r["fact"]) == 1


def test_big_layer_is_trimmed_by_overlap(store):
    for i in range(14):
        store.add_item("fact", f"Fact number {i} about topic{i}", SRC_MANUAL)
    store.add_item("fact", "Treasurer of the photography club", SRC_MANUAL)
    r = store.retrieve("Who is the photography club treasurer?", max_other=3)
    assert r["fact"] and r["fact"][0]["text"].startswith("Treasurer")
    assert len(r["fact"]) <= 3 and all(i["score"] > 0 for i in r["fact"])


def test_voice_notes_come_before_examples(store):
    store.add_item("voice", "an example line", SRC_EXEMPLAR)
    store.add_item("voice", "a style note", SRC_EXTRACTED)
    assert [i["source"] for i in store.retrieve("hello")["voice"]] == [SRC_EXTRACTED, SRC_EXEMPLAR]


# ---------------------------------------------------------------- seeding
def test_parse_own_lines():
    text = "# comment\n\nfirst message\ncontinues here\n\nsecond one\n"
    assert parse_own_lines(text) == ["first message continues here", "second one"]


def test_seed_stores_examples_and_model_notes_with_sources(store):
    llm = FakeLLM(NOTES)
    res = seed_self_model(store, OWN, llm=llm, source_ref="own.txt")
    assert res["voice_examples"] == 6 and res["voice"] == 1 and res["decision"] == 1 and res["fact"] == 1
    items = store.list_items()
    assert {i["source"] for i in items} == {SRC_EXEMPLAR, SRC_EXTRACTED}
    assert all(i["source_ref"] == "own.txt" for i in items)
    assert [i["layer"] for i in items if i["source"] == SRC_EXTRACTED].count("decision") == 1


def test_seed_only_ever_sends_own_text_and_says_so(store):
    llm = FakeLLM(NOTES)
    seed_self_model(store, OWN, llm=llm)
    assert llm.calls[0]["third_party"] is False
    prompt = " ".join(m.content for m in llm.calls[0]["messages"])
    assert "Braj" in prompt and "banner" in prompt


def test_seed_refuses_a_chat_export(store):
    export = [
        "12/03/2026, 9:41 pm - Riya: are you coming?",
        "12/03/2026, 9:42 pm - Braj: yes",
        "12/03/2026, 9:43 pm - Riya: great",
        "12/03/2026, 9:44 pm - Aman: me too",
        "12/03/2026, 9:45 pm - Riya: ok",
        "12/03/2026, 9:46 pm - Braj: cool",
    ]
    with pytest.raises(SelfModelError, match="chat export"):
        seed_self_model(store, export, llm=None)
    assert store.count() == 0


def test_seed_needs_enough_lines(store):
    with pytest.raises(SelfModelError):
        seed_self_model(store, OWN[:2])


def test_seed_masks_before_storing_and_before_the_model(store):
    lines = OWN[:5] + ["call me on 9876543210 after class"]
    llm = FakeLLM(NOTES)
    seed_self_model(store, lines, llm=llm)
    assert all("9876543210" not in i["text"] for i in store.list_items())
    assert "9876543210" not in " ".join(m.content for m in llm.calls[0]["messages"])


def test_seed_without_model_stores_examples_only(store):
    res = seed_self_model(store, OWN, llm=None)
    assert res["voice_examples"] == 6 and store.count("decision") == 0 and res["seeder_version"] is None


def test_reseeding_skips_duplicates_and_replace_archives_old(store):
    seed_self_model(store, OWN, llm=FakeLLM(NOTES))
    n = store.count()
    again = seed_self_model(store, OWN, llm=FakeLLM(NOTES))
    assert store.count() == n and again["skipped_duplicates"] >= 6
    swapped = seed_self_model(store, OWN, llm=FakeLLM(NOTES), replace=True)
    assert swapped["archived_earlier_seed_items"] == n and store.count() == n


def test_manual_items_survive_a_replace(store):
    store.add_item("fact", "Typed by hand", SRC_MANUAL)
    seed_self_model(store, OWN, llm=None)
    seed_self_model(store, OWN, llm=None, replace=True)
    assert any(i["source"] == SRC_MANUAL for i in store.list_items())


def test_bad_model_json_stops_seeding_cleanly(store):
    with pytest.raises(SelfModelError):
        seed_self_model(store, OWN, llm=FakeLLM("sorry, no"))


def test_overlong_or_wrong_typed_notes_are_skipped(store):
    llm = FakeLLM({"voice": ["ok", "x" * 500, 5], "decisions": "not a list", "facts": []})
    res = seed_self_model(store, OWN, llm=llm)
    assert res["voice"] == 1


def test_cli_add_edit_archive(engine, monkeypatch):
    from scripts import seed_self_model as cli

    monkeypatch.setattr(cli.db, "get_engine", lambda: engine)

    assert cli.main(["--add", "decision", "Ask which date before agreeing to a call."]) == 0
    store = SelfModelStore(engine)
    item = store.list_items("decision")[0]
    assert item["source"] == "manual"
    assert cli.main(["--edit", item["item_id"], "Ask which date first. Call me on 9876543210"]) == 0
    assert "[PHONE]" in store.get(item["item_id"])["text"]  # masked like every other item
    assert cli.main(["--archive", item["item_id"]]) == 0
    assert store.list_items("decision") == []
    assert cli.main(["--archive", "sm_nope"]) == 1
