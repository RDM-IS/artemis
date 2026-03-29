"""Migrate SQLite data to PostgreSQL (acos + public schemas).

Migrates:
  commitments        → public.commitments (if table exists)
  guardrail_violations → acos.guardrail_violations

Reads credentials from AWS Secrets Manager via knowledge.secrets.

Required env vars:
  RDS_SECRET_ARN  — ARN of the RDS secret in Secrets Manager
  RDS_HOST        — RDS endpoint hostname
  RDS_DB          — database name (default: crm)
  SQLITE_DB_PATH  — path to artemis.db

Usage:
    RDS_SECRET_ARN=arn:... RDS_HOST=... python migrations/migrate_sqlite_to_postgres.py
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


def migrate_commitments(sqlite_conn, pg_conn):
    """Migrate commitments from SQLite to public.commitments."""
    if not table_exists_sqlite(sqlite_conn, "commitments"):
        print("  [SKIP] commitments table not found in SQLite")
        return

    if not table_exists_pg(pg_conn, "public", "commitments"):
        print("  [SKIP] public.commitments table not found in PostgreSQL")
        return

    rows = sqlite_conn.execute("SELECT * FROM commitments").fetchall()
    print(f"  SQLite commitments: {len(rows)} rows")

    inserted = 0
    skipped = 0
    for row in rows:
        r = dict(row)
        with pg_conn.cursor() as cur:
            # Check for duplicate by title + created_at
            cur.execute(
                "SELECT 1 FROM public.commitments WHERE description = %s",
                (r.get("title", ""),),
            )
            if cur.fetchone():
                skipped += 1
                continue

            try:
                cur.execute(
                    """INSERT INTO public.commitments (description, due_date, status, created_at)
                       VALUES (%s, %s, %s, %s)""",
                    (
                        r.get("title", ""),
                        r.get("due_date"),
                        "open" if r.get("status") == "active" else r.get("status", "open"),
                        r.get("created_at"),
                    ),
                )
                inserted += 1
            except Exception as e:
                pg_conn.rollback()
                print(f"  [WARN] Failed to insert commitment: {e}")
                skipped += 1
                continue

    pg_conn.commit()
    print(f"  Commitments: {inserted} inserted, {skipped} skipped")


def migrate_guardrail_violations(sqlite_conn, pg_conn):
    """Migrate guardrail_violations from SQLite to acos.guardrail_violations."""
    if not table_exists_sqlite(sqlite_conn, "guardrail_violations"):
        print("  [SKIP] guardrail_violations table not found in SQLite")
        return

    if not table_exists_pg(pg_conn, "acos", "guardrail_violations"):
        print("  [SKIP] acos.guardrail_violations table not found in PostgreSQL")
        return

    rows = sqlite_conn.execute("SELECT * FROM guardrail_violations").fetchall()
    print(f"  SQLite guardrail_violations: {len(rows)} rows")

    inserted = 0
    skipped = 0
    for row in rows:
        r = dict(row)
        with pg_conn.cursor() as cur:
            # Check for duplicate by timestamp + event_summary
            cur.execute(
                "SELECT 1 FROM acos.guardrail_violations WHERE event_summary = %s AND created_at = %s",
                (r.get("event_summary", ""), r.get("timestamp")),
            )
            if cur.fetchone():
                skipped += 1
                continue

            ext_attendees = r.get("external_attendees", "")
            ext_list = [a.strip() for a in ext_attendees.split(",") if a.strip()] if ext_attendees else []

            try:
                cur.execute(
                    """INSERT INTO acos.guardrail_violations
                       (created_at, guardrail_type, event_summary, external_attendees, outcome)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        r.get("timestamp"),
                        "external_calendar_attendee",
                        r.get("event_summary", ""),
                        ext_list,
                        r.get("outcome", "blocked"),
                    ),
                )
                inserted += 1
            except Exception as e:
                pg_conn.rollback()
                print(f"  [WARN] Failed to insert violation: {e}")
                skipped += 1
                continue

    pg_conn.commit()
    print(f"  Guardrail violations: {inserted} inserted, {skipped} skipped")


def main():
    sqlite_conn = get_sqlite()
    pg_conn = get_pg()

    print("Migrating SQLite → PostgreSQL...\n")
    migrate_commitments(sqlite_conn, pg_conn)
    migrate_guardrail_violations(sqlite_conn, pg_conn)

    sqlite_conn.close()
    pg_conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
