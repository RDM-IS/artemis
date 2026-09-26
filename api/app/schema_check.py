"""Live acos-schema probe — 15 checks against the running database.

**THIS IS RUNTIME CODE.** It is reachable over HTTP in production, through
`POST /admin/run-tests`, so it lives with the runtime code that exposes it. It
used to live at `tests/test_phase1_schema.py` and the endpoint shelled out to
`/var/task/tests/test_phase1_schema.py` with `sys.executable`, which is why the
Lambda package had to ship all of `tests/` — and why a test-only commit moved the
package identity and made DRIFT-ALARM cry wolf (PACKAGE-IDENTITY, 2026-09-26).

It is also why `tests/test_phase1_schema.py` needed the single TEST-DB-GUARD
exemption: the file was runtime code sitting in `tests/`, connecting to the real
database on purpose. With the probe here, the test beside it is an ordinary
guarded unit test.

Returns DATA, never prints and never calls `sys.exit()`. `format_report()`
renders the same text the script used to write to stdout, so the endpoint's
`stdout` field keeps its shape for anything already parsing it.

It writes: check 11 inserts and deletes a probe entity, and check 15 attempts an
insert it expects a CHECK constraint to refuse. That is deliberate and unchanged
— it is how those two constraints get exercised against the live schema.
"""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extras

#: The eight tables Phase 1 created.
EXPECTED_TABLES = (
    "entities", "relationships", "osint_signals",
    "data_vault_satellites", "audit_log", "velocity_ledger",
    "circuit_breaker_status", "guardrail_violations",
)

_PROBE_NAME = "__test_promote__"
_PROBE_CONTENT = "test"
_NIL_UUID = "00000000-0000-0000-0000-000000000000"
_NIL_UUID_1 = "00000000-0000-0000-0000-000000000001"


class SchemaCheckError(RuntimeError):
    """The probe could not run at all — not "the schema is wrong"."""


def _connect():
    host = os.environ.get("RDS_HOST")
    if not host:
        raise SchemaCheckError("RDS_HOST is not set")
    from knowledge.secrets import get_rds_credentials
    try:
        creds = get_rds_credentials()
    except Exception as exc:
        raise SchemaCheckError(f"could not read the RDS credentials: {exc}") from exc
    return psycopg2.connect(
        host=host, port=5432, dbname=os.environ.get("RDS_DB", "crm"),
        user=creds["username"], password=creds["password"], connect_timeout=10,
    )


def table_exists(cur, schema: str, table: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s", (schema, table))
    return cur.fetchone() is not None


def view_exists(cur, schema: str, view: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.views "
                "WHERE table_schema=%s AND table_name=%s", (schema, view))
    return cur.fetchone() is not None


def run_checks(conn=None) -> dict:
    """Run every check and return the results.

    `conn` is injectable so the test beside this can drive it without a database
    — that is the whole point of one implementation rather than two.

    Returns::

        {"ok": bool, "passed": int, "failed": int, "total": int,
         "checks": [{"name": str, "ok": bool, "detail": str}, ...]}
    """
    close_after = conn is None
    if conn is None:
        conn = _connect()
    checks: list[dict] = []

    def check(name: str, condition, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(condition),
                       "detail": "" if condition else str(detail)})

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        missing = [t for t in EXPECTED_TABLES if not table_exists(cur, "acos", t)]
        check("1. All 8 acos tables exist", not missing,
              f"missing: {missing}" if missing else "")

        check("2. acos.schema_migrations exists", table_exists(cur, "acos", "schema_migrations"))

        if view_exists(cur, "acos", "v_gold_contacts"):
            try:
                cur.execute("SELECT * FROM acos.v_gold_contacts LIMIT 1")
                check("3. v_gold_contacts view queryable", True)
            except Exception as exc:
                conn.rollback()
                check("3. v_gold_contacts view queryable", False, exc)
        else:
            check("3. v_gold_contacts view exists", False, "view not found")

        cur.execute("SELECT * FROM acos.entities "
                    "WHERE name = 'Brian Pivar' AND entity_type = 'Person'")
        pivar = cur.fetchone()
        check("4. Brian Pivar entity: gold, confidence=1.0, osint_source=null",
              pivar and pivar["layer"] == "gold" and pivar["confidence"] == 1.0
              and pivar["osint_source"] is None,
              f"got: {dict(pivar) if pivar else 'NOT FOUND'}")

        cur.execute("SELECT * FROM acos.entities WHERE name = 'TTI (Techtronic Industries)' "
                    "AND entity_type = 'Organization'")
        tti = cur.fetchone()
        check("5. TTI entity: gold, confidence=1.0",
              tti and tti["layer"] == "gold" and tti["confidence"] == 1.0,
              f"got: {dict(tti) if tti else 'NOT FOUND'}")

        cur.execute("SELECT * FROM acos.entities WHERE name = 'Lucint Pilot TTI' "
                    "AND entity_type = 'Project'")
        lucint = cur.fetchone()
        check("6. Lucint Pilot TTI: gold, domain=lucint",
              lucint and lucint["layer"] == "gold" and lucint["domain"] == "lucint",
              f"got: {dict(lucint) if lucint else 'NOT FOUND'}")

        cur.execute("SELECT * FROM acos.entities "
                    "WHERE name = 'Bradley Spaits' AND entity_type = 'Person'")
        spaits = cur.fetchone()
        check("7. Bradley Spaits: gold, tags contain 'mentor'",
              spaits and spaits["layer"] == "gold" and "mentor" in (spaits["tags"] or []),
              f"got: {dict(spaits) if spaits else 'NOT FOUND'}")

        for num, label, other in ((8, "TTI", tti), (9, "Lucint Pilot", lucint)):
            name = f"{num}. Pivar → {label} relationship exists with context"
            if pivar and other:
                cur.execute("SELECT * FROM acos.relationships "
                            "WHERE source_entity_id = %s AND target_entity_id = %s",
                            (pivar["id"], other["id"]))
                rel = cur.fetchone()
                check(name, rel and rel["relationship_context"]
                      and len(rel["relationship_context"].strip()) > 0,
                      f"got: {dict(rel) if rel else 'NOT FOUND'}")
            else:
                check(name, False, "missing entities")

        check("10. CRM tables not in acos schema",
              not any(table_exists(cur, "acos", t)
                      for t in ("contacts", "organizations", "deals")))

        # 11 — writes: insert a silver probe, confirm the promotion gate, delete it.
        from knowledge.db import PromotionBlockedError, promote_entity
        try:
            cur.execute("INSERT INTO acos.entities (entity_type, name, layer, confidence) "
                        "VALUES ('Person', %s, 'silver', 0.5) RETURNING id", (_PROBE_NAME,))
            test_id = str(cur.fetchone()["id"])
            conn.commit()
            blocked = False
            try:
                promote_entity(test_id, "gold", ryan_confirmed=False)
            except PromotionBlockedError:
                blocked = True
            check("11. promote_entity blocks silver→gold without ryan_confirmed", blocked)
            cur.execute("DELETE FROM acos.entities WHERE id = %s", (test_id,))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            check("11. promote_entity blocks silver→gold", False, exc)

        from knowledge.db import create_relationship
        for suffix, context in (("", ""), ("b", None)):
            raised = False
            try:
                create_relationship(_NIL_UUID, _NIL_UUID_1, "Test", context)
            except ValueError:
                raised = True
            except Exception:
                pass          # an FK violation is fine — the ValueError is the point
            check(f"12{suffix}. create_relationship raises ValueError on "
                  f"{'empty' if context == '' else 'None'} context", raised)

        cur.execute("SELECT count(*) as cnt FROM acos.circuit_breaker_status")
        cb_count = cur.fetchone()["cnt"]
        check("13. circuit_breaker_status exists and is empty", cb_count == 0,
              f"count={cb_count}")

        try:
            cur.execute("SELECT count(*) FROM acos.velocity_ledger")
            check("14. velocity_ledger queryable", True)
        except Exception as exc:
            conn.rollback()
            check("14. velocity_ledger queryable", False, exc)

        # 15 — attempts an insert the CHECK constraint must refuse.
        blocked_insert = False
        try:
            cur.execute("INSERT INTO acos.data_vault_satellites "
                        "(entity_id, satellite_type, crm_syncable, content) VALUES "
                        "((SELECT id FROM acos.entities LIMIT 1), 'sensitive', true, %s)",
                        (_PROBE_CONTENT,))
            conn.commit()
            cur.execute("DELETE FROM acos.data_vault_satellites "
                        "WHERE content = %s AND satellite_type = 'sensitive'", (_PROBE_CONTENT,))
            conn.commit()
        except psycopg2.errors.CheckViolation:
            blocked_insert = True
            conn.rollback()
        except Exception as exc:
            conn.rollback()
            if "chk_sensitive_not_syncable" in str(exc):
                blocked_insert = True
        check("15. Sensitive satellite blocked from crm_syncable=true", blocked_insert)

        cur.close()
    finally:
        if close_after:
            conn.close()

    passed = sum(1 for c in checks if c["ok"])
    failed = len(checks) - passed
    return {"ok": failed == 0, "passed": passed, "failed": failed,
            "total": len(checks), "checks": checks}


def format_report(result: dict) -> str:
    """The text the script used to print, so `stdout` keeps its shape."""
    lines = ["", "=== Phase 1 Schema Validation ===", ""]
    for c in result["checks"]:
        lines.append(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}"
                     + (f" — {c['detail']}" if not c["ok"] and c["detail"] else ""))
    lines += ["", "=" * 40, f"Results: {result['passed']}/{result['total']} passed"]
    if result["failed"]:
        lines.append(f"         {result['failed']} FAILED")
    return "\n".join(lines) + "\n"
