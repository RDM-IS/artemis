"""EXPORT-1 — scripts/export_report.py builders, no DB.

Run:
    python3.11 tests/test_export_report.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import export_report as er  # noqa: E402

WED = date(2026, 9, 16)
GEN = datetime(2026, 9, 18, 13, 0)
TYPES = ["strength_a", "rest_mobility", "strength_b", "recovery_flow", "recovery_flow", "strength_c", "cardio_z2"]


def plans(start=WED):
    return [{"plan_id": 100 + i, "plan_date": start + timedelta(days=i), "phase": 1, "week_num": 1,
             "session_type": st,
             # LOCATION-1: the class travels on the row — names imply nothing.
             "blocks": {"display_name": st, "location": "office gym",
                        "exercises": [{"name": n, "equipment_class": c} for n, c in (
                            ("Captain's chair knee raise", "bodyweight"),
                            ("Seated back extension", "machine"),
                            ("45° back extension", "machine"),
                            ("Leg press", "machine"),
                            ("DB bench press", "dumbbell"))]},
             "target_rpe": 6.0, "est_duration_min": 40} for i, st in enumerate(TYPES)]


def s(plan_id, d, ex, n, w, reps=12, rpe=7.0, minute=0):
    return {"log_id": plan_id * 100 + n, "plan_date": d, "plan_id": plan_id, "log_type": "strength_set",
            "exercise": ex, "set_num": n, "round_num": None, "reps_done": reps, "weight_lbs": w,
            "rpe_actual": rpe, "duration_sec": None, "notes": None, "is_skipped": False,
            "logged_at": datetime(d.year, d.month, d.day, 10, minute, tzinfo=timezone.utc)}


def week(today, logs=(), prior=(), checkins=None):
    return er.Data(WED, WED + timedelta(days=6), today, plans(), list(logs), checkins or {},
                   list(prior), [], {"weeks_total": 7})


class TestWeekly(unittest.TestCase):
    def test_empty_week_prints_every_section_and_not_yet_tracked(self):
        out = er.md(er.build_weekly(week(WED - timedelta(days=1)), GEN))
        for h in ("Adherence", "Sessions", "Weight progress", "Check-in trends", "Body weight",
                  "Pain and soreness", "Open pain patterns", "Adjustments applied",
                  "Weekly evaluation (EVAL-1)", "Watch data", "Nutrition"):
            self.assertIn(f"## {h}", out)
        self.assertEqual(out.count(er.NOT_TRACKED), 2)   # watch, nutrition
        self.assertIn("**0 of 0**", out)
        self.assertIn("No sets logged.", out)

    def test_partial_week_statuses_and_adherence(self):
        logs = [s(100, WED, "Leg press", 1, 130), s(100, WED, "Leg press", 2, 160, minute=20)]
        data = week(date(2026, 9, 18), logs)
        st = {p["plan_date"]: er.day_status(data, p) for p in data.plans}
        self.assertEqual(st[WED], "done")
        self.assertEqual(st[date(2026, 9, 17)], "rest")
        self.assertEqual(st[date(2026, 9, 18)], "not logged yet")
        self.assertEqual(st[date(2026, 9, 19)], "upcoming")
        self.assertEqual(er.adherence(data), (1, 1, 5))
        out = er.md(er.build_weekly(data, GEN))
        self.assertIn("**Partial period**", out)
        self.assertIn("| Wed 9/16 | strength_a (office gym) | done | 2 |", out)
        self.assertIn("20 min", out)

    def test_a_past_day_with_no_logs_is_missed(self):
        data = week(date(2026, 9, 20))
        self.assertEqual(er.day_status(data, data.plans[2]), "missed")

    def test_week_over_week_change_groups_the_renamed_exercise(self):
        prior_day = WED - timedelta(days=5)
        prior = [s(90, prior_day, "45° back extension", 1, 50)]
        logs = [s(102, WED + timedelta(days=2), "Seated back extension", 1, 60),
                s(102, WED + timedelta(days=2), "Leg extension", 1, 110)]
        out = er.md(er.build_weekly(week(date(2026, 9, 22), logs, prior), GEN))
        self.assertIn("| Seated back extension | 60 lb | 50 lb | +10 lb |", out)
        self.assertIn("| Leg extension | 110 lb | — | first week |", out)

    def test_a_machine_with_no_weight_is_no_load_logged_not_bodyweight(self):
        logs = [s(102, WED + timedelta(days=2), "Seated back extension", 1, None)]
        out = er.md(er.build_weekly(week(date(2026, 9, 18), logs), GEN))
        self.assertIn("| Seated back extension | — | — | no load logged |", out)

    def test_bodyweight_sets_are_not_a_zero_load(self):
        logs = [s(100, WED, "Captain's chair knee raise", 1, None, reps=15)]
        out = er.md(er.build_weekly(week(WED, logs), GEN))
        self.assertIn("| Captain's chair knee raise | — | — | bodyweight |", out)

    def test_checkins_and_pain_summary(self):
        ci = {WED: {"weight_lbs": 284.5, "sleep_hrs": 6.5, "energy": 5, "soreness": None,
                    "resting_hr": None, "free_text": None},
              WED + timedelta(days=1): {"weight_lbs": 283.0, "sleep_hrs": 6, "energy": 4,
                                        "soreness": {"overall": 0, "pain": {"shoulder": 2}},
                                        "resting_hr": None, "free_text": None}}
        out = er.md(er.build_weekly(week(date(2026, 9, 18), checkins=ci), GEN))
        self.assertIn("284.5 lb (Wed) → 283 lb (Thu), -1.5 lb over 2 weigh-in(s)", out)
        self.assertIn("pain — shoulder: peak 2/5 (Thu 9/17 2)", out)
        self.assertIn("| Fri 9/18 | no check-in |", out)


class TestSides(unittest.TestCase):
    def test_sided_soreness_reads_as_right_knee(self):
        sore = {"knee": 1, "sides": {"knee": "right"}, "pain": {"shoulder": 2},
                "pain_sides": {"shoulder": "unspecified"}}
        self.assertEqual(er.soreness_text(sore), "right knee 1, pain shoulder 2")
        data = week(date(2026, 9, 20), checkins={WED: {"weight_lbs": None, "sleep_hrs": None,
                    "energy": None, "soreness": sore, "resting_hr": None, "free_text": None}})
        self.assertEqual(er.pain_summary(data), ["pain — shoulder: peak 2/5 (Wed 9/16 2)",
                                                 "soreness — right knee: peak 1/5 (Wed 9/16 1)"])


class TestDailyAndMonthly(unittest.TestCase):
    def test_daily_flags_the_renamed_exercise_and_untracked_parts(self):
        d = WED + timedelta(days=2)
        data = er.Data(d, d, d, [plans()[2]], [s(102, d, "45° back extension", 1, None)], {}, [], [], None)
        out = er.md(er.build_daily(data, GEN))
        self.assertIn("| 45° back extension * | 1 | no load logged |", out)
        self.assertIn("counted as Seated back extension", out)
        self.assertIn("## Machine settings\n\nnot yet tracked", out)
        self.assertIn("## Check-in\n\nno check-in", out)
        self.assertIn("## Watch data (average / max heart rate, calories)\n\nnot yet tracked", out)

    def test_rows_before_the_program_start_are_not_counted(self):
        old = [{"plan_id": 1 + i, "plan_date": date(2026, 9, 10) + timedelta(days=i), "phase": 3,
                "week_num": 14, "session_type": "strength_b", "blocks": {"display_name": "old"},
                "target_rpe": 7, "est_duration_min": 50} for i in range(6)]
        prog = {"name": "Foundation", "phase": 1, "anchor": "2026-09-16", "weeks_total": 7,
                "deload_week": 7, "end": "2026-11-03"}
        data = er.Data(date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 18), old + plans(),
                       [s(100, WED, "Leg press", 1, 160)], {}, [], [], prog)
        self.assertEqual(er.day_status(data, old[0]), "pre-program")
        self.assertEqual(er.adherence(data)[0:2], (1, 1))      # 9/18 not logged yet
        out = er.md(er.build_monthly(data, GEN))
        self.assertIn("**1** done · 0 missed", out)
        self.assertIn("6 day(s) before the program start (9/16) not counted", out)
        self.assertIn("week 1 of 7 (started 9/16", out)

    def test_monthly_main_lift_start_vs_end(self):
        logs = [s(100, WED, "Leg press", 1, 130), s(100, WED, "Leg press", 2, 160),
                s(107, WED + timedelta(days=7), "Leg press", 1, 180)]
        data = er.Data(date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 24), plans(), logs, {}, [], [],
                       {"name": "Foundation", "phase": 1, "weeks_total": 7, "deload_week": 7, "end": "2026-11-03"})
        out = er.md(er.build_monthly(data, GEN))
        self.assertIn("| Leg press | 160 lb (9/16) | 180 lb (9/23) | +20 lb |", out)
        self.assertIn("Leg press: +20 lb top set", out)
        self.assertNotIn("| Set |", out)   # no per-set detail


class TestHtml(unittest.TestCase):
    def test_html_is_standalone_and_escaped(self):
        page = er.to_html([("h1", "A <b> & C"), ("p", er.NOT_TRACKED)], "t")
        self.assertIn("A &lt;b&gt; &amp; C", page)
        self.assertIn('<span class="nt">not yet tracked</span>', page)
        self.assertNotIn("http", page)   # no external resources


if __name__ == "__main__":
    unittest.main()
