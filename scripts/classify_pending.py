"""Classify edited messages that were captured but not classified (model was down, free-tier limit ...).

    python -m scripts.classify_pending            # uses DATABASE_URL, LLM_PROVIDER / LLM_MODEL and OWN_DATA_ONLY_MODE from .env

Safe to run any time: already-classified messages are skipped, one `edit_classified` event per message at most.
Rows made under the guarded (third-party) path need a verified provider; they are left alone otherwise.
"""
from app import db
from app.classifier.classifier import classify_pending
from app.config import get_settings
from app.events import EventWriter


def main() -> int:
    engine = db.get_engine()
    counts = classify_pending(engine, EventWriter(engine), settings=get_settings())
    print(counts or "Nothing to classify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
