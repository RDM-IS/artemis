"""Phase 1 schema validation — 15 checks against the live acos schema.

Reads credentials from AWS Secrets Manager. Never uses plaintext passwords.

Required env vars:
    RDS_SECRET_ARN  — ARN of the RDS secret in Secrets Manager
    RDS_HOST        — RDS endpoint hostname
    RDS_DB          — database name (default: crm)

Run: RDS_SECRET_ARN=arn:... RDS_HOST=... python tests/test_phase1_schema.py
"""

import json
import os
import sys

import boto3
import psycopg2
import psycopg2.extras

# Add parent dir so we can import knowledge
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_conn():
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


def table_exists(cur, schema, table):
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return cur.fetchone() is not None


def view_exists(cur, schema, view):
    cur.execute(
        "SELECT 1 FROM information_schema.views WHERE table_schema=%s AND table_name=%s",
        (schema, view),
    )
    return cur.fetchone() is not None


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name}{' — ' + detail if detail else ''}")
        failed += 1


def main():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("\n=== Phase 1 Schema Validation ===\n")

    # 1. All 8 acos tables exist
    expected_tables = [
        "entities", "relationships", "osint_signals",
        "data_vault_satellites", "audit_log", "velocity_ledger",
        "circuit_breaker_status", "guardrail_violations",
    ]
    all_exist = True
    missing = []
    for t in expected_tables:
        if not table_exists(cur, "acos", t):
            all_exist = False
            missing.append(t)
    check("1. All 8 acos tables exist", all_exist, f"missing: {missing}" if missing else "")

    # 2. schema_migrations table exists
    check("2. acos.schema_migrations exists", table_exists(cur, "acos", "schema_migrations"))

    # 3. v_gold_contacts view exists and is queryable
    v_exists = view_exists(cur, "acos", "v_gold_contacts")
    if v_exists:
        try:
            cur.execute("SELECT * FROM acos.v_gold_contacts LIMIT 1")
            check("3. v_gold_contacts view queryable", True)
        except Exception as e:
            conn.rollback()
            check("3. v_gold_contacts view queryable", False, str(e))
    else:
        check("3. v_gold_contacts view exists", False, "view not found")

    # 4. Brian Pivar: gold, confidence 1.0, osint_source null
    cur.execute(
        "SELECT * FROM acos.entities WHERE name = 'Brian Pivar' AND entity_type = 'Person'"
    )
    pivar = cur.fetchone()
    check(
        "4. Brian Pivar entity: gold, confidence=1.0, osint_source=null",
        pivar and pivar["layer"] == "gold" and pivar["confidence"] == 1.0 and pivar["osint_source"] is None,
        f"got: {dict(pivar) if pivar else 'NOT FOUND'}",
    )

    # 5. TTI: gold, confidence 1.0
    cur.execute(
        "SELECT * FROM acos.entities WHERE name = 'TTI (Techtronic Industries)' AND entity_type = 'Organization'"
    )
    tti = cur.fetchone()
    check(
        "5. TTI entity: gold, confidence=1.0",
        tti and tti["layer"] == "gold" and tti["confidence"] == 1.0,
        f"got: {dict(tti) if tti else 'NOT FOUND'}",
    )

    # 6. Lucint Pilot TTI: gold, domain=lucint
    cur.execute(
        "SELECT * FROM acos.entities WHERE name = 'Lucint Pilot TTI' AND entity_type = 'Project'"
    )
    lucint = cur.fetchone()
    check(
        "6. Lucint Pilot TTI: gold, domain=lucint",
        lucint and lucint["layer"] == "gold" and lucint["domain"] == "lucint",
        f"got: {dict(lucint) if lucint else 'NOT FOUND'}",
    )

    # 7. Bradley Spaits: gold, tags contain 'mentor'
    cur.execute(
        "SELECT * FROM acos.entities WHERE name = 'Bradley Spaits' AND entity_type = 'Person'"
    )
    spaits = cur.fetchone()
    check(
        "7. Bradley Spaits: gold, tags contain 'mentor'",
        spaits and spaits["layer"] == "gold" and "mentor" in (spaits["tags"] or []),
        f"got: {dict(spaits) if spaits else 'NOT FOUND'}",
    )

    # 8. Brian Pivar → TTI relationship with non-empty context
    if pivar and tti:
        cur.execute(
            """SELECT * FROM acos.relationships
               WHERE source_entity_id = %s AND target_entity_id = %s""",
            (pivar["id"], tti["id"]),
        )
        rel = cur.fetchone()
        check(
            "8. Pivar → TTI relationship exists with context",
            rel and rel["relationship_context"] and len(rel["relationship_context"].strip()) > 0,
            f"got: {dict(rel) if rel else 'NOT FOUND'}",
        )
    else:
        check("8. Pivar → TTI relationship", False, "missing entities")

    # 9. Brian Pivar → Lucint Pilot relationship with non-empty context
    if pivar and lucint:
        cur.execute(
            """SELECT * FROM acos.relationships
               WHERE source_entity_id = %s AND target_entity_id = %s""",
            (pivar["id"], lucint["id"]),
        )
        rel = cur.fetchone()
        check(
            "9. Pivar → Lucint Pilot relationship exists with context",
            rel and rel["relationship_context"] and len(rel["relationship_context"].strip()) > 0,
            f"got: {dict(rel) if rel else 'NOT FOUND'}",
        )
    else:
        check("9. Pivar → Lucint Pilot relationship", False, "missing entities")

    # 10. crm tables NOT in acos schema
    crm_in_acos = False
    for t in ["contacts", "organizations", "deals"]:
        if table_exists(cur, "acos", t):
            crm_in_acos = True
    check("10. CRM tables not in acos schema", not crm_in_acos)

    # 11. promote_entity blocks silver→gold without ryan_confirmed
    from knowledge.db import promote_entity, PromotionBlockedError
    try:
        # Create a test entity at silver layer
        cur.execute(
            """INSERT INTO acos.entities (entity_type, name, layer, confidence)
               VALUES ('Person', '__test_promote__', 'silver', 0.5)
               RETURNING id"""
        )
        test_id = str(cur.fetchone()["id"])
        conn.commit()

        blocked = False
        try:
            promote_entity(test_id, "gold", ryan_confirmed=False)
        except PromotionBlockedError:
            blocked = True
        check("11. promote_entity blocks silver→gold without ryan_confirmed", blocked)

        # Cleanup
        cur.execute("DELETE FROM acos.entities WHERE id = %s", (test_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        check("11. promote_entity blocks silver→gold", False, str(e))

    # 12. create_relationship raises ValueError if context empty
    from knowledge.db import create_relationship
    raised = False
    try:
        create_relationship("00000000-0000-0000-0000-000000000000", "00000000-0000-0000-0000-000000000001", "Test", "")
    except ValueError:
        raised = True
    except Exception:
        pass  # FK violation is fine — we care about the ValueError
    check("12. create_relationship raises ValueError on empty context", raised)

    # Also test None
    raised_none = False
    try:
        create_relationship("00000000-0000-0000-0000-000000000000", "00000000-0000-0000-0000-000000000001", "Test", None)
    except ValueError:
        raised_none = True
    except Exception:
        pass
    check("12b. create_relationship raises ValueError on None context", raised_none)

    # 13. circuit_breaker_status exists and is empty
    cur.execute("SELECT count(*) as cnt FROM acos.circuit_breaker_status")
    cb_count = cur.fetchone()["cnt"]
    check("13. circuit_breaker_status exists and is empty", cb_count == 0, f"count={cb_count}")

    # 14. velocity_ledger exists and is queryable
    try:
        cur.execute("SELECT count(*) FROM acos.velocity_ledger")
        check("14. velocity_ledger queryable", True)
    except Exception as e:
        conn.rollback()
        check("14. velocity_ledger queryable", False, str(e))

    # 15. sensitive satellite_type cannot be crm_syncable=true
    blocked_insert = False
    try:
        cur.execute(
            """INSERT INTO acos.data_vault_satellites
               (entity_id, satellite_type, crm_syncable, content)
               VALUES (
                   (SELECT id FROM acos.entities LIMIT 1),
                   'sensitive', true, 'test'
               )"""
        )
        conn.commit()
        # If we got here, the constraint didn't fire — FAIL
        # Clean up the bad row
        cur.execute("DELETE FROM acos.data_vault_satellites WHERE content = 'test' AND satellite_type = 'sensitive'")
        conn.commit()
    except psycopg2.errors.CheckViolation:
        blocked_insert = True
        conn.rollback()
    except Exception as e:
        conn.rollback()
        # Some other error — still counts as blocked
        if "chk_sensitive_not_syncable" in str(e):
            blocked_insert = True
    check("15. Sensitive satellite blocked from crm_syncable=true", blocked_insert)

    cur.close()
    conn.close()

    total = passed + failed
    print(f"\n{'='*40}")
    print(f"Results: {passed}/{total} passed")
    if failed > 0:
        print(f"         {failed} FAILED")
    print()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
