"""SESSION-OPEN step 3a — strength_bw (approved 2026-11-09). No database;
synthetic 2031 dates only (PUBLIC-FIXTURES)."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import re
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from artemis import health_office as office
from artemis import health_regions as hr
from artemis import strength_bw as sb
from knowledge import cardio, load_config
from knowledge import session_types as st
from knowledge import strength_quota as sq

ROOT = Path(__file__).resolve().parent.parent
D = date(2031, 3, 4)          # after every dated equipment move


def _build(key, on=D):
    blocks, rpe, zone, est = sb.build(key, key, on=on)
    return blocks, rpe, zone, est


def _seg(blocks, i):
    return [e["name"] for e in blocks["exercises"] if e["segment"] == i]


class ContentTest(unittest.TestCase):
    """The approved content, in FULLBODY-1's shape."""

    def test_shape(self):
        b, rpe, zone, est = _build("office")
        self.assertEqual((b["type"], b["display_name"], rpe, zone, est), ("circuit", "Bodyweight", 7.0, 3, 60))
        self.assertEqual([s["rounds"] for s in b["segments"]], [3, 4, 2])
        self.assertEqual(b["segments"][0]["rest_between_rounds_sec"], 60)
        self.assertEqual(_seg(b, 0), ["Reverse lunge", "Single-leg glute bridge",
                                      "Hand-supported single-leg RDL"])
        self.assertEqual(_seg(b, 1)[:2], ["Tempo squat (3-1-1)", "Push-up"])
        self.assertEqual(_seg(b, 1)[3], "Good morning")
        self.assertEqual(_seg(b, 2), ["Dead bug", "Side plank", "Bird dog"])
        z3 = next(e for e in b["exercises"] if e["name"].endswith("— Z3"))
        self.assertEqual(z3["duration_sec"], 180)
        self.assertIn("hip / T-spine mobility", b["warmup"])
        self.assertEqual(b["cooldown"], "5 min easy + stretch")

    def test_progression_is_density_never_load(self):
        notes = " ".join(_build("office")[0]["setup_notes"]).lower()
        for words in ("4 → 5", "shorter rests", "machine target", "decline push-up",
                      "rear-foot-elevated split squat", "no load"):
            self.assertIn(words.lower(), notes)

    def test_every_exercise_is_classed_no_load_and_has_regions(self):
        for name in sb.all_names():
            cls = office.class_for(name)                    # raises when unclassed
            self.assertIn(cls, load_config.NO_LOAD_CLASSES, name)
            self.assertIn(name, hr.EXERCISE_REGIONS, name)
        for key in ("office", "richfield", "brown_deer"):
            for e in _build(key)[0]["exercises"]:
                self.assertEqual(e["equipment_class"], office.class_for(e["name"]))
                self.assertNotIn("target_load_lbs", e)


class FullBodyRegionsTest(unittest.TestCase):
    def test_every_full_body_name_has_regions(self):
        """FULLBODY-1 shipped seven names the pain ladder couldn't see; this
        keeps the next one from shipping the same way."""
        from artemis import full_body
        self.assertEqual(sorted(n for n in full_body.all_names() if n not in hr.EXERCISE_REGIONS), [])


class PerRoomTest(unittest.TestCase):
    def test_richfield_rows_on_the_trx_and_rides_the_bike(self):
        b = _build("richfield")[0]
        self.assertIn("TRX row", _seg(b, 1))
        self.assertIn("Bike — Z3", _seg(b, 1))
        self.assertIn("TRX", b["equipment"])

    def test_office_and_brown_deer_have_no_strap_so_y_t_w(self):
        office_b, bd_b = _build("office")[0], _build("brown_deer")[0]
        self.assertIn("Prone Y-T-W", _seg(office_b, 1))
        self.assertIn("Upright bike — Z3", _seg(office_b, 1))
        self.assertIn("Prone Y-T-W", _seg(bd_b, 1))
        self.assertIn("Treadmill — Z3", _seg(bd_b, 1))

    def test_a_band_room_rows_with_the_band(self):
        with mock.patch.dict(load_config.BY_LOCATION, {"synthetic_bands": {"bands": {"mode": "none"},
                                                                            "bodyweight": {"mode": "none"}}}), \
             mock.patch.dict(cardio.INVENTORY, {"synthetic_bands": (("treadmill", "treadmill"),)}):
            b = _build("synthetic_bands")[0]
        self.assertIn("Band row", _seg(b, 1))
        self.assertIn("bands", b["equipment"])

    def test_a_room_with_no_machine_does_high_knees(self):
        with mock.patch.dict(load_config.BY_LOCATION, {"synthetic_floor": {"bodyweight": {"mode": "none"}}}), \
             mock.patch.dict(cardio.INVENTORY, {"synthetic_floor": ()}):
            b = _build("synthetic_floor")[0]
        self.assertIn("High knees / jacks — Z3", _seg(b, 1))
        self.assertIn("marching / jacks", b["warmup"])

    def test_the_machine_follows_the_dated_move(self):
        """msp_home has the rower only after it moved; the cardio read is dated."""
        with mock.patch.dict(load_config.BY_LOCATION, {"msp_home": {"bodyweight": {"mode": "none"}}}):
            after = _build("msp_home", on=D)[0]
            before = _build("msp_home", on=date(2026, 9, 1))[0]
        self.assertIn("Row — Z3", _seg(after, 1))
        self.assertIn("High knees / jacks — Z3", _seg(before, 1))


class FailClosedTest(unittest.TestCase):
    def test_an_unrecorded_equipment_inventory_raises_never_y_t_w(self):
        # A room load_config records as UNKNOWN (absent), not empty.
        with mock.patch.dict(cardio.INVENTORY, {"synthetic_unknown": ()}):
            self.assertIsNone(load_config.for_location("synthetic_unknown"))
            with self.assertRaises(sb.InventoryUnreadable):
                _build("synthetic_unknown")

    def test_home_is_y_t_w_and_the_rower(self):
        """msp_home confirmed 2026-11-10: rower, floor, mat — no strap, no bands."""
        b = _build("msp_home")[0]
        self.assertIn("Prone Y-T-W", _seg(b, 1))
        self.assertIn("Row — Z3", _seg(b, 1))
        self.assertIn("water rower", b["warmup"])
        self.assertEqual(b["equipment"], ["mat", "water rower"])

    def test_a_room_missing_from_the_cardio_inventory_raises(self):
        with mock.patch.dict(load_config.BY_LOCATION, {"synthetic_x": {"bodyweight": {"mode": "none"}}}):
            with self.assertRaises(sb.InventoryUnreadable):
                _build("synthetic_x")

    def test_transit_and_unknown_keys_raise(self):
        for key in ("transit", "nowhere"):
            with self.assertRaises(sb.InventoryUnreadable):
                _build(key)

    def test_can_hold_follows_the_builder(self):
        for key, want in (("office", True), ("richfield", True), ("brown_deer", True),
                          ("msp_home", True), ("transit", False)):
            self.assertEqual(office.can_hold(key, "strength_bw"), want, key)


class DispatchTest(unittest.TestCase):
    def test_strength_bw_never_reaches_the_a_b_c_builder(self):
        with mock.patch.object(office, "_strength", side_effect=AssertionError("A/B/C builder")):
            blocks, *_ = office._build_inner("strength_bw", 1, location="Richfield",
                                             location_key="richfield", on=D)
        self.assertEqual(blocks["display_name"], "Bodyweight")

    def test_an_unknown_session_type_raises(self):
        with self.assertRaises(ValueError):
            office._build_inner("strength_zz", 1, location_key="office")
        with self.assertRaises(ValueError):
            office._build_inner("walk", 1, location_key="office")

    def test_rest_mobility_still_builds(self):
        blocks, *_ = office._build_inner("rest_mobility", 1, location_key="office")
        self.assertTrue(blocks)

    def test_legal_types_and_migration_059_agree(self):
        sql = (ROOT / "migrations" / "059_strength_bw.sql").read_text()
        body = sql.split("CHECK (session_type = ANY (ARRAY[", 1)[1].split("]", 1)[0]
        check = set(re.findall(r"'([a-z0-9_]+)'::text", body))
        self.assertIn("strength_bw", check)
        self.assertIn("strength_bw", office.LEGAL_SESSION_TYPES)
        self.assertTrue(office.LEGAL_SESSION_TYPES <= check)


class QuotaAndRecommendTest(unittest.TestCase):
    def test_a_strength_bw_day_counts_toward_the_quota(self):
        self.assertIn("strength_bw", st.STRENGTH_DAY_TYPES)
        self.assertTrue(st.is_strength_day_type("strength_bw"))
        p = sq.params(D, "UTC")
        self.assertIn("strength_bw", p["types"])
        q = sq.compute(D, [date(2031, 3, 3), D], today=D, counts_from=date(2000, 1, 2))
        self.assertEqual(q["count"], 2)

    def test_it_is_a_lift_but_not_a_step_in_the_rotation(self):
        from knowledge import recommend as rec
        self.assertNotIn("strength_bw", st.STRENGTH_TYPES)           # rotation untouched
        self.assertIn("strength_bw", st.LIFT_TYPES)                  # 48 h clock
        self.assertEqual(rec.button_for("strength_bw", {}), "strength")
        self.assertEqual(st.STRENGTH_LABEL["strength_bw"], "Bodyweight")

    def test_the_bare_word_does_not_shadow_the_circuit(self):
        from artemis import health
        self.assertEqual(health._detect_session_type("how was my bodyweight circuit"), "bodyweight_circuit")
        self.assertEqual(health._detect_session_type("how did bodyweight go"), "strength_bw")


class LibraryTest(unittest.TestCase):
    def _lib(self):
        from artemis import cycle
        from artemis import session_library as sl
        with mock.patch.object(cycle, "override_for", return_value=None), \
             mock.patch.object(cycle, "locations", return_value=cycle.DEFAULT_LOCATIONS), \
             mock.patch.object(cycle, "anchor", return_value=cycle.DEFAULT_ANCHOR):
            return sl.build(date(2026, 11, 12))

    def test_every_real_room_lists_it(self):
        lib = self._lib()
        by = {l["key"]: [s["session_type"] for s in l["sessions"]] for l in lib["locations"]}
        self.assertEqual(sorted(by), ["brown_deer", "msp_home", "office", "richfield"])
        for key in by:
            self.assertIn("strength_bw", by[key], key)

    def test_a_room_whose_inventory_cant_be_read_fails_loudly_not_silently(self):
        from artemis import session_library as sl
        with mock.patch.dict(load_config.BY_LOCATION, clear=False) as bl:
            bl.pop("brown_deer")
            with self.assertLogs("artemis.session_library", level="ERROR") as logs:
                lib = self._lib()
        by = {l["key"]: [s["session_type"] for s in l["sessions"]] for l in lib["locations"]}
        self.assertNotIn("strength_bw", by["brown_deer"])
        self.assertTrue(any("strength_bw at brown_deer failed to build" in m for m in logs.output))

    def test_the_library_entry_is_the_builder_output(self):
        from artemis import session_library as sl
        e = sl.entry_for("strength_bw", "richfield", date(2026, 11, 12))
        self.assertEqual(e["session_type"], "strength_bw")
        self.assertEqual(e["display_name"], "Bodyweight")
        self.assertIn("TRX row", [x["name"] for x in e["blocks"]["exercises"]])


if __name__ == "__main__":
    unittest.main()
