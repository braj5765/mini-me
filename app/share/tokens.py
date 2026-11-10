"""Personal, revocable API tokens (decisions 5.20 pt 6). Only a SHA-256 hash is stored, never the token.

A token is 32 random bytes (`mm_` + url-safe text), so a plain hash is enough (nothing to brute-force). The token
is shown once, when it is created. `revoke` takes effect on the next request.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from app import db

PREFIX = "mm_"
SHOWN_PREFIX_CHARS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(engine: Engine, name: str, user_id: str = "braj") -> tuple[str, str]:
    """Returns (token_id, the token). The token cannot be read back later: only its hash is kept."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A token needs a name, e.g. 'phone shortcut'.")
    token = PREFIX + secrets.token_urlsafe(32)
    token_id = "tk_" + uuid.uuid4().hex[:10]
    with engine.begin() as conn:
        conn.execute(db.api_tokens.insert().values(
            token_id=token_id, user_id=user_id, name=name[:80], token_hash=hash_token(token),
            token_prefix=token[: len(PREFIX) + SHOWN_PREFIX_CHARS], created_at=_now()))
    return token_id, token


def verify_token(engine: Engine, token: str | None) -> dict[str, Any] | None:
    """The token row if the token is valid and not revoked (and stamps last_used_at), else None."""
    if not token or not token.startswith(PREFIX) or len(token) > 200:
        return None
    t = db.api_tokens
    with engine.begin() as conn:
        row = conn.execute(select(t).where(t.c.token_hash == hash_token(token))).mappings().first()
        if row is None or row["revoked_at"] is not None:
            return None
        conn.execute(update(t).where(t.c.token_id == row["token_id"]).values(last_used_at=_now()))
    return dict(row)


def list_tokens(engine: Engine, user_id: str = "braj") -> list[dict[str, Any]]:
    t = db.api_tokens
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(t.c.user_id == user_id).order_by(t.c.created_at)).mappings().all()
    return [{k: v for k, v in r.items() if k != "token_hash"} for r in rows]


def revoke_token(engine: Engine, token_id: str) -> bool:
    """True if a live token was revoked now; False if it does not exist or was already revoked."""
    t = db.api_tokens
    with engine.begin() as conn:
        res = conn.execute(update(t).where(t.c.token_id == token_id, t.c.revoked_at.is_(None))
                           .values(revoked_at=_now()))
    return res.rowcount == 1


def revoke_all(engine: Engine, user_id: str = "braj") -> int:
    t = db.api_tokens
    with engine.begin() as conn:
        res = conn.execute(update(t).where(t.c.user_id == user_id, t.c.revoked_at.is_(None))
                           .values(revoked_at=_now()))
    return res.rowcount
