"""Soreness / pain side — "sore 1 right knee" keeps the side.

Runs the real parser and handlers against the in-memory FakeDB from
test_checkin_adjust. No RDS.

Run:
    python3 tests/test_soreness_side.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_checkin_adjust import FRI, FakeDB, names, office_row  # noqa: E402

from artemis import health_checkin as hc  # noqa: E402
from artemis import health_patterns as hp  # noqa: E402
from artemis import health_regions as hr  # noqa: E402

NOW = datetime(2026, 9, 18, 10, 10, tzinfo=timezone.utc)


class TestParse(unittest.TestCase):
    def test_sore_1_right_knee(self):
        ci = hc.parse_checkin("Sleep 9, energy 4, sore 1 right knee, weight 286")
        self.assertEqual(ci.soreness, {"knee": 1})
        self.assertEqual(ci.sides, {"knee": "right"})
        self.assertEqual(ci.soreness_json(), {"knee": 1, "sides": {"knee": "right"}})

    def test_sore_knees_2_is_unspecified(self):
        ci = hc.parse_checkin("sore knees 2")
        self.assertEqual(ci.soreness, {"knee": 2})
        self.assertEqual(ci.soreness_json(), {"knee": 2, "sides": {"knee": "unspecified"}})

    def test_side_word_after_the_region_and_pain_kept_apart(self):
        ci = hc.parse_checkin("knee right sore 2, left shoulder pain 3")
        self.assertEqual(ci.sides, {"knee": "right"})
        self.assertEqual(ci.pain_sides, {"shoulder": "left"})
        self.assertEqual(ci.soreness_json()["pain_sides"], {"shoulder": "left"})

    def test_both_sides_or_two_sides_is_unspecified(self):
        for text in ("both knees sore 2", "left and right knee sore 2",
                     "sore left knee 1, sore right knee 3"):
            with self.subTest(text=text):
                ci = hc.parse_checkin(text)
                self.assertEqual(ci.sides["knee"], "unspecified")
        # the higher score wins when the sides differ
        self.assertEqual(hc.parse_checkin("sore left knee 1, sore right knee 3").soreness, {"knee": 3})

    def test_sided_parts_joined_by_and(self):
        ci = hc.parse_checkin("sore left knee and right shoulder 2")
        self.assertEqual(ci.soreness, {"knee": 2, "shoulder": 2})
        self.assertEqual(ci.sides, {"knee": "left", "shoulder": "right"})

    def test_overall_soreness_has_no_side(self):
        self.assertNotIn("sides", hc.parse_checkin("sore 3").soreness_json() or {})


class TestReplyAndMatching(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(office_row(FRI))
        self.cur = self.db.cursor()

    def checkin(self, text):
        return hc.process_checkin(self.cur, text, FRI, checkin_id="p", now=NOW, adjust=True)

    def test_reply_echoes_the_side_and_the_side_is_stored(self):
        reply = self.checkin("sore right knee 4")
        self.assertIn("Right knee 4/5 → removed", reply)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"knee": 4, "sides": {"knee": "right"}})

    def test_pain_reply_echoes_the_side(self):
        reply = self.checkin("right knee pain 1")
        self.assertIn("Pain right knee 1/5", reply)

    def test_sided_soreness_counts_as_the_region_for_bilateral_exercises(self):
        self.checkin("sore right knee 4")
        right = names(self.db)
        db2 = FakeDB(office_row(FRI))
        hc.process_checkin(db2.cursor(), "sore knees 4", FRI, checkin_id="q", now=NOW, adjust=True)
        self.assertEqual(right, names(db2))   # same removals as the unsided region

    def test_a_one_sided_exercise_only_matches_its_side(self):
        with patch.dict(hr.EXERCISE_SIDE, {"Leg extension": "left"}):
            self.assertFalse(hr.uses_any("Leg extension", ["knee"], sides={"knee": "right"}))
            self.assertTrue(hr.uses_any("Leg extension", ["knee"], sides={"knee": "left"}))
            self.assertTrue(hr.uses_any("Leg extension", ["knee"], sides={"knee": "unspecified"}))
            self.assertTrue(hr.uses_any("Leg extension", ["knee"]))
            self.checkin("sore right knee 4")
            self.assertIn("Leg extension", names(self.db))      # right knee spares a left-only move

    def test_side_key_round_trip(self):
        for region, side in (("knee", "right"), ("low back", "left"), ("knee", "unspecified")):
            self.assertEqual(hr.split_side_key(hr.side_key(region, side)), (region, side))


class TestPatternsSplitBySide(unittest.TestCase):
    def test_notes_and_checkins_key_on_region_plus_side(self):
        self.assertEqual(hp.parse_pain_notes("pain=right knee:3; pain=knee:2; felt off"),
                         [("right knee", 3), ("knee", 2)])
        self.assertEqual(hp._pain_of({"pain": {"knee": 2}, "pain_sides": {"knee": "left"}}),
                         {"left knee": 2})
        self.assertEqual(hp._pain_of({"pain": {"knee": 2}}), {"knee": 2})

    def test_a_pattern_splits_by_side(self):
        today = date(2026, 9, 20)
        days = [today - timedelta(days=k) for k in (12, 9, 6, 3)]
        logs = [(d, "Leg extension", "pain=right knee:3", False) for d in days[:3]]
        logs.append((days[3], "Leg extension", "pain=left knee:3", False))
        # the morning after the last session: left knee pain from the check-in
        checkins = {days[3] + timedelta(days=1): {"pain": {"knee": 2}, "pain_sides": {"knee": "left"}}}
        t = hp.tally(logs, checkins, today)
        self.assertEqual(set(t), {("Leg extension", "right knee"), ("Leg extension", "left knee")})
        right, left = t[("Leg extension", "right knee")], t[("Leg extension", "left knee")]
        self.assertEqual((right.hits, right.exposures, right.qualifies), (3, 4, True))
        self.assertEqual((left.hits, left.qualifies), (1, False))   # one hit, noted once
        row = {"region": "right knee", "exercise": "Leg extension", "hits": 3, "exposures": 4,
               "evidence": {}}
        self.assertIn("right knee pain ≥2 after **leg extension** — 3 of 4", hp.render(row))


class TestReadersIgnoreTheSideMaps(unittest.TestCase):
    def test_lambda_overview_scores_skip_sides(self):
        try:
            from api.app.routers.health import _scores
        except Exception as e:  # fastapi/sqlalchemy not installed here
            self.skipTest(f"api deps missing: {e}")
        sore, pain = _scores({"knee": 1, "sides": {"knee": "right"},
                              "pain": {"shoulder": 2}, "pain_sides": {"shoulder": "left"}})
        self.assertEqual((sore, pain), ({"knee": 1}, {"shoulder": 2}))


if __name__ == "__main__":
    unittest.main()
