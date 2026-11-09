"""D11a results view tests: /stats arithmetic, exclusions, data-mode labelling, and the read-only /events table."""
import pytest

from app import db
from app.classifier import rules
from app.config import get_settings
from app.events import EventName, EventWriter
from app.results.event_view import events_text
from app.results.stats import compute_stats, format_stats, make_stats_provider
from app.telegram.bot import BotDeps, handle_update
from app.results.event_view import make_events_provider

from test_classifier import force_outcome, seed
from test_telegram import BRAJ, FakeTelegram, Clock, _msg


def mark(engine, mid, **cols):
    """Set record columns the writer would not let a test set (scorable, autonomy_decision ...)."""
    with engine.begin() as c:
        c.execute(db.message_records.update().where(db.message_records.c.message_id == mid).values(**cols))


@pytest.fixture
def world(engine, writer):
    """A known set of rows. Expected numbers are worked out by hand in the tests below."""
    n = [0]

    def add(draft="Haan", sent="Haan", **kw):
        n[0] += 1
        mid = f"m{n[0]}"
        seed(writer, mid, draft, sent, **kw)
        return mid

    for _ in range(3):
        add(outcome="sent_as_is", edit_class="none")                       # as is, shown
    add(outcome="sent_as_is", edit_class="none", shown="blind")            # as is, blind
    add("Haan", "Nahi", edit_class="decision")                             # shown
    add("Haan", "Haan [phone 2]", edit_class="fact", shown="blind")        # blind
    add("Haan", "Haan bhai", edit_class="voice")
    add("Haan", "Haan bhai pakka", edit_class="voice")
    add("Haan", "Haan!", edit_class="cosmetic")
    add("Haan", None, outcome="discarded")
    u1 = add("Haan", None, outcome="waiting"); mark(engine, u1, outcome="unknown", scorable=False)
    u2 = add("Haan", None, outcome="waiting"); mark(engine, u2, outcome="unknown", scorable=False)
    add("Haan", None, outcome="waiting")                                   # waiting, shown
    add("Haan", None, outcome="waiting", shown="blind")                    # waiting, blind (draft held)
    add("Haan", "my own words", outcome="user_authored")
    add("Haan", "Haan bhai", )                                             # edited, not classified yet
    a = add(outcome="sent_as_is", edit_class="none"); mark(engine, a, autonomy_decision="ask_first")
    nr = add("Haan", None, outcome="waiting"); force_outcome(engine, nr, "not_replying")
    add(outcome="sent_as_is", stub=True)                                   # stub rows: never counted
    add("Haan", "Nahi", stub=True, edit_class="decision")
    add(outcome="sent_as_is", data_mode="third_party_guarded", edit_class="none")
    add("Haan", None, outcome="discarded", data_mode="third_party_guarded")
    add(outcome="sent_as_is", data_mode=None, edit_class="none")           # older row, no data_mode
    writer.upsert_message("failed", channel="telegram", outcome="unknown")  # draft failed: no draft event
    writer.log(EventName.outcome_unknown, message_id="failed", payload={"reason": "draft_failed"})
    writer.log(EventName.outcome_unknown, message_id=u1, payload={"reason": "capture_window_expired"})
    writer.log(EventName.outcome_unknown, message_id=u2, payload={"reason": "capture_window_expired"})
    return engine


def test_counts_for_own_or_made_up_rows(world):
    st = compute_stats(world)
    b = st.blocks["own_or_made_up"]
    assert b.as_is == 4 and b.edited == {"decision": 1, "fact": 1, "voice": 2, "cosmetic": 1}
    assert b.edited_total == 5 and b.discarded == 1 and b.scored == 10        # 4 + 5 + 1
    assert (b.shown.scored, b.blind.scored) == (8, 2)                         # shown 3+1+2+1+1, blind 1+1
    assert (b.shown.as_is, b.blind.as_is) == (3, 1)
    assert b.unclassified == 1                                                # captured, not read yet: not counted
    assert b.unknown == 2 and b.unknown_reasons == {"capture_window_expired": 2}
    assert b.waiting == 2 and b.waiting_blind == 1
    assert b.not_replying == 1 and b.user_authored == 1 and b.ask_first == 1


def test_never_light_and_undecided_split(world):
    b = compute_stats(world).blocks["own_or_made_up"]
    assert b.never_light == 2 and b.undecided == 3                           # decision+fact / voice+cosmetic


def test_north_star_is_a_range_and_excludes_unscored_rows(world):
    b = compute_stats(world).blocks["own_or_made_up"]
    assert b.north_star() == (4, 7, 10)                                       # low: as is only; high: + voice/cosmetic
    txt = format_stats(compute_stats(world))
    assert "40% to 70%" in txt and "(4 to 7 of 10)" in txt and "not set yet (Q1a)" in txt


def test_stub_and_missing_draft_rows_are_left_out_and_counted_once(world):
    st = compute_stats(world)
    assert st.stub_excluded == 2 and st.no_draft == 1
    scored_everywhere = sum(b.scored for b in st.blocks.values())
    assert scored_everywhere == 10 + 2 + 1                                    # own + guarded(2) + unlabelled(1)
    assert "Left out of everything above: stub drafts 2 · no draft made 1" in format_stats(st)


def test_rows_are_grouped_by_data_mode_and_labelled(world):
    st = compute_stats(world)
    assert set(st.blocks) == {"own_or_made_up", "third_party_guarded", "unlabelled"}
    txt = format_stats(st)
    assert "OWN / MADE-UP DATA - a demo of the loop, not results" in txt
    assert "THIRD-PARTY DATA (guarded path)" in txt and "OLDER ROWS WITH NO DATA MODE" in txt
    assert txt.index("OWN / MADE-UP") < txt.index("OLDER ROWS") < txt.index("THIRD-PARTY")
    assert "no accuracy is claimed" in txt
    g = st.blocks["third_party_guarded"]
    assert (g.as_is, g.discarded, g.scored) == (1, 1, 2)                      # kept apart from the own-data numbers


def test_text_labels_light_and_heavy_as_not_set_yet_and_shows_the_mix(world):
    txt = format_stats(compute_stats(world))
    assert "• Light edits: not set yet" in txt and "• Heavy edits: not set yet" in txt
    assert "Edited in total: 5 - decision 1 · fact 1 · voice 2 · cosmetic 1" in txt
    assert "decision and fact are never light: 2" in txt and "voice and cosmetic could be light or heavy: 3" in txt
    assert "Scored: 10 (shown 8 · blind 2)" in txt and "• Sent as is: 4" in txt and "• Discarded: 1" in txt
    assert "Edited but not classified yet: 1 (not counted)" in txt
    assert "Shown vs blind: shown 8 scored, 3 as is · blind 2 scored, 1 as is" in txt
    assert "outcome unknown 2 (window expired 2)" in txt and "not replying 1" in txt
    assert "waiting 2 (1 blind, draft held)" in txt and "ask-first / your own reply 2" in txt


def test_no_data_and_nothing_scored_yet(engine):
    assert "No real drafts yet" in format_stats(compute_stats(engine))


def test_nothing_scored_gives_no_percentage(engine, writer):
    seed(writer, "m1", "Haan", None, outcome="waiting")
    txt = format_stats(compute_stats(engine))
    assert "not enough scored messages yet" in txt and "%" not in txt


def test_discards_stay_in_the_denominator(engine, writer):
    seed(writer, "a", "Haan", "Haan", outcome="sent_as_is", edit_class="none")
    seed(writer, "b", "Haan", None, outcome="discarded")
    assert compute_stats(engine).blocks["own_or_made_up"].north_star() == (1, 1, 2)


def test_outcome_unknown_is_never_an_accept(engine, writer):
    seed(writer, "a", "Haan", None, outcome="waiting")
    mark(engine, "a", outcome="unknown", scorable=False)
    b = compute_stats(engine).blocks["own_or_made_up"]
    assert b.scored == 0 and b.as_is == 0 and b.unknown == 1


def test_stats_stay_short_enough_for_one_telegram_message(world):
    assert len(format_stats(compute_stats(world))) < 3500


def test_no_gate_wording_or_accuracy_claims_in_stats(world):
    txt = format_stats(compute_stats(world)).lower()
    for banned in ("unlock", "gate", "level up", "accuracy of", "accurate"):
        assert banned not in txt


def test_boundary_is_still_a_placeholder():
    assert rules.BOUNDARY_SET is False


# ------------------------------------------------------------------------------------------ in the bot
@pytest.fixture
def bot(world):
    monkey = get_settings()
    monkey.telegram_allowed_user_id = str(BRAJ)
    fake = FakeTelegram()
    deps = BotDeps(settings=monkey, engine=world, writer=EventWriter(world), client=fake, drafter=None,
                   stats_provider=make_stats_provider(), events_provider=make_events_provider(),
                   rng=lambda: 0.99, now=Clock())
    deps.fake = fake
    return deps


def test_stats_command_returns_the_real_view(bot):
    handle_update(_msg("/stats"), bot)
    out = bot.fake.sent[-1]["text"]
    assert out.startswith("Mini Me stats") and "Scored: 10 (shown 8 · blind 2)" in out


def test_events_command_latest_and_help_mentions_it(bot):
    handle_update(_msg("/events"), bot)
    out = bot.fake.sent[-1]["text"]
    assert out.startswith("Latest steps") and "outcome_unknown" in out
    handle_update(_msg("/help"), bot)
    assert "/events" in bot.fake.sent[-1]["text"]


def test_events_by_number_shows_every_step_without_message_text(engine, writer):
    seed(writer, "m1", "Haan aa jaunga", "Nahi, nahi ho payega", items=[{"layer": "voice", "item_id": "s1"}])
    writer.log(EventName.draft_acted, message_id="m1", idempotency_key="k-acted-m1", payload={"action": "edited_sent", "capture": "reply_text"})
    writer.log(EventName.draft_sent, message_id="m1",
               payload={"sent_text": "SECRET-SENT-WORDS", "edited": True, "capture": "reply_text"})
    writer.log(EventName.edit_classified, message_id="m1", classifier_version="clf-0.1",
               payload={"edit_class": "decision", "classes": ["decision", "voice"], "method": "llm",
                        "never_light": True, "model_reason": "SECRET-REASON"})
    out = events_text(engine, {"m1": 1}, {1: "m1"}.get, "1")
    assert out.startswith("Draft #1 - every step (UTC):")
    assert "draft_generated" in out and "draft_acted" in out and "edit_classified" in out
    assert "class=decision all=decision+voice method=llm never light" in out
    assert "SECRET-SENT-WORDS" not in out and "SECRET-REASON" not in out and "can you come friday" not in out


def test_events_bad_argument_and_unknown_number(engine):
    assert "no draft #9" in events_text(engine, {}, {}.get, "9")
    assert "Use /events" in events_text(engine, {}, {}.get, "abc")
    assert events_text(engine, {}, {}.get, "") == "No events yet."


def test_m1_loop_script_runs_offline_and_its_checks_pass():
    """Smoke test for scripts/run_m1_loop.py: the whole loop on the made-up messages with the scripted model."""
    import subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "scripts.run_m1_loop"], cwd=root, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    assert "FAIL" not in r.stdout and "OFFLINE scripted model" in r.stdout
    (root / "m1_loop_run.db").unlink(missing_ok=True)
