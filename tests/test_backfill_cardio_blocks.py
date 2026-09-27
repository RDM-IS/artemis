"""CARDIO-REQUIRED — the cardio-block backfill's selection is pure and fail-closed."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_cardio_blocks",
    Path(__file__).resolve().parent.parent / "scripts" / "backfill_cardio_blocks.py")
bf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bf   # dataclasses resolve their module by name
_spec.loader.exec_module(bf)

D = date(2027, 1, 5)   # synthetic dates only (PUBLIC-FIXTURES)


def fake_resolve(key):
    return {"modality": "row" if key == "home_a" else None, "device": key}


class TestPlanBackfill(unittest.TestCase):
    def test_selection(self):
        rows = [
            (1, D, "cardio_z2", {"type": "steady", "location_key": "home_a"}, 0),   # write
            (2, D, "cardio_z2", {"type": "steady", "location_key": "home_a",
                                 "cardio": {"modality": "bike"}}, 0),              # already
            (3, D, "cardio_z2", {"type": "steady", "location_key": "home_a"}, 1),   # logged
            (4, D, "cardio_z2", {"type": "steady"}, 0),                              # no key
            (5, D, "strength_a", {"type": "circuit", "location_key": "home_a"}, 0),  # not cardio
            (6, D, "cardio_z2", '{"type": "steady", "location_key": "home_b"}', 0),  # json text
        ]
        p = bf.plan_backfill(rows, resolve=fake_resolve)
        self.assertEqual([t[0] for t in p.targets], [1, 6])
        self.assertEqual(p.targets[0][2], {"modality": "row", "device": "home_a"})
        # A location with no cardio still gets its explicit "none" resolution.
        self.assertEqual(p.targets[1][2], {"modality": None, "device": "home_b"})
        self.assertEqual(p.already, [2])
        self.assertEqual(p.logged, [3])
        self.assertEqual(p.no_location, [4])   # reported, never defaulted
        self.assertEqual(p.not_steady, [5])

    def test_md5_is_order_and_format_stable(self):
        a = bf.md5_of([(1, {"b": 1, "a": 2})])
        b = bf.md5_of([(1, '{"a": 2, "b": 1}')])
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
