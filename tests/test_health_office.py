"""HEALTH-2 — office gym rebuild.

Covers the 9/16-11/08 office schedule + ramp, location-from-blocks resolution,
the retired bike branch / trainer command / ramp job, the plan_detail + morning
calibrated renders for an office strength day, the validator, and the regression
guard: no plan row from 2026-09-21 onward references a rower or bike on trainer.

No live DB. Run:
    python3.11 tests/test_health_office.py
"""

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


def _wk_date(week_num: int, weekday: int) -> date:
    return office.WEEK1_MONDAY + timedelta(days=7 * (week_num - 1) + weekday)


class TestSchedule(unittest.TestCase):
    def test_window_and_count(self):
        self.assertEqual(len(_ROWS), 54)
        self.assertEqual(min(_BY_DATE), date(2026, 9, 16))
        self.assertEqual(max(_BY_DATE), date(2026, 11, 8))
        office.validate_rows(_ROWS)

    def test_rampup_days(self):
        expected = {date(2026, 9, 16): "cardio_z2", date(2026, 9, 17): "strength_a",
                    date(2026, 9, 18): "cardio_z2", date(2026, 9, 19): "walk",
                    date(2026, 9, 20): "rest_mobility"}
        for d, st in expected.items():
            r = _BY_DATE[d]
            self.assertEqual(r["session_type"], st, d)
            self.assertEqual(r["week_num"], 1)
            self.assertIn("wk0", r["notes"])
        self.assertEqual(_BY_DATE[date(2026, 9, 17)]["blocks"]["rounds"], 2)
        self.assertEqual(_BY_DATE[date(2026, 9, 18)]["blocks"]["duration_min"], 20)
        self.assertEqual(_BY_DATE[date(2026, 9, 19)]["blocks"]["display_name"], "Recovery Walk")

    def test_weekly_pattern_and_weeks(self):
        pattern = ["strength_a", "cardio_z2", "rest_mobility", "strength_b",
                   "strength_c", "rest_mobility", "walk"]
        for wk in range(1, 8):
            for wd, st in enumerate(pattern):
                r = _BY_DATE[_wk_date(wk, wd)]
                self.assertEqual(r["session_type"], st)
                self.assertEqual(r["week_num"], wk)
                self.assertEqual(r["phase"], 1)
                self.assertEqual(r["generated_by"], "manual")

    def test_ramp_sets_rpe_z2(self):
        ramp = {1: (2, 6.0, 20), 2: (2, 6.0, 20), 3: (3, 7.0, 30), 4: (3, 7.0, 30),
                5: (3, 7.5, 40), 6: (3, 7.5, 40), 7: (2, 6.0, 30)}
        for wk, (sets, rpe, z2) in ramp.items():
            for wd in (0, 3, 4):
                r = _BY_DATE[_wk_date(wk, wd)]
                self.assertEqual(r["blocks"]["rounds"], sets)
                self.assertEqual(r["target_rpe"], rpe)
            z = _BY_DATE[_wk_date(wk, 1)]
            self.assertEqual(z["est_duration_min"], z2)
            self.assertEqual(z["target_hr_zone"], 2)
            self.assertIn("conversational pace", z["blocks"]["setup_notes"][0])
        self.assertEqual(_BY_DATE[_wk_date(5, 1)]["blocks"]["target_range_min"], [35, 40])

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
        a = _BY_DATE[_wk_date(1, 0)]["blocks"]["exercises"]
        self.assertEqual([e["name"] for e in a], _A_EXERCISES)
        self.assertEqual(a[0]["target_reps"], 12)
        self.assertEqual(a[4]["target_reps"], 15)
        b = _BY_DATE[_wk_date(1, 3)]["blocks"]["exercises"]
        self.assertEqual(b[0]["name"], "DB goblet squat")
        self.assertEqual(len(b), 7)
        pallof = next(e for e in b if e["name"] == "Cable Pallof press")
        self.assertEqual(pallof["target_reps"], 10)
        self.assertIn("each side", pallof["notes"])
        c = _BY_DATE[_wk_date(1, 4)]["blocks"]["exercises"]
        self.assertEqual(c[0]["name"], "DB Romanian deadlift")

    def test_machine_setting_note_week1_only(self):
        lp1 = _BY_DATE[_wk_date(1, 0)]["blocks"]["exercises"][0]
        self.assertIn("log seat + pin setting", lp1["notes"])
        db1 = _BY_DATE[_wk_date(1, 0)]["blocks"]["exercises"][1]
        self.assertNotIn("pin setting", db1["notes"])
        lp2 = _BY_DATE[_wk_date(2, 0)]["blocks"]["exercises"][0]
        self.assertNotIn("pin setting", lp2["notes"])

    def test_finisher_and_smith_alt_weeks_5_6_only(self):
        for wk in range(1, 8):
            c = _BY_DATE[_wk_date(wk, 4)]["blocks"]
            b = _BY_DATE[_wk_date(wk, 3)]["blocks"]
            goblet = b["exercises"][0]["notes"]
            if wk in (5, 6):
                self.assertEqual(c["finisher"]["rounds"], 6)
                self.assertEqual(c["finisher"]["exercises"][0]["duration_sec"], 30)
                self.assertEqual(c["finisher"]["exercises"][0]["rest_after_sec"], 90)
                self.assertIn("Smith squat", goblet)
            else:
                self.assertNotIn("finisher", c)
                self.assertNotIn("Smith", goblet)
            for wd in (0, 3):
                self.assertNotIn("finisher", _BY_DATE[_wk_date(wk, wd)]["blocks"])

    def test_walk_and_rest(self):
        w = _BY_DATE[_wk_date(3, 6)]
        self.assertEqual(w["blocks"]["location"], "outside")
        self.assertEqual(w["est_duration_min"], 30)
        self.assertEqual(_BY_DATE[_wk_date(3, 2)]["blocks"]["type"], "mobility")


class TestRegressionNoRetiredEquipment(unittest.TestCase):
    def test_no_rower_or_bike_on_trainer_from_0921(self):
        for r in _ROWS:
            if r["plan_date"] < date(2026, 9, 21):
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
            blocks=_BY_DATE[_wk_date(1, 1)]["blocks"])
        self.assertEqual(r["location"], "office gym")
        self.assertIsNone(r["notes"])

    def test_walk_weather_still_applies(self):
        r = health.resolve_equipment_and_location(
            "walk", weather={"temp_f": 30.0}, blocks=_BY_DATE[_wk_date(1, 6)]["blocks"])
        self.assertIn("Cold", r["notes"])


class TestRenders(unittest.TestCase):
    def test_plan_detail_0921_office_a_no_bike_weather(self):
        row = dict(_BY_DATE[date(2026, 9, 21)])
        text = health._render_full_block(date(2026, 9, 21), row, date(2026, 9, 21))
        self.assertIn("Office Strength A", text)
        for name in _A_EXERCISES:
            self.assertIn(name, text)
        low = text.lower()
        for bad in ("bike", "weather", "rower", "bike on trainer", "trainer set"):
            self.assertNotIn(bad, low)

    def test_friday_wk5_renders_conditioning_finisher(self):
        d = _wk_date(5, 4)
        text = health._render_full_block(d, dict(_BY_DATE[d]), d)
        self.assertIn("**Conditioning finisher** — 6 rounds", text)

    def test_morning_calibrated_post_where_office_gym(self):
        row = _BY_DATE[date(2026, 9, 21)]
        plan = {"session_type": row["session_type"], "est_duration_min": row["est_duration_min"],
                "blocks": row["blocks"]}
        resolved = health.resolve_equipment_and_location(row["session_type"], blocks=row["blocks"])
        post = health.build_calibrated_plan_post(plan, resolved, state=None)
        self.assertIn("Where: office gym", post)
        self.assertIn("First lift: Leg press", post)
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

    def test_reseed_ramp_refuses(self):
        import scripts.reseed_health_plan_v2 as reseed
        with patch.object(sys, "argv", ["reseed", "--ramp", "--commit"]):
            with self.assertRaises(SystemExit) as cm:
                reseed.main()
        self.assertIn("retired", str(cm.exception.code))

    def test_plan_term_regex_office_terms(self):
        for msg in ("show my leg press", "what's the pulldown weight", "my stepmill intervals"):
            self.assertEqual(health.detect_health_intent(msg), health.INTENT_PLAN_DETAIL, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
