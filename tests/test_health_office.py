"""HEALTH-2 — office gym rebuild.

Covers the 9/16-11/08 office schedule + ramp, location-from-blocks resolution,
the retired bike branch / trainer command / ramp job, the plan_detail + morning
calibrated renders for an office strength day, the validator, and the regression
guard: no plan row from 2026-09-21 onward references a rower or bike on trainer.

No live DB. Run:
    python3.11 tests/test_health_office.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import json
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

import artemis.health_office as office  # noqa: E402
from artemis import health  # noqa: E402

_ROWS = office.build_rows()
_BY_DATE = {r["plan_date"]: r for r in _ROWS}

_A_EXERCISES = ["Leg press", "DB bench press", "Lat pulldown", "Seated leg curl",
                "Cable face pull (rope)", "Captain's chair knee raise"]


def _wk_date(week_num: int, day_in_week: int) -> date:
    """SCHEDULE-2: weeks run Sun..Sat from WEEK2_START (week 2), so
    day_in_week 0 = Sunday. Week 1 is the 9/16..9/19 stub and isn't built."""
    return office.WEEK2_START + timedelta(days=7 * (week_num - 2) + day_in_week)


def _row(week_num: int, session_type: str) -> dict:
    """The row for a session type in a program week (SCHEDULE-2 moved the days)."""
    return next(r for r in _ROWS if r["week_num"] == week_num
                and r["session_type"] == session_type)


class TestSchedule(unittest.TestCase):
    def test_window_and_count(self):
        # SCHEDULE-2: weeks 2..7 are Sun..Sat; the 9/16..9/19 week-1 stub is
        # logged history and is not regenerated.
        self.assertEqual(len(_ROWS), 42)          # 6 weeks x 7 days
        self.assertEqual(min(_BY_DATE), date(2026, 9, 20))
        self.assertEqual(max(_BY_DATE), date(2026, 10, 31))
        office.validate_rows(_ROWS)

    def test_first_day_of_week_2_is_the_cycle_anchor(self):
        """SCHEDULE-2: Sun 9/20 is cycle day 1 and the start of program week 2."""
        r = _BY_DATE[date(2026, 9, 20)]
        self.assertEqual(date(2026, 9, 20).weekday(), 6, "9/20 must be a Sunday")
        self.assertEqual(office.CYCLE_ANCHOR, date(2026, 9, 20))
        self.assertEqual(r["session_type"], "recovery_flow")
        self.assertEqual(r["week_num"], 2)
        # the first lift of the cycle is the Monday
        a = _BY_DATE[date(2026, 9, 21)]
        self.assertEqual(a["session_type"], "strength_a")
        self.assertEqual(a["week_num"], 2)
        self.assertEqual(a["phase"], 1)
        self.assertEqual(a["blocks"]["display_name"], "Office Strength A")
        self.assertEqual(a["blocks"]["location"], "office gym")
        self.assertEqual(a["blocks"]["rounds"], 2)
        self.assertEqual(a["target_rpe"], 6.0)

    def test_no_rampup_rows_remain(self):
        self.assertFalse([r for r in _ROWS if "wk0" in r["notes"]])
        self.assertFalse([r for r in _ROWS if r["blocks"]["display_name"] == "Recovery Walk"])

    def test_strength_only_on_office_days(self):
        """SCHEDULE-2: strength lands only on msp_work days; wi and travel days
        get sessions that need no gym."""
        for r in _ROWS:
            d = r["plan_date"]
            self.assertNotEqual(r["session_type"], "rest_mobility")
            if r["session_type"].startswith("strength"):
                self.assertEqual(office.day_type(d), "msp_work", d)
            if office.day_type(d) in ("wi", "travel"):
                self.assertIn(r["session_type"], ("recovery_flow", "cardio_z2", "walk"), d)

    def test_exactly_one_back_to_back_pair_per_week(self):
        """SCHEDULE-2's 1st/3rd/4th office-day rule puts two lifts together:
        Wed+Thu in cycle week 1, Thu+Fri in week 2. ACCEPTED AND INTENTIONAL
        (Ryan, 2026-09-19) — this test pins it so it can't drift silently."""
        pairs = []
        by_date = sorted(_BY_DATE)
        for a, b in zip(by_date, by_date[1:]):
            if (_BY_DATE[a]["session_type"].startswith("strength")
                    and _BY_DATE[b]["session_type"].startswith("strength")):
                pairs.append((a, b))
        self.assertEqual(len(pairs), 6)          # one per program week
        for a, b in pairs:
            self.assertEqual((_BY_DATE[a]["session_type"], _BY_DATE[b]["session_type"]),
                             ("strength_b", "strength_c"))

    def test_weekly_pattern_and_weeks(self):
        # Sun..Sat. Odd cycle weeks lift Mon/Wed/Thu, even ones Tue/Thu/Fri.
        odd = ["recovery_flow", "strength_a", "cardio_z2", "strength_b",
               "strength_c", "recovery_flow", "walk"]
        even = ["recovery_flow", "walk", "strength_a", "cardio_z2", "strength_b",
                "strength_c", "recovery_flow"]
        for wk in range(2, 8):
            pattern = odd if (wk % 2 == 0) else even
            for wd, st in enumerate(pattern):
                r = _BY_DATE[_wk_date(wk, wd)]
                self.assertEqual(r["session_type"], st, r["plan_date"])
                self.assertEqual(r["week_num"], wk)
                self.assertEqual(r["phase"], 1)
                self.assertEqual(r["generated_by"], "manual")

    def test_ramp_sets_rpe_z2(self):
        ramp = {1: (2, 6.0, 20), 2: (2, 6.0, 20), 3: (3, 7.0, 30), 4: (3, 7.0, 30),
                5: (3, 7.5, 40), 6: (3, 7.5, 40), 7: (2, 6.0, 30)}
        for wk, (sets, rpe, z2) in ramp.items():
            wk_rows = [r for r in _ROWS if r["week_num"] == wk]
            if not wk_rows:                  # week 1 is the un-regenerated stub
                continue
            lifts = [r for r in wk_rows if r["session_type"].startswith("strength")]
            self.assertEqual(len(lifts), 3, wk)
            for r in lifts:
                self.assertEqual(r["blocks"]["rounds"], sets)
                self.assertEqual(r["target_rpe"], rpe)
            z = next(r for r in wk_rows if r["session_type"] == "cardio_z2")
            # office Z2 carries the 5 min Stretch Trainer cooldown
            self.assertEqual(z["blocks"]["duration_min"], z2)
            self.assertEqual(z["est_duration_min"], z2 + office.COOLDOWN_MIN)
            self.assertEqual(z["blocks"]["cooldown"], office.COOLDOWN)
            self.assertIn(office.EQ_STRETCH, z["blocks"]["equipment"])
            self.assertEqual(z["target_hr_zone"], 2)
            self.assertIn("conversational pace", z["blocks"]["setup_notes"][0])
        wk5_z2 = next(r for r in _ROWS if r["week_num"] == 5 and r["session_type"] == "cardio_z2")
        self.assertEqual(wk5_z2["blocks"]["target_range_min"], [35, 40])

    def test_strength_day_contract(self):
        for r in _ROWS:
            if not r["session_type"].startswith("strength"):
                continue
            b = r["blocks"]
            self.assertEqual(b["location"], "office gym")
            self.assertEqual(b["warmup"], "5 min elliptical, easy")
            self.assertEqual(b["cooldown"], "5 min Stretch Trainer")
            for ex in b["exercises"]:
                self.assertEqual(ex["format"], "reps")
                if r["week_num"] <= 2:
                    self.assertIn("target_load_lbs", ex)
                    self.assertIsNone(ex["target_load_lbs"])
                else:
                    self.assertNotIn("target_load_lbs", ex)

    def test_exercise_lists_and_top_of_range(self):
        a = _row(2, "strength_a")["blocks"]["exercises"]
        self.assertEqual([e["name"] for e in a], _A_EXERCISES)
        self.assertEqual(a[0]["target_reps"], 12)
        self.assertEqual(a[4]["target_reps"], 15)
        b = _row(2, "strength_b")["blocks"]["exercises"]
        self.assertEqual(b[0]["name"], "DB goblet squat")
        self.assertEqual(len(b), 7)
        pallof = next(e for e in b if e["name"] == "Cable Pallof press")
        self.assertEqual(pallof["target_reps"], 10)
        self.assertIn("each side", pallof["notes"])
        c = _row(2, "strength_c")["blocks"]["exercises"]
        self.assertEqual(c[0]["name"], "DB Romanian deadlift")

    def test_machine_setting_note_week1_only(self):
        # Week 1 is the stub; the seat/pin note belongs to it, so weeks 2+ are clean.
        for wk in range(2, 8):
            lp = _row(wk, "strength_a")["blocks"]["exercises"][0]
            self.assertNotIn("pin setting", lp["notes"], wk)

    def test_finisher_and_smith_alt_weeks_5_6_only(self):
        for wk in range(2, 8):
            c = _row(wk, "strength_c")["blocks"]
            b = _row(wk, "strength_b")["blocks"]
            goblet = b["exercises"][0]["notes"]
            if wk in (5, 6):
                self.assertEqual(c["finisher"]["rounds"], 6)
                self.assertEqual(c["finisher"]["exercises"][0]["duration_sec"], 30)
                self.assertEqual(c["finisher"]["exercises"][0]["rest_after_sec"], 90)
                self.assertIn("Smith squat", goblet)
            else:
                self.assertNotIn("finisher", c)
                self.assertNotIn("Smith", goblet)
            for wd in (0, 2):   # strength_a, strength_b never carry it
                self.assertNotIn("finisher", _BY_DATE[_wk_date(wk, wd)]["blocks"])

    def test_walk_and_flow(self):
        w = next(r for r in _ROWS if r["session_type"] == "walk")
        self.assertEqual(w["blocks"]["location"], "outside")
        self.assertEqual(w["est_duration_min"], 30)
        f = next(r for r in _ROWS if r["session_type"] == "recovery_flow")
        self.assertEqual(f["blocks"]["type"], "recovery_flow")


class TestRegressionNoRetiredEquipment(unittest.TestCase):
    def test_no_rower_or_bike_on_trainer_at_the_office(self):
        """The retired kit stays out of OFFICE rows. Richfield really has a
        rower and a bike on a trainer, so its Z2 rows may name them."""
        for r in _ROWS:
            if r["blocks"].get("location") != office.LOCATION:
                continue
            blob = (json.dumps(r["blocks"]) + " " + r["notes"]).lower()
            self.assertNotIn("rower", blob, r["plan_date"])
            self.assertNotIn("bike on trainer", blob, r["plan_date"])

    def test_validator_rejects_injected_rower(self):
        rows = office.build_rows()
        rows[10]["blocks"].setdefault("equipment", []).append("water rower")
        with self.assertRaises(AssertionError):
            office.validate_rows(rows)

    def test_live_validator_selftest_pass_and_fault(self):
        import scripts.validate_health_plan as v
        self.assertEqual(v.report(v.evaluate(v.selftest_live(fault=False))), 0)
        results = v.evaluate(v.selftest_live(fault=True))
        failed = {r["category"] for r in results if not r["ok"]}
        self.assertEqual(v.report(results), 1)
        self.assertTrue({"NO_RETIRED", "SESSION_TYPE", "PRESENT", "HARD_STOP"} <= failed)


class TestResolver(unittest.TestCase):
    def test_blocks_location_and_equipment_win(self):
        r = health.resolve_equipment_and_location(
            "strength_a", blocks={"location": "home gym", "equipment": ["DBs"]})
        self.assertEqual(r["location"], "home gym")
        self.assertEqual(r["equipment"], ["DBs"])

    def test_fallback_map_office(self):
        for st, lift in (("strength_a", "Leg press"), ("strength_b", "DB goblet squat"),
                         ("strength_c", "DB Romanian deadlift")):
            r = health.resolve_equipment_and_location(st)
            self.assertEqual(r["location"], "office gym")
            self.assertEqual(r["first_lift"], lift)
        self.assertIn("pulldown", health.resolve_equipment_and_location("strength_a")["equipment"])
        self.assertIn("stepmill", health.resolve_equipment_and_location("cardio_intervals")["equipment"])
        self.assertNotIn("downstairs gym", json.dumps(health._EQUIPMENT_MAP))

    def test_cardio_ignores_weather(self):
        r = health.resolve_equipment_and_location(
            "cardio_z2", weather={"temp_f": 20.0, "precip_next_90min": True},
            blocks=next(x for x in _ROWS if x["session_type"] == "cardio_z2")["blocks"])
        # SCHEDULE-2: Z2 runs at the office; the flows are what travel.
        self.assertEqual(r["location"], "office gym")
        self.assertIsNone(r["notes"])

    def test_walk_weather_still_applies(self):
        r = health.resolve_equipment_and_location(
            "walk", weather={"temp_f": 30.0},
            blocks=next(r for r in _ROWS if r["session_type"] == "walk")["blocks"])
        self.assertIn("Cold", r["notes"])


class TestRenders(unittest.TestCase):
    def test_plan_detail_0916_office_a_no_bike_weather(self):
        row = dict(_BY_DATE[date(2026, 9, 21)])
        text = health._render_full_block(date(2026, 9, 21), row, date(2026, 9, 21))
        self.assertIn("Office Strength A", text)
        for name in _A_EXERCISES:
            self.assertIn(name, text)
        low = text.lower()
        for bad in ("bike", "weather", "rower", "bike on trainer", "trainer set"):
            self.assertNotIn(bad, low)

    def test_monday_wk5_renders_conditioning_finisher(self):
        d = _wk_date(5, 5)   # Mon strength_c
        text = health._render_full_block(d, dict(_BY_DATE[d]), d)
        self.assertIn("**Conditioning finisher** — 6 rounds", text)

    def test_wake_workout_section_where_office_gym(self):
        from artemis import wake
        row = dict(_BY_DATE[date(2026, 9, 21)])
        post = "\n".join(wake._workout_section(row))
        self.assertIn("Today: **Office Strength A** —", post)
        self.assertNotIn("Push/Legs", post)
        self.assertIn("Where: office gym", post)
        self.assertIn("1. Leg press — 2×10-12 · RPE ≤6", post)
        self.assertIn("Warmup: 5 min elliptical, easy", post)


class TestRetired(unittest.TestCase):
    def test_bike_symbols_removed(self):
        for name in ("_BIKE_SESSIONS", "_BIKE_ALTERNATIVE", "_blocks_use_bike", "is_bike_session",
                     "read_bike_override", "write_bike_override", "handle_trainer_override",
                     "INTENT_TRAINER_OVERRIDE", "_swap_weather_context"):
            self.assertFalse(hasattr(health, name), name)

    def test_trainer_set_is_honest_retired_reply(self):
        for msg in ("trainer set indoor", "@artemis trainer set outdoor"):
            self.assertEqual(health.detect_health_intent(msg), health.INTENT_TRAINER_RETIRED)
        reply = health.format_trainer_retired()
        self.assertIn("Nothing changed", reply)
        self.assertIsNone(health.claims_unverified_action(reply))
        self.assertNotIn("trainer set", health.format_health_help())

    def test_ramp_job_not_registered(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        self.assertNotIn("job_health_ramp", src)
        self.assertNotIn('id="health_ramp"', src)

    def test_reseed_ramp_flag_is_gone(self):
        # RAMP-RETIRE (2026-09-19): --ramp no longer exists; argparse rejects it.
        import scripts.reseed_health_plan_v2 as reseed
        with patch.object(sys, "argv", ["reseed", "--ramp", "--commit"]), \
             patch("sys.stderr"):
            with self.assertRaises(SystemExit) as cm:
                reseed.main()
        self.assertEqual(cm.exception.code, 2)

    def test_ramp_engine_is_deleted(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("artemis.health_ramp"))

    def test_plan_term_regex_office_terms(self):
        for msg in ("show my leg press", "what's the pulldown weight", "my stepmill intervals"):
            self.assertEqual(health.detect_health_intent(msg), health.INTENT_PLAN_DETAIL, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
