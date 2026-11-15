"""Create tables and run the one-off data migrations. Run on every deploy: python -m app.migrate"""
from app import db

if __name__ == "__main__":
    result = db.init_db(raise_migration_errors=True)   # creates missing tables, then app.migrations (idempotent)
    print("tables ready:", ", ".join(sorted(db.metadata.tables)))
    print("migrations:", result)
