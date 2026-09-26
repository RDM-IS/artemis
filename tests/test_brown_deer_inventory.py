"""BROWN-DEER-INVENTORY — confirmed 2026-09-26, and the distinction it turns on.

A treadmill, a yoga mat, bodyweight only, 7 ft ceiling. No loadable implements,
no bench, no anchors.

The point of this file is the difference between two kinds of nothing (Ryan,
2026-09-26): an ABSENT config means "we do not know what is in this room";
a PRESENT config with no numeric class means "we know, and there is nothing to
load". Collapsing them is how a room ends up seeded with another gym's numbers.

Run:
    python3.11 -m unittest tests.test_brown_deer_inventory
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health_office as office  # noqa: E402
from knowledge import cardio, load_config as lc, warmup as prep  # noqa: E402


class TestKnownEmptyIsNotUnknown(unittest.TestCase):
    """The distinction Ryan asked for, asserted both ways."""

    def test_brown_deer_is_known_and_empty_of_numeric_load(self):
        cfg = lc.for_location("brown_deer")
        self.assertIsNotNone(cfg, "an absent entry would read as 'unknown'")
        self.assertFalse(lc.has_numeric_load("brown_deer"))
        self.assertEqual(set(cfg), {"bodyweight", "cardio"})
        for v in cfg.values():
            self.assertEqual(v["mode"], "none")

    def test_msp_home_is_still_unknown_and_must_stay_that_way(self):
        self.assertIsNone(lc.for_location("msp_home"),
                          "msp_home has no confirmed inventory — absent, not empty")
        self.assertFalse(lc.has_numeric_load("msp_home"))

    def test_has_numeric_load_alone_cannot_tell_them_apart(self):
        """Both are False, which is exactly why for_location() is the gate."""
        self.assertEqual(lc.has_numeric_load("brown_deer"),
                         lc.has_numeric_load("msp_home"))
        self.assertIsNotNone(lc.for_location("brown_deer"))
        self.assertIsNone(lc.for_location("msp_home"))

    def test_no_loadable_class_is_listed(self):
        for cls in ("dumbbell", "barbell", "machine", "cable", "smith"):
            with self.subTest(cls=cls):
                self.assertNotIn(cls, lc.classes_at("brown_deer"))

    def test_no_anchor_classes_either(self):
        """No wall mount and no strap, so bands and trx are absent — unlike
        Richfield, which has both."""
        for cls in ("bands", "trx"):
            with self.subTest(cls=cls):
                self.assertNotIn(cls, lc.classes_at("brown_deer"))
                self.assertIn(cls, lc.classes_at("richfield"))


class TestTheCeilingIsRecordedData(unittest.TestCase):
    """It decides whether overhead work is possible, so it is not a comment."""

    def test_both_measured_ceilings_are_recorded(self):
        self.assertEqual(lc.ceiling_ft("richfield"), 6.5)
        self.assertEqual(lc.ceiling_ft("brown_deer"), 7.0)

    def test_an_unmeasured_ceiling_is_none_not_a_guess(self):
        self.assertIsNone(lc.ceiling_ft("office"))
        self.assertIsNone(lc.ceiling_ft("msp_home"))

    def test_standing_overhead_is_three_state_and_none_is_not_permission(self):
        self.assertIs(lc.standing_overhead("richfield"), False)   # 6.5 ft, decided
        self.assertIsNone(lc.standing_overhead("brown_deer"))     # 7 ft, undetermined
        self.assertIs(lc.standing_overhead("office"), True)
        # None must never be truthy-tested into a yes
        self.assertFalse(bool(lc.standing_overhead("brown_deer")))

    def test_the_constraints_table_cannot_be_mistaken_for_equipment_classes(self):
        for key in lc.CONSTRAINTS:
            with self.subTest(location=key):
                self.assertNotIn("ceiling_ft", lc.classes_at(key))
                self.assertNotIn("standing_overhead", lc.classes_at(key))


class TestCardioMatchesTheInventory(unittest.TestCase):
    def test_the_entry_is_the_treadmill_and_only_the_treadmill(self):
        self.assertEqual(cardio.INVENTORY["brown_deer"], (("treadmill", "treadmill"),))

    def test_it_resolves_to_the_treadmill_as_a_substitute(self):
        r = cardio.resolve("brown_deer")
        self.assertEqual(r["modality"], "treadmill")
        self.assertTrue(r["is_substitute"], "only rowing advances the baseline")

    def test_the_load_config_agrees_that_cardio_carries_no_load(self):
        self.assertEqual(lc.for_location("brown_deer")["cardio"]["mode"], "none")


class TestPrepComesFromTheRoom(unittest.TestCase):
    def test_the_warmup_and_cooldown_name_only_what_is_there(self):
        self.assertEqual(prep.warmup_for("brown_deer"), "5 min treadmill, easy")
        self.assertEqual(prep.cooldown_for("brown_deer"), "5 min mat mobility")
        self.assertEqual(prep.cooldown_min("brown_deer"), 5)

    def test_a_seeded_row_no_longer_carries_the_unknown_state(self):
        b, _, _, est = office._build("cardio_z2", 3, location="Brown Deer",
                                     location_key="brown_deer")
        self.assertNotIn("prep_unknown", b)
        self.assertEqual(b["cooldown"], "5 min mat mobility")
        self.assertEqual(est, 35)              # 30 work + 5 cooldown
        self.assertIn("mat", b["equipment"])

    def test_a_rest_row_is_unaffected(self):
        b, *_ = office._build("rest", 3, location="Brown Deer", location_key="brown_deer")
        self.assertEqual(b["equipment"], [])


class TestStrengthStillCannotBeSeededThere(unittest.TestCase):
    """Knowing the room is empty does NOT make it hold a lift."""

    def test_no_strength_session_can_be_held_at_brown_deer(self):
        for s in ("strength_a", "strength_b", "strength_c"):
            with self.subTest(session=s):
                self.assertFalse(office.can_hold("brown_deer", s))

    def test_and_the_room_has_nothing_to_load(self):
        self.assertFalse(lc.has_numeric_load("brown_deer"))


if __name__ == "__main__":
    unittest.main()
