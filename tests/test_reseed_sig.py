"""The reseed's change-detection has to be able to see its own work.

`scripts/reseed_program2.py` decides what to rewrite by comparing a SIGNATURE of
the built block against the stored one. When `_z2` learned to emit `zones`
(2026-09-28), all 42 rows in scope reported "unchanged" and the reseed declined
to write, because `zones` was not in the signature -- so the rows kept a zone
name with no bpm and every guard passed. A signature narrower than the row is a
reseed that cannot see its own work, which is the quiet version of the failure
the md5 guard is loud about.
"""
import importlib.util
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reseed_program2.py"
_spec = importlib.util.spec_from_file_location("reseed_program2", _PATH)
reseed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reseed)


class TestTheSignatureSeesWhatTheCardReads(unittest.TestCase):
    def test_a_changed_zone_range_changes_the_signature(self):
        lo = {"type": "steady", "display_name": "Zone 2 – Bike", "duration_min": 40,
              "zones": {"work": {"zone": "Z2", "low_bpm": 105, "high_bpm": 122}}}
        hi = {**lo, "zones": {"work": {"zone": "Z2", "low_bpm": 110, "high_bpm": 128}}}
        self.assertNotEqual(reseed._sig(lo), reseed._sig(hi))

    def test_gaining_a_zone_block_changes_the_signature(self):
        """The exact case that went undetected: a row with no zones gaining them."""
        without = {"type": "steady", "display_name": "Zone 2 – Bike", "duration_min": 40}
        with_ = {**without, "zones": {"work": {"zone": "Z2", "low_bpm": 105, "high_bpm": 122}}}
        self.assertNotEqual(reseed._sig(without), reseed._sig(with_))

    def test_the_signature_covers_every_field_the_card_reads(self):
        """Adding a field to a block without adding it here makes the reseed
        blind to it. This list is the contract; extend both together."""
        expected = {"display_name", "type", "exercises", "suggested_extra",
                    "intervals", "zones", "intensity", "ran_as",
                    "duration_min", "location_key"}
        self.assertEqual(set(reseed._sig({})), expected)

    def test_a_z2_variant_is_distinguishable_from_a_real_interval_session(self):
        """Same date, same location, different session -- `ran_as` is what tells
        them apart once the gate has downgraded one."""
        real = {"type": "intervals", "ran_as": "intervals",
                "intervals": {"reps": 6, "work_sec": 60, "easy_sec": 120}}
        downgraded = {"type": "steady", "ran_as": "z2_variant", "duration_min": 40}
        self.assertNotEqual(reseed._sig(real), reseed._sig(downgraded))

    def test_an_identical_block_is_reported_unchanged(self):
        """The point of the signature is still to avoid pointless rewrites."""
        b = {"type": "circuit", "display_name": "Strength A", "duration_min": None,
             "exercises": [{"name": "Leg press"}, {"name": "Chest press"}],
             "suggested_extra": "core", "location_key": "office"}
        self.assertEqual(reseed._sig(b), reseed._sig(dict(b)))


class TestTheFloorHolds(unittest.TestCase):
    def test_the_floor_is_the_approved_start_date(self):
        import datetime
        self.assertEqual(reseed.FLOOR, datetime.date(2026, 10, 5))


if __name__ == "__main__":
    unittest.main()
