"""In-memory only: the restored (real-value) draft for the authenticated user's own session (5.21 pt 2).

The database, the event log and everything sent to the model stay masked. The web app gets real values in the
response that made the draft; this cache lets the same draft screen be re-opened (a page reload) for a short time
without writing real values anywhere durable. It lives in this process only: a restart, or the TTL, empties it,
and the draft then shows its tokens (as after the 7-day mapping deletion).
"""
import threading
import time

_lock = threading.Lock()
_items: dict[str, tuple[float, str, list[str]]] = {}


def put(message_id: str, draft: str, unrestored: list[str], minutes: float) -> None:
    with _lock:
        _items[message_id] = (time.monotonic() + minutes * 60, draft, list(unrestored))
        _purge()


def get(message_id: str) -> tuple[str, list[str]] | None:
    with _lock:
        _purge()
        hit = _items.get(message_id)
        return (hit[1], list(hit[2])) if hit else None


def drop(message_id: str) -> None:
    with _lock:
        _items.pop(message_id, None)


def clear() -> None:
    with _lock:
        _items.clear()


def _purge() -> None:
    now = time.monotonic()
    for k in [k for k, v in _items.items() if v[0] <= now]:
        _items.pop(k, None)
