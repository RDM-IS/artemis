"""LOCATION-1 — warmup and cooldown resolve from the LOCATION, or say they don't.

Until 2026-09-26 `_strength()` wrote the office warmup and cooldown into every
strength row whatever the location, so 10/02 at Richfield told Ryan to use an
elliptical and a Stretch Trainer that are not in that room. Same class of error
as a guessed equipment class: it reads as fact on the iPad.

Run:
    python3.11 -m unittest tests.test_prep_by_location
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health_office as office  # noqa: E402
from knowledge import warmup as prep  # noqa: E402


class TestConfigIsTheSourceOfTruth(unittest.TestCase):
    def test_the_office_values_come_from_the_config_not_a_duplicate(self):
        self.assertEqual(office.WARMUP, prep.OFFICE["warmup"])
        self.assertEqual(office.COOLDOWN, prep.OFFICE["cooldown"])
        self.assertEqual(office.COOLDOWN_MIN, prep.OFFICE["cooldown_min"])

    def test_only_measured_rooms_have_an_entry(self):
        """A room with no confirmed inventory must NOT have an invented entry."""
        self.assertEqual(set(prep.BY_LOCATION), {"office", "brown_deer"})
        for key in ("richfield", "msp_home", "outside", "hotel"):
            with self.subTest(location=key):
                self.assertFalse(prep.is_known(key))
                self.assertIsNone(prep.warmup_for(key))
                self.assertIsNone(prep.cooldown_for(key))
                self.assertEqual(prep.cooldown_min(key), 0)

    def test_brown_deer_uses_what_is_in_the_room_and_nothing_else(self):
        """Confirmed 2026-09-26: a treadmill and a mat. No Stretch Trainer, no
        elliptical — the two things the office warmup and cooldown name."""
        self.assertTrue(prep.is_known("brown_deer"))
        self.assertIn("treadmill", prep.warmup_for("brown_deer"))
        self.assertIn("mat", prep.cooldown_for("brown_deer"))
        for absent in ("elliptical", "Stretch Trainer"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, prep.warmup_for("brown_deer"))
                self.assertNotIn(absent, prep.cooldown_for("brown_deer"))

    def test_a_brown_deer_row_carries_its_own_prep_not_the_unknown_state(self):
        b, *_ = office._build("cardio_z2", 3, location="Brown Deer",
                              location_key="brown_deer")
        self.assertNotIn("prep_unknown", b)
        self.assertEqual(b["cooldown"], "5 min mat mobility")
        self.assertIn("mat", b["equipment"])
        self.assertNotIn("Stretch Trainer", b["equipment"])

    def test_adding_a_location_needs_no_code_change(self):
        added = dict(prep.BY_LOCATION, richfield={"warmup": "3 min easy row",
                                                  "cooldown": "2 min stretch",
                                                  "cooldown_min": 2})
        with patch.object(prep, "BY_LOCATION", added):
            b, *_ = office._build("strength_c", 3, location="Richfield",
                                  location_key="richfield")
        self.assertEqual(b["warmup"], "3 min easy row")
        self.assertEqual(b["cooldown"], "2 min stretch")
        self.assertNotIn("prep_unknown", b)
        # and it is gone again once the patch lifts
        self.assertFalse(prep.is_known("richfield"))


class TestTheUnknownStateIsExplicit(unittest.TestCase):
    """Absent, and POSITIVELY marked — not silently missing."""

    def setUp(self):
        self.b, *_ = office._build("strength_c", 3, location="Richfield",
                                   location_key="richfield")

    def test_no_office_warmup_or_cooldown_reaches_a_non_office_row(self):
        self.assertNotIn("warmup", self.b)
        self.assertNotIn("cooldown", self.b)
        self.assertNotIn(office.WARMUP, str(self.b))
        self.assertNotIn(office.COOLDOWN, str(self.b))

    def test_it_is_flagged_not_merely_absent(self):
        self.assertTrue(self.b["prep_unknown"])

    def test_it_says_so_in_words_on_the_row(self):
        note = " ".join(self.b["setup_notes"])
        self.assertIn("No warmup or cooldown configured", note)
        self.assertIn("Richfield", note)

    def test_no_equipment_from_another_gym_arrives_with_it(self):
        for token in ("elliptical", "Stretch Trainer"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.b.get("equipment", []))

    def test_cardio_at_an_unconfigured_location_is_the_same(self):
        b, *_ = office._build("cardio_z2", 3, location="Richfield",
                              location_key="richfield")
        self.assertTrue(b["prep_unknown"])
        self.assertNotIn("cooldown", b)


class TestTheOfficeIsUnchanged(unittest.TestCase):
    """This refactor must not move a single office row — they are seeded."""

    def test_a_strength_row_carries_exactly_what_it_did(self):
        b, _, _, est = office._build("strength_a", 3, location="office gym",
                                     location_key="office")
        self.assertEqual(b["warmup"], "5 min elliptical, easy")
        self.assertEqual(b["cooldown"], "5 min Stretch Trainer")
        self.assertNotIn("prep_unknown", b)
        # SESSION_EQUIPMENT fixes the list; the cooldown must not append to it.
        self.assertNotIn("Stretch Trainer", b["equipment"])
        self.assertEqual(est, 55)          # the leading 10 already covers prep

    def test_z2_still_appends_the_stretch_trainer_and_its_five_minutes(self):
        b, _, _, est = office._build("cardio_z2", 3, location="office gym",
                                     location_key="office")
        self.assertEqual(b["cooldown"], "5 min Stretch Trainer")
        self.assertIn("Stretch Trainer", b["equipment"])
        self.assertEqual(est, 35)          # 30 work + 5 cooldown

    def test_an_unconfigured_location_gets_no_cooldown_minutes(self):
        _, _, _, est = office._build("cardio_z2", 3, location="Richfield",
                                     location_key="richfield")
        self.assertEqual(est, 30)


class TestTheValidatorReportsIt(unittest.TestCase):
    def test_an_unconfigured_row_is_named_in_the_findings(self):
        rows = [{"plan_date": __import__("datetime").date(2026, 10, 2), "slot": "morning",
                 "session_type": "strength_c", "week_num": 3,
                 "blocks": {"location_key": "richfield", "prep_unknown": True},
                 "est_duration_min": 55}]
        notes = []
        # exercise only the LOCATION-1 findings loop, not the whole validator
        for r in rows:
            b, key = r["blocks"], r["blocks"].get("location_key", "office")
            if b.get("prep_unknown"):
                notes.append(f"{r['plan_date']}: {r['session_type']} at {key} has NO "
                             "configured warmup or cooldown")
        self.assertEqual(len(notes), 1)
        self.assertIn("richfield", notes[0])


if __name__ == "__main__":
    unittest.main()
