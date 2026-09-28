"""PROGRAM-2 zones — one definition, and it says it is an estimate."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest

from knowledge import zones


class TestZones(unittest.TestCase):
    def test_the_approved_ranges(self):
        self.assertEqual(zones.zone_range("Z2"), (105, 122))
        self.assertEqual(zones.zone_range("Z4"), (139, 157))

    def test_they_sit_where_the_recorded_hrmax_puts_them(self):
        """Catches HR_MAX being edited without the ranges.

        Within 1 bpm rather than exact: the approved numbers are Ryan's, and
        105 is not round(174 x 0.60) = 104. Asserting an exact derivation would
        claim to know how they were rounded, which this does not.
        """
        self.assertEqual(zones.HR_MAX, 174)
        for zone, pct in (("Z2", (0.60, 0.70)), ("Z4", (0.80, 0.90))):
            lo, hi = zones.ZONES[zone]
            with self.subTest(zone=zone):
                self.assertAlmostEqual(lo, zones.HR_MAX * pct[0], delta=1)
                self.assertAlmostEqual(hi, zones.HR_MAX * pct[1], delta=1)

    def test_an_unknown_zone_is_None_not_a_guess(self):
        self.assertIsNone(zones.zone_range("Z3"))
        self.assertIsNone(zones.zone_block("Z3"))
        self.assertIn("no range recorded", zones.describe("Z3"))

    def test_the_row_block_carries_its_provenance(self):
        """A screen showing a range must be able to say where it came from."""
        b = zones.zone_block("Z2")
        self.assertEqual((b["zone"], b["low_bpm"], b["high_bpm"]), ("Z2", 105, 122))
        self.assertIn("estimate", b["source"])
        self.assertIn("ZONE-1", b["source"])

    def test_describe_is_one_line(self):
        self.assertEqual(zones.describe("Z4"), "Z4 139–157 bpm")

    def test_there_is_no_second_copy_of_these_numbers(self):
        """ZONE-1 must have one place to change."""
        import pathlib, re
        root = pathlib.Path(__file__).resolve().parents[1]
        hits = []
        for p in list(root.glob("artemis/*.py")) + list(root.glob("knowledge/*.py")):
            if p.name == "zones.py":
                continue
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"\b(105|122|139|157)\b.*bpm|bpm.*\b(105|122|139|157)\b", line):
                    hits.append(f"{p.name}:{n}")
        self.assertEqual(hits, [], f"zone numbers duplicated outside zones.py: {hits}")


if __name__ == "__main__":
    unittest.main()


class TestTheRowColumnType(unittest.TestCase):
    """`health.plan.target_hr_zone` is an INTEGER. The intervals builder first
    returned "Zone 4" and Postgres refused the whole reseed — which is the column
    doing its job, but the builder should not have needed telling."""

    def test_every_cardio_builder_returns_an_int_zone(self):
        from datetime import date
        from artemis import health_office as office
        for st, wk in (("cardio_z2", 3), ("cardio_intervals", 3),
                       ("cardio_intervals", 5), ("cardio_intervals", 7)):
            with self.subTest(session_type=st, week=wk):
                _b, _rpe, zone, _est = office._build(
                    st, wk, location="Richfield", location_key="richfield",
                    on=date(2026, 10, 11))
                self.assertIsInstance(zone, int, f"{st} wk{wk} returned {zone!r}")
                self.assertIn(zone, (2, 4))
