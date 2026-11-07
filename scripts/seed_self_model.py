"""Seed the self-model from your OWN sent messages (D6a).

    python -m scripts.seed_self_model seed/own_lines.txt            # examples + model-written notes
    python -m scripts.seed_self_model seed/own_lines.txt --no-llm   # verbatim examples only
    python -m scripts.seed_self_model seed/own_lines.txt --replace  # archive the earlier seed first
    python -m scripts.seed_self_model --list                        # show what is stored
    python -m scripts.seed_self_model --archive sm_xxx sm_yyy       # remove items (soft, stays in the table)
    python -m scripts.seed_self_model --edit sm_xxx "new text"      # rewrite one item
    python -m scripts.seed_self_model --add decision "text here"    # add one item (voice | decision | fact)

File format: your own messages, one per block, blocks separated by a blank line; lines starting with # are
ignored. Only your lines: a chat export (timestamps with other people's names) is refused.
Uses DATABASE_URL (point it at Neon to seed production) and, unless --no-llm, LLM_PROVIDER / LLM_MODEL plus the
provider's key from the environment or .env. Only your own text is sent to the model.
"""
import argparse
import sys
from pathlib import Path

from app import db
from app.llm.base import LLMError, get_llm
from app.selfmodel.seed import parse_own_lines, seed_self_model
from app.selfmodel.store import SRC_MANUAL, SelfModelError, SelfModelStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="text file with your own sent messages")
    ap.add_argument("--no-llm", action="store_true", help="store verbatim examples only; no model call")
    ap.add_argument("--replace", action="store_true", help="archive earlier seed items first")
    ap.add_argument("--list", action="store_true", help="print the stored self-model and exit")
    ap.add_argument("--archive", nargs="+", metavar="ID", help="archive these item ids")
    ap.add_argument("--edit", nargs=2, metavar=("ID", "TEXT"), help="replace one item's text")
    ap.add_argument("--add", nargs=2, metavar=("LAYER", "TEXT"), help="add one item by hand")
    a = ap.parse_args(argv)

    engine = db.get_engine()
    db.init_db(engine)
    store = SelfModelStore(engine)

    if a.archive or a.edit or a.add:
        try:
            for item_id in a.archive or []:
                store.archive(item_id)
                print(f"archived {item_id}")
            if a.edit:
                print(f"edited {store.update_text(a.edit[0], a.edit[1])['item_id']}")
            if a.add:
                print(f"added {store.add_item(a.add[0], a.add[1], SRC_MANUAL)['item_id']}")
        except SelfModelError as e:
            print(f"Stopped: {e}", file=sys.stderr)
            return 1
        print(f"version tag: {store.version_tag()}")
        return 0

    if a.list or not a.file:
        for layer in ("voice", "decision", "fact"):
            print(f"\n== {layer.upper()} ==")
            for it in store.list_items(layer):
                print(f"{it['item_id']}  [{it['source']}]  {it['text']}")
        print(f"\nversion tag: {store.version_tag()}")
        return 0

    try:
        lines = parse_own_lines(Path(a.file).read_text(encoding="utf-8-sig"))
        llm = None if a.no_llm else get_llm()
        print(seed_self_model(store, lines, llm=llm, source_ref=Path(a.file).name, replace=a.replace))
    except (SelfModelError, LLMError, OSError) as e:
        print(f"Seeding stopped: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
