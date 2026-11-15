"""Data controls for Braj (decisions 5.14 pt 7, 5.24 pt 1). Runs against DATABASE_URL (Neon) from your laptop.

  python -m scripts.data_controls summary                      # row counts; how many tag labels are readable
  python -m scripts.data_controls export --out my-data.json    # Export my data (includes sender-tag labels)
  python -m scripts.data_controls delete --confirm "delete everything"
  python -m scripts.data_controls rewrap-tags                  # after setting / rotating DATA_ENCRYPTION_KEY

The laptop's .env must hold the SAME DATA_ENCRYPTION_KEY (and APP_SECRET) as Render, or tag labels show as (unreadable).
"""
import argparse
import json
import sys

from sqlalchemy import select

from app import db
from app.config import get_settings
from app.privacy import data_controls
from app.privacy.crypto import decrypt_text, has_dedicated_key


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts.data_controls", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    e = sub.add_parser("export")
    e.add_argument("--out", required=True, help="file to write (JSON)")
    d = sub.add_parser("delete")
    d.add_argument("--confirm", default="", help='type exactly: delete everything')
    sub.add_parser("rewrap-tags")
    args = p.parse_args(argv)

    engine, settings = db.get_engine(), get_settings()
    if args.cmd == "summary":
        for name, n in data_controls.counts(engine).items():
            print(f"{name:18} {n}")
        with engine.connect() as conn:
            encs = [r[0] for r in conn.execute(select(db.sender_tags.c.label_enc)).all()]
        ok = sum(1 for x in encs if decrypt_text(x, settings))
        print(f"tag labels readable with this key: {ok} of {len(encs)}")
        print("dedicated DATA_ENCRYPTION_KEY set:", "yes" if has_dedicated_key(settings) else "no (using the APP_SECRET fallback)")
        return 0
    if args.cmd == "export":
        data = data_controls.export_my_data(engine, settings)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        print(f"Wrote {args.out}: " + ", ".join(f"{k} {v}" for k, v in data["counts"].items()))
        print("This file holds your data (messages are masked; sender-tag labels are readable). Keep it private.")
        return 0
    if args.cmd == "delete":
        try:
            deleted = data_controls.delete_everything(engine, args.confirm, settings)
        except ValueError as err:
            print(err)
            return 2
        print("Deleted: " + ", ".join(f"{k} {v}" for k, v in deleted.items()))
        return 0
    print(data_controls.rewrap_tags(engine, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
