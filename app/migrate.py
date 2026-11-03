"""Create tables. Run on every deploy (Fly release_command): python -m app.migrate"""
from app import db

if __name__ == "__main__":
    db.init_db()
    print("tables ready:", ", ".join(sorted(db.metadata.tables)))
