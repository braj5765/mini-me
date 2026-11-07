"""Telegram private assistant bot (D4a, v0.1 part). Pure logic: takes Telegram update dicts, talks to a
TelegramAPI, EventWriter and Drafter. No web framework in here, so it is easy to test.

What it does (decisions 5.7a, 5.13 pts 2-3, 5.14, 5.15 pt 3):
  1. Private bot for one user. Anyone else is ignored (fail closed if no allowed id is set).
  2. Forwarded / pasted message -> mask -> pseudonymise sender -> message record -> `message_received`.
  3. Drafter (stub for now) -> draft in the DM with confidence + one-line rationale -> `draft_generated`,
     `draft_shown`.  About 1 in N (config) drafts are BLIND: held until the user has replied first.
  4. Capture: "Sent as is" button, or a reply carrying the final text (or /sent <text>), or Discard.
     No capture inside the window -> `outcome_unknown` (never counted as a clean accept).
  5. Hooks: `on_captured` (D9a classifier goes here), `stats_provider` (D11a /stats goes here).

Raw text is never stored: incoming text, sent text and drafts are all stored masked.
Telegram is draft-only. The bot never sends anything on the user's behalf.
"""
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.engine import Engine

from app.config import Settings
from app.drafting.base import DraftError, DraftRequest, Drafter
from app.events import EventName, EventWriter
from app.events.writer import make_key
from app.privacy.masking import EMAIL_TOKEN, PHONE_TOKEN, mask_text, pseudonymise
from app.telegram import format as fmt
from app.telegram.client import TelegramAPI
from app.telegram.store import ThreadStore, aware

log = logging.getLogger("minime.telegram")

CHANNEL = "telegram"
DOMAIN = "group_queries"
MODE = "shadow"  # Telegram is draft-only; live autonomy is Gmail only this version (5.13 pt 1)


# --------------------------------------------------------------------------- hooks (D9a / D11a)
def noop_classifier_hook(message_id: str, capture: str) -> None:
    """D9a replaces this. Called once after a sent text is captured (sent as-is, edited, or blind reply).
    D9a: compare message_records.draft_text with sent_text, then write edit_class + classifier_version on
    the record and log `edit_classified` (once per message)."""


def stub_stats(store: "BotDeps") -> str:
    """D11a replaces this with the counts of as-is / light / heavy / discard and the class mix."""
    return fmt.STATS_STUB


@dataclass
class BotDeps:
    settings: Settings
    engine: Engine
    writer: EventWriter
    client: TelegramAPI
    drafter: Drafter
    on_captured: Callable[[str, str], None] = noop_classifier_hook
    stats_provider: Callable[["BotDeps"], str] = stub_stats
    rng: Callable[[], float] = random.random
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    store: ThreadStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = ThreadStore(self.engine)


# --------------------------------------------------------------------------- helpers
def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _message_id(chat_id: int, tg_msg_id: int) -> str:
    return "tg-" + hashlib.sha256(f"{chat_id}:{tg_msg_id}".encode()).hexdigest()[:16]


def _allowed(deps: BotDeps, user_id: Any, chat: dict | None) -> bool:
    raw = (deps.settings.telegram_allowed_user_id or "").strip()
    if not raw.lstrip("-").isdigit() or user_id is None:
        return False  # fail closed
    if int(raw) != int(user_id):
        return False
    return chat is None or (chat.get("type") == "private" and int(chat.get("id", 0)) == int(raw))


def _origin(m: dict) -> tuple[str | None, str]:
    """(stable identifier or None, 'individual' | 'group' | 'pasted'). The identifier never leaves this
    function except through pseudonymise(). '@' keeps masking.normalise_identifier from treating a long Telegram
    id as a phone number (see handoff flag)."""
    fo = m.get("forward_origin")
    if fo:
        t = fo.get("type")
        if t == "user":
            return f"user-{fo['sender_user']['id']}@tg.invalid", "individual"
        if t == "hidden_user":
            return f"name-{fo.get('sender_user_name', '')}@tg.invalid", "individual"
        if t in ("chat", "channel"):
            chat = fo.get("sender_chat") or fo.get("chat") or {}
            return f"chat-{chat.get('id', '')}@tg.invalid", "group"
    if m.get("forward_from"):
        return f"user-{m['forward_from']['id']}@tg.invalid", "individual"
    if m.get("forward_from_chat"):
        return f"chat-{m['forward_from_chat']['id']}@tg.invalid", "group"
    if m.get("forward_sender_name"):
        return f"name-{m['forward_sender_name']}@tg.invalid", "individual"
    return None, "pasted"


def _masked_count(masked: str) -> int:
    return masked.count(PHONE_TOKEN) + masked.count(EMAIL_TOKEN)


def _ms_since(deps: BotDeps, shown_at: datetime | None) -> int | None:
    if shown_at is None:
        return None
    return max(0, int((deps.now() - aware(shown_at)).total_seconds() * 1000))


def _common(contact_type: str | None = None) -> dict:
    out = {"channel": CHANNEL, "domain": DOMAIN, "mode": MODE, "device_type": None}
    if contact_type:
        out["contact_type"] = contact_type
    return out


def _safe_clear_buttons(deps: BotDeps, chat_id: Any, msg_id: int | None) -> None:
    if msg_id is None:
        return
    try:
        deps.client.clear_buttons(chat_id, msg_id)
    except Exception:  # noqa: BLE001  (cosmetic; never block recording)
        log.warning("could not clear buttons")


def _mark_draft_message(deps: BotDeps, chat_id: Any, msg_id: int, original: str | None, status: str) -> None:
    """Show what happened on the draft itself (so 'done' is visible in the chat). Falls back to just
    removing the buttons if Telegram did not give us the text or refuses the edit."""
    if original:
        try:
            deps.client.edit_message_text(chat_id, msg_id, fmt.with_status(original, status))
            return
        except Exception:  # noqa: BLE001
            log.warning("could not edit the draft message")
    _safe_clear_buttons(deps, chat_id, msg_id)


def _run_hook(deps: BotDeps, message_id: str, capture: str) -> None:
    try:
        deps.on_captured(message_id, capture)
    except Exception:  # noqa: BLE001
        log.exception("on_captured hook failed")  # logging must not block the user (NFR-6)


# --------------------------------------------------------------------------- entry point
def handle_update(update: dict, deps: BotDeps) -> None:
    """Handle one Telegram update. Never raises for bad input; unauthorised users get no reply."""
    try:
        sweep_expired(deps)  # opportunistic: the machine may have been asleep when windows ended
    except Exception:  # noqa: BLE001
        log.exception("sweep failed")

    if "callback_query" in update:
        cq = update["callback_query"]
        chat = (cq.get("message") or {}).get("chat")
        if not _allowed(deps, (cq.get("from") or {}).get("id"), chat):
            log.warning("ignored callback from unauthorised user")
            return
        _handle_callback(cq, deps)
        return

    m = update.get("message")
    if not m:
        return
    if not _allowed(deps, (m.get("from") or {}).get("id"), m.get("chat")):
        log.warning("ignored message from unauthorised user")  # no content, no reply, no event
        return
    _handle_message(m, deps)


# --------------------------------------------------------------------------- messages
def _handle_message(m: dict, deps: BotDeps) -> None:
    chat_id = m["chat"]["id"]
    text = m.get("text") or m.get("caption")
    is_forward = bool(m.get("forward_origin") or m.get("forward_from") or m.get("forward_from_chat")
                      or m.get("forward_sender_name"))

    if not text or not text.strip():
        deps.client.send_message(chat_id, fmt.NO_TEXT)
        return

    if text.startswith("/") and not is_forward:
        _handle_command(m, text, chat_id, deps)
        return

    reply_to = m.get("reply_to_message")
    if reply_to and not is_forward:
        thread = deps.store.by_prompt(chat_id, reply_to["message_id"])
        if thread:
            _capture_final_text(thread, text, chat_id, deps)
            return
    _ingest_incoming(m, text, chat_id, deps)


def _handle_command(m: dict, text: str, chat_id: int, deps: BotDeps) -> None:
    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        deps.client.send_message(chat_id, fmt.HELP)
    elif cmd == "/stats":
        deps.client.send_message(chat_id, deps.stats_provider(deps))
    elif cmd == "/pending":
        items = []
        for t in deps.store.list_open(chat_id):
            rec = deps.writer.get_message(t["message_id"]) or {}
            items.append((t["number"], rec.get("incoming_text") or "", t["state"]))
        deps.client.send_message(chat_id, fmt.pending_list(items))
    elif cmd == "/sent":
        thread = deps.store.latest_open(chat_id)
        if not thread:
            deps.client.send_message(chat_id, fmt.NOTHING_WAITING)
        elif not arg.strip():
            deps.client.send_message(chat_id, "Type /sent followed by what you actually sent.")
        else:
            _capture_final_text(thread, arg, chat_id, deps)
    else:
        deps.client.send_message(chat_id, fmt.HELP)


# --------------------------------------------------------------------------- ingest + draft
def _ingest_incoming(m: dict, text: str, chat_id: int, deps: BotDeps) -> None:
    mid = _message_id(chat_id, m["message_id"])
    if deps.writer.get_message(mid):
        return  # Telegram redelivered this update; already handled

    masked = mask_text(text)  # local, before storage and before any model call
    ident, kind = _origin(m)
    if ident is None:  # pasted: no sender known. Unique pseudonym per message, so "known" is never faked.
        ident = f"pasted-{mid}@tg.invalid"
    pseud = pseudonymise(ident)

    known = deps.store.is_known_contact(pseud)
    shape = "group" if kind in ("group", "pasted") else "individual"  # clear default for pasted: group
    contact_type = f"{'known' if known else 'new'}_{shape}"
    common = _common(contact_type)

    deps.writer.upsert_message(
        mid, channel=CHANNEL, domain=DOMAIN, contact_type=contact_type, contact_pseudonym=pseud,
        incoming_text=masked, device_type=None, mode=MODE, outcome="waiting",
    )
    deps.writer.log(
        EventName.message_received, message_id=mid,
        payload={"sender_type": "known" if known else "new", "origin": "pasted" if kind == "pasted" else "forwarded",
                 "masked_count": _masked_count(masked)},
        **common,
    )

    prev_open = deps.store.latest_open(chat_id) if kind == "pasted" else None  # for the nudge, see below

    t0 = time.monotonic()
    try:
        result = deps.drafter.draft(DraftRequest(
            message_id=mid, masked_text=masked, channel=CHANNEL, domain=DOMAIN,
            contact_type=contact_type, contact_pseudonym=pseud))
    except Exception as e:  # noqa: BLE001  (DraftError or anything unexpected: tell the user, never score it)
        _draft_failed(deps, mid, chat_id, m["message_id"], e, common)
        return
    gen_ms = int((time.monotonic() - t0) * 1000)

    blind = deps.rng() < deps.settings.telegram_blind_ratio
    shown_state = "blind" if blind else "shown"
    draft_text = mask_text(result.text)

    deps.writer.upsert_message(
        mid, draft_text=draft_text, confidence=result.confidence, rationale=result.rationale,
        autonomy_decision="draft_notify", shown_state=shown_state, mode=MODE,
    )
    deps.writer.log(
        EventName.draft_generated, message_id=mid,
        payload={**result.meta,
                 "items_used": [i.__dict__ for i in result.items_used], "drafter": result.drafter,
                 "drafter_version": result.drafter_version, "is_stub": result.is_stub,
                 "confidence": result.confidence, "gen_latency_ms": gen_ms, "shown_state": shown_state},
        **{**common, "self_model_version": result.meta.get("self_model_version")},
    )

    now = deps.now()
    deps.store.create(mid, chat_id, m["message_id"], "blind_held" if blind else "shown", now,
                      deps.settings.telegram_capture_window_hours)
    number = deps.store.number_of(chat_id, mid)
    try:
        if blind:
            prompt_id = deps.client.send_message(chat_id, fmt.BLIND_NOTICE, reply_to_message_id=m["message_id"])
            deps.store.set_prompt(mid, prompt_id)
            deps.writer.log(EventName.draft_held_blind, message_id=mid,
                            payload={"ratio": deps.settings.telegram_blind_ratio}, **common)
        else:
            prompt_id = deps.client.send_message(
                chat_id, fmt.draft_message(result, number, masked), reply_markup=fmt.keyboard(mid),
                reply_to_message_id=m["message_id"])
            deps.store.set_prompt(mid, prompt_id)
            deps.writer.log(EventName.draft_shown, message_id=mid,
                            idempotency_key=make_key("draft_shown", mid, 1), payload={"view_no": 1}, **common)
    except Exception:  # noqa: BLE001  (message stays 'waiting'; the sweep will mark it unknown)
        log.exception("could not send the draft message")
        return

    # A plain (not forwarded) message while another one is still open is often the user's final text, typed
    # without using Reply. It was treated as new (decision: only Reply or /sent captures); say so, calmly.
    if prev_open is not None:
        try:
            deps.client.send_message(chat_id, fmt.nudge(deps.store.number_of(chat_id, prev_open["message_id"])))
        except Exception:  # noqa: BLE001
            log.warning("could not send the nudge")


def _draft_failed(deps: BotDeps, mid: str, chat_id: int, incoming_tg_id: int, err: Exception, common: dict) -> None:
    """The drafter could not draft (e.g. provider not approved, model down, unusable output). Say so plainly,
    keep no draft, and record the message as outcome unknown: never scored, never counted as an accept."""
    reason = err.reason if isinstance(err, DraftError) else "error"
    # Reason code plus the drafter's own short note (HTTP status or what was wrong with the output); never message text.
    log.warning("drafter failed: %s %s", reason, str(err)[:200] if isinstance(err, DraftError) else "")
    deps.writer.upsert_message(mid, outcome="unknown")
    deps.writer.log(EventName.outcome_unknown, message_id=mid,
                    payload={"reason": "draft_failed", "draft_error": reason}, **common)
    try:
        deps.client.send_message(chat_id, fmt.draft_failed(reason), reply_to_message_id=incoming_tg_id)
    except Exception:  # noqa: BLE001
        log.warning("could not send the draft-failed note")


# --------------------------------------------------------------------------- capture
def _capture_final_text(thread: dict, final_text: str, chat_id: int, deps: BotDeps) -> None:
    mid = thread["message_id"]
    state = thread["state"]
    if not deps.store.claim(mid):
        deps.client.send_message(chat_id, fmt.ALREADY_RECORDED)
        return
    rec = deps.writer.get_message(mid) or {}
    draft_text = rec.get("draft_text") or ""
    sent = mask_text(final_text.strip())  # the user's own words, masked before storage
    matched = _norm(sent) == _norm(draft_text)
    outcome = "sent_as_is" if matched else "edited_sent"
    tta = _ms_since(deps, thread.get("shown_at"))
    common = _common(rec.get("contact_type"))
    via = "reply_text"
    number = deps.store.number_of(chat_id, mid)

    deps.writer.upsert_message(mid, sent_text=sent, outcome=outcome, time_to_action_ms=tta)
    deps.writer.log(EventName.draft_acted, message_id=mid, idempotency_key=make_key("draft_acted", mid),
                    payload={"action": "sent" if matched else "edited_sent", "capture": via,
                             "time_to_action_ms": tta, "shown_state": "blind" if state == "blind_held" else "shown"},
                    **common)
    deps.writer.log(EventName.draft_sent, message_id=mid,
                    payload={"sent_text": sent, "sent_by": "user", "edited": not matched, "capture": via},
                    **common)

    if state == "blind_held":
        # Blind sample (5.15 pt 3): the reply came first; only now show the held draft.
        deps.client.send_message(chat_id, fmt.released_message(draft_text, matched, number))
        deps.writer.log(EventName.draft_released_after_reply, message_id=mid,
                        payload={"matched": matched}, **common)
    else:
        _safe_clear_buttons(deps, chat_id, thread.get("prompt_tg_msg_id"))
        deps.client.send_message(chat_id, fmt.saved(number, matched))

    _run_hook(deps, mid, via)


def _handle_callback(cq: dict, deps: BotDeps) -> None:
    data = cq.get("data") or ""
    chat_id = cq["message"]["chat"]["id"]
    prompt_id = cq["message"]["message_id"]
    try:
        action, mid = data.split(":", 1)
    except ValueError:
        deps.client.answer_callback(cq["id"], "")
        return
    thread = deps.store.get(mid)
    if not thread or thread["state"] != "shown" or action not in ("sent", "disc") or not deps.store.claim(mid):
        # Closed, blind (no buttons exist for blind), unknown, or a retry that lost the race.
        deps.client.answer_callback(cq["id"], fmt.ALREADY_RECORDED)
        _safe_clear_buttons(deps, chat_id, prompt_id)
        return

    rec = deps.writer.get_message(mid) or {}
    tta = _ms_since(deps, thread.get("shown_at"))
    common = _common(rec.get("contact_type"))

    if action == "sent":
        sent = rec.get("draft_text") or ""
        deps.writer.upsert_message(mid, sent_text=sent, outcome="sent_as_is", time_to_action_ms=tta)
        deps.writer.log(EventName.draft_acted, message_id=mid, idempotency_key=make_key("draft_acted", mid),
                        payload={"action": "sent", "capture": "button", "time_to_action_ms": tta,
                                 "shown_state": "shown"}, **common)
        deps.writer.log(EventName.draft_sent, message_id=mid,
                        payload={"sent_text": sent, "sent_by": "user", "edited": False, "capture": "button"},
                        **common)
        deps.client.answer_callback(cq["id"], "Saved: sent as written.")
        _mark_draft_message(deps, chat_id, prompt_id, cq["message"].get("text"), fmt.STATUS_SENT)
        _run_hook(deps, mid, "button")
    else:
        deps.writer.upsert_message(mid, outcome="discarded", time_to_action_ms=tta)
        deps.writer.log(EventName.draft_acted, message_id=mid, idempotency_key=make_key("draft_acted", mid),
                        payload={"action": "discarded", "capture": "button", "time_to_action_ms": tta,
                                 "shown_state": "shown"}, **common)
        deps.writer.log(EventName.draft_discarded, message_id=mid, payload={"reason": None}, **common)
        deps.client.answer_callback(cq["id"], "Discarded.")
        _mark_draft_message(deps, chat_id, prompt_id, cq["message"].get("text"), fmt.STATUS_DISCARDED)


# --------------------------------------------------------------------------- outcome unknown
def sweep_expired(deps: BotDeps) -> int:
    """Close threads whose capture window has passed with no sent text: log `outcome_unknown`, never score.
    Called on every update, by a background loop while the machine is awake, and by POST /telegram/sweep."""
    n = 0
    for t in deps.store.expired(deps.now()):
        if not deps.store.claim(t["message_id"]):
            continue
        mid = t["message_id"]
        rec = deps.writer.get_message(mid)
        if rec and rec.get("outcome") == "waiting":
            deps.writer.upsert_message(mid, outcome="unknown")
            deps.writer.log(
                EventName.outcome_unknown, message_id=mid, occurred_at=t["deadline_at"],  # when it became unknown
                payload={"reason": "capture_window_expired",
                         "window_hours": deps.settings.telegram_capture_window_hours,
                         "shown_state": "blind" if t["state"] == "blind_held" else "shown"},
                **_common(rec.get("contact_type")))
            n += 1
        if t["state"] == "shown":
            _safe_clear_buttons(deps, int(t["chat_id"]), t.get("prompt_tg_msg_id"))
    return n
