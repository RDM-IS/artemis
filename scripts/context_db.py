"""Dump live schema + current-week health.plan into the context snapshot.
Uses knowledge.db helpers (execute_query). Read-only."""
import sys
try:
    from knowledge.db import execute_query
except Exception as e:
    print(f"- (could not import knowledge.db: {e})"); sys.exit(0)

def main():
    schemas = execute_query("""
        select table_schema, table_name
        from information_schema.tables
        where table_schema in ('public','acos','health')
        order by table_schema, table_name
    """)
    cur = None
    for r in schemas:
        s = r["table_schema"] if isinstance(r, dict) else r[0]
        t = r["table_name"] if isinstance(r, dict) else r[1]
        if s != cur:
            print(f"\n### schema `{s}`"); cur = s
        print(f"- {t}")
    print("\n### health.plan — next 3 days")
    rows = execute_query("""
        select plan_date, session_type
        from health.plan
        where plan_date >= current_date
        order by plan_date limit 3
    """)
    for r in rows:
        d = r["plan_date"] if isinstance(r, dict) else r[0]
        st = r["session_type"] if isinstance(r, dict) else r[1]
        print(f"- {d}: {st}")

if __name__ == "__main__":
    main()
