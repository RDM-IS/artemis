"""CARDIO-LOC — cardio resolves by location, from config.

The four tests the build was judged on, plus the device preference:

  1. each location resolves to its expected modality
  2. MSP home produces the explicit no-equipment state
  3. a substitute session does not advance the rowing baseline
  4. reordering the CONFIG changes resolution with no code change
  5. the office picks upright before recumbent

Run:
    python3.11 tests/test_cardio_loc.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import cardio_baseline as cb  # noqa: E402
from knowledge import cardio  # noqa: E402


class TestResolution(unittest.TestCase):
    """1 — each location resolves to what its inventory actually holds."""

    def test_each_location_resolves_to_its_expected_modality(self):
        cases = {"richfield": "row", "brown_deer": "treadmill", "office": "bike"}
        for loc, want in cases.items():
            with self.subTest(location=loc):
                self.assertEqual(cardio.resolve(loc)["modality"], want)

    def test_rowing_is_primary_and_everything_else_is_a_substitute(self):
        self.assertFalse(cardio.resolve("richfield")["is_substitute"])
        for loc in ("brown_deer", "office"):
            with self.subTest(location=loc):
                self.assertTrue(cardio.resolve(loc)["is_substitute"])

    def test_an_unknown_location_is_the_no_equipment_state_not_a_guess(self):
        self.assertIsNone(cardio.resolve("hotel_gym")["modality"])
        self.assertIsNone(cardio.resolve(None)["modality"])


class TestNoEquipment(unittest.TestCase):
    """2 — MSP home says so, and says what exists elsewhere."""

    def setUp(self):
        self.r = cardio.resolve("msp_home")

    def test_it_is_explicit_not_empty_and_not_another_gyms_equipment(self):
        self.assertIsNone(self.r["modality"])
        self.assertIsNone(self.r["device"])
        self.assertEqual(self.r["available"], [])
        self.assertEqual(self.r["reason"], "no cardio equipment at this location")

    def test_it_names_what_is_available_at_the_office(self):
        self.assertEqual(self.r["elsewhere_location"], "office")
        self.assertEqual(set(self.r["elsewhere"]), {"bike", "elliptical", "treadmill"})
        line = cardio.describe(self.r)
        self.assertIn("no cardio equipment at this location", line)
        self.assertIn("office", line)

    def test_the_seeded_row_carries_the_state_rather_than_crashing(self):
        from artemis import health_office as office
        blocks, *_ = office._z2(1, location="MSP home", location_key="msp_home")
        self.assertTrue(blocks["no_equipment"])
        self.assertIsNone(blocks["cardio"]["modality"])
        self.assertIn("no cardio equipment", blocks["setup_notes"][0])
        self.assertEqual(blocks["equipment"], [])


class TestDevicePreference(unittest.TestCase):
    """5 — modality alone cannot choose between the office's two bikes."""

    def test_the_office_picks_upright_before_recumbent(self):
        self.assertEqual(cardio.resolve("office")["device"], "upright")
        self.assertEqual(cardio.device_for("office", "bike"), "upright")

    def test_reordering_devices_changes_the_pick_with_no_code_change(self):
        with patch.dict(cardio.DEVICE_ORDER, {"bike": ("recumbent", "upright")}):
            self.assertEqual(cardio.resolve("office")["device"], "recumbent")
        self.assertEqual(cardio.resolve("office")["device"], "upright")   # restored

    def test_a_device_the_order_does_not_list_still_resolves(self):
        self.assertEqual(cardio.device_for("richfield", "row"), "water")
        self.assertEqual(cardio.device_for("richfield", "bike"), "indoor trainer")

    def test_a_modality_the_location_lacks_has_no_device(self):
        self.assertIsNone(cardio.device_for("brown_deer", "row"))


class TestConfigIsTheSourceOfTruth(unittest.TestCase):
    """4 — reorder the config, resolution changes. No code edit."""

    def test_reordering_modalities_changes_what_the_office_resolves_to(self):
        self.assertEqual(cardio.resolve("office")["modality"], "bike")
        with patch.object(cardio, "MODALITY_ORDER", ("treadmill", "row", "bike", "elliptical")):
            self.assertEqual(cardio.resolve("office")["modality"], "treadmill")
        self.assertEqual(cardio.resolve("office")["modality"], "bike")

    def test_the_rower_moving_to_msp_is_an_inventory_edit(self):
        """The 2026-10-04 question, answered by one line of config."""
        moved = dict(cardio.INVENTORY)
        moved["richfield"] = (("bike", "indoor trainer"),)
        moved["msp_home"] = (("row", "water"),)
        with patch.object(cardio, "INVENTORY", moved):
            self.assertEqual(cardio.resolve("msp_home")["modality"], "row")
            self.assertEqual(cardio.resolve("richfield")["modality"], "bike")
            self.assertTrue(cardio.resolve("richfield")["is_substitute"])
        self.assertEqual(cardio.resolve("richfield")["modality"], "row")

    def test_every_inventory_modality_is_a_known_one(self):
        for loc, items in cardio.INVENTORY.items():
            for modality, _ in items:
                with self.subTest(location=loc, modality=modality):
                    self.assertIn(modality, cardio.MODALITIES)

    def test_the_order_covers_every_modality(self):
        self.assertEqual(set(cardio.MODALITY_ORDER), set(cardio.MODALITIES))


class FakeCursor:
    """Returns rows only for the modality the query actually filters on."""

    def __init__(self, rows_by_modality):
        self.rows_by_modality = rows_by_modality
        self.last_params = None
        self._result = None

    def execute(self, sql, params=None):
        self.last_params = params
        modality = params[0] if params else None
        self._result = self.rows_by_modality.get(modality, [])

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


class TestBaseline(unittest.TestCase):
    """3 — a substitute runs the session but never advances the baseline."""

    ROW_SESSION = (date(2026, 9, 20), 1800, 3.0, 128, "water")

    def test_only_rowing_advances_it(self):
        self.assertTrue(cb.advances_baseline("row"))
        for other in ("bike", "treadmill", "elliptical", None, "unrecorded"):
            with self.subTest(modality=other):
                self.assertFalse(cb.advances_baseline(other))

    def test_the_baseline_query_filters_on_rowing(self):
        cur = FakeCursor({"row": [self.ROW_SESSION]})
        got = cb.rowing_baseline(cur)
        self.assertEqual(cur.last_params[0], "row")
        self.assertEqual(got["minutes"], 30.0)
        self.assertEqual(got["device"], "water")

    def test_a_week_of_substitutes_leaves_the_baseline_where_it_was(self):
        """Three treadmill sessions after a 30 min row: still 30 min of rowing."""
        cur = FakeCursor({
            "row": [self.ROW_SESSION],
            "treadmill": [(date(2026, 9, 24), 3600, 4.0, 140, "treadmill")],
        })
        self.assertEqual(cb.rowing_baseline(cur)["duration_sec"], 1800)

    def test_no_rowing_history_is_no_baseline_not_a_zero(self):
        self.assertIsNone(cb.rowing_baseline(FakeCursor({})))

    def test_substitutes_are_still_counted_as_sessions_done(self):
        """They are real training — they just do not move the rowing number.
        sessions_by_modality does not filter, so its first param is the window."""
        cur = FakeCursor({date(2026, 9, 20): [("treadmill", 3), ("row", 1)]})
        got = cb.sessions_by_modality(cur, since=date(2026, 9, 20), until=date(2026, 9, 26))
        self.assertEqual(got, {"treadmill": 3, "row": 1})


class TestZoneOneForwardCompatibility(unittest.TestCase):
    """Effort 0–5 lives in its own column, so zone time can arrive beside it."""

    def test_effort_and_zone_time_do_not_occupy_the_same_field(self):
        src = (Path(__file__).resolve().parent.parent / "migrations"
               / "043_session_log_modality.sql").read_text()
        self.assertNotIn("rpe_actual", src, "043 must not touch the effort column")
        self.assertIn("modality", src)
        self.assertIn("device", src)

    def test_the_check_lists_exactly_the_known_modalities(self):
        src = (Path(__file__).resolve().parent.parent / "migrations"
               / "043_session_log_modality.sql").read_text()
        for m in cardio.MODALITIES:
            self.assertIn(f"'{m}'", src)


if __name__ == "__main__":
    unittest.main()
