"""Self-model store (D6a). Three layers (decisions 4.1): Voice, Decision rules, Facts.

Each item: id, layer, text, source, created / updated time (plus status + version, so D6b can add
versioning, undo and edit events without changing this table). Text is stored MASKED (5.14, 5.17).
Items are plain rows the user can later see and edit (screen 4); nothing here is hidden state.

Retrieval (`retrieve`) is build-level and deliberately simple for v0.1: a small store is sent whole;
a bigger one is trimmed by word overlap with the incoming message. See docs for the numbers.
"""
import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from app import db
from app.privacy.masking import mask_text

USER_ID = "braj"
MAX_ITEM_CHARS = 400


class SelfModelError(ValueError):
    pass


class Layer(str, Enum):
    voice = "voice"
    decision = "decision"
    fact = "fact"


STATUS_ACTIVE, STATUS_ARCHIVED = "active", "archived"

# Source labels. Anything the user types later is "manual"; calibration answers get their own (D7).
SRC_EXEMPLAR = "seed_own_line"      # a verbatim own line, kept as a voice example
SRC_EXTRACTED = "seed_own_lines"    # a note derived (by a model) from own lines only
SRC_MANUAL = "manual"

_STOP = set(
    "the a an and or but if is are was were be been to of in on at for with from by as it this that these those "
    "i me my you your we our they them he she his her not no yes can will would could should do did does have has "
    "had just also so than then there here what when where which who how any all some about into out up down over "
    "per let get got hi hey hello thanks thank please pls ok okay".split()
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def overlap(a: str, b: str) -> int:
    """Number of shared meaningful words (build-level relevance score)."""
    return len(_words(a) & _words(b))


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SelfModelStore:
    def __init__(self, engine: Engine | None = None, user_id: str = USER_ID):
        self.engine = engine or db.get_engine()
        self.user_id = user_id

    # ------------------------------------------------------------------ create / read
    def add_item(self, layer: Layer | str, text: str, source: str, source_ref: str | None = None) -> dict:
        try:
            layer = Layer(layer).value
        except ValueError:
            raise SelfModelError(f"Unknown layer '{layer}'. Use voice, decision or fact.") from None
        clean = mask_text((text or "").strip())
        if not clean:
            raise SelfModelError("Item text is empty.")
        if len(clean) > MAX_ITEM_CHARS:
            raise SelfModelError(f"Item text is longer than {MAX_ITEM_CHARS} characters.")
        if not source:
            raise SelfModelError("Every item needs a source.")
        now = _now()
        row = {
            "item_id": "sm_" + uuid.uuid4().hex[:10], "user_id": self.user_id, "layer": layer, "text": clean,
            "source": source, "source_ref": source_ref, "status": STATUS_ACTIVE, "version": 1,
            "created_at": now, "updated_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(insert(db.self_model_items).values(**row))
        return row

    def get(self, item_id: str) -> dict | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(db.self_model_items).where(db.self_model_items.c.item_id == item_id)
                             ).mappings().first()
        return dict(r) if r else None

    def list_items(self, layer: Layer | str | None = None, *, include_archived: bool = False) -> list[dict]:
        t = db.self_model_items
        q = select(t).where(t.c.user_id == self.user_id).order_by(t.c.created_at, t.c.item_id)
        if layer is not None:
            q = q.where(t.c.layer == Layer(layer).value)
        if not include_archived:
            q = q.where(t.c.status == STATUS_ACTIVE)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def count(self, layer: Layer | str | None = None) -> int:
        return len(self.list_items(layer))

    # ------------------------------------------------------------------ edit / remove
    def update_text(self, item_id: str, text: str) -> dict:
        """Change an item's text. Bumps `version` and `updated_at`. (D6b will also write the old text to a
        versions table and log `selfmodel_item_edited`; that is additive and not done here.)"""
        item = self.get(item_id)
        if not item or item["status"] != STATUS_ACTIVE:
            raise SelfModelError(f"No active item {item_id}.")
        clean = mask_text((text or "").strip())
        if not clean or len(clean) > MAX_ITEM_CHARS:
            raise SelfModelError(f"Item text must be 1-{MAX_ITEM_CHARS} characters.")
        t = db.self_model_items
        with self.engine.begin() as conn:
            conn.execute(update(t).where(t.c.item_id == item_id).values(
                text=clean, version=item["version"] + 1, updated_at=_now()))
        return self.get(item_id)

    def archive(self, item_id: str) -> None:
        """Soft-remove (the row stays, so D6b undo can bring it back)."""
        item = self.get(item_id)
        if not item:
            raise SelfModelError(f"No item {item_id}.")
        t = db.self_model_items
        with self.engine.begin() as conn:
            conn.execute(update(t).where(t.c.item_id == item_id).values(
                status=STATUS_ARCHIVED, version=item["version"] + 1, updated_at=_now()))

    def archive_source(self, source_prefix: str) -> int:
        n = 0
        for it in self.list_items():
            if it["source"].startswith(source_prefix):
                self.archive(it["item_id"])
                n += 1
        return n

    def has_text(self, layer: Layer | str, text: str) -> bool:
        key = _norm(mask_text(text) or "")
        return any(_norm(i["text"]) == key for i in self.list_items(layer))

    # ------------------------------------------------------------------ version tag
    def version_tag(self) -> str:
        """Short tag for logging which self-model produced a draft. Changes when any active item or its
        version changes. D6b replaces this with real version numbers."""
        parts = sorted(f"{i['item_id']}:{i['version']}" for i in self.list_items())
        return "sm-0.1-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]

    # ------------------------------------------------------------------ retrieval
    def retrieve(self, masked_incoming: str, *, max_voice: int = 10, max_other: int = 6,
                 send_all_up_to: int = 12) -> dict[str, list[dict]]:
        """Pick the items to show the drafter. Each returned item has an added `score` (word overlap).

        - voice: style notes first (all, up to max_voice), then own-line examples ranked by overlap.
        - decision / fact: if the layer has <= send_all_up_to items, all of them (the model decides which
          apply); otherwise the top max_other by overlap with the message, overlap > 0 only.
        """
        out: dict[str, list[dict]] = {"voice": [], "decision": [], "fact": []}
        for layer in ("voice", "decision", "fact"):
            items = self.list_items(layer)
            for it in items:
                it["score"] = overlap(masked_incoming, it["text"])
            if layer == "voice":
                notes = [i for i in items if i["source"] != SRC_EXEMPLAR]
                ex = sorted((i for i in items if i["source"] == SRC_EXEMPLAR), key=lambda i: -i["score"])
                out[layer] = (notes + ex)[:max_voice]
            elif len(items) <= send_all_up_to:
                out[layer] = items
            else:
                ranked = sorted((i for i in items if i["score"] > 0), key=lambda i: -i["score"])
                out[layer] = ranked[:max_other]
        return out
