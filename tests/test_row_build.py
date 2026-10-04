"""ROW-BUILD — the ladder, the block shape, and which step a day runs.

No live DB: a scripted cursor stands in for the three reads.

Run:
    python -m pytest -q tests/test_row_build.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import health_office as office  # noqa: E402
from artemis import row_build as rb  # noqa: E402

# Synthetic dates: far outside the program, tied to no real session.
DAY = date(2031, 3, 9)
PREV = date(2031, 3, 2)


class Ladder(unittest.TestCase):
    def test_the_ladder_ryan_set(self):
        got = [(s["work_min"], s["rate"], s["rest_rate"], s["rounds"])
               for s in map(rb.step_spec, range(1, 10))]
        self.assertEqual(got, [
            (4, 22, 0, 8), (5, 22, 0, 7), (4, 22, 18, 8), (5, 22, 18, 7),
            (4, 23, 0, 8), (5, 23, 0, 7), (4, 23, 18, 8), (5, 23, 18, 7),
            (4, 24, 0, 8),
        ])

    def test_rest_follows_every_round(self):
        self.assertEqual(rb.total_min(rb.step_spec(1)), 40)
        self.assertEqual(rb.total_min(rb.step_spec(2)), 42)
        self.assertTrue(all(rb.step_spec(n)["rest_sec"] == 60 for n in range(1, 30)))

    def test_step_zero_is_refused(self):
        with self.assertRaises(ValueError):
            rb.step_spec(0)


class Shape(unittest.TestCase):
    def _steady(self, modality="row"):
        return {"type": "steady", "display_name": "Zone 2 – Row", "duration_min": 40,
                "zones": {"work": {}}, "cardio": {"modality": modality, "device": "water"},
                "setup_notes": ["row (water rower)", "Zone 2 — conversational pace"]}

    def test_is_the_shape_the_ipad_timer_runs(self):
        b = rb.apply(self._steady(), 3)
        self.assertEqual(b["type"], "intervals")
        self.assertEqual(b["rounds"], 8)
        self.assertEqual(b["intervals_template"],
                         {"work_sec": 240, "work_settings": "Row at 22 strokes/min",
                          "rest_sec": 60, "rest_settings": "Easy at 18 strokes/min"})
        # PROGRAM-2's `intervals` key would render "hard / easy" with Zone 4.
        self.assertNotIn("intervals", b)
        self.assertIn("zones", b, "every cardio row keeps its work zone")
        self.assertNotIn("duration_min", b)
        self.assertEqual(b["cardio"]["modality"], "row")
        json.dumps(b)

    def test_seeded_row_says_the_step_is_not_set(self):
        b = rb.apply(self._steady())
        self.assertTrue(b["row_build"]["provisional"])
        self.assertEqual(b["row_build"]["step"], 1)
        self.assertTrue(any("set on the morning" in n for n in b["setup_notes"]))

    def test_applying_twice_does_not_stack_notes(self):
        once = rb.apply(self._steady(), 2)
        twice = rb.apply(once, 2)
        self.assertEqual(once, twice)
        self.assertEqual(rb.apply(rb.apply(self._steady()), 2), once)

    def test_only_the_rower(self):
        self.assertFalse(rb.applies(self._steady("bike")))
        self.assertTrue(rb.applies(self._steady()))
        z4 = {"type": "intervals", "ran_as": "intervals", "cardio": {"modality": "row"}}
        self.assertFalse(rb.applies(z4), "a gate-passed Zone 4 day is left alone")

    def test_does_not_mutate_its_input(self):
        src = self._steady()
        before = json.dumps(src, sort_keys=True)
        rb.apply(src, 1)
        self.assertEqual(json.dumps(src, sort_keys=True), before)


class Seeder(unittest.TestCase):
    def test_rower_sessions_seed_as_the_row_build(self):
        for build in (lambda: office._z2(1, "MSP home", "msp_home", on=DAY),
                      lambda: office._intervals(1, "MSP home", "msp_home", on=DAY)):
            blocks, _, zone, est = build()
            self.assertEqual(blocks["display_name"], rb.DISPLAY_NAME)
            self.assertTrue(blocks["row_build"]["provisional"])
            self.assertEqual(zone, 2)
            self.assertGreaterEqual(est, 40)

    def test_other_modalities_are_untouched(self):
        blocks, *_ = office._z2(1, "Richfield", "richfield", on=DAY)
        self.assertEqual(blocks["type"], "steady")
        self.assertNotIn("row_build", blocks)

    def test_the_name_is_in_the_pain_region_map(self):
        from artemis import health_regions
        primary, _ = health_regions.regions_for(rb.DISPLAY_NAME)
        self.assertTrue(primary, "an unregistered name unhooks the pain ladder")


class Cursor:
    """Answers by matching a fragment of the SQL. Records writes."""

    def __init__(self, *, last=None, hold=None, today_rows=(), logged=()):
        self.last, self.hold = last, hold
        self.rows = {r["plan_id"]: r for r in today_rows}
        self.logged = set(logged)
        self.writes, self.state, self._out = [], {}, None

    def execute(self, sql, params=()):
        if "blocks -> 'row_build' IS NOT NULL" in sql:
            self._out = [self.last] if self.last else []
        elif "FROM acos.system_state" in sql:
            self._out = [(self.hold,)] if self.hold else []
        elif sql.startswith("INSERT INTO acos.system_state"):
            self.state[params[0]] = params[1]
            self._out = []
        elif sql.startswith("SELECT plan_id, slot, blocks"):
            self._out = [(r["plan_id"], r["slot"], r["blocks"]) for r in self.rows.values()]
        elif sql.startswith("SELECT 1 FROM health.session_log"):
            self._out = [(1,)] if params[0] in self.logged else []
        elif sql.startswith("UPDATE health.plan"):
            self.writes.append(params)
            self.rows[params[2]]["blocks"] = json.loads(params[0])
            self._out = []
        elif sql.startswith("SELECT blocks FROM health.plan"):
            self._out = [(self.rows[params[0]]["blocks"],)]
        else:
            self._out = []

    def fetchone(self):
        return self._out[0] if self._out else None

    def fetchall(self):
        return list(self._out or [])


def _last(step, duration):
    return (PREV, {"row_build": rb.step_spec(step)}, duration)


class WhichStep(unittest.TestCase):
    def test_no_history_is_step_one(self):
        self.assertEqual(rb.step_for(Cursor(), DAY)["step"], 1)

    def test_a_finished_session_advances(self):
        full = rb.finish_sec(rb.step_spec(1))
        self.assertEqual(rb.step_for(Cursor(last=_last(1, full)), DAY)["step"], 2)

    def test_the_skipped_final_rest_still_counts_as_finished(self):
        # The screen skips the last rest; the clock stops at the last work end.
        self.assertEqual(
            rb.step_for(Cursor(last=_last(4, rb.finish_sec(rb.step_spec(4)) - 30)), DAY)["step"], 5)

    def test_a_short_session_repeats(self):
        got = rb.step_for(Cursor(last=_last(3, 20 * 60)), DAY)
        self.assertEqual(got["step"], 3)
        self.assertIn("again", got["why"])

    def test_no_duration_repeats(self):
        self.assertEqual(rb.step_for(Cursor(last=_last(2, None)), DAY)["step"], 2)

    def test_row_repeat_holds_a_finished_session(self):
        full = rb.finish_sec(rb.step_spec(2))
        cur = Cursor(last=_last(2, full), hold=PREV.isoformat())
        self.assertEqual(rb.step_for(cur, DAY)["step"], 2)

    def test_a_hold_names_one_session_only(self):
        full = rb.finish_sec(rb.step_spec(2))
        cur = Cursor(last=_last(2, full), hold="2031-02-01")
        self.assertEqual(rb.step_for(cur, DAY)["step"], 3)

    def test_hold_last_stores_the_session_date(self):
        cur = Cursor(last=_last(2, 100))
        got = rb.hold_last(cur, DAY)
        self.assertEqual((got["held"], got["step"]), (True, 2))
        self.assertEqual(cur.state[rb.HOLD_KEY], PREV.isoformat())

    def test_hold_with_nothing_logged_changes_nothing(self):
        cur = Cursor()
        self.assertFalse(rb.hold_last(cur, DAY)["held"])
        self.assertEqual(cur.state, {})


class Resolve(unittest.TestCase):
    def _row(self, plan_id=7, blocks=None):
        seeded = rb.apply({"type": "steady", "cardio": {"modality": "row"},
                           "setup_notes": []})
        return {"plan_id": plan_id, "slot": "morning", "blocks": blocks or seeded}

    def _resolve(self, cur):
        import knowledge.cognition as cog
        orig, cog.log_decision = cog.log_decision, lambda *a, **k: self.logged.append(k)
        try:
            return rb.resolve_day(cur, DAY)
        finally:
            cog.log_decision = orig

    def setUp(self):
        self.logged = []

    def test_writes_the_derived_step_and_records_why(self):
        cur = Cursor(last=_last(1, rb.finish_sec(rb.step_spec(1))), today_rows=[self._row()])
        got = self._resolve(cur)
        self.assertEqual((got["resolved"], got["step"]), (True, 2))
        written = cur.rows[7]["blocks"]["row_build"]
        self.assertEqual((written["step"], written["provisional"]), (2, False))
        self.assertEqual(cur.writes[0][1], 42)
        self.assertEqual(self.logged[0]["outcome"], "step_2")

    def test_a_logged_day_is_never_rewritten(self):
        cur = Cursor(today_rows=[self._row()], logged=[7])
        got = self._resolve(cur)
        self.assertFalse(got["resolved"])
        self.assertEqual(cur.writes, [])
        self.assertEqual(self.logged, [])

    def test_a_steady_row_day_is_converted_on_the_day(self):
        steady = {"type": "steady", "duration_min": 40, "cardio": {"modality": "row"}}
        cur = Cursor(today_rows=[self._row(blocks=steady)])
        self._resolve(cur)
        self.assertEqual(cur.rows[7]["blocks"]["display_name"], rb.DISPLAY_NAME)

    def test_other_sessions_are_left_alone(self):
        bike = {"type": "steady", "cardio": {"modality": "bike"}}
        cur = Cursor(today_rows=[self._row(blocks=bike)])
        self.assertFalse(self._resolve(cur)["resolved"])
        self.assertEqual(cur.writes, [])

    def test_idempotent(self):
        cur = Cursor(today_rows=[self._row()])
        self._resolve(cur)
        first = json.dumps(cur.rows[7]["blocks"], sort_keys=True)
        self._resolve(cur)
        self.assertEqual(json.dumps(cur.rows[7]["blocks"], sort_keys=True), first)

    def test_a_write_that_does_not_persist_raises(self):
        cur = Cursor(today_rows=[self._row()])
        real = cur.execute

        def lossy(sql, params=()):
            if sql.startswith("UPDATE health.plan"):
                cur._out = []
                return
            real(sql, params)
        cur.execute = lossy
        with self.assertRaises(RuntimeError):
            self._resolve(cur)


if __name__ == "__main__":
    unittest.main()
