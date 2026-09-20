"""LAMBDA-DRIFT option C — the deploy-ordering preflight for api/deploy.sh.

Refuses a Lambda deploy whose code still names a column that a PENDING
migration drops or renames. That is exactly the 2026-09-20 outage: migration
039 dropped `health.plan.status`, the deploying code still listed `status` in
`_plan_days`' SELECT, the deploy went out, the migration ran, and `/plan` and
`/overview` returned 500 for seven minutes.

Why a smoke test cannot do this job: it runs while the column still exists, so
it passes for the wrong reason. Hence COLUMN-GREP in CLAUDE.md, and hence this.

What it does:
  * reads acos.schema_migrations (via `ssh rdmis` — the Mac cannot reach RDS);
  * parses every migrations/*.sql that has NOT been applied for DROP COLUMN
    and RENAME COLUMN, keeping the table each belongs to;
  * greps the code the Lambda actually ships (api/app, knowledge) for those
    column names.

A hit inside a SQL statement that also names the table is BLOCKING — that is
a real reference and it will 500. A hit anywhere else (an HTTP status, a dict
key, a docstring) is ADVISORY: printed, not fatal. `status` is a common word,
and a gate that cries wolf only teaches people to --force past it.

Reads only. It changes nothing and deploys nothing; it exits non-zero and
`set -e` in api/deploy.sh stops the deploy.

    python3 scripts/lambda_preflight.py
    python3 scripts/lambda_preflight.py --force "<reason>"      # logged
    python3 scripts/lambda_preflight.py --applied-from <json>   # tests
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO / "migrations"
# What the Lambda actually ships: api/deploy.sh zips api/app and ../knowledge.
CODE_ROOTS = ("api/app", "knowledge")
# The box is the only host that can reach RDS.
SSH_HOST = "rdmis"
# A string literal counts as SQL when it reads like a statement. Anything
# looser matches docstrings; anything tighter misses fragments like
# "WHERE plan_date = %s AND status <> 'planned'".
SQL_SHAPE = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|WHERE|"
                       r"SET|RETURNING|ORDER\s+BY|JOIN)\b", re.I)

ALTER_RE = re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w.\"]*)", re.I)
DROP_RE = re.compile(r"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?\"?([A-Za-z_]\w*)\"?", re.I)
RENAME_RE = re.compile(
    r"\bRENAME\s+COLUMN\s+\"?([A-Za-z_]\w*)\"?\s+TO\s+\"?([A-Za-z_]\w*)\"?", re.I)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def migration_files(repo: Path = REPO) -> list[Path]:
    return sorted(p for p in (repo / "migrations").glob("*.sql"))


def applied_migrations(applied_from: str | None = None) -> set[str] | None:
    """Migration names in acos.schema_migrations (basenames, as the runner
    stores them). None when they cannot be read — the caller fails closed."""
    if applied_from:
        return set(json.loads(Path(applied_from).read_text()))
    remote = (
        "cd ~/artemis && set -a; [ -f .env ] && . ./.env; set +a; "
        "PYTHONPATH=$PWD /usr/bin/python3.11 -c "
        "'import json; from knowledge.db import execute_query as q; "
        "print(json.dumps([r[\"migration_name\"] for r in "
        "q(\"select migration_name from acos.schema_migrations\", ())]))'"
    )
    try:
        out = subprocess.run(["ssh", SSH_HOST, remote],
                             capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return None
        return set(json.loads(out.stdout.strip().splitlines()[-1]))
    except Exception:
        return None


def pending_column_changes(applied: set[str], repo: Path = REPO) -> list[dict]:
    """Columns dropped or renamed by migrations that have NOT run yet, each
    with the table it belongs to."""
    changes: list[dict] = []
    for path in migration_files(repo):
        if path.name in applied:
            continue
        for stmt in _strip_comments(path.read_text()).split(";"):
            table = ALTER_RE.search(stmt)
            table = table.group(1).replace('"', "") if table else None
            for col in DROP_RE.findall(stmt):
                changes.append({"migration": path.name, "action": "drops",
                                "column": col, "table": table})
            for old, new in RENAME_RE.findall(stmt):
                changes.append({"migration": path.name, "action": "renames",
                                "column": old, "to": new, "table": table})
    return changes


def _table_res(table: str | None) -> list[re.Pattern]:
    """How this table is written inside SQL.

    A qualified `health.plan` is unambiguous. A bare `plan` is not — the word
    is everywhere in this codebase — so it only counts after a clause keyword
    that can introduce a table.
    """
    if not table:
        return []
    bare = table.split(".")[-1]
    out = [re.compile(rf"\b(?:FROM|JOIN|UPDATE|INTO|TABLE)\s+{re.escape(bare)}\b", re.I)]
    if "." in table:
        out.append(re.compile(rf"\b{re.escape(table)}\b", re.I))
    return out


def sql_literals(source: str) -> list[tuple[int, str]]:
    """(line, text) for every string literal in `source` that looks like SQL.

    Limitation, stated rather than hidden: SQL assembled by concatenating
    fragments across literals is only seen a fragment at a time. Everything in
    api/app and knowledge writes whole statements in one literal.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    # Docstrings are prose, not statements — this module's own docstring lists
    # its routes and would otherwise read as a reference to every column in it.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            first = (node.body or [None])[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and SQL_SHAPE.search(node.value)):
            out.append((node.lineno, node.value))
    return out


def code_hits(column: str, table: str | None,
              repo: Path = REPO) -> tuple[list[str], list[str]]:
    """(blocking, advisory) hits in the SHIPPING code.

    BLOCKING: the column name appears inside a SQL statement that also names
    the table. That is a genuine reference — reading the value and merely
    listing the column in a SELECT both 500 once the column is gone.

    ADVISORY: the name appears anywhere else. `status` is a common word; an
    HTTP status or a dict key is not this column, and blocking on those would
    turn --force into a reflex.
    """
    blocking: list[str] = []
    advisory: list[str] = []
    col_re = re.compile(rf"\b{re.escape(column)}\b")
    table_res = _table_res(table)

    for root in CODE_ROOTS:
        base = repo / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text()
            rel = path.relative_to(repo)
            blocking_lines = set()
            for line, sql in sql_literals(source):
                if not col_re.search(sql) or not any(r.search(sql) for r in table_res):
                    continue
                # Report the line inside the statement, not the literal's start.
                for offset, sql_line in enumerate(sql.splitlines()):
                    if col_re.search(sql_line):
                        blocking_lines.add((line + offset, sql_line.strip()))
            for line, text in sorted(blocking_lines):
                blocking.append(f"{rel}:{line}: {text[:110]}")

            claimed = {n for n, _ in blocking_lines}
            for n, line in enumerate(source.splitlines(), 1):
                if n in claimed or not col_re.search(line):
                    continue
                if line.strip().startswith("#"):
                    continue
                advisory.append(f"{rel}:{n}: {line.strip()[:110]}")
    return blocking, advisory


def check(applied_from: str | None = None, repo: Path = REPO) -> tuple[int, list[str]]:
    """(exit code, report lines). 0 = safe to deploy, 1 = refuse, 2 = abort."""
    out: list[str] = []
    applied = applied_migrations(applied_from)
    if applied is None:
        return 2, ["[preflight] ABORT: could not read acos.schema_migrations "
                   f"(ssh {SSH_HOST}). Failing closed rather than assuming "
                   "nothing is pending."]

    pending = pending_column_changes(applied, repo)
    if not pending:
        return 0, ["[preflight] no pending column drops or renames."]

    refusals: list[tuple[dict, list[str]]] = []
    for change in pending:
        where = f"{change['table'] or '?'}.{change['column']}"
        blocking, advisory = code_hits(change["column"], change.get("table"), repo)
        if blocking:
            refusals.append((change, blocking))
        elif advisory:
            out.append(f"[preflight] ADVISORY: {change['migration']} "
                       f"{change['action']} {where}; {len(advisory)} mention(s) of "
                       f"{change['column']!r} elsewhere, none near the table:")
            out += [f"    {h}" for h in advisory[:8]]
        else:
            out.append(f"[preflight] OK: {change['migration']} {change['action']} "
                       f"{where} — not named in the shipping code.")

    for change, hits in refusals:
        where = f"{change['table'] or '?'}.{change['column']}"
        out.append("")
        out.append(f"[preflight] REFUSED: {change['migration']} {change['action']} "
                   f"{where}, and the code being deployed still names it:")
        out += [f"    {h}" for h in hits[:12]]
        if len(hits) > 12:
            out.append(f"    ... and {len(hits) - 12} more")

    if refusals:
        out += ["",
                "Ship the code that stops using the column FIRST, then apply the "
                "migration. See COLUMN-GREP in CLAUDE.md (the 2026-09-20 outage)."]
        return 1, out
    return 0, out


def main() -> int:
    ap = argparse.ArgumentParser(description="Lambda deploy preflight (COLUMN-GREP).")
    ap.add_argument("--force", metavar="REASON",
                    help="deploy anyway; the override and its reason are logged")
    ap.add_argument("--applied-from", metavar="JSON",
                    help="file holding a JSON list of applied migration names (tests)")
    args = ap.parse_args()

    code, lines = check(args.applied_from)
    for line in lines:
        print(line)
    if code == 0:
        return 0
    if args.force:
        print(f"\n[preflight] OVERRIDDEN --force: {args.force}")
        print("[preflight] proceeding against the preflight's advice.")
        return 0
    print('\n[preflight] refusing to deploy. Override: --force "<reason>".')
    return code


if __name__ == "__main__":
    sys.exit(main())
