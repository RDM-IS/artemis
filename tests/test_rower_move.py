"""ROWER-MOVE — the rower goes Richfield → MSP home on 2026-10-04.

Dated rather than a flag-day edit, because a plan is seeded ahead of itself: a
row for 10/10 must resolve to where the rower WILL be while a row for 10/01
resolves to where it is now.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from datetime import date

from knowledge import cardio

BEFORE = date(2026, 10, 3)
ON = date(2026, 10, 4)
AFTER = date(2026, 10, 11)


class TestBothSidesOfTheDate(unittest.TestCase):
    def test_richfield_has_the_rower_before_and_not_after(self):
        self.assertIn(("row", "water"), cardio.available("richfield", BEFORE))
        self.assertNotIn(("row", "water"), cardio.available("richfield", ON))
        self.assertNotIn(("row", "water"), cardio.available("richfield", AFTER))

    def test_msp_home_gains_it_on_the_day(self):
        self.assertEqual(cardio.available("msp_home", BEFORE), ())
        self.assertIn(("row", "water"), cardio.available("msp_home", ON))
        self.assertIn(("row", "water"), cardio.available("msp_home", AFTER))

    def test_richfield_still_has_its_bike_afterwards(self):
        """Only the rower moves."""
        self.assertIn(("bike", "indoor trainer"), cardio.available("richfield", AFTER))

    def test_the_resolved_modality_follows(self):
        self.assertEqual(cardio.resolve("richfield", BEFORE)["modality"], "row")
        self.assertEqual(cardio.resolve("richfield", AFTER)["modality"], "bike")
        self.assertEqual(cardio.resolve("msp_home", AFTER)["modality"], "row")

    def test_msp_home_before_the_move_is_the_explicit_no_equipment_state(self):
        r = cardio.resolve("msp_home", BEFORE)
        self.assertIsNone(r["modality"])
        self.assertIn("no cardio equipment", r["reason"])

    def test_a_substitute_is_still_marked_as_one(self):
        """Richfield's bike after the move is a substitute; the rowing baseline
        advances only on `row` (CARDIO-LOC)."""
        self.assertTrue(cardio.resolve("richfield", AFTER)["is_substitute"])
        self.assertFalse(cardio.resolve("msp_home", AFTER)["is_substitute"])

    def test_no_date_means_the_BASE_inventory_not_today(self):
        """A resolver whose answer changes with the wall clock is the hardest
        kind to reason about, so an undated call gets the unmoved answer."""
        self.assertEqual(cardio.available("richfield"),
                         cardio.available("richfield", BEFORE))

    def test_one_date_to_change_if_the_move_slips(self):
        self.assertEqual(len(cardio.MOVES), 1)
        self.assertEqual(cardio.MOVES[0]["on"], date(2026, 10, 4))
        self.assertEqual(cardio.MOVES[0]["item"], ("row", "water"))


if __name__ == "__main__":
    unittest.main()
