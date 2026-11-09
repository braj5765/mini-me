"""Run the whole M1 loop on the made-up messages: forward -> draft -> capture -> classified -> event log -> /stats.

    python -m scripts.run_m1_loop                         # offline: scripted model, checks the plumbing only
    python -m scripts.run_m1_loop --live --pause 20       # real drafter + real classifier (LLM_PROVIDER / LLM_MODEL, .env)
    python -m scripts.run_m1_loop --live --out m1_run.md --pause 20

It drives the real bot code (`handle_update`) with a recording stand-in for Telegram and a throw-away SQLite file
(`m1_loop_run.db`, deleted at the start). Nothing touches Neon, Fly or Telegram. Made-up messages only.
`--live` needs a provider key and sets OWN_DATA_ONLY_MODE=true for this run, as v0.1 allows (decision 5.18).

What the "user" does, per message number n (a fixed plan, so runs can be compared):
  n % 10 in 1,2,3 -> taps "Sent as is"      4 -> sends the draft with a cosmetic change (greeting / emoji / punctuation)
  5,6,7,8 -> replies with the made-up final text (seed/made_up_replies.txt)
  9 -> taps Discard       0 -> does nothing (outcome unknown after the window)
  Blind drafts (the bot's own 1-in-5 dice, seed 14) have no buttons, so the user replies first with the made-up text.
Results are QUALITY NOTES about the loop, not accuracy numbers.
"""
import argparse
import json
import os
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER = 4242


class Recorder:
    """Stands in for the Telegram API: remembers what the bot would send."""
    def __init__(self):
        self.sent, self._id = [], 1000

    def send_message(self, chat_id, text, *, reply_markup=None, reply_to_message_id=None):
        self._id += 1
        self.sent.append({"id": self._id, "text": text, "markup": reply_markup})
        return self._id

    def answer_callback(self, *a, **k): pass
    def clear_buttons(self, *a, **k): pass
    def edit_message_text(self, *a, **k): pass
    def get_webhook_url(self): return ""
    def set_webhook(self, *a, **k): pass


def _offline_model():
    """Scripted stand-in for the model (plumbing check only; it says nothing about real quality)."""
    from app.llm.base import _Base

    def stance(t):
        t = t.lower()
        if any(w in t for w in ("nahi", "nahin", "can't", "sorry", "cannot")):
            return "no"
        if any(w in t for w in ("check", "dekh", "bata deta", "batata", "puch", "try")):
            return "defer"
        return "yes"

    class Offline(_Base):
        provider = "gemini"   # not no-training-verified: the real guard still applies

        def _call(self, messages, max_tokens, temperature):
            system, user = messages[0].content, messages[-1].content
            if "You draft replies" in system:
                inc = re.search(r"<incoming_message>\s*(.*?)\s*</incoming_message>", user, re.S)
                text = inc.group(1) if inc else ""
                if "thank" in text.lower():
                    d = "arre koi baat nahi 🙂"
                elif text.endswith("?"):
                    d = "check karke bata deta hu"
                else:
                    d = "ok, dekh leta hu"
                return json.dumps({"draft": d, "confidence": 0.5, "rationale": "Short, non-committal.", "used": []})
            dr = re.search(r"<draft>\s*(.*?)\s*</draft>", user, re.S).group(1)
            se = re.search(r"<sent>\s*(.*?)\s*</sent>", user, re.S).group(1)
            classes = []
            if stance(dr) != stance(se):
                classes.append("decision")
            if set(re.findall(r"\d+", dr)) != set(re.findall(r"\d+", se)):
                classes.append("fact")
            if not classes:
                classes.append("voice")
            return json.dumps({"classes": classes, "reason": "offline heuristic"})

    return Offline("offline-script")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="use the real model (LLM_PROVIDER / LLM_MODEL)")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between messages (Groq free tier: 20)")
    ap.add_argument("--out", help="write the report (markdown) to this file")
    a = ap.parse_args(argv)

    db_path = ROOT / "m1_loop_run.db"
    db_path.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path}"          # never Neon
    os.environ["TELEGRAM_ALLOWED_USER_ID"] = str(USER)
    os.environ["TELEGRAM_BLIND_RATIO"] = "0.2"
    os.environ["TELEGRAM_CAPTURE_WINDOW_HOURS"] = "24"
    os.environ["OWN_DATA_ONLY_MODE"] = "true"          # made-up text only; v0.1 allows this (decision 5.18)

    from sqlalchemy import create_engine, select
    from app import db
    from app.classifier.classifier import EditClassifier
    from app.config import get_settings
    from app.drafting.llm_drafter import LLMDrafter
    from app.events import EventWriter
    from app.llm.base import get_llm
    from app.results.event_view import make_events_provider
    from app.results.stats import make_stats_provider
    from app.selfmodel.seed import parse_own_lines
    from app.selfmodel.store import SRC_EXEMPLAR, SelfModelStore
    from app.telegram import bot as tgbot

    s = get_settings()
    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    db.metadata.create_all(engine)
    llm = get_llm() if a.live else _offline_model()
    store = SelfModelStore(engine)
    for line in parse_own_lines((ROOT / "seed/own_lines.example.txt").read_text(encoding="utf-8-sig"))[:10]:
        store.add_item("voice", line, SRC_EXEMPLAR, "own_lines.example.txt")   # made-up example lines only

    writer, fake = EventWriter(engine), Recorder()
    clock = {"t": datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)}
    dice = random.Random(14)
    deps = tgbot.BotDeps(
        settings=s, engine=engine, writer=writer, client=fake,
        drafter=LLMDrafter(llm, store, settings=s),
        on_captured=EditClassifier(engine, writer, settings=s, llm=llm),
        stats_provider=make_stats_provider(), events_provider=make_events_provider(),
        rng=dice.random, now=lambda: clock["t"])

    msgs = parse_own_lines((ROOT / "seed/made_up_messages.txt").read_text(encoding="utf-8-sig"))
    replies = parse_own_lines((ROOT / "seed/made_up_replies.txt").read_text(encoding="utf-8-sig"))
    assert len(msgs) == len(replies), "messages and replies must line up"

    counter = [500]

    def nxt():
        counter[0] += 1
        return counter[0]

    def send(text, reply_to=None):
        m = {"message_id": nxt(), "from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "text": text}
        if reply_to:
            m["reply_to_message"] = {"message_id": reply_to}
        tgbot.handle_update({"update_id": nxt(), "message": m}, deps)

    def tap(data, prompt_id, text):
        tgbot.handle_update({"update_id": nxt(), "callback_query": {
            "id": f"cb{nxt()}", "from": {"id": USER}, "data": data,
            "message": {"message_id": prompt_id, "chat": {"id": USER, "type": "private"}, "text": text}}}, deps)

    import time
    rows = []
    for n, (msg, final) in enumerate(zip(msgs, replies), 1):
        if n > 1 and a.pause:
            time.sleep(a.pause)
        before = len(fake.sent)
        send(msg)
        new = fake.sent[before:]
        prompt = next((x for x in new if x["markup"]), None)
        blind = prompt is None and any("reply first" in x["text"] for x in new)
        notice = next((x for x in new if "reply first" in x["text"]), None)
        plan = n % 10
        if blind:
            action = "reply (blind)"
            send(final, reply_to=notice["id"])
        elif prompt is None:
            action = "draft failed"
        elif plan in (1, 2, 3):
            action = "sent as is"
            tap("sent:" + _mid(engine, n), prompt["id"], prompt["text"])
        elif plan == 4:
            action = "cosmetic tweak"
            send(_cosmetic(_draft_of(engine, n)), reply_to=prompt["id"])
        elif plan in (5, 6, 7, 8):
            action = "reply"
            send(final, reply_to=prompt["id"])
        elif plan == 9:
            action = "discard"
            tap("disc:" + _mid(engine, n), prompt["id"], prompt["text"])
        else:
            action = "no action"
        rows.append((n, action))
        clock["t"] += timedelta(minutes=3)

    clock["t"] += timedelta(hours=25)                                      # past the capture window
    tgbot.sweep_expired(deps)

    m = db.message_records
    with engine.connect() as c:
        recs = list(c.execute(select(m).order_by(m.c.created_at)).mappings())
    act = dict(rows)
    out = ["# M1 loop run (made-up messages, " + ("LIVE model" if a.live else "OFFLINE scripted model - plumbing only") + ")", "",
           "| # | shown | action | outcome | draft | sent | class | note |", "|---|---|---|---|---|---|---|---|"]
    esc = lambda x: str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")
    for i, r in enumerate(recs, 1):
        out.append("| " + " | ".join(esc(x) for x in (
            i, r["shown_state"], act.get(i, ""), r["outcome"], r["draft_text"], r["sent_text"], r["edit_class"] or "-",
            "failed draft" if r["draft_text"] is None else "")) + " |")

    out += ["", "## /stats", "```", make_stats_provider()(deps), "```", "", "## /events 3", "```",
            make_events_provider()(deps, "3", USER), "```"]
    checks = _checks(engine, recs)
    out += ["", "## Plumbing checks"] + [f"- {'OK' if ok else 'FAIL'}: {t}" for t, ok in checks]
    text = "\n".join(out)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8-sig")
    print(text)
    return 0 if all(ok for _, ok in checks) else 1


def _mid(engine, n):
    from sqlalchemy import select
    from app import db
    m = db.message_records
    with engine.connect() as c:
        return c.execute(select(m.c.message_id).order_by(m.c.created_at)).all()[n - 1][0]


def _draft_of(engine, n):
    from sqlalchemy import select
    from app import db
    m = db.message_records
    with engine.connect() as c:
        return c.execute(select(m.c.draft_text).order_by(m.c.created_at)).all()[n - 1][0]


def _cosmetic(draft: str) -> str:
    return "Hi, " + draft.rstrip(".!") + "! 🙂"


def _checks(engine, recs):
    from sqlalchemy import func, select
    from app import db
    e = db.events
    with engine.connect() as c:
        classified = {r[0]: r[1] for r in c.execute(select(e.c.message_id, func.count()).where(
            e.c.event_name == "edit_classified").group_by(e.c.message_id))}
        names = {r[0] for r in c.execute(select(e.c.event_name).distinct())}
    captured = [r for r in recs if r["outcome"] in ("sent_as_is", "edited_sent")]
    return [
        ("every captured message was classified exactly once",
         all(classified.get(r["message_id"]) == 1 and r["edit_class"] for r in captured)),
        ("discarded and unknown messages were not classified",
         all(r["edit_class"] is None for r in recs if r["outcome"] in ("discarded", "unknown"))),
        ("sent-as-is messages have edit_class none", all(r["edit_class"] == "none" for r in recs if r["outcome"] == "sent_as_is")),
        ("classifier_version is stamped on every classified record",
         all(r["classifier_version"] == "clf-0.1" for r in recs if r["edit_class"])),
        ("event log has the loop's events", {"message_received", "draft_generated", "draft_sent", "edit_classified"} <= names),
        ("unknown outcomes are never scorable", all(not r["scorable"] for r in recs if r["outcome"] == "unknown")),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
