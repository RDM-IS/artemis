"""Migration runner — applies numbered SQL files to the acos schema.

Reads DATABASE_URL from environment. Tracks applied migrations in
acos.schema_migrations. Idempotent — safe to run multiple times.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/db python migrations/run_migrations.py
"""

import glob
import os
import sys

import psycopg2


def get_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)
    return psycopg2.connect(url)


def ensure_migration_table(conn):
    """Create the schema and migrations table if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS acos")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS acos.schema_migrations (
                migration_name VARCHAR(255) PRIMARY KEY,
                applied_at     TIMESTAMPTZ DEFAULT now()
            )
        """)
    conn.commit()


def get_applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT migration_name FROM acos.schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migration(conn, name: str, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO acos.schema_migrations (migration_name) VALUES (%s)",
            (name,),
        )
    conn.commit()


def main():
    migrations_dir = os.path.dirname(os.path.abspath(__file__))
    sql_files = sorted(glob.glob(os.path.join(migrations_dir, "*.sql")))

    if not sql_files:
        print("No SQL files found in", migrations_dir)
        return

    conn = get_connection()
    ensure_migration_table(conn)
    applied = get_applied(conn)

    for path in sql_files:
        name = os.path.basename(path)
        if name in applied:
            print(f"  [SKIPPED] {name}")
        else:
            print(f"  [APPLYING] {name} ... ", end="", flush=True)
            try:
                sql = open(path).read()
                apply_migration(conn, name, sql)
                print("OK")
            except Exception as e:
                conn.rollback()
                print(f"FAILED: {e}")
                sys.exit(1)

    conn.close()
    print("\nMigrations complete.")


if __name__ == "__main__":
    main()
