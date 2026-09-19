"""TIME-CAP — 45 min is a target, not a limit (Ryan, 2026-09-19).

Run:
    python3 tests/test_time_cap.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import copy
import logging
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health_office as office  # noqa: E402

ROWS = office.build_rows()


def row(est, session_type="strength_a", week=3, d=date(2026, 9, 30)):
    return {"plan_date": d, "session_type": session_type, "week_num": week, "est_duration_min": est}


class TestVerdict(unittest.TestCase):
    def test_44_passes_silently(self):
        self.assertEqual(office.duration_verdict("strength_a", 3, 44), ("ok", ""))
        self.assertEqual(office.duration_findings([row(44)]), ([], []))

    def test_52_passes_with_a_note(self):
        rejects, notes = office.duration_findings([row(52)])
        self.assertEqual(rejects, [])
        self.assertEqual(notes, ["2026-09-30 Wed strength_a wk3: ~52 min — target 45 min"])

    def test_61_is_rejected(self):
        rejects, notes = office.duration_findings([row(61)])
        self.assertEqual(rejects, ["2026-09-30 Wed strength_a wk3: ~61 min — 60+ is rejected"])
        self.assertEqual(notes, [])

    def test_boundaries(self):
        self.assertEqual(office.duration_verdict("strength_a", 3, 45)[0], "note")
        self.assertEqual(office.duration_verdict("strength_a", 3, 59)[0], "note")
        self.assertEqual(office.duration_verdict("strength_a", 3, 60)[0], "reject")
        self.assertEqual(office.duration_verdict("strength_a", 3, None)[0], "ok")

    def test_calibration_pending_slots_warn_instead_of_reject(self):
        for st, wk, est in (("strength_b", 3, 62), ("strength_b", 6, 62), ("strength_c", 5, 67)):
            with self.subTest(st=st, wk=wk):
                kind, msg = office.duration_verdict(st, wk, est)
                self.assertEqual(kind, "pending")
                self.assertIn("allowed until the estimate is recalibrated", msg)
        # the same length outside the pending slots is still rejected
        self.assertEqual(office.duration_verdict("strength_c", 4, 67)[0], "reject")
        self.assertEqual(office.duration_verdict("strength_b", 7, 62)[0], "reject")


class TestValidateRows(unittest.TestCase):
    def test_current_program_validates_with_notes_and_is_unchanged(self):
        notes = office.validate_rows(ROWS)
        self.assertEqual(len([n for n in notes if "recalibrated" in n]), 6)
        # weeks 3-6 stay as planned: exercises and the finisher untouched
        by = {(r["session_type"], r["week_num"]): r for r in ROWS}
        self.assertEqual(len(by[("strength_b", 3)]["blocks"]["exercises"]), 7)
        self.assertIn("finisher", by[("strength_c", 5)]["blocks"])
        self.assertEqual(by[("strength_c", 5)]["est_duration_min"], 67)

    def test_a_60_plus_row_outside_the_pending_slots_fails_the_reseed(self):
        rows = copy.deepcopy(ROWS)
        wk_c = next(r for r in rows if r["session_type"] == "strength_c" and r["week_num"] == 2)
        wk_c["est_duration_min"] = 61
        with self.assertRaises(AssertionError) as cm:
            office.validate_rows(rows)
        self.assertIn("est_duration_min >= 60", str(cm.exception))

    def test_notes_are_logged_never_cut(self):
        with self.assertLogs("artemis.health_office", level=logging.WARNING) as logs:
            office.validate_rows(ROWS)
        self.assertTrue(any("TIME-CAP" in m and "~55 min" in m for m in logs.output))
        self.assertEqual(ROWS, office.build_rows())   # validation never edits a row


class TestWording(unittest.TestCase):
    def test_estimate_wording(self):
        self.assertEqual(office.format_estimate(44), "44 min")
        self.assertEqual(office.format_estimate(45), "45 min")
        self.assertEqual(office.format_estimate(52), "~52 min")
        self.assertEqual(office.format_estimate(None), "")


class TestReports(unittest.TestCase):
    def test_weekly_and_daily_show_planned_vs_logged(self):
        import export_report as er
        wed = date(2026, 9, 16)
        plans = [{"plan_id": 1, "plan_date": wed, "phase": 1, "week_num": 1,
                  "session_type": "strength_b", "blocks": {"display_name": "B"},
                  "target_rpe": 6.0, "est_duration_min": 52}]
        t0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
        logs = [{"log_id": i, "plan_date": wed, "plan_id": 1, "log_type": "strength_set",
                 "exercise": "Leg press", "set_num": i, "round_num": None, "reps_done": 12,
                 "weight_lbs": 100, "rpe_actual": 7, "duration_sec": None, "notes": None,
                 "is_skipped": False, "logged_at": t0 + timedelta(minutes=m)}
                for i, m in ((1, 0), (2, 38))]
        data = er.Data(wed, wed + timedelta(days=6), wed + timedelta(days=1), plans, logs, {}, [],
                       [], {"anchor": "2026-09-16", "weeks_total": 7})
        weekly = er.md(er.build_weekly(data))
        self.assertIn("| Planned | Logged span |", weekly)
        self.assertIn("| ~52 min | 38 min |", weekly)
        self.assertIn("Planned vs logged: planned avg 52 min, logged span avg 38 min over 1 session(s)",
                      weekly)
        day = er.Data(wed, wed, wed, plans, logs, {}, [], [], None)
        self.assertIn("Time: planned ~52 min · logged span 38 min", er.md(er.build_daily(day)))


if __name__ == "__main__":
    unittest.main()
