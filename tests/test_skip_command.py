"""MAKEUP-1 — `skip <reason>`: record intent, move nothing.

Ryan, 2026-09-20: a deliberate skip and a missed session are different facts
and must stop looking identical in the reports. Moving a session (options B/D)
is deferred until a missed day is a recurring problem.

Run:
    python3 tests/test_skip_command.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health  # noqa: E402
from artemis import health_eval as ev  # noqa: E402

TODAY = date(2026, 9, 21)          # Mon — Strength A at the office


class TestParsing(unittest.TestCase):
    def test_reason_is_required(self):
        """A skip with no reason is what `missed` already means."""
        self.assertIsNone(health.parse_skip("skip", TODAY))
        self.assertIsNone(health.parse_skip("skip   ", TODAY))

    def test_todays_skip(self):
        self.assertEqual(health.parse_skip("skip sick", TODAY), (TODAY, "sick"))
        self.assertEqual(health.parse_skip("skip — back is off", TODAY), (TODAY, "back is off"))

    def test_a_named_date(self):
        got = health.parse_skip("skip friday travelling", TODAY)
        self.assertIsNotNone(got)
        day, reason = got
        self.assertEqual(reason, "travelling")
        self.assertEqual(day.weekday(), 4)

    def test_not_a_skip_falls_through(self):
        for msg in ("what's today?", "slept 7 energy 4", "skipping rope 10 min", ""):
            self.assertIsNone(health.parse_skip(msg, TODAY), msg)


class TestHandler(unittest.TestCase):
    def plan_row(self, **kw):
        return {"plan_id": 1, "session_type": "strength_a", "is_skipped": False,
                "blocks": {"display_name": "Office Strength A"}, **kw}

    def test_records_the_reason_and_says_it_moved_nothing(self):
        writes = []
        with patch("knowledge.db.execute_one", return_value=self.plan_row()), \
             patch("knowledge.db.execute_write", side_effect=lambda *a: writes.append(a)):
            reply = health.handle_skip("skip sick", TODAY)
        self.assertIn("Office Strength A", reply)
        self.assertIn("sick", reply)
        self.assertIn("won't count as missed", reply)
        self.assertIn("isn't moved", reply)
        sql, params = writes[0]
        self.assertIn("is_skipped = TRUE", sql)
        self.assertIn("skip_reason", sql)
        self.assertEqual(params, ("sick", TODAY))

    def test_no_plan_that_day_says_so_rather_than_writing(self):
        writes = []
        with patch("knowledge.db.execute_one", return_value=None), \
             patch("knowledge.db.execute_write", side_effect=lambda *a: writes.append(a)):
            reply = health.handle_skip("skip sick", TODAY)
        self.assertIn("nothing to skip", reply)
        self.assertEqual(writes, [])

    def test_a_db_failure_is_reported_not_swallowed(self):
        with patch("knowledge.db.execute_one", return_value=self.plan_row()), \
             patch("knowledge.db.execute_write", side_effect=RuntimeError("no db")):
            self.assertIn("Couldn't record the skip", health.handle_skip("skip sick", TODAY))

    def test_it_never_changes_the_date(self):
        """Option C only: intent is recorded, the session stays where it is."""
        writes = []
        with patch("knowledge.db.execute_one", return_value=self.plan_row()), \
             patch("knowledge.db.execute_write", side_effect=lambda *a: writes.append(a)):
            health.handle_skip("skip travelling", TODAY)
        for sql, _ in writes:
            self.assertNotIn("plan_date =", sql.split("WHERE")[0])
            self.assertNotIn("original_date", sql)


class TestReportsDistinguishSkipped(unittest.TestCase):
    """EVAL-1 must stop calling a deliberate skip a missed session."""

    def plans(self, skipped_reason=None):
        return [{"plan_id": 1, "plan_date": TODAY, "session_type": "strength_a",
                 "week_num": 2, "target_rpe": 6.0, "blocks": {"display_name": "Office Strength A"},
                 "is_skipped": skipped_reason is not None, "skip_reason": skipped_reason}]

    def test_a_skipped_day_is_not_missed_and_does_not_count_as_due(self):
        r = ev.evaluate(self.plans("sick"), [], [], start=TODAY, end=TODAY + timedelta(days=6),
                        today=TODAY + timedelta(days=1), anchor=TODAY)
        self.assertEqual(r["sessions"][0]["status"], "skipped")
        self.assertEqual(r["missed"], [])
        self.assertEqual(r["counts"]["due"], 0)
        self.assertEqual(r["skipped"], [{"date": TODAY.isoformat(),
                                         "label": "Office Strength A", "reason": "sick"}])
        line = next(l for l in ev.render_lines(r) if l.startswith("Skipped:"))
        self.assertIn("Office Strength A — sick", line)
        self.assertIn("Missed: none", ev.render_lines(r))

    def test_the_same_day_unskipped_is_still_missed(self):
        r = ev.evaluate(self.plans(), [], [], start=TODAY, end=TODAY + timedelta(days=6),
                        today=TODAY + timedelta(days=1), anchor=TODAY)
        self.assertEqual(r["sessions"][0]["status"], "missed")
        self.assertEqual(len(r["missed"]), 1)
        self.assertEqual(r["counts"]["due"], 1)
        self.assertEqual(r["skipped"], [])

    def test_a_skip_with_no_reason_still_reads_as_skipped(self):
        r = ev.evaluate(self.plans(""), [], [], start=TODAY, end=TODAY + timedelta(days=6),
                        today=TODAY + timedelta(days=1), anchor=TODAY)
        self.assertEqual(r["skipped"][0]["reason"], None)
        self.assertIn("Skipped: Mon 9/21 Office Strength A", ev.render_lines(r))

    def test_the_skip_line_carries_no_judgement(self):
        r = ev.evaluate(self.plans("travelling"), [], [], start=TODAY,
                        end=TODAY + timedelta(days=6), today=TODAY + timedelta(days=1),
                        anchor=TODAY)
        line = next(l for l in ev.render_lines(r) if l.startswith("Skipped:"))
        self.assertNotRegex(line.lower(), r"\b(should|try|excuse|again|only|poor|good)\b")


class TestNagAndBackstopRespectIt(unittest.TestCase):
    """Both already gate on is_skipped — this pins that they still do."""

    def test_the_nag_and_the_backstop_check_is_skipped(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "health.py").read_text()
        for fn in ("def run_nag_check", "def insert_inferred_summary"):
            body = src[src.index(fn):src.index(fn) + 1600]
            self.assertIn("is_skipped", body, fn)


if __name__ == "__main__":
    unittest.main()
