"""EVAL-1 — the read-only weekly evaluator (artemis.health_eval). No DB.

Run:
    python3 tests/test_health_eval.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health_eval as ev  # noqa: E402

WED = date(2026, 9, 16)
TUE = WED + timedelta(days=6)
PATTERN = ["strength_a", "rest_mobility", "strength_b", "recovery_flow", "walk", "strength_c",
           "cardio_z2"]
CAPS = [6.0, 2.0, 6.0, 2.0, None, 6.0, 4.0]


def plans(start=WED, adjust=None):
    rows = []
    for i, (st, cap) in enumerate(zip(PATTERN, CAPS)):
        b = {"display_name": st}
        if adjust and i in adjust:
            b["adjustment"] = adjust[i]
        rows.append({"plan_id": 100 + i + (start - WED).days, "plan_date": start + timedelta(days=i),
                     "session_type": st, "week_num": 1, "target_rpe": cap, "blocks": b})
    return rows


def s(pid, d, ex, w, rpe=7.0):
    return {"plan_date": d, "plan_id": pid, "log_type": "strength_set", "exercise": ex,
            "weight_lbs": w, "reps_done": 12, "rpe_actual": rpe, "is_skipped": False}


def summary(pid, d, rpe=None):
    return {"plan_date": d, "plan_id": pid, "log_type": "session_summary", "exercise": None,
            "weight_lbs": None, "reps_done": None, "rpe_actual": rpe, "is_skipped": False}


def full_week_logs():
    d = lambda i: WED + timedelta(days=i)  # noqa: E731
    return [s(100, d(0), "Leg press", 160), summary(100, d(0), 7.0),
            s(102, d(2), "DB goblet squat", 40), summary(102, d(2), 7.5),
            summary(103, d(3)), summary(104, d(4)),
            s(105, d(5), "DB Romanian deadlift", 50), summary(105, d(5), 6.0),
            summary(106, d(6), 4.0)]


class TestWeek(unittest.TestCase):
    def test_week_of_is_wed_to_tue_from_the_anchor(self):
        self.assertEqual(ev.week_of(date(2026, 9, 19)), (WED, TUE))
        self.assertEqual(ev.week_of(date(2026, 9, 22)), (WED, TUE))
        self.assertEqual(ev.week_of(date(2026, 9, 23)), (date(2026, 9, 23), date(2026, 9, 29)))

    def test_full_week(self):
        r = ev.evaluate(plans(), full_week_logs(), [], start=WED, end=TUE, today=TUE + timedelta(days=1))
        self.assertEqual(r["counts"], {"planned": 6, "done": 6, "due": 6, "missed": 0, "upcoming": 0})
        self.assertFalse(r["week"]["partial"])
        self.assertEqual(r["week"]["program_week"], 1)
        # rest day is neither done nor missed
        self.assertEqual([x["status"] for x in r["sessions"]][1], "rest")
        # effort: sessions with both an RPE and a cap — A 7 vs 6, B 7.5 vs 6, C 6 vs 6, Z2 4 vs 4
        self.assertEqual(r["rpe"]["avg_session_rpe"], 6.1)
        self.assertEqual(r["rpe"]["avg_cap"], 5.5)
        self.assertEqual([o["date"] for o in r["rpe"]["over_cap"]], ["2026-09-16", "2026-09-18"])

    def test_partial_week_counts_only_what_is_due(self):
        logs = full_week_logs()[:4]                    # A and B logged
        r = ev.evaluate(plans(), logs, [], start=WED, end=TUE, today=date(2026, 9, 19))
        self.assertTrue(r["week"]["partial"])
        self.assertEqual(r["week"]["through"], "2026-09-19")
        self.assertEqual(r["counts"], {"planned": 6, "done": 2, "due": 2, "missed": 0, "upcoming": 4})
        self.assertEqual([x["status"] for x in r["sessions"][2:5]], ["done", "today", "upcoming"])
        self.assertIn("partial", ev.render_lines(r)[0])

    def test_missed_session(self):
        mon = date(2026, 9, 21)
        logs = [l for l in full_week_logs() if l["plan_id"] != 102 and l["plan_date"] <= mon]
        r = ev.evaluate(plans(), logs, [], start=WED, end=TUE, today=mon)
        self.assertEqual(r["missed"], [{"date": "2026-09-18", "label": "strength_b"}])
        # A, flow, walk, C (today, logged) done; B missed; Tue still to come
        self.assertEqual((r["counts"]["done"], r["counts"]["due"], r["counts"]["upcoming"]), (4, 5, 1))
        self.assertIn("Missed: Fri 9/18 strength_b", ev.render_lines(r))

    def test_adjustments_applied(self):
        adj = {2: {"rules_fired": ["pain_mobility", "lighten_sore"], "reason": "Pain hip 3/5 …"}}
        r = ev.evaluate(plans(adjust=adj), full_week_logs(), [], start=WED, end=TUE, today=TUE)
        self.assertEqual(r["adjustments"], [{"date": "2026-09-18",
                                             "rules": ["pain_mobility", "lighten_sore"],
                                             "reason": "Pain hip 3/5 …"}])
        self.assertIn("Adjustments: Fri 9/18 pain_mobility, lighten_sore", ev.render_lines(r))

    def test_empty_week(self):
        r = ev.evaluate([], [], [], start=WED, end=TUE, today=TUE)
        self.assertEqual(r["counts"], {"planned": 0, "done": 0, "due": 0, "missed": 0, "upcoming": 0})
        self.assertIsNone(r["rpe"])
        self.assertEqual(r["loads"], [])
        lines = ev.render_lines(r)
        self.assertIn("Missed: none", lines)
        self.assertIn("Effort: no session RPE logged", lines)

    def test_renamed_exercise_is_not_a_new_exercise(self):
        prior_day = WED - timedelta(days=5)
        prior = [s(90, prior_day, "45° back extension", 50)]
        logs = [s(102, WED + timedelta(days=2), "Seated back extension", 60)]
        r = ev.evaluate(plans(), logs, prior, start=WED, end=TUE, today=TUE)
        self.assertEqual(r["loads"], [{"exercise": "Seated back extension", "this_week": 60.0,
                                       "last_week": 50.0, "change": 10.0, "note": None}])
        self.assertIn("Loads vs last week: seated back extension +10 lb", ev.render_lines(r))


class TestLoads(unittest.TestCase):
    def test_no_load_is_reported_as_such_not_zero(self):
        d = WED + timedelta(days=2)
        logs = [s(102, d, "Captain's chair knee raise", None), s(102, d, "Seated back extension", None),
                s(102, d, "Leg press", 160)]
        prior = [s(90, WED - timedelta(days=5), "Leg press", None)]
        r = ev.evaluate(plans(), logs, prior, start=WED, end=TUE, today=TUE)
        by = {l["exercise"]: l for l in r["loads"]}
        self.assertEqual(by["Captain's chair knee raise"]["note"], "bodyweight")
        self.assertEqual(by["Seated back extension"]["note"], "no load logged")
        self.assertEqual(by["Leg press"]["note"], "no load last week")
        self.assertIsNone(by["Leg press"]["change"])

    def test_top_set_and_first_week(self):
        d = WED
        r = ev.evaluate(plans(), [s(100, d, "Leg press", 130), s(100, d, "Leg press", 160)], [],
                        start=WED, end=TUE, today=TUE)
        self.assertEqual(r["loads"][0]["this_week"], 160.0)
        self.assertEqual(r["loads"][0]["note"], "first week")


class TestReadOnlyAndNoAdvice(unittest.TestCase):
    def test_pre_program_rows_are_ignored(self):
        old = plans(start=WED - timedelta(days=7))
        r = ev.evaluate(old, [], [], start=WED - timedelta(days=7), end=WED - timedelta(days=1),
                        today=TUE)
        self.assertEqual(r["counts"]["planned"], 0)
        self.assertIsNone(r["week"]["program_week"])

    def test_render_is_data_only(self):
        adj = {2: {"rules_fired": ["pain_day_off"], "reason": "x"}}
        logs = [l for l in full_week_logs() if l["plan_id"] != 105]
        r = ev.evaluate(plans(adjust=adj), logs, [], start=WED, end=TUE, today=TUE)
        text = " ".join(ev.render_lines(r)).lower()
        self.assertNotRegex(text, r"\b(should|recommend|try|consider|increase|decrease|deload|"
                                  r"great|good job|well done|too)\b")

    def test_module_never_writes(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "health_eval.py").read_text()
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        self.assertNotRegex(code, r"\b(INSERT|UPDATE|DELETE|execute_write|post_or_hold)\b")


if __name__ == "__main__":
    unittest.main()
