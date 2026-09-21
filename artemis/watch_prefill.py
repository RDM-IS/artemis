"""WATCH-1 — pre-fill the morning check-in from the watch (box side).

Three values only: sleep hours, resting heart rate and weight. Ryan still
types energy and soreness; nothing here touches them.

The logic lives in knowledge.watch_prefill so the Lambda can run the same code
on every ingest. This module is the wake job's entry point: it runs the
pre-fill for today before the 04:30 post, and leaves an audit row naming which
metric each number came from (`sleep_source_metric`).

Rules (see knowledge.watch_prefill for the detail):
  1. **Manual always wins** — a typed field is FINAL for that date.
  2. **Nothing is invented or carried forward** — no value is today's unless
     it was measured today (last night, for sleep). Otherwise it's missing.
  3. **"Today" is local** — the ACTIVE timezone, never UTC.
  4. **Recorded, never acted on** — watch values don't adjust the plan.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from knowledge.watch_prefill import audit_view, describe as _describe, prefill_day, read_values

logger = logging.getLogger(__name__)


def describe(values: dict) -> str | None:
    from artemis.quiet_hours import local_tz
    return _describe(values, local_tz())


def today_line(cur, day: date | None = None) -> str | None:
    """The wake post's watch line for `day`, from what the watch has now.
    Read-only — the wake job has already written the pre-fill."""
    from artemis.quiet_hours import local_today, local_tz
    return _describe(read_values(cur, day or local_today(), local_tz()), local_tz())


def run(day: date | None = None) -> dict:
    """Read today's watch values and write them. Returns the values, with
    `written` saying whether anything landed. Safe to run repeatedly."""
    from knowledge.db import get_connection
    from artemis.quiet_hours import local_today, local_tz

    day = day or local_today()
    values: dict = {"written": False}
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            values = prefill_day(cur, day, local_tz())
            cur.execute(
                "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
                "outcome, token_count, api_cost_usd, metadata) "
                "VALUES (%s, NULL, %s, %s, NULL, %s, 0, 0, %s::jsonb)",
                ("watch_prefill", "watch_prefill", "health", "executed",
                 json.dumps({"day": day.isoformat(), "trigger": "wake",
                             "values": audit_view(values)}, default=str)))
            conn.commit()
    except Exception:
        logger.exception("watch pre-fill failed for %s — the check-in still works typed", day)
    logger.info("Watch pre-fill %s: %s (sleep from %s)", day,
                describe(values) or "nothing available", values.get("sleep_source_metric"))
    return values
