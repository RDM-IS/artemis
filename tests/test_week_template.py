"""WEEK-TEMPLATE + FULLBODY-1 (approved 2026-11-01). No database: the cycle's
override read is stubbed, so these test the table and the resolution, not RDS."""
import os
import re
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

os.environ["ARTEMIS_TEST_NO_DB"] = "1"

from artemis import block2, cycle, full_body, week_template as wt  # noqa: E402
from artemis import health_office as office  # noqa: E402
from knowledge import session_types  # noqa: E402

LOC = cycle.DEFAULT_LOCATIONS
ROOT = Path(__file__).resolve().parent.parent


def _spec(pos, key, d=date(2031, 3, 3)):
    return {"plan_date": d, "slot": "morning", "pos": pos, "location_key": key,
            "location": key, "session_type": "cardio_z2", "week_num": 1,
            "day_type": "x", "wk0": False}


class TableTest(unittest.TestCase):
    def test_every_position_has_a_kind(self):
        self.assertEqual(sorted(wt.TABLE), list(range(14)))

    def test_two_lifts_a_week_and_every_sunday_rest(self):
        for week in (range(0, 7), range(7, 14)):
            kinds = [wt.TABLE[p] for p in week]
            self.assertEqual(kinds.count(wt.STRENGTH), 2)
            self.assertEqual(kinds[0], wt.REST)

    def test_no_floor_work_the_day_after_strength(self):
        for p in range(14):
            if wt.TABLE[p] == wt.STRENGTH:
                self.assertNotEqual(wt.TABLE[(p + 1) % 14], wt.CIRCUIT, p)

    def test_never_two_circuits_in_a_row(self):
        for p in range(14):
            self.assertFalse(wt.TABLE[p] == wt.CIRCUIT == wt.TABLE[(p + 1) % 14], p)


class PlaceTest(unittest.TestCase):
    def test_strength_in_msp_goes_to_the_office(self):
        out = wt.place(_spec(3, "office"), LOC)
        self.assertEqual((out["session_type"], out["location_key"]), ("strength_full", "office"))
        out = wt.place(_spec(8, "msp_home"), LOC)          # an off Monday in MSP
        self.assertEqual(out["location_key"], "office")

    def test_strength_at_richfield(self):
        out = wt.place(_spec(5, "richfield"), LOC)
        self.assertEqual((out["session_type"], out["location_key"]), ("strength_full", "richfield"))

    def test_strength_where_it_cant_run_is_that_rooms_ladder(self):
        out = wt.place(_spec(5, "brown_deer"), LOC)
        self.assertEqual((out["session_type"], out["location_key"]), ("cardio_z2", "brown_deer"))
        self.assertNotIn("row_kind", out)

    def test_msp_cardio_is_the_rower_at_home(self):
        for pos, kind in ((1, "circuit"), (2, "ladder"), (12, "circuit")):
            out = wt.place(_spec(pos, "office"), LOC)
            self.assertEqual((out["row_kind"], out["location_key"]), (kind, "msp_home"))

    def test_a_circuit_outside_msp_runs_as_the_ladder(self):
        out = wt.place(_spec(1, "richfield"), LOC)
        self.assertEqual(out["session_type"], "cardio_z2")
        self.assertNotIn("row_kind", out)

    def test_an_unknown_room_rests(self):
        self.assertEqual(wt.place(_spec(2, "outside"), LOC)["session_type"], "rest")

    def test_place_does_not_mutate(self):
        spec = _spec(3, "office")
        wt.place(spec, LOC)
        self.assertEqual(spec["session_type"], "cardio_z2")


class FullBodyTest(unittest.TestCase):
    def test_both_rooms_build_with_known_names(self):
        for key in full_body.ROOMS:
            blocks, rpe, zone, est = full_body.build(key, key, on=date(2031, 3, 3))
            self.assertEqual(len(blocks["segments"]), 3)
            for ex in blocks["exercises"]:
                self.assertIn(ex["segment"], (0, 1, 2))
                self.assertIn(ex["name"], office.EQUIPMENT_CLASS)
            self.assertEqual(est, 60)
            self.assertEqual(office.duration_verdict("strength_full", 1, est)[0], "note")

    def test_no_trx_at_the_office(self):
        blocks, *_ = full_body.build("office", "office gym", on=date(2031, 3, 3))
        self.assertFalse([e for e in blocks["exercises"] if e["equipment_class"] == "trx"])

    def test_first_session_is_three_rounds_at_rpe_6(self):
        blocks, rpe, _, est = full_body.build("office", "office gym",
                                              on=full_body.FIRST_SESSION_ON_OR_BEFORE)
        self.assertEqual((blocks["segments"][1]["rounds"], rpe), (3, 6.0))
        self.assertLess(est, 60)

    def test_unilateral_work_is_in_the_first_segment(self):
        for key in full_body.ROOMS:
            blocks, *_ = full_body.build(key, key, on=date(2031, 3, 3))
            first = [e["name"] for e in blocks["exercises"] if e["segment"] == 0]
            self.assertTrue(all("each side" in e["notes"] for e in blocks["exercises"]
                                if e["name"] in first))

    def test_brown_deer_fails_closed(self):
        with self.assertRaises(ValueError):
            full_body.build("brown_deer", "Brown Deer")
        self.assertFalse(office.can_hold("brown_deer", "strength_full"))

    def test_every_lift_has_a_profile(self):
        lifts = {n for n in full_body.all_names()
                 if office.EQUIPMENT_CLASS[n] not in ("cardio", "bodyweight")
                 or n == "Push-up"}
        self.assertEqual(sorted(n for n in lifts if n not in office.LIFT_PROFILE), [])


class Block2Test(unittest.TestCase):
    def _rows(self):
        with mock.patch.object(cycle, "override_for", lambda d, **k: None), \
             mock.patch("artemis.quiet_hours.get_system_value", lambda k: None):
            return block2.build_rows([], placements={}, away_stays=[])

    def test_mornings_follow_the_template_from_its_first_day(self):
        rows = [r for r in self._rows() if r["slot"] == "morning"
                and r["plan_date"] >= wt.TEMPLATE_FROM]
        self.assertTrue(rows)
        for r in rows:
            kind = wt.TABLE[office.cycle_pos(r["plan_date"])]
            st = r["session_type"]
            if kind == wt.REST:
                self.assertEqual(st, "rest", r["plan_date"])
            elif kind == wt.STRENGTH:
                self.assertIn(st, ("strength_full", "cardio_z2"), r["plan_date"])
            else:
                self.assertEqual(st, "cardio_z2", r["plan_date"])

    def test_one_morning_a_day_and_no_row_kind_on_an_evening(self):
        rows = self._rows()
        mornings = [r["plan_date"] for r in rows if r["slot"] == "morning"]
        self.assertEqual(len(mornings), len(set(mornings)))
        for r in rows:
            if r["slot"] == "evening":
                self.assertNotIn("row_kind", r["blocks"])

    def test_full_body_rpe_is_6_first_then_7_and_no_block_2_deload(self):
        """Ryan, 2026-11-01: first session RPE 6, every later one 7; no scheduled
        deload in block 2 (the 11/21-30 break is it)."""
        lifts = [r for r in self._rows() if r["session_type"] == "strength_full"]
        self.assertTrue(lifts)
        for r in lifts:
            want = 6.0 if r["plan_date"] <= full_body.FIRST_SESSION_ON_OR_BEFORE else 7.0
            self.assertEqual(r["target_rpe"], want, r["plan_date"])

    def test_a_placement_still_wins(self):
        d = wt.TEMPLATE_FROM + timedelta(days=2)
        with mock.patch.object(cycle, "override_for", lambda d, **k: None), \
             mock.patch("artemis.quiet_hours.get_system_value", lambda k: None):
            rows = block2.build_rows([], placements={d: "rest"}, away_stays=[])
        got = [r for r in rows if r["plan_date"] == d and r["slot"] == "morning"]
        self.assertEqual(got[0]["session_type"], "rest")


class OneStrengthListTest(unittest.TestCase):
    """SAME-FACT-MANY-PLACES: a fresh literal copy of the strength tuple fails."""
    PATTERN = re.compile(r'\(\s*"strength_a",\s*"strength_b",\s*"strength_c"\s*\)')

    def test_no_literal_copy_outside_session_types(self):
        hits = []
        for sub in ("artemis", "knowledge", "api"):
            for p in (ROOT / sub).rglob("*.py"):
                if p.name == "session_types.py":
                    continue
                if self.PATTERN.search(p.read_text()):
                    hits.append(str(p.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_the_list_includes_full_body(self):
        self.assertIn("strength_full", session_types.STRENGTH_TYPES)
        self.assertTrue(session_types.is_training("strength_full"))


if __name__ == "__main__":
    unittest.main()
