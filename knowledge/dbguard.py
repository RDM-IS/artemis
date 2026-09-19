"""TEST-DB-GUARD — no test may reach a real database.

Every path that opens a real connection (knowledge.db's pool, the Lambda API's
SQLAlchemy engine, the maintenance scripts) calls refuse_real_db() first. It
raises when a test is running:

  * the explicit flag ARTEMIS_TEST_NO_DB=1 (every test_*.py (tests/, artemis/, knowledge/) sets it on
    import; tests/test_db_guard.py enforces that), or
  * pytest (PYTEST_CURRENT_TEST, or pytest imported).

Why: the tests `os.environ.setdefault("RDS_HOST", ...)`, which keeps the REAL
host once .env is sourced — so on the box a test wrote into production
(2026-09-19: test_confirm_dispatch → 56 calendar_audit + 8 guardrail rows).

Tests that want a database patch knowledge.db.get_connection with a fake, or
connect to a throwaway local Postgres directly; neither passes through here.
"""

import os
import sys

FLAG = "ARTEMIS_TEST_NO_DB"


class RealDbInTestError(RuntimeError):
    """A real database connection was attempted while a test was running."""


def testing() -> bool:
    if os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes"):
        return True
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def refuse_real_db(what: str) -> None:
    if testing():
        raise RealDbInTestError(
            f"{what}: real database connection refused — a test is running "
            f"({FLAG} / pytest). Patch knowledge.db.get_connection with a fake."
        )
