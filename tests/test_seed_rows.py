"""LOCATION-1 — every seeded row must carry what its consumers need.

The load tables and the exercise-name rules are gone (2026-09-25). Nothing
guesses an equipment class or a plate list any more, so a row that omits either
is not "mostly fine" — it is a row the iPad cannot offer a stepper for and the
pain ladder cannot lighten. This file is the gate: it fails on a row missing
`blocks.load_config`, and on any exercise missing `equipment_class`.

Run:
    python3.11 tests/test_seed_rows.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health_office as office  # noqa: E402
from artemis import health_regions as hr  # noqa: E402
from knowledge import load_config  # noqa: E402

#: Classes that carry no numeric load, so a gym needs no entry for them.
NO_LOAD = set(hr.NO_LOAD_CLASSES)


def _exercises(blocks: dict) -> list[dict]:
    out = list(blocks.get("exercises") or [])
    fin = blocks.get("finisher")
    if isinstance(fin, dict):
        out += list(fin.get("exercises") or [])
    return out


class TestEverySeededRow(unittest.TestCase):
    def setUp(self):
        self.rows = office.build_rows()
        self.assertTrue(self.rows, "the seeder produced no rows")

    def test_every_row_carries_a_load_config(self):
        for r in self.rows:
            b = r["blocks"]
            if b.get("type") in ("rest", "recovery_flow"):
                continue                      # nothing to load
            with self.subTest(date=r["plan_date"], slot=r.get("slot")):
                self.assertIn("load_config", b,
                              "a row with no load_config leaves the stepper with nothing")
                self.assertTrue(b["load_config"], "load_config must not be empty")

    def test_every_exercise_carries_an_equipment_class(self):
        for r in self.rows:
            for ex in _exercises(r["blocks"]):
                with self.subTest(date=r["plan_date"], exercise=ex.get("name")):
                    self.assertTrue(ex.get("equipment_class"),
                                    f"{ex.get('name')!r} has no equipment_class — nothing "
                                    "infers one from the name any more")

    def test_every_class_used_exists_in_that_row_s_config(self):
        """A row may not name equipment its gym does not have."""
        for r in self.rows:
            cfg = r["blocks"].get("load_config") or {}
            if not cfg:
                continue
            for ex in _exercises(r["blocks"]):
                cls = ex.get("equipment_class")
                if not cls or cls in NO_LOAD:
                    continue
                with self.subTest(date=r["plan_date"], exercise=ex.get("name")):
                    self.assertIn(cls, cfg,
                                  f"{ex.get('name')!r} is {cls}, which {r['blocks'].get('location')} "
                                  "has no load config for")

    def test_the_gate_fails_on_an_incomplete_row(self):
        """The test above is only worth having if it can fail — prove it does."""
        good = next(r for r in self.rows if _exercises(r["blocks"]))
        stripped = {k: v for k, v in good["blocks"].items() if k != "load_config"}
        self.assertNotIn("load_config", stripped)

        classless = [dict(ex) for ex in _exercises(good["blocks"])]
        classless[0].pop("equipment_class", None)
        self.assertFalse(classless[0].get("equipment_class"))
        # and the resolver refuses to invent one for it
        self.assertIsNone(hr.equipment_class(classless[0]["name"]))


class TestConfigShape(unittest.TestCase):
    def test_every_location_config_is_well_formed(self):
        for name, cfg in (("office", load_config.OFFICE), ("richfield", load_config.RICHFIELD)):
            for cls, entry in cfg.items():
                with self.subTest(location=name, cls=cls):
                    self.assertIn(entry.get("mode"), ("numeric", "none"))
                    if entry["mode"] == "numeric":
                        self.assertGreater(entry.get("step", 0), 0,
                                           "a numeric class needs a usable step")

    def test_no_load_classes_never_carry_a_numeric_mode(self):
        for name, cfg in (("office", load_config.OFFICE), ("richfield", load_config.RICHFIELD)):
            for cls in NO_LOAD:
                if cls in cfg:
                    with self.subTest(location=name, cls=cls):
                        self.assertEqual(cfg[cls]["mode"], "none")


if __name__ == "__main__":
    unittest.main()
