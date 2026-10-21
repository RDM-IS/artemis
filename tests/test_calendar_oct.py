"""CAL-OCT — block 1 honours stays and placements (Ryan's 2026-10-21 calendar).

The overrides are faked: `override_for` answers the location row and
`away_context` the stays and placements, exactly as the script writes them.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RDS_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:test")
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import away, cycle  # noqa: E402
from artemis import health_office as office  # noqa: E402
from artemis import row_schedule as rs  # noqa: E402

D = date
LOC = {D(2026, 10, 23): ("msp_home", "msp_home"), D(2026, 10, 24): ("msp_home", "away"),
       D(2026, 10, 25): ("msp_home", "away"), D(2026, 10, 26): ("msp_home", "travel"),
       D(2026, 10, 27): ("outside", "away"), D(2026, 10, 28): ("outside", "away"),
       D(2026, 10, 29): ("msp_home", "travel"), D(2026, 10, 30): ("msp_home", "msp_home")}
STAYS = [away.Stay(D(2026, 10, 24), D(2026, 10, 25), "vacation", "no_gym"),
         away.Stay(D(2026, 10, 27), D(2026, 10, 27), "work", "hotel_gym"),
         away.Stay(D(2026, 10, 28), D(2026, 10, 28), "work", "hotel_gym")]
PLACED = {
          D(2026, 10, 27): {"session": "strength_c", "location_key": "outside"},
          D(2026, 10, 28): {"session": "cardio_z2", "location_key": "outside"},
          D(2026, 10, 29): {"session": "rest", "location_key": "msp_home",
                            "evening": "recovery_flow", "extra": "mobility"}}


def _override(d, *a, **k):
    if d in LOC:
        loc, dt = LOC[d]
        return {"location": loc, "day_type": dt, "start_date": d, "end_date": d}
    return None


def _build():
    with patch.object(rs, "ROW_FROM", D(2026, 10, 20)), \
         patch.object(cycle, "override_for", side_effect=_override), \
         patch.object(office, "away_context", return_value=(STAYS, PLACED)):
        rows = office.build_rows()
        office.validate_rows(rows)
    return rows


class TestRyansOctober(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _build()
        cls.m = {r["plan_date"]: r for r in cls.rows if r["slot"] == "morning"}
        cls.ev = {r["plan_date"] for r in cls.rows if r["slot"] == "evening"}

    def test_friday_off_rows_at_home(self):
        r = self.m[D(2026, 10, 23)]
        self.assertEqual(r["blocks"]["location_key"], "msp_home")
        self.assertIn(r["blocks"].get("row_kind"), ("ladder", "circuit"))

    def test_the_vacation_weekend_plans_nothing_and_has_no_evening(self):
        for d in (D(2026, 10, 24), D(2026, 10, 25)):
            self.assertEqual(self.m[d]["session_type"], "rest", d)
            self.assertNotIn(d, self.ev, d)

    def test_leaving_msp_rows_in_the_morning(self):
        r = self.m[D(2026, 10, 26)]
        self.assertEqual(r["blocks"]["location_key"], "msp_home")
        self.assertIn(r["blocks"].get("row_kind"), ("ladder", "circuit"))

    def test_driving_home_is_rest_then_yoga_with_mobility(self):
        self.assertEqual(self.m[D(2026, 10, 29)]["session_type"], "rest")
        ev = next(r for r in self.rows if r["plan_date"] == D(2026, 10, 29) and r["slot"] == "evening")
        self.assertEqual(ev["session_type"], "recovery_flow")
        self.assertEqual(ev["blocks"].get("suggested_extra"), "mobility")

    def test_hotel_days_build_for_the_hotel(self):
        tue, wed = self.m[D(2026, 10, 27)], self.m[D(2026, 10, 28)]
        self.assertEqual((tue["session_type"], tue["blocks"]["location_key"]), ("strength_c", "hotel"))
        self.assertEqual((wed["session_type"], wed["blocks"]["location_key"]), ("cardio_z2", "hotel"))
        self.assertNotIn("row_kind", tue["blocks"])

    def test_back_home_friday_rows_again(self):
        r = self.m[D(2026, 10, 30)]
        self.assertEqual(r["blocks"]["location_key"], "msp_home")
        self.assertEqual(r["blocks"].get("row_kind"), "circuit")

    def test_untold_days_are_unchanged(self):
        with patch.object(rs, "ROW_FROM", D(2026, 10, 20)), \
             patch.object(cycle, "override_for", return_value=None), \
             patch.object(office, "away_context", return_value=([], {})):
            base = {r["plan_date"]: r for r in office.build_rows() if r["slot"] == "morning"}
        d = D(2026, 10, 31)
        self.assertEqual(self.m[d]["session_type"], base[d]["session_type"])
        self.assertEqual(self.m[d]["blocks"].get("row_kind"), base[d]["blocks"].get("row_kind"))


class TestFailClosed(unittest.TestCase):
    def test_an_unreadable_override_table_raises(self):
        class Boom(Exception):
            pass
        with patch("knowledge.db.get_connection", side_effect=Boom("down")):
            with self.assertRaises(Boom):
                office.away_context(D(2026, 10, 1), D(2026, 10, 31))


if __name__ == "__main__":
    unittest.main()
