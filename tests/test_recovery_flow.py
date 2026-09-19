"""YOGA-1 — Recovery Flow: builder, side validator, reseed diff, rules, ramp,
nudge, nag, wake post. No RDS.

Run:
    python3.11 tests/test_recovery_flow.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import copy
import json
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from test_checkin_adjust import FakeDB, office_row, rest_row  # noqa: E402

from artemis import health_checkin as hc  # noqa: E402
from artemis import health_office as office  # noqa: E402

THU = date(2026, 9, 17)
SAT = date(2026, 9, 19)
NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
ROWS = {r["plan_date"]: r for r in office.build_rows()}


def _reseed():
    spec = importlib.util.spec_from_file_location(
        "reseed_v2", _HERE.parent / "scripts" / "reseed_health_plan_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def flow(**over) -> dict:
    b = copy.deepcopy(ROWS[SAT]["blocks"])
    b.update(over)
    return b


class TestBuilder(unittest.TestCase):
    def test_thu_office_and_sat_home(self):
        thu, sat = ROWS[THU], ROWS[SAT]
        for r in (thu, sat):
            self.assertEqual(r["session_type"], "recovery_flow")
            self.assertEqual(r["blocks"]["type"], "recovery_flow")
            self.assertEqual(r["target_rpe"], 2.0)
            self.assertEqual(r["blocks"]["rounds"], 2)
            self.assertEqual(len(r["blocks"]["flow"]), 20)
            self.assertEqual(r["blocks"]["close"]["duration_sec"], 180)
        self.assertEqual(thu["blocks"]["location"], "office gym")
        self.assertEqual(sat["blocks"]["location"], "home")
        self.assertEqual(thu["blocks"]["pre"], [{"name": "Stretch Trainer", "side": None,
                                                  "duration_sec": 480,
                                                  "cue": "Follow the 8 placard stretches"}])
        self.assertEqual(sat["blocks"]["pre"], [])
        self.assertIn("Stretch Trainer", thu["blocks"]["equipment"])
        self.assertNotIn("Stretch Trainer", sat["blocks"]["equipment"])

    def test_every_thu_and_sat_is_a_flow(self):
        for d, r in ROWS.items():
            is_flow = r["session_type"] == "recovery_flow"
            self.assertEqual(is_flow, d.weekday() in (3, 5), d)
            if is_flow:
                self.assertEqual(r["blocks"]["location"],
                                 "office gym" if d.weekday() == 3 else "home")

    def test_steps_match_the_table(self):
        got = [(s["step"], s["name"], s["side"], s["duration_sec"], s["mirror_group"])
               for s in ROWS[SAT]["blocks"]["flow"]]
        self.assertEqual(got, [
            ("1", "Child's pose", None, 30, None),
            ("2", "Cobra", None, 30, None),
            ("3", "Downward dog", None, 60, None),
            ("4", "Standing forward bend", None, 30, None),
            ("5", "High lunge", "R", 30, "lunge-unit"),
            ("6", "Crescent lunge", "R", 30, "lunge-unit"),
            ("7", "Extended puppy", None, 30, None),
            ("8", "High lunge", "L", 30, "lunge-unit"),
            ("9", "Crescent lunge", "L", 30, "lunge-unit"),
            ("10", "Bridge", None, 30, None),
            ("11a", "Supine twist", "R", 30, "twist-supine"),
            ("11b", "Supine twist", "L", 30, "twist-supine"),
            ("12a", "Wind release", "R", 30, "wind"),
            ("12b", "Wind release", "L", 30, "wind"),
            ("13a", "Seated side bend", "L", 30, "side-bend"),
            ("13b", "Seated side bend", "R", 30, "side-bend"),
            ("14a", "Seated twist", "L", 30, "twist-seated"),
            ("14b", "Seated twist", "R", 30, "twist-seated"),
            ("15", "Seated mountain", None, 30, None),
            ("16", "Easy pose", None, 30, None),
        ])
        by = {s["step"]: s for s in ROWS[SAT]["blocks"]["flow"]}
        self.assertEqual(by["3"]["easier"], "Dolphin — forearms down")
        for st in ("5", "6", "8", "9"):
            self.assertEqual(by[st]["easier"], "Knee down")
        self.assertEqual(by["5"]["side_label"], "Right leg forward")
        self.assertEqual(by["13a"]["side_label"], "Lean left")
        self.assertTrue(all(s["cue"] for s in ROWS[SAT]["blocks"]["flow"]))
        self.assertNotIn("link", by["1"])

    def test_round_two_doubles_steps_10_to_16_only(self):
        b = ROWS[SAT]["blocks"]
        r1 = office.flow_step_holds(b, 1)
        r2 = office.flow_step_holds(b, 2)
        for s, a, c in zip(b["flow"], r1, r2):
            n = office._step_no(s["step"])
            self.assertEqual(c, a * 2 if 10 <= n <= 16 else a, s["step"])
        self.assertEqual((sum(r1), sum(r2)), (630, 960))

    def test_totals(self):
        # Two full rounds (Ryan, 9/16: time doesn't matter).
        self.assertEqual(office.flow_total_sec(ROWS[THU]["blocks"]), 2250)   # 37.5 min
        self.assertEqual(office.flow_total_sec(ROWS[SAT]["blocks"]), 1770)   # 29.5 min
        self.assertEqual((ROWS[THU]["est_duration_min"], ROWS[SAT]["est_duration_min"]), (38, 30))


class TestProgramState(unittest.TestCase):
    def test_program_state_for_the_status_page(self):
        self.assertEqual(office.program_state(), {
            "name": "Foundation", "phase": 1, "anchor": "2026-09-16", "weeks_total": 7,
            "deload_week": 7, "end": "2026-11-03"})

    def test_written_with_the_reseed(self):
        cur = MagicMock()
        office.write_program_state(cur)
        sql, params = cur.execute.call_args.args
        self.assertIn("acos.system_state", sql)
        self.assertEqual(params[0], "health_program")
        self.assertEqual(json.loads(params[1])["anchor"], "2026-09-16")


class TestSideValidator(unittest.TestCase):
    def test_real_flow_passes(self):
        office.validate_flow(ROWS[THU]["blocks"])
        office.validate_rows(list(ROWS.values()))

    def test_rejects_a_missing_side(self):
        b = flow()
        b["flow"] = [s for s in b["flow"] if s["step"] != "12b"]
        with self.assertRaisesRegex(office.FlowError, "wind: missing side L"):
            office.validate_flow(b)

    def test_rejects_unequal_time(self):
        b = flow()
        next(s for s in b["flow"] if s["step"] == "11a")["duration_sec"] = 45
        with self.assertRaisesRegex(office.FlowError, r"twist-supine: R 45s ≠ L 30s"):
            office.validate_flow(b)

    def test_lunge_unit_compares_as_a_group(self):
        b = flow()
        by = {s["step"]: s for s in b["flow"]}
        # R: 20 + 40, L: 40 + 20 — poses differ, the unit is equal.
        by["5"]["duration_sec"], by["6"]["duration_sec"] = 20, 40
        by["8"]["duration_sec"], by["9"]["duration_sec"] = 40, 20
        office.validate_flow(b)
        by["9"]["duration_sec"] = 30
        with self.assertRaisesRegex(office.FlowError, "lunge-unit"):
            office.validate_flow(b)

    def test_rejects_a_pair_split_across_the_doubling_boundary(self):
        # Round 1 equal, round 2 not: 9 is not doubled but a partner at 10 is.
        b = flow()
        by = {s["step"]: s for s in b["flow"]}
        by["10"].update(side="L", mirror_group="edge")
        by["4"].update(side="R", mirror_group="edge")
        with self.assertRaisesRegex(office.FlowError, r"edge: .*round 2"):
            office.validate_flow(b)

    def test_side_without_group_is_rejected(self):
        b = flow()
        b["flow"][0]["side"] = "R"
        with self.assertRaises(office.FlowError):
            office.validate_flow(b)


class TestReseedDiff(unittest.TestCase):
    def test_only_thu_and_sat_from_9_19(self):
        rs = _reseed()
        rows = rs.flow_rows(date(2026, 9, 19))
        self.assertEqual([r["plan_date"].isoformat() for r in rows], [
            "2026-09-19", "2026-09-24", "2026-09-26", "2026-10-01", "2026-10-03",
            "2026-10-08", "2026-10-10", "2026-10-15", "2026-10-17", "2026-10-22",
            "2026-10-24", "2026-10-29", "2026-10-31"])
        existing = {r["plan_date"]: {"phase": 1, "week_num": r["week_num"],
                                     "session_type": "rest_mobility",
                                     "display_name": "Rest / Mobility"} for r in rows}
        lines = rs.flow_diff_lines(existing, rows)
        self.assertIn("2026-09-19 Sat p1 wk1  rest_mobility  Rest / Mobility", lines[2])
        self.assertIn("recovery_flow  Recovery Flow · home · 30 min · RPE 2", lines[2])
        self.assertIn("Recovery Flow · office gym · 38 min · RPE 2", lines[3])
        self.assertEqual(lines[-2], "13 Recovery Flow rows rewritten; no other dates touched.")
        self.assertIn('"anchor": "2026-09-16"', lines[-1])

    def test_preflight_needs_migration_033(self):
        rs = _reseed()
        cur = MagicMock()
        cur.fetchone.return_value = ("CHECK ((session_type = ANY (ARRAY['rest_mobility'::text])))",)
        self.assertEqual(rs._flow_preflight(cur)[0], False)
        cur.fetchone.return_value = ("CHECK (... 'recovery_flow'::text ...)",)
        self.assertEqual(rs._flow_preflight(cur)[0], True)

    def test_migration_033_widens_the_check(self):
        sql = (_HERE.parent / "migrations" / "033_recovery_flow_session_type.sql").read_text()
        self.assertIn("plan_session_type_check", sql)
        self.assertIn("'recovery_flow'", sql)
        self.assertIn("'rest_mobility'", sql)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(office_row(THU, plan_id=104))
        self.cur = self.db.cursor()
        self.before = copy.deepcopy(self.db.plan[THU])

    def checkin(self, text):
        return hc.process_checkin(self.cur, text, THU, checkin_id="c", now=NOW, adjust=True)

    def test_pain_day_off_overrides_the_flow(self):
        reply = self.checkin("shoulder pain 4")
        self.assertEqual(reply, "Pain shoulder 4/5 → day off. Reply `original` to undo.")
        row = self.db.plan[THU]
        self.assertEqual(row["session_type"], "rest_mobility")
        self.assertEqual(row["blocks"]["display_name"], "Day off")
        self.assertEqual(row["blocks"]["original"]["session_type"], "recovery_flow")
        self.assertEqual(hc.process_original(self.cur, THU), "Restored — run Recovery Flow as written.")
        self.assertEqual(self.db.plan[THU]["blocks"]["type"], "recovery_flow")

    def test_rising_day_off_overrides_the_flow(self):
        self.db.daily[date(2026, 9, 15)] = {"soreness": {"pain": {"knee": 1}}}
        self.db.daily[date(2026, 9, 16)] = {"soreness": {"pain": {"knee": 2}}}
        self.assertIn("(rising) → day off", self.checkin("knee pain 3"))
        self.assertEqual(self.db.plan[THU]["session_type"], "rest_mobility")

    def test_pain_2_and_3_change_nothing(self):
        self.assertEqual(self.checkin("shoulder pain 3"),
                         "Pain shoulder 3/5 — noted.\nCheck-in logged — Recovery Flow as planned.")
        self.assertEqual(self.checkin("legs pain 2"),
                         "Pain legs 2/5 — noted.\nCheck-in logged — Recovery Flow as planned.")
        self.assertEqual(self.db.plan[THU], self.before)

    def test_soreness_and_recovery_rules_do_not_apply(self):
        for text in ("sore shoulder 4 and legs 5", "legs sore 3", "slept 4 energy 1"):
            self.assertEqual(self.checkin(text), "Check-in logged — Recovery Flow as planned.", text)
        self.assertEqual(self.db.plan[THU], self.before)


class TestRampIgnoresFlows(unittest.TestCase):
    def test_a_missed_flow_is_never_loaded_slid_or_missed(self):
        from artemis import health_ramp as ramp
        cur = MagicMock()
        cur.description = [(c,) for c in ("plan_id", "plan_date", "week_num", "session_type",
                                          "status", "original_date", "blocks")]
        cur.fetchall.return_value = [
            (1, date(2026, 9, 16), 1, "strength_a", "completed", None, {}),
            (2, date(2026, 9, 17), 1, "recovery_flow", "planned", None, {}),
            (3, date(2026, 9, 18), 1, "strength_b", "planned", None, {}),
        ]
        rows = ramp._load_ramp_rows(cur)
        self.assertEqual([r["plan_id"] for r in rows], [1, 3])
        with patch.object(ramp, "_set_status") as set_status:
            actions = ramp.apply_slides(MagicMock(), rows, date(2026, 9, 25))
        touched = {c.args[1] for c in set_status.call_args_list}
        self.assertNotIn(2, touched)
        self.assertTrue(all(a["row"]["session_type"] != "recovery_flow" for a in actions))
        self.assertIn("recovery_flow", ramp.RAMP_EXCLUDED_TYPES)


class TestNudgeAndFollowups(unittest.TestCase):
    def test_nudge_fires_on_a_flow_day(self):
        from artemis.scheduler import ArtemisScheduler
        db = FakeDB(office_row(THU, plan_id=104))

        @contextmanager
        def conn():
            yield db
        sched = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch("knowledge.db.get_connection", conn), \
             patch("artemis.scheduler._local_today", return_value=THU), \
             patch.object(sched, "_post") as post:
            sched.job_checkin_nudge()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1], "No check-in yet — run Recovery Flow as written.")
        self.assertEqual(post.call_args.kwargs["tier"], "health")

    def test_no_nudge_after_a_checkin(self):
        from artemis.scheduler import ArtemisScheduler
        db = FakeDB(office_row(THU, plan_id=104))
        db.daily[THU] = {"energy": 4}

        @contextmanager
        def conn():
            yield db
        sched = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch("knowledge.db.get_connection", conn), \
             patch("artemis.scheduler._local_today", return_value=THU), \
             patch.object(sched, "_post") as post:
            sched.job_checkin_nudge()
        post.assert_not_called()

    def test_no_debrief_nag_and_no_inferred_miss_on_flow_days(self):
        from artemis import health
        plan = {"plan_id": 104, "session_type": "recovery_flow", "target_rpe": 2.0,
                "is_skipped": False}
        with patch("knowledge.db.execute_one", return_value=plan), \
             patch("knowledge.db.execute_query", return_value=[]) as q, \
             patch("knowledge.db.execute_write") as w:
            self.assertIsNone(health.run_nag_check())
            self.assertFalse(health.insert_inferred_summary())
        w.assert_not_called()


class TestWakePost(unittest.TestCase):
    def test_plan_exact_list_with_total(self):
        from artemis import wake
        lines = wake.flow_lines(ROWS[THU]["blocks"])
        self.assertEqual(lines[0], "\U0001f9d8 Today: **Recovery Flow** (office gym) — 37:30 total, hands-free.")
        self.assertEqual(lines[1], "· Stretch Trainer — 8 min: Follow the 8 placard stretches")
        self.assertTrue(lines[2].startswith("· Round 1 (10:30): Child's pose 30s · Cobra 30s · Downward dog 60s"))
        self.assertIn("High lunge R 30s", lines[2])
        self.assertIn("Seated side bend L 30s · Seated side bend R 30s", lines[2])
        self.assertEqual(lines[3], "· Round 2 (16 min): same order; bridge → easy pose held 2×")
        self.assertEqual(lines[4], "· Close: easy pose breathing — 3 min")
        home = wake.flow_lines(ROWS[SAT]["blocks"])
        self.assertEqual(home[0], "\U0001f9d8 Today: **Recovery Flow** (home) — 29:30 total, hands-free.")
        self.assertFalse(any("Stretch Trainer" in l for l in home))

    def test_wake_message_has_no_workout_later_wording(self):
        from artemis import wake
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        for row in (ROWS[THU],
                    # Tonight's 9/17 row: rest_mobility carrying flow blocks.
                    {**rest_row(THU), "blocks": ROWS[THU]["blocks"], "est_duration_min": 38}):
            with patch("artemis.health.get_today_plan", return_value=row), \
                 patch.object(wake, "_weather_line", return_value=None), \
                 patch.object(wake, "_depart_commitments", return_value=[]), \
                 patch.object(wake, "get_timezone_override", return_value=None), \
                 patch.object(wake, "local_now",
                              return_value=dt(2026, 9, 17, 4, 30, tzinfo=ZoneInfo("America/Chicago"))), \
                 patch.object(wake, "local_today", return_value=THU):
                text = wake.build_wake_message(calendar=None, held_health=[])
            self.assertIn("**Recovery Flow** (office gym) — 37:30 total", text)
            self.assertIn("Round 2 (16 min)", text)
            self.assertNotIn("workout is later", text.lower())
            self.assertEqual(wake.prompt_type_for(row), "logging_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
