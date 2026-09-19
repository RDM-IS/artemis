"""SCHEMA-DRIFT — the migrations on disk vs what RDS has applied.

Read-only. Compares migrations/*.sql (by file name, as run_migrations.py
records them) with acos.schema_migrations:

  unapplied   a file on the box that RDS never ran — a bare `git pull` or a
              merge deployed without scripts/deploy.sh (016/017 sat like this
              for weeks).
  unknown     a name RDS applied that isn't on disk — a deleted or renamed file,
              or the box running an older checkout than the database.

The Monday update check posts only when either list is non-empty. A migration
merged on GitHub but not yet pulled is the existing "update available" post.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def on_disk(directory: Path = MIGRATIONS_DIR) -> list[str]:
    return sorted(p.name for p in directory.glob("*.sql"))


def applied() -> set[str]:
    from knowledge.db import execute_query
    return {r["migration_name"] for r in execute_query(
        "SELECT migration_name FROM acos.schema_migrations")}


def compare(disk: list[str], done: set[str]) -> dict[str, list[str]]:
    return {"unapplied": [m for m in sorted(disk) if m not in done],
            "unknown": sorted(done - set(disk))}


def message(drift: dict[str, list[str]]) -> str | None:
    """The ops post, or None when there is no drift."""
    lines = []
    if drift["unapplied"]:
        lines.append(f"{len(drift['unapplied'])} migration(s) on the box not applied to RDS: "
                     + ", ".join(drift["unapplied"])
                     + ". Deploy with `bash scripts/deploy.sh` (it migrates first).")
    if drift["unknown"]:
        lines.append(f"{len(drift['unknown'])} applied migration(s) not in the repo: "
                     + ", ".join(drift["unknown"]) + ".")
    return ("⚠️ Schema drift — " + " ".join(lines)) if lines else None


def check() -> str | None:
    return message(compare(on_disk(), applied()))
