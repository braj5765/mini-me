"""Database tables (SQLAlchemy Core). Same code runs on Postgres (Neon) and SQLite (tests)."""
from functools import lru_cache

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Index, Integer, MetaData, String, Table, Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from app.config import get_settings

metadata = MetaData()
_JSON = JSON().with_variant(JSONB(), "postgresql")

# Append-only event log. One row per event. Never updated.
events = Table(
    "events", metadata,
    Column("event_id", String(36), primary_key=True),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("event_name", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    # common fields (every event)
    Column("user_id", String(64), nullable=False),
    Column("session_id", String(64)),
    Column("device_type", String(16)),          # mobile | desktop | NULL (system events)
    Column("channel", String(16)),              # telegram | email | web
    Column("domain", String(64)),
    Column("contact_type", String(32)),         # new/known x individual/group
    Column("app_version", String(32), nullable=False),
    Column("self_model_version", String(64)),
    Column("message_id", String(64)),
    Column("mode", String(8)),                  # shadow | live
    Column("classifier_version", String(64)),
    Column("payload", _JSON, nullable=False),   # event-specific fields (PRD section 6)
    Index("ix_events_name_time", "event_name", "occurred_at"),
    Index("ix_events_message", "message_id"),
)

# Per-draft record (5.7): one row per message, updated as the message moves through the loop.
# Derived from events; the event log stays the source of truth for history.
message_records = Table(
    "message_records", metadata,
    Column("message_id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("channel", String(16)),
    Column("domain", String(64)),
    Column("contact_type", String(32)),
    Column("contact_pseudonym", String(32)),    # never a real name/number (5.14 point 2)
    # third-party text: MASKED ONLY. Purged after outcome capture / 7 days by D3b (5.14 point 3).
    Column("incoming_text", Text),
    Column("incoming_text_expires_at", DateTime(timezone=True)),
    Column("draft_text", Text),
    Column("rationale", Text),
    Column("confidence", Float),                # 0.00 - 1.00
    Column("sent_text", Text),                  # the user's own words: kept
    Column("outcome", String(16), nullable=False, default="waiting"),  # 'unknown' excluded from scores
    Column("edit_class", String(16)),
    Column("signal", String(24)),
    Column("autonomy_decision", String(32)),
    Column("hard_limit", String(16)),
    Column("topic_triggered", String(200)),
    Column("shown_state", String(8)),           # shown | blind (5.15)
    Column("time_to_action_ms", Integer),
    Column("device_type", String(16)),
    Column("classifier_version", String(64)),
    Column("mode", String(8)),
    Column("scorable", Boolean, nullable=False, default=False),  # True only once outcome is known
    Index("ix_msg_channel_created", "channel", "created_at"),
)


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True, future=True)


def init_db(engine: Engine | None = None) -> None:
    """Create tables if missing. Alembic arrives with D1b; until then the schema is create-only."""
    metadata.create_all(engine or get_engine())
