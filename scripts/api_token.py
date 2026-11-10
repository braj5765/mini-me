"""Create, list and revoke personal API tokens for share-to-draft (Dev 6).

Run from the repo with DATABASE_URL pointing at the database the app uses (Neon for production):

    python -m scripts.api_token create "phone shortcut"
    python -m scripts.api_token list
    python -m scripts.api_token revoke tk_1a2b3c4d5e
    python -m scripts.api_token revoke --all

`create` prints the token ONCE. Only its hash is stored, so a lost token cannot be shown again: revoke it and
create a new one. Keep tokens out of chat, screenshots and git.
"""
import argparse
import sys

from app import db
from app.share import tokens


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="make a new token")
    c.add_argument("name", help="a label, e.g. 'phone shortcut'")
    sub.add_parser("list", help="show all tokens (never the secret)")
    r = sub.add_parser("revoke", help="stop a token from working")
    r.add_argument("token_id", nargs="?", help="e.g. tk_1a2b3c4d5e (see `list`)")
    r.add_argument("--all", action="store_true", help="revoke every live token")
    args = ap.parse_args(argv)

    engine = db.get_engine()
    db.init_db(engine)   # no-op if the tables exist

    if args.cmd == "create":
        token_id, token = tokens.create_token(engine, args.name)
        print(f"Created {token_id} ({args.name}).")
        print("Copy the token now. It is shown only once:\n")
        print(token)
        return 0
    if args.cmd == "list":
        rows = tokens.list_tokens(engine)
        if not rows:
            print("No tokens yet.")
        for t in rows:
            state = f"REVOKED {_fmt(t['revoked_at'])}" if t["revoked_at"] else "active"
            print(f"{t['token_id']}  {t['name']:<24} {t['token_prefix']}...  created {_fmt(t['created_at'])}  "
                  f"last used {_fmt(t['last_used_at'])}  {state}")
        return 0
    if args.all:
        print(f"Revoked {tokens.revoke_all(engine)} token(s).")
        return 0
    if not args.token_id:
        print("Give a token id (see `list`) or use --all.")
        return 2
    if tokens.revoke_token(engine, args.token_id):
        print(f"Revoked {args.token_id}. It stops working on the next request.")
        return 0
    print(f"Nothing to revoke: {args.token_id} does not exist or was already revoked.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
