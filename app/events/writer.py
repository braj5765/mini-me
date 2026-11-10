"""Idempotent event writer (D2a). Every component logs through this class.

Rules enforced here:
- event_name must be in EventName; enum-typed fields are validated.
- Idempotency: the same idempotency_key never creates a second row. Events that happen once per
  message get a default key (event_name:message_id); repeatable events must pass an explicit key.
- Third-party text is masked here too (defence in depth): payload key `incoming_text` and the
  message-record field `incoming_text` always pass through masking.
- Messages whose sent text was never captured get outcome 'unknown' and scorable=False (5.13).
"""
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from app import db
from app.config import get_settings
from app.events.schema import (
    COMMON_FIELDS, ENUM_FIELDS, MESSAGE_RECORD_FIELDS, EventName, Outcome,
)
from app.privacy.masking import mask_text

log = logging.getLogger("minime.events")

# Events that occur at most once per message: default key = "<event_name>:<message_id>".
ONCE_PER_MESSAGE = {
    EventName.message_received, EventName.draft_generated, EventName.autonomy_decision,
    EventName.edit_classified, EventName.draft_sent, EventName.draft_discarded,
    EventName.outcome_unknown, EventName.shadow_comparison, EventName.draft_held_blind,
    EventName.draft_released_after_reply,
    EventName.not_replying,
}

# Payload keys that carry third-party text and must be masked before storage.
THIRD_PARTY_TEXT_KEYS = ("incoming_text", "text_ref_masked")

THIRD_PARTY_TEXT_DAYS = 7  # 5.14 point 3 [adjustable after testing]; purge job is D3b

# Outcomes that count as "known" (scorable). 'waiting', 'unknown', 'user_authored' do not.
_SCORABLE = {Outcome.sent_as_is, Outcome.edited_sent, Outcome.discarded, Outcome.not_me}


def make_key(event_name: str | EventName, *parts: Any) -> str:
    """Deterministic key from a name plus discriminating parts (e.g. message_id, button-press id)."""
    raw = "|".join([str(event_name), *[str(p) for p in parts]])
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_enums(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in values.items():
        if v is not None and k in ENUM_FIELDS:
            v = ENUM_FIELDS[k](v).value  # raises ValueError on a bad value
        out[k] = v
    return out


def _upsert(engine: Engine):
    return pg_insert if engine.dialect.name == "postgresql" else sqlite_insert


class EventWriter:
    def __init__(self, engine: Engine | None = None, user_id: str = "braj"):
        self.engine = engine or db.get_engine()
        self.user_id = user_id

    # ---------------------------------------------------------------- events
    def log(
        self,
        event_name: str | EventName,
        *,
        idempotency_key: str | None = None,
        message_id: str | None = None,
        occurred_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
        **common: Any,
    ) -> tuple[str, bool]:
        """Write one event. Returns (event_id, inserted). inserted=False means a duplicate was ignored."""
        name = EventName(event_name)
        unknown = set(common) - set(COMMON_FIELDS)
        if unknown:
            raise ValueError(f"Unknown common field(s): {sorted(unknown)}")

        key = idempotency_key
        if key is None:
            if name in ONCE_PER_MESSAGE and message_id:
                key = f"{name.value}:{message_id}"
            else:
                raise ValueError(
                    f"{name.value} is repeatable; pass idempotency_key (see make_key)."
                )

        fields = _validate_enums(common)
        payload = dict(payload or {})
        for k in THIRD_PARTY_TEXT_KEYS:
            if isinstance(payload.get(k), str):
                payload[k] = mask_text(payload[k])

        settings = get_settings()
        row = {
            "event_id": str(uuid.uuid4()),
            "idempotency_key": key,
            "event_name": name.value,
            "occurred_at": occurred_at or _utcnow(),
            "recorded_at": _utcnow(),
            "user_id": fields.pop("user_id", None) or self.user_id,
            "app_version": fields.pop("app_version", None) or settings.app_version,
            "message_id": message_id,
            "payload": payload,
            **{k: fields.get(k) for k in COMMON_FIELDS if k not in ("user_id", "app_version", "message_id")},
        }
        stmt = _upsert(self.engine)(db.events).values(**row).on_conflict_do_nothing(
            index_elements=["idempotency_key"]
        )
        with self.engine.begin() as conn:
            inserted = conn.execute(stmt).rowcount == 1
            if inserted:
                return row["event_id"], True
            existing = conn.execute(
                select(db.events.c.event_id).where(db.events.c.idempotency_key == key)
            ).scalar_one()
            return existing, False

    def log_safe(self, *args: Any, **kwargs: Any) -> tuple[str, bool] | None:
        """Never raises: logging must not block the user (NFR-6). D2b adds the retry queue."""
        try:
            return self.log(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("event write failed (will be retried once D2b lands)")
            return None

    # -------------------------------------------------------- message record
    def upsert_message(self, message_id: str, **fields: Any) -> None:
        """Create or update the per-draft record. Only the fields you pass are changed."""
        unknown = set(fields) - set(MESSAGE_RECORD_FIELDS) - {"incoming_text"}
        if unknown:
            raise ValueError(f"Unknown message-record field(s): {sorted(unknown)}")
        fields = _validate_enums(fields)
        now = _utcnow()

        if isinstance(fields.get("incoming_text"), str):
            fields["incoming_text"] = mask_text(fields["incoming_text"])
            fields["incoming_text_expires_at"] = now + timedelta(days=THIRD_PARTY_TEXT_DAYS)

        # Outcome drives 'scorable'. Unknown outcome is never scored (5.13 point 2).
        if "outcome" in fields and fields["outcome"] is not None:
            fields["scorable"] = Outcome(fields["outcome"]) in _SCORABLE

        values = {
            "message_id": message_id, "user_id": self.user_id,
            "created_at": now, "updated_at": now, "outcome": "waiting", "scorable": False,
            **fields,
        }
        update = {**fields, "updated_at": now}
        stmt = _upsert(self.engine)(db.message_records).values(**values).on_conflict_do_update(
            index_elements=["message_id"], set_=update
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(
                select(db.message_records).where(db.message_records.c.message_id == message_id)
            ).mappings().first()
            return dict(r) if r else None

    def mark_outcome_unknown(self, message_id: str, **common: Any) -> None:
        """No sent text captured: log it and exclude from scores. Never assume a clean accept (5.13)."""
        self.upsert_message(message_id, outcome=Outcome.unknown.value)
        self.log(EventName.outcome_unknown, message_id=message_id, **common)
