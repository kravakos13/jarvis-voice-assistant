"""Sync-ready SQLite schema helpers: every row gets a stable uuid + updated_at (UTC),
deletions leave tombstones. Implemented with triggers, so the regular CRUD code stays simple."""
from __future__ import annotations

import sqlite3

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def ensure_sync_schema(conn: sqlite3.Connection, table: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "uuid" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN uuid TEXT")
    if "updated_at" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN updated_at TEXT")
    conn.execute(f"UPDATE {table} SET uuid=lower(hex(randomblob(16))) WHERE uuid IS NULL")
    conn.execute(f"UPDATE {table} SET updated_at={NOW} WHERE updated_at IS NULL")
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_uuid ON {table}(uuid)")
    conn.execute("CREATE TABLE IF NOT EXISTS tombstones(tbl TEXT NOT NULL, uuid TEXT NOT NULL, deleted_at TEXT NOT NULL, "
                 "PRIMARY KEY(tbl, uuid))")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_ins AFTER INSERT ON {table}
        WHEN NEW.uuid IS NULL OR NEW.updated_at IS NULL BEGIN
        UPDATE {table} SET uuid=COALESCE(NEW.uuid, lower(hex(randomblob(16)))), updated_at=COALESCE(NEW.updated_at, {NOW})
        WHERE rowid=NEW.rowid; END""")
    # local edits bump updated_at; the sync engine sets updated_at explicitly (then the trigger stays quiet)
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_upd AFTER UPDATE ON {table}
        WHEN NEW.updated_at IS OLD.updated_at BEGIN
        UPDATE {table} SET updated_at={NOW} WHERE rowid=NEW.rowid; END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_del AFTER DELETE ON {table}
        WHEN OLD.uuid IS NOT NULL BEGIN
        INSERT OR REPLACE INTO tombstones(tbl, uuid, deleted_at) VALUES('{table}', OLD.uuid, {NOW}); END""")
    conn.commit()
