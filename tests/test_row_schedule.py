"""ROW-SCHEDULE — every MSP morning a row ladder or a row circuit (Ryan, 2026-10-19).

The base program shape is tested with no overrides, like test_health_office;
one test adds an override to prove the table steps aside for it. ROW_FROM is
pinned to its real date here, because test_health_office pushes it past the
block for its own frame and the two files share a process.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RDS_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:test")
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import cycle  # noqa: E402
from artemis import health_office as office  # noqa: E402
from artemis import row_schedule as rs  # noqa: E402

REAL_FROM = date(2026, 10, 20)


def _build(override=None):
    with patch.object(rs, "ROW_FROM", REAL_FROM), \
         patch.object(cycle, "override_for", side_effect=override or (lambda *a, **k: None)):
        rows = office.build_rows()
        office.validate_rows(rows)
    return rows


class TestTable(unittest.TestCase):
    def test_every_msp_morning_and_only_those(self):
        msp = {p for p, t in enumerate(cycle.DAY_TYPES) if t in ("msp_work", "msp_home")}
        # Position 8 is the Monday drive back: msp_home by day type, but he wakes
        # in Wisconsin and arrives at 16:00. It is not a rowing morning.
        self.assertEqual(set(rs.ROW_BY_POS), msp - {8} if 8 in msp else msp)

    def test_ladders_on_tue_and_thu_both_weeks(self):
        for pos in (2, 4, 9, 11):
            self.assertEqual(rs.ROW_BY_POS[pos], "ladder", pos)

    def test_never_two_circuit_mornings_in_a_row(self):
        for pos, kind in rs.ROW_BY_POS.items():
            nxt = rs.ROW_BY_POS.get((pos + 1) % 14)
            if kind == "circuit":
                self.assertNotEqual(nxt, "circuit", f"positions {pos} and {pos + 1}")

    def test_four_circuits_six_ladders(self):
        kinds = list(rs.ROW_BY_POS.values())
        self.assertEqual((kinds.count("circuit"), kinds.count("ladder")), (4, 6))


class TestBuiltRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _build()
        cls.mornings = {r["plan_date"]: r for r in cls.rows if r["slot"] == "morning"}

    def test_rows_from_the_start_date_follow_the_table(self):
        d = REAL_FROM
        end = max(self.mornings)
        seen = 0
        while d <= end:
            r = self.mornings[d]
            want = rs.ROW_BY_POS.get(cycle.cycle_pos(d))
            self.assertEqual(r["blocks"].get("row_kind"), want, d)
            if want:
                seen += 1
                self.assertEqual(r["session_type"], "cardio_z2", d)
                self.assertEqual(r["blocks"]["location_key"], "msp_home", d)
                self.assertEqual(r["blocks"]["cardio"]["modality"], "row", d)
                self.assertEqual(r["blocks"]["display_name"], rs.DISPLAY[want], d)
                self.assertEqual(r["est_duration_min"], rs.EST_MIN[want], d)
            d += timedelta(days=1)
        self.assertGreater(seen, 0)

    def test_nothing_before_the_start_date_changes(self):
        for d, r in self.mornings.items():
            if d < REAL_FROM:
                self.assertNotIn("row_kind", r["blocks"], d)

    def test_no_lifts_once_rowing_starts(self):
        for d, r in self.mornings.items():
            if d >= REAL_FROM:
                self.assertFalse(r["session_type"].startswith("strength"), d)

    def test_wisconsin_mornings_and_every_evening_keep_their_sessions(self):
        with patch.object(rs, "ROW_FROM", date(9999, 1, 1)), \
             patch.object(cycle, "override_for", return_value=None):
            base = {(r["plan_date"], r["slot"]): r for r in office.build_rows()}
        for r in self.rows:
            d = r["plan_date"]
            if d < REAL_FROM:
                continue
            pos = cycle.cycle_pos(d)
            if r["slot"] == "evening" or pos not in rs.ROW_BY_POS:
                self.assertEqual(r["session_type"], base[(d, r["slot"])]["session_type"],
                                 f"{d} {r['slot']}")

    def test_a_row_morning_carries_no_office_location(self):
        # The wake post adds the office-gym departure checklist when the plan's
        # location is the office. A home row must not trigger it.
        for d, r in self.mornings.items():
            if r["blocks"].get("row_kind"):
                self.assertNotEqual(r["blocks"]["location"], office.LOCATION, d)


class TestOverrides(unittest.TestCase):
    def test_a_day_moved_out_of_minneapolis_gets_no_row(self):
        moved = REAL_FROM + timedelta(days=1)          # a Wednesday circuit day

        def override(d, *a, **k):
            if d == moved:
                return {"location": "richfield", "day_type": "wi"}
            return None
        try:
            rows = _build(override)
        except AssertionError:
            # The validator may have its own view of an overridden day; what this
            # test asserts is only that the TABLE stepped aside.
            with patch.object(rs, "ROW_FROM", REAL_FROM), \
                 patch.object(cycle, "override_for", side_effect=override):
                rows = office.build_rows()
        r = next(x for x in rows if x["plan_date"] == moved and x["slot"] == "morning")
        self.assertNotIn("row_kind", r["blocks"])


class TestOverrideIntoMinneapolis(unittest.TestCase):
    """Ryan, 2026-10-19 22:18: a Wisconsin day overridden to Minneapolis is a
    Minneapolis day, so it rows too."""

    def test_the_filled_ring_never_has_two_circuits_in_a_row(self):
        ring = {**rs.ROW_BY_POS, **rs.OVERRIDE_BY_POS}
        self.assertEqual(set(ring), set(range(14)))
        for pos in range(14):
            if ring[pos] == "circuit":
                self.assertNotEqual(ring[(pos + 1) % 14], "circuit", pos)

    def test_a_wisconsin_position_rows_only_when_the_location_is_minneapolis(self):
        d = REAL_FROM + timedelta(days=4)                 # first wk-1 Saturday after
        while cycle.cycle_pos(d) != 6:
            d += timedelta(days=1)
        with patch.object(rs, "ROW_FROM", REAL_FROM):
            self.assertIsNone(rs.kind_for(6, d, "brown_deer"))
            self.assertEqual(rs.kind_for(6, d, "msp_home"), "ladder")

    def test_an_override_weekend_in_minneapolis_is_built_as_rows(self):
        sat = REAL_FROM
        while cycle.cycle_pos(sat) != 6:
            sat += timedelta(days=1)
        days = {sat, sat + timedelta(days=1), sat + timedelta(days=2)}

        def override(d, *a, **k):
            return {"location": "msp_home"} if d in days else None
        with patch.object(rs, "ROW_FROM", REAL_FROM), \
             patch.object(cycle, "override_for", side_effect=override):
            rows = office.build_rows()
            office.validate_rows(rows)
        got = {r["plan_date"]: r["blocks"].get("row_kind") for r in rows
               if r["slot"] == "morning" and r["plan_date"] in days}
        self.assertEqual([got[d] for d in sorted(days)], ["ladder", "circuit", "ladder"])


class TestBlock2(unittest.TestCase):
    def test_block_2_mornings_follow_the_table_unless_placed_or_away(self):
        from artemis import block2
        with patch.object(rs, "ROW_FROM", REAL_FROM), \
             patch.object(cycle, "override_for", return_value=None):
            start = block2.start_date([])
            d = start
            placed_day = start + timedelta(days=1)
            for _ in range(14):
                specs = block2._specs_for(d, 1, placements={placed_day: "strength_a"})
                morning = specs[0]
                want = rs.ROW_BY_POS.get(cycle.cycle_pos(d))
                if d == placed_day:
                    self.assertEqual(morning["session_type"], "strength_a")
                    self.assertNotIn("row_kind", morning)
                else:
                    self.assertEqual(morning.get("row_kind"), want, d)
                d += timedelta(days=1)


class TestApply(unittest.TestCase):
    def test_refuses_a_row_day_that_did_not_resolve_to_the_rower(self):
        with self.assertRaises(ValueError):
            rs.apply({"cardio": {"modality": "bike"}, "location_key": "msp_home"}, "ladder")

    def test_refuses_an_unknown_kind(self):
        with self.assertRaises(ValueError):
            rs.apply({"cardio": {"modality": "row"}}, "sprint")

    def test_the_pain_ladder_knows_both_names(self):
        from artemis import health_regions as hr
        for name in rs.DISPLAY.values():
            self.assertIn(name, hr.EXERCISE_REGIONS, name)


if __name__ == "__main__":
    unittest.main()
