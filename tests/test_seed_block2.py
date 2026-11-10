"""seed_block2 --from and the full-row read-back check. No database; synthetic
2031 dates only (PUBLIC-FIXTURES)."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import importlib.util
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seed_block2", ROOT / "scripts" / "seed_block2.py")
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

START, END = date(2031, 3, 2), date(2031, 3, 15)
FROM = date(2031, 3, 10)


def _row(d, slot="morning", st="cardio_z2", **kw):
    r = {"plan_date": d, "slot": slot, "phase": 2, "week_num": 1, "session_type": st,
         "blocks": {"display_name": "Synthetic", "location_key": "office"},
         "target_rpe": 4.0, "target_hr_zone": None, "est_duration_min": 30,
         "generated_by": "test", "notes": "n"}
    r.update(kw)
    return r


DAYS = [START + timedelta(days=i) for i in range((END - START).days + 1)]


class WindowTest(unittest.TestCase):
    def test_without_from_the_whole_block_is_written(self):
        write, drop, lo = sb.window([_row(d) for d in DAYS], set(), set(), START, END)
        self.assertEqual((lo, len(write), drop), (START, len(DAYS), []))

    def test_from_leaves_every_earlier_date_alone(self):
        rows = [_row(d) for d in DAYS] + [_row(START, "evening")]
        stale = {(START + timedelta(days=1), "evening"), (FROM + timedelta(days=1), "evening")}
        write, drop, lo = sb.window(rows, stale, set(), START, END, FROM)
        self.assertEqual(lo, FROM)
        self.assertTrue(write and all(r["plan_date"] >= FROM for r in write))
        self.assertEqual(len(write), (END - FROM).days + 1)
        # a stale row before --from is NOT removed; one after it is
        self.assertEqual(drop, [(FROM + timedelta(days=1), "evening")])

    def test_a_logged_date_is_never_written_or_removed(self):
        logged = {FROM + timedelta(days=2)}
        rows = [_row(d) for d in DAYS]
        write, drop, _ = sb.window(rows, {(FROM + timedelta(days=2), "evening")}, logged,
                                   START, END, FROM)
        self.assertNotIn(FROM + timedelta(days=2), {r["plan_date"] for r in write})
        self.assertEqual(drop, [])

    def test_from_before_the_block_is_the_block_start(self):
        _, _, lo = sb.window([], set(), set(), START, END, START - timedelta(days=30))
        self.assertEqual(lo, START)

    def test_the_flag_parses(self):
        import argparse
        self.assertIn("--from", Path(_spec.origin).read_text())
        with self.assertRaises((SystemExit, ValueError, argparse.ArgumentTypeError)):
            date.fromisoformat("11/10")


class RowMismatchTest(unittest.TestCase):
    def test_identical_reads_back_clean(self):
        built = _row(FROM)
        got = dict(built, blocks=json.dumps(built["blocks"]), target_rpe="4.0")
        self.assertEqual(sb.row_mismatches(got, built), [])

    def test_every_written_column_is_checked(self):
        built = _row(FROM)
        for col, wrong in (("phase", 9), ("week_num", 9), ("session_type", "rest"),
                           ("target_rpe", 7.0), ("target_hr_zone", "Z3"),
                           ("est_duration_min", 99), ("generated_by", "x"), ("notes", "x")):
            got = dict(built, **{col: wrong})
            self.assertTrue(sb.row_mismatches(got, built), col)

    def test_a_change_deep_in_blocks_is_caught(self):
        """The old check read only session_type and location_key."""
        built = _row(FROM, blocks={"display_name": "S", "location_key": "office",
                                   "load_config": {"dumbbell": {"max": 50}}})
        got = dict(built, blocks={"display_name": "S", "location_key": "office",
                                  "load_config": {"dumbbell": {"max": 45}}})
        self.assertEqual(sb.row_mismatches(got, built), ["blocks"])

    def test_missing_row(self):
        self.assertEqual(sb.row_mismatches(None, _row(FROM)), ["missing"])

    def test_written_matches_upsert_params_order(self):
        """WRITTEN is the upsert's column list minus the key — one fact."""
        src = (ROOT / "artemis" / "health_office.py").read_text()
        cols = src.split("INSERT INTO health.plan", 1)[1].split(")", 1)[0]
        cols = [c.strip() for c in cols.replace("(", "").split(",")]
        self.assertEqual(tuple(c for c in cols if c not in ("plan_date", "slot")), sb.WRITTEN)


if __name__ == "__main__":
    unittest.main()
