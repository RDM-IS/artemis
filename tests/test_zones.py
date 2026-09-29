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


class TestEveryCardioRowCarriesItsRange(unittest.TestCase):
    """The first readout of the seeded fortnight had ranges on the two interval
    rows and none on the six Zone 2 rows -- `_z2` predates PROGRAM-2 and never
    learned to emit `zones`. A Z2 session that names a zone but not its bpm asks
    him to remember what Z2 means, which is the whole point of putting the
    numbers on the row.
    """

    def _cardio_blocks(self):
        from artemis import health_office as o
        out = []
        for lk, loc in (("office", "the office"), ("richfield", "Richfield"),
                        ("brown_deer", "Brown Deer"), ("msp_home", "MSP home")):
            for wk in (4, 5, 6, 7):
                out.append((f"_z2 wk{wk} {lk}", o._z2(wk, location=loc, location_key=lk)[0]))
                out.append((f"_intervals(blocked) wk{wk} {lk}",
                            o._intervals(wk, location=loc, location_key=lk)[0]))
                out.append((f"_intervals(passed) wk{wk} {lk}",
                            o._intervals(wk, location=loc, location_key=lk,
                                         gate={"ok": True})[0]))
        return out

    def test_every_cardio_block_names_a_work_range(self):
        for label, blocks in self._cardio_blocks():
            with self.subTest(label):
                work = (blocks.get("zones") or {}).get("work")
                self.assertIsNotNone(work, f"{label} carries no work zone")
                self.assertIsInstance(work.get("low_bpm"), int)
                self.assertIsInstance(work.get("high_bpm"), int)
                self.assertLess(work["low_bpm"], work["high_bpm"])
                # The estimate must say it is one, so nobody reads it as measured.
                self.assertIn("estimate", work.get("source", ""))

    def test_an_interval_session_also_says_what_easy_means(self):
        from artemis import health_office as o
        # The gate has to be PASSED for this to be an interval session at all --
        # an absent gate is a fail and builds the Z2 variant. This test is about
        # the zones an interval session carries, so it asks for one.
        blocks = o._intervals(5, location="MSP home", location_key="msp_home",
                              gate={"ok": True})[0]
        self.assertEqual(blocks["type"], "intervals")
        easy = blocks["zones"]["easy"]
        self.assertEqual((easy["low_bpm"], easy["high_bpm"]), zones.ZONES["Z2"])

    def test_the_range_is_on_the_card_not_only_in_the_notes(self):
        """setup_notes is prose. The screen reads `zones`, so the numbers have to
        be there as numbers -- a note alone cannot be rendered as a target."""
        from artemis import health_office as o
        blocks = o._z2(5, location="the office", location_key="office")[0]
        self.assertIn("105", " ".join(blocks["setup_notes"]))
        self.assertEqual(blocks["zones"]["work"]["low_bpm"], 105)
