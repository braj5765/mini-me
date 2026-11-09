"""Read-only event table (D11a, v0.1 part), shown in Telegram as `/events` and `/events <draft number>`.

Telegram-only on purpose: v0.1 has no web screens (decisions 5.8 adjustment / 5.15 pt 2), and the web event log
is D11b / D18. Only whitelisted fields are shown (ids, counts, labels, versions). Message text is never shown,
even though it is stored masked. Times are shown in IST (Asia/Kolkata); stored times stay UTC.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
    DISPLAY_TZ = ZoneInfo("Asia/Kolkata")
except Exception:                       # no tz database on the machine: IST has no daylight saving, so a fixed offset is exact
    DISPLAY_TZ = timezone(timedelta(hours=5, minutes=30), "IST")
TZ_LABEL = "IST"

from app.events import queries

MAX_LINES = 40


def _g(p: dict, *keys: str) -> list[str]:
    out = []
    for k in keys:
        v = p.get(k)
        if v not in (None, "", [], {}):
            out.append(f"{k}={v}")
    return out


def _detail(name: str, p: dict[str, Any]) -> str:
    if name == "message_received":
        parts = _g(p, "origin", "masked_count")
    elif name == "draft_generated":
        parts = _g(p, "drafter_version", "confidence", "data_mode", "llm_ms")
        parts.append("stub" if p.get("is_stub") else "real")
        parts.append(f"items={len(p.get('items_used') or [])}")
    elif name in ("draft_acted",):
        parts = _g(p, "action", "capture", "shown_state")
    elif name == "draft_sent":
        parts = _g(p, "capture") + [f"edited={bool(p.get('edited'))}"]
    elif name == "draft_released_after_reply":
        parts = _g(p, "matched")
    elif name == "draft_held_blind":
        parts = _g(p, "ratio")
    elif name == "outcome_unknown":
        parts = _g(p, "reason", "draft_error", "shown_state")
    elif name == "edit_classified":
        classes = "+".join(p.get("classes") or []) or "-"
        parts = [f"class={p.get('edit_class')}", f"all={classes}"] + _g(p, "method", "fallback", "model")
        if p.get("never_light"):
            parts.append("never light")
        if p.get("token_changes"):
            parts.append("token changed")
    else:
        parts = []
    return " ".join(parts)


def to_ist(t: datetime) -> datetime:
    """Stored times are UTC. SQLite hands back naive datetimes, which are UTC too."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(DISPLAY_TZ)


def _when(ev: dict) -> str:
    t = ev["occurred_at"]
    return to_ist(t).strftime("%d %b %H:%M:%S") if isinstance(t, datetime) else str(t)


def _line(ev: dict, label: str) -> str:
    d = _detail(ev["event_name"], ev.get("payload") or {})
    return f"{_when(ev)} {label}{ev['event_name']}" + (f" | {d}" if d else "")


def events_text(engine, numbers: dict[str, int], by_number: Callable[[int], str | None], arg: str = "") -> str:
    """`numbers`: message_id -> draft number (#N). `by_number`: draft number -> message_id."""
    arg = (arg or "").strip().lstrip("#")
    if arg:
        if not arg.isdigit():
            return "Use /events for the latest steps, or /events 7 for everything that happened to draft #7."
        mid = by_number(int(arg))
        if not mid:
            return f"There is no draft #{arg}."
        evs = queries.events_for_message(engine, mid)
        if not evs:
            return f"No events logged for draft #{arg}."
        lines = [f"Draft #{arg} - every step ({TZ_LABEL}):"] + [_line(e, "") for e in evs[:MAX_LINES]]
        return "\n".join(lines)

    evs = queries.recent_events(engine, 12)
    if not evs:
        return "No events yet."
    lines = [f"Latest steps, oldest first ({TZ_LABEL}). /events 7 shows one draft in full:"]
    for e in evs:
        n = numbers.get(e.get("message_id") or "")
        lines.append(_line(e, f"#{n} " if n else ""))
    return "\n".join(lines)


def make_events_provider():
    def provider(deps, arg: str, chat_id) -> str:
        nums = deps.store.numbers(chat_id)
        inv = {n: m for m, n in nums.items()}
        return events_text(deps.engine, nums, inv.get, arg)
    return provider
