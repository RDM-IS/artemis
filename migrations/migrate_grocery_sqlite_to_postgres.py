"""One-time data migration: SQLite grocery_list -> acos.grocery_list (Postgres).

Copies every existing row from the SQLite `grocery_list` table (the legacy
life_ops backend) into `acos.grocery_list`. Idempotent: a row already present
(matched on item + added_at) is skipped, so re-running is safe.

Run this AFTER migrations/017_grocery_to_acos.sql has created the Postgres table.

Reads credentials from AWS Secrets Manager via knowledge.secrets.

Required env vars:
  RDS_SECRET_ARN  — ARN of the RDS secret in Secrets Manager
  RDS_HOST        — RDS endpoint hostname
  RDS_DB          — database name (default: crm)
  SQLITE_DB_PATH  — path to artemis.db (default: artemis.db)

Usage:
    RDS_SECRET_ARN=arn:... RDS_HOST=... python migrations/migrate_grocery_sqlite_to_postgres.py
"""

import os
import sqlite3
import sys

import psycopg2

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowledge.secrets import get_rds_credentials


def get_pg():
    host = os.environ.get("RDS_HOST")
    db = os.environ.get("RDS_DB", "crm")

    if not host:
        print("ERROR: RDS_HOST not set")
        sys.exit(1)

    try:
        creds = get_rds_credentials()
    except Exception as e:
        print(f"ERROR: Failed to get RDS credentials: {e}")
        sys.exit(1)

    return psycopg2.connect(
        host=host,
        port=5432,
        dbname=db,
        user=creds["username"],
        password=creds["password"],
        connect_timeout=10,
    )


def get_sqlite():
    path = os.environ.get("SQLITE_DB_PATH", "artemis.db")
    if not os.path.exists(path):
        print(f"ERROR: SQLite database not found at {path}")
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists_sqlite(conn, name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def table_exists_pg(conn, schema: str, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
            (schema, name),
        )
        return cur.fetchone() is not None


def _to_bool(val) -> bool:
    """SQLite stored is_purchased as INTEGER 0/1 (or possibly NULL)."""
    return bool(val)


def migrate_grocery_list(sqlite_conn, pg_conn):
    """Copy grocery_list from SQLite into acos.grocery_list."""
    if not table_exists_sqlite(sqlite_conn, "grocery_list"):
        print("  [SKIP] grocery_list table not found in SQLite")
        return

    if not table_exists_pg(pg_conn, "acos", "grocery_list"):
        print("  [SKIP] acos.grocery_list table not found in PostgreSQL "
              "(run 017_grocery_to_acos.sql first)")
        return

    rows = sqlite_conn.execute("SELECT * FROM grocery_list").fetchall()
    print(f"  SQLite grocery_list: {len(rows)} rows")

    inserted = 0
    skipped = 0
    for row in rows:
        r = dict(row)
        with pg_conn.cursor() as cur:
            # Idempotency: skip if an identical (item, added_at) row already exists.
            cur.execute(
                "SELECT 1 FROM acos.grocery_list WHERE item = %s AND added_at = %s",
                (r.get("item", ""), r.get("added_at")),
            )
            if cur.fetchone():
                skipped += 1
                continue

            try:
                cur.execute(
                    """INSERT INTO acos.grocery_list
                       (item, category, quantity, store, added_at,
                        purchased_at, is_purchased, notes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        r.get("item", ""),
                        r.get("category"),
                        r.get("quantity"),
                        r.get("store"),
                        r.get("added_at"),
                        r.get("purchased_at"),
                        _to_bool(r.get("is_purchased")),
                        r.get("notes"),
                    ),
                )
                inserted += 1
            except Exception as e:
                pg_conn.rollback()
                print(f"  [WARN] Failed to insert grocery row {r.get('item')!r}: {e}")
                skipped += 1
                continue

    pg_conn.commit()
    print(f"  Grocery list: {inserted} inserted, {skipped} skipped")


def main():
    sqlite_conn = get_sqlite()
    pg_conn = get_pg()

    print("Migrating SQLite grocery_list -> acos.grocery_list...\n")
    migrate_grocery_list(sqlite_conn, pg_conn)

    sqlite_conn.close()
    pg_conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
