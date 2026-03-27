"""Migration runner — applies numbered SQL files to the acos schema.

Reads credentials from AWS Secrets Manager. Never uses plaintext passwords.

Required env vars:
    RDS_SECRET_ARN  — ARN of the RDS secret in Secrets Manager
    RDS_HOST        — RDS endpoint hostname
    RDS_DB          — database name (default: crm)

Usage:
    RDS_SECRET_ARN=arn:... RDS_HOST=... python migrations/run_migrations.py
"""

import glob
import json
import os
import sys

import boto3
import psycopg2


def get_connection():
    secret_arn = os.environ.get("RDS_SECRET_ARN")
    host = os.environ.get("RDS_HOST")
    db = os.environ.get("RDS_DB", "crm")

    if not secret_arn:
        print("ERROR: RDS_SECRET_ARN not set")
        sys.exit(1)
    if not host:
        print("ERROR: RDS_HOST not set")
        sys.exit(1)

    try:
        client = boto3.client("secretsmanager", region_name="us-east-1")
        response = client.get_secret_value(SecretId=secret_arn)
        creds = json.loads(response["SecretString"])
    except Exception as e:
        print(f"ERROR: Failed to fetch credentials from Secrets Manager: {e}")
        sys.exit(1)

    return psycopg2.connect(
        host=host,
        port=5432,
        dbname=db,
        user=creds["username"],
        password=creds["password"],
        connect_timeout=10,
    )


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


def get_applied(conn) -> set:
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