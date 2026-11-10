"""SESSION-OPEN steps 1 + 2: STRENGTH_DAY_TYPES and the weekly quota reader.

No database. PUBLIC-FIXTURES: every date is in 2031, far outside any logged
session, and nothing here is a real load, RPE or bodyweight.
"""

import os
import re
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ["ARTEMIS_TEST_NO_DB"] = "1"

try:
    import sqlalchemy  # noqa: F401  (Lambda dependency; absent on the box)
    HAVE_SA = True
except ImportError:
    HAVE_SA = False

from knowledge import row_circuit, session_types as st, strength_quota as sq

ROOT = Path(__file__).resolve().parent.parent

# A synthetic week: Sun 2031-03-02 .. Sat 2031-03-08.
SUN, MON, TUE, WED, THU, FRI, SAT = (date(2031, 3, d) for d in range(2, 9))
#: A counts-from Sunday long before the synthetic week, so it is counted.
CF = date(2000, 1, 2)


class StrengthDayTypesTest(unittest.TestCase):
    def test_lifts_and_the_circuit(self):
        for t in st.STRENGTH_TYPES:
            self.assertIn(t, st.STRENGTH_DAY_TYPES)
        self.assertIn("row_circuit", st.STRENGTH_DAY_TYPES)

    def test_excluded(self):
        for t in ("yoga_strength", "row_ladder", "ride_ladder", "run_ladder", "cardio_z2",
                  "cardio_intervals", "recovery_flow", "rest", "mobility", "core"):
            self.assertNotIn(t, st.STRENGTH_DAY_TYPES, t)

    def test_every_floor_kind_is_a_strength_day_and_no_other(self):
        # Adding ride+floor / run+floor to row_circuit.KINDS fails here until it
        # is added to CIRCUIT_TYPES too.
        for kind, spec in row_circuit.KINDS.items():
            self.assertEqual(spec["session_type"] in st.STRENGTH_DAY_TYPES, bool(spec["floor"]),
                             kind)

    def test_away_bodyweight_circuit_counts(self):
        # Ryan, 2026-11-09 (Q5): AWAY's no-gym bodyweight circuit is a strength day.
        self.assertIn("bodyweight_circuit", st.STRENGTH_DAY_TYPES)

    def test_planned_circuit_row_counts_as_a_circuit(self):
        self.assertEqual(st.day_type("cardio_z2", "circuit"), "row_circuit")
        self.assertTrue(st.is_strength_day_type("cardio_z2", "circuit"))
        self.assertFalse(st.is_strength_day_type("cardio_z2", "ladder"))
        self.assertFalse(st.is_strength_day_type("cardio_z2", None))
        self.assertTrue(st.is_strength_day_type("strength_full", None))
        self.assertEqual(st.PLANNED_CIRCUIT_TYPE, row_circuit.KINDS["circuit"]["session_type"])


class NoLiteralCopyTest(unittest.TestCase):
    """SAME-FACT-MANY-PLACES: a fresh literal copy of the strength-day list fails."""
    PATTERNS = (
        re.compile(r'[\(\[\{][^\)\]\}]*"strength_(?:a|b|c|full)"[^\)\]\}]*"row_circuit"'),
        re.compile(r'[\(\[\{][^\)\]\}]*"row_circuit"[^\)\]\}]*"strength_(?:a|b|c|full)"'),
        re.compile(r'STRENGTH_TYPES\s*\+\s*\(\s*"row_circuit"'),
    )

    def _hits(self, text: str) -> bool:
        return any(p.search(text) for p in self.PATTERNS)

    def test_the_scan_can_fail(self):
        self.assertTrue(self._hits('X = ("strength_a", "strength_b", "row_circuit")'))
        self.assertTrue(self._hits('X = ["row_circuit",\n  "strength_full"]'))
        self.assertTrue(self._hits('X = STRENGTH_TYPES + ("row_circuit",)'))
        self.assertFalse(self._hits('X = st.STRENGTH_DAY_TYPES'))

    def test_no_copy_outside_session_types(self):
        hits = []
        for sub in ("artemis", "knowledge", "api", "scripts"):
            for p in (ROOT / sub).rglob("*.py"):
                if p.name == "session_types.py":
                    continue
                if self._hits(p.read_text()):
                    hits.append(str(p.relative_to(ROOT)))
        self.assertEqual(hits, [])


class ComputeTest(unittest.TestCase):
    def test_week_of_is_sun_to_sat(self):
        for d in (SUN, WED, SAT):
            self.assertEqual(sq.week_of(d), (SUN, SAT))

    def test_mid_week_on_track(self):
        q = sq.compute(WED, [MON], today=WED, counts_from=CF)
        self.assertEqual((q["count"], q["needed"], q["days_left"], q["status"]),
                         (1, 2, 4, "on_track"))
        self.assertFalse(q["nag_due"])

    def test_tight_is_nag_due(self):
        q = sq.compute(THU, [MON], today=FRI, counts_from=CF)   # needs 2, Fri+Sat left
        self.assertEqual((q["needed"], q["days_left"], q["status"]), (2, 2, "tight"))
        self.assertTrue(q["nag_due"])

    def test_short_with_days_left_is_nag_due(self):
        q = sq.compute(SAT, [], today=SAT, counts_from=CF)
        self.assertEqual((q["needed"], q["days_left"], q["status"]), (3, 1, "short"))
        self.assertTrue(q["nag_due"])

    def test_met(self):
        q = sq.compute(FRI, [MON, WED, FRI], today=FRI, counts_from=CF)
        self.assertEqual((q["needed"], q["status"]), (0, "met"))
        self.assertFalse(q["nag_due"])

    def test_today_already_counted_is_not_a_day_left(self):
        q = sq.compute(FRI, [WED, FRI], today=FRI, counts_from=CF)   # needs 1, only Sat left
        self.assertEqual((q["needed"], q["days_left"], q["status"]), (1, 1, "tight"))

    def test_finished_week(self):
        q = sq.compute(SAT, [MON, WED], today=date(2031, 3, 9), counts_from=CF)
        self.assertEqual((q["finished"], q["days_left"], q["status"]), (True, 0, "short"))
        self.assertFalse(q["nag_due"])   # nothing left to nag about

    def test_future_week(self):
        q = sq.compute(WED, [], today=date(2031, 2, 26), counts_from=CF)
        self.assertEqual((q["days_left"], q["status"]), (7, "on_track"))

    def test_weeks_before_counts_from_are_not_counted(self):
        q = sq.compute(SAT, [], today=date(2031, 3, 10), counts_from=date(2031, 3, 9))
        self.assertEqual((q["status"], q["counted"], q["nag_due"], q["repeat_due"]),
                         ("not_counted", False, False, False))
        q = sq.compute(SAT, [], today=date(2031, 3, 10), counts_from=SUN)
        self.assertTrue(q["repeat_due"])

    def test_counts_from_has_no_default(self):
        with self.assertRaises(TypeError):
            sq.compute(WED, [], today=WED)                       # noqa

    def test_parse_counts_from_is_fail_closed(self):
        self.assertEqual(sq.parse_counts_from("2031-03-02"), SUN)
        self.assertEqual(sq.parse_counts_from('"2031-03-02"'), SUN)   # JSON-quoted
        for bad in (None, "", "  ", "soon", "2031-03-03"):            # last: a Monday
            with self.assertRaises(sq.QuotaUnreadable, msg=repr(bad)):
                sq.parse_counts_from(bad)

    def test_repeat_due_only_once_the_week_has_ended(self):
        self.assertFalse(sq.compute(SAT, [], today=SAT, counts_from=CF)["repeat_due"])
        self.assertTrue(sq.compute(SAT, [], today=date(2031, 3, 9), counts_from=CF)["repeat_due"])
        self.assertFalse(sq.compute(SAT, [MON, WED, FRI], today=date(2031, 3, 9), counts_from=CF)["repeat_due"])

    def test_days_outside_the_week_and_duplicates_ignored(self):
        q = sq.compute(WED, [MON, MON, date(2031, 3, 1), date(2031, 3, 9)], today=WED, counts_from=CF)
        self.assertEqual(q["strength_days"], [MON.isoformat()])


def _fake(days, counts_from="2000-01-02", seen=None):
    """Answers the two reads read_pg makes: the counts-from key, then the days."""
    def execute(sql, params):
        if "acos.system_state" in sql:
            assert params == {"k": sq.COUNTS_FROM_KEY}, params
            return [{"value": counts_from}] if counts_from is not None else []
        if seen is not None:
            seen.update(sql=sql, params=params)
        return [{"day": d} for d in days]
    return execute


class ReadTest(unittest.TestCase):
    def test_pg_binds_tz_as_a_parameter(self):
        seen = {}
        q = sq.read_pg(WED, tz="Pacific/Auckland", today=WED, execute=_fake([MON, WED], seen=seen))
        self.assertEqual(q["count"], 2)
        self.assertEqual(q["status"], "on_track")
        self.assertEqual(seen["params"]["tz"], "Pacific/Auckland")
        self.assertEqual((seen["params"]["week_start"], seen["params"]["week_end"]), (SUN, SAT))
        self.assertEqual(seen["params"]["types"], list(st.STRENGTH_DAY_TYPES))
        self.assertNotIn("Auckland", seen["sql"])
        self.assertNotIn("America/", seen["sql"])

    def test_counts_from_comes_from_system_state(self):
        q = sq.read_pg(WED, tz="UTC", today=date(2031, 3, 10), execute=_fake([], "2031-03-09"))
        self.assertEqual(q["status"], "not_counted")
        q = sq.read_pg(WED, tz="UTC", today=date(2031, 3, 10), execute=_fake([], "2031-03-02"))
        self.assertEqual(q["status"], "short")

    def test_an_unset_counts_from_is_unreadable_not_counted(self):
        for v in (None, "", "2031-03-03"):
            with self.assertRaises(sq.QuotaUnreadable, msg=repr(v)):
                sq.read_pg(WED, tz="UTC", today=WED, execute=_fake([MON], v))

    def test_fail_closed(self):
        def boom(sql, params):
            raise RuntimeError("connection refused")
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(WED, tz="UTC", today=WED, execute=boom)
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(WED, tz="", today=WED, execute=_fake([]))
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(WED, tz="UTC", today=WED, execute=_fake(["2031-03-03"]))

    @unittest.skipUnless(HAVE_SA, "sqlalchemy not installed")
    def test_sa_matches_pg(self):
        class Res:
            def __init__(self, rows): self.rows = rows
            def mappings(self): return self
            def all(self): return self.rows

        class DB:
            def execute(self, clause, params):
                if "acos.system_state" in str(clause):
                    self.k = params
                    return Res([{"value": "2000-01-02"}])
                self.sql, self.params = str(clause), params
                return Res([{"day": TUE}])

        db = DB()
        q = sq.read_sa(db, WED, tz="UTC", today=WED)
        self.assertEqual(q["strength_days"], [TUE.isoformat()])
        self.assertEqual(db.k, {"k": sq.COUNTS_FROM_KEY})
        # binds only: `::date` is a cast, not a parameter
        self.assertEqual(re.sub(r"(?<!:):([a-z_]\w*)", r"%(\1)s", db.sql), sq.SQL_PG)

    @unittest.skipUnless(HAVE_SA, "sqlalchemy not installed")
    def test_sa_fail_closed(self):
        class DB:
            def execute(self, *a):
                raise RuntimeError("UndefinedColumn")
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_sa(DB(), WED, tz="UTC", today=WED)

    def test_sql_reads_real_logs_only(self):
        self.assertIn("IS DISTINCT FROM 'inferred'", sq.SQL_PG)
        self.assertIn("is_skipped", sq.SQL_PG)
        self.assertIn("LEFT JOIN health.plan", sq.SQL_PG)   # plan_id nullable
        self.assertNotIn("dnf", sq.SQL_PG)                    # DNF rounds count


class DeployScriptTest(unittest.TestCase):
    """scripts/set_strength_quota_counts_from.py: dry run by default, a Sunday
    only, never silently moves an existing value, verifies what it wrote."""

    def _run(self, argv, current=None, back=None):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location(
            "setcf", ROOT / "scripts" / "set_strength_quota_counts_from.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        reads = iter([{"value": current} if current else None, back])
        with mock.patch("knowledge.db.execute_one", side_effect=lambda *a: next(reads)), \
             mock.patch("knowledge.db.execute_write") as w, \
             mock.patch("artemis.quiet_hours.local_today", return_value=WED):
            return m.main(argv), w

    def test_dry_run_writes_nothing_and_defaults_to_this_weeks_sunday(self):
        rc, w = self._run([])
        self.assertEqual(rc, 0)
        w.assert_not_called()

    def test_commit_writes_this_weeks_sunday_and_verifies(self):
        rc, w = self._run(["--commit"], back={"value": SUN.isoformat()})
        self.assertEqual(rc, 0)
        self.assertEqual(w.call_args[0][1], (sq.COUNTS_FROM_KEY, SUN.isoformat()))

    def test_a_bad_re_read_fails(self):
        rc, _ = self._run(["--commit"], back=None)
        self.assertEqual(rc, 1)

    def test_refuses_to_move_an_existing_value(self):
        rc, w = self._run(["--commit"], current="2031-02-23")
        self.assertEqual(rc, 1)
        w.assert_not_called()

    def test_refuses_a_non_sunday(self):
        with self.assertRaises(sq.QuotaUnreadable):
            self._run(["--sunday", "2031-03-03"])


if __name__ == "__main__":
    unittest.main()
