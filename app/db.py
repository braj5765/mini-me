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


# Telegram bot plumbing (D4a). Holds Telegram ids and timers only: NO message text of any kind.
# Additive table (create_all adds it; nothing existing changes). Needed because the bot must map a
# Telegram message id back to a message_id, and survive Fly scale-to-zero restarts.
telegram_threads = Table(
    "telegram_threads", metadata,
    Column("message_id", String(64), primary_key=True),
    Column("chat_id", String(32), nullable=False),
    Column("incoming_tg_msg_id", Integer),       # the forwarded / pasted message
    Column("prompt_tg_msg_id", Integer),         # the bot's draft message, or its blind-sample notice
    Column("state", String(16), nullable=False),  # shown | blind_held | closed
    Column("shown_at", DateTime(timezone=True)),  # when the user could first act (draft shown / notice sent)
    Column("deadline_at", DateTime(timezone=True), nullable=False),  # capture window end
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_tgthreads_state_deadline", "state", "deadline_at"),
    Index("ix_tgthreads_prompt", "chat_id", "prompt_tg_msg_id"),
)


# Self-model (D6a). Three layers: voice, decision, fact. One row per item. All text is stored MASKED.
# D6b is additive: it adds `self_model_item_versions` (item_id + version -> old text, who, when) and a
# snapshot table; this table already has `status` (active | archived, so nothing is hard-deleted) and
# `version` (bumped on every text change), so no column here has to change.
self_model_items = Table(
    "self_model_items", metadata,
    Column("item_id", String(32), primary_key=True),       # e.g. sm_3fa91c2b7d
    Column("user_id", String(64), nullable=False),
    Column("layer", String(16), nullable=False),          # voice | decision | fact
    Column("text", Text, nullable=False),                 # masked
    Column("source", String(32), nullable=False),         # seed_own_line | seed_own_lines | manual | ...
    Column("source_ref", String(200)),                    # e.g. the seed file name; never message text
    Column("status", String(12), nullable=False, default="active"),  # active | archived
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_selfmodel_layer_status", "layer", "status"),
)


# Personal API tokens for share-to-draft (Dev 6). Only a SHA-256 hash of each token is stored, never the token.
# New table: create_all adds it; nothing existing changes.
api_tokens = Table(
    "api_tokens", metadata,
    Column("token_id", String(16), primary_key=True),       # tk_ + 10 hex; shown in `list`, used by `revoke`
    Column("user_id", String(64), nullable=False),
    Column("name", String(80), nullable=False),             # e.g. "phone shortcut"
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("token_prefix", String(16), nullable=False),     # first characters, so a token can be recognised
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)


# Sender tags for the "From:" row (Dev 7, 5.17 pt 1). A tag is a short label the user typed ("Cultural committee").
# Stored as the user's own words (masked for phones / emails). Messages never store the label: they get the
# tag's pseudonym. New tables: create_all adds them; nothing existing changes.
sender_tags = Table(
    "sender_tags", metadata,
    Column("tag_id", String(16), primary_key=True),          # st_ + 10 hex
    Column("user_id", String(64), nullable=False),
    Column("label", String(60), nullable=False),
    Column("kind", String(12), nullable=False),              # individual | group
    Column("domain", String(64), nullable=False),            # remembered domain (5.18); default club
    Column("pseudonym", String(32), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True)),
)

# Which tag an item carries (for the waiting list's "group" line). One row per item.
item_tags = Table(
    "item_tags", metadata,
    Column("message_id", String(64), primary_key=True),
    Column("tag_id", String(16), nullable=False),
    Column("tagged_at", DateTime(timezone=True), nullable=False),
)


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True, future=True)


def init_db(engine: Engine | None = None) -> None:
    """Create tables if missing. Alembic arrives with D1b; until then the schema is create-only."""
    metadata.create_all(engine or get_engine())
