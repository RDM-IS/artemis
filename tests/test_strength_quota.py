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

from knowledge import cycle_days, row_circuit, session_types as st, strength_quota as sq

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


# STATUS-3 phase 2: goal weeks run Mon..Sun. 2031-03-03 is a Monday.
M, TU, W, TH, F, SA, SU = (date(2031, 3, d) for d in range(3, 10))
CF = date(2000, 1, 3)                                   # a Monday, long ago


def free(ws=M, **kinds):
    """A resolved week with every day free, except the ones named:
    free(SU="travel") / free(SA="away")."""
    out = cycle_days.resolve_days(ws, ws + timedelta(days=6), [])
    for info in out.values():
        info.update(day_type="msp_home", away=False)
    names = {"M": 0, "TU": 1, "W": 2, "TH": 3, "F": 4, "SA": 5, "SU": 6}
    for name, kind in kinds.items():
        info = out[ws + timedelta(days=names[name])]
        if kind == "away":
            info["away"] = True
        else:
            info["day_type"] = kind
    return out


def q(day, done, today, days=None, **kw):
    return sq.compute(day, done, today=today, counts_from=kw.pop("counts_from", CF),
                      days=days if days is not None else free(), **kw)


class ComputeTest(unittest.TestCase):
    def test_week_of_is_mon_to_sun(self):
        for d in (M, W, SU):
            self.assertEqual(sq.week_of(d), (M, SU))
        self.assertEqual(sq.week_of(date(2031, 3, 2)), (date(2031, 2, 24), date(2031, 3, 2)))

    def test_ryans_example_week_of_11_9(self):
        """Ryan, 2026-11-10: 0 done after Tue -> Wed (red), Fri, Sun; the day
        before the red day is orange."""
        mon = date(2026, 11, 9)
        days = cycle_days.resolve_days(mon, mon + timedelta(days=6), [])
        r = sq.compute(mon + timedelta(days=1), [], today=mon + timedelta(days=1),
                       counts_from=CF, days=days)
        self.assertEqual(r["placements"], ["2026-11-11", "2026-11-13", "2026-11-15"])
        self.assertEqual(r["last_chance"], "2026-11-11")
        self.assertEqual(r["urgency"], {"2026-11-11": "red", "2026-11-10": "orange"})
        self.assertEqual(r["status"], "tight")
        self.assertFalse(r["nag_due"])                    # today is not the last chance

    def test_placed_as_late_as_feasible(self):
        r = q(M, [], M)
        self.assertEqual(r["placements"], [W.isoformat(), F.isoformat(), SU.isoformat()])
        self.assertEqual((r["capacity"], r["status"]), (4, "on_track"))   # M, W, F, SU
        self.assertEqual(r["urgency"], {W.isoformat(): "red", TU.isoformat(): "orange"})

    def test_no_orange_next_to_a_day_already_done(self):
        r = q(SA, [F], SA)                                 # Sat is next to Fri's lift
        self.assertEqual(r["placements"], [SU.isoformat()])
        self.assertEqual(r["urgency"], {SU.isoformat(): "red"})

    def test_travel_and_away_days_are_never_placed(self):
        r = q(M, [], M, free(SU="travel"))
        self.assertNotIn(SU.isoformat(), r["placements"])
        self.assertEqual(r["placements"], [TU.isoformat(), TH.isoformat(), SA.isoformat()])
        self.assertEqual(r["blocked_days"], [SU.isoformat()])

    def test_a_week_with_an_away_day_is_not_counted(self):
        r = q(M, [], W, free(SA="away"))
        self.assertEqual((r["status"], r["counted"], r["away"], r["urgency"], r["nag_due"]),
                         ("not_counted", False, True, {}, False))

    def test_never_next_to_a_day_already_done(self):
        r = q(W, [TH], W)                                  # done Thu: Wed and Fri are out
        self.assertNotIn(W.isoformat(), r["placements"])
        self.assertNotIn(F.isoformat(), r["placements"])
        self.assertEqual(r["placements"], [SU.isoformat()])   # Sat and Sun are adjacent
        self.assertEqual(r["needed"], 2)
        self.assertEqual(r["capacity"], 1)                 # only Sat or Sun, not both
        self.assertEqual(r["status"], "short")

    def test_today_is_the_last_chance_is_nag_due(self):
        r = q(F, [M, W], F)                                # needs 1; Fri, Sat, Sun left
        self.assertEqual(r["placements"], [SU.isoformat()])
        self.assertFalse(r["nag_due"])
        r = q(F, [M], F)                                   # needs 2: Fri and Sun
        self.assertEqual(r["placements"], [F.isoformat(), SU.isoformat()])
        self.assertEqual((r["last_chance"], r["status"]), (F.isoformat(), "tight"))
        self.assertTrue(r["nag_due"])

    def test_short(self):
        r = q(SA, [], SA)
        self.assertEqual((r["needed"], r["days_left"], r["capacity"], r["status"]), (3, 2, 1, "short"))
        self.assertEqual(r["placements"], [SU.isoformat()])
        self.assertFalse(r["nag_due"])                     # the red day is Sunday
        r = q(SU, [], SU)
        self.assertTrue(r["nag_due"])

    def test_met(self):
        r = q(F, [M, W, F], F)
        self.assertEqual((r["needed"], r["status"], r["placements"], r["urgency"]),
                         (0, "met", [], {}))
        self.assertFalse(r["nag_due"])

    def test_today_already_counted_is_not_a_day_left(self):
        r = q(F, [W, F], F)
        self.assertEqual((r["needed"], r["days_left"], r["placements"]), (1, 2, [SU.isoformat()]))

    def test_finished_week(self):
        r = q(SA, [M, W], date(2031, 3, 10))
        self.assertEqual((r["finished"], r["days_left"], r["status"], r["placements"]),
                         (True, 0, "short", []))
        self.assertFalse(r["nag_due"])

    def test_future_week(self):
        r = q(W, [], date(2031, 2, 26))
        self.assertEqual((r["days_left"], r["status"]), (7, "on_track"))
        self.assertEqual(r["placements"], [W.isoformat(), F.isoformat(), SU.isoformat()])

    def test_weeks_before_counts_from_are_not_counted(self):
        r = q(SA, [], date(2031, 3, 11), counts_from=date(2031, 3, 10))
        self.assertEqual((r["status"], r["counted"], r["nag_due"], r["repeat_due"]),
                         ("not_counted", False, False, False))
        self.assertTrue(q(SA, [], date(2031, 3, 11), counts_from=M)["repeat_due"])

    def test_counts_from_has_no_default(self):
        with self.assertRaises(TypeError):
            sq.compute(W, [], today=W)                       # noqa

    def test_parse_counts_from_is_fail_closed(self):
        self.assertEqual(sq.parse_counts_from("2031-03-03"), M)
        self.assertEqual(sq.parse_counts_from('"2031-03-03"'), M)      # JSON-quoted
        # the SESSION-OPEN-era Sunday reads as the Monday after it
        self.assertEqual(sq.parse_counts_from("2031-03-02"), M)
        for bad in (None, "", "  ", "soon", "2031-03-05"):              # last: a Wednesday
            with self.assertRaises(sq.QuotaUnreadable, msg=repr(bad)):
                sq.parse_counts_from(bad)

    def test_for_program_week(self):
        self.assertEqual(sq.for_program_week(date(2031, 3, 2)), M)
        with self.assertRaises(ValueError):
            sq.for_program_week(M)

    def test_repeat_due_only_once_the_week_has_ended(self):
        self.assertFalse(q(SA, [], SU)["repeat_due"])
        self.assertTrue(q(SA, [], date(2031, 3, 10))["repeat_due"])
        self.assertFalse(q(SA, [M, W, F], date(2031, 3, 10))["repeat_due"])

    def test_days_outside_the_week_and_duplicates_ignored(self):
        r = q(W, [M, M, date(2031, 3, 2), date(2031, 3, 10)], W)
        self.assertEqual(r["strength_days"], [M.isoformat()])


class CycleDaysTest(unittest.TestCase):
    def test_the_newest_override_decides_type_and_location(self):
        rows = [  # newest first
            {"override_id": 2, "start_date": TU, "end_date": TU, "day_type": None, "location": "richfield"},
            {"override_id": 1, "start_date": M, "end_date": W, "day_type": "travel", "location": None},
        ]
        got = cycle_days.resolve_days(M, W, rows)
        self.assertEqual(got[M]["day_type"], "travel")
        # the newest covering row names no type, so the PATTERN's type stands
        self.assertEqual(got[TU]["day_type"], cycle_days.DAY_TYPES[cycle_days.cycle_pos(TU)])
        self.assertEqual((got[TU]["location"], got[TU]["override_id"]), ("richfield", 2))

    def test_any_away_override_makes_an_away_day(self):
        rows = [{"override_id": 5, "start_date": M, "end_date": M, "day_type": "wi", "location": "richfield"},
                {"override_id": 4, "start_date": M, "end_date": M, "day_type": "away", "location": None}]
        got = cycle_days.resolve_days(M, M, rows)
        self.assertTrue(got[M]["away"])
        self.assertEqual(got[M]["day_type"], "wi")

    def test_matches_the_box_cycle_on_the_pattern(self):
        from artemis import cycle
        from unittest.mock import patch
        start = cycle.DEFAULT_ANCHOR
        got = cycle_days.resolve_days(start, start + timedelta(days=13), [])
        with patch.object(cycle, "override_for", return_value=None):
            for d, info in got.items():
                self.assertEqual(info["day_type"], cycle.day_type(d), d)
                self.assertEqual(info["location"], cycle.anchor_location(d), d)

    def test_fail_closed(self):
        def boom(sql, params):
            raise RuntimeError("UndefinedTable")
        with self.assertRaises(cycle_days.CycleUnreadable):
            cycle_days.read_pg(M, SU, boom)
        with self.assertRaises(cycle_days.CycleUnreadable):
            cycle_days.parse_anchor("soon")
        self.assertEqual(cycle_days.parse_anchor(None), cycle_days.DEFAULT_ANCHOR)


def _fake(days, counts_from="2000-01-03", seen=None, overrides=(), boom_on=None):
    """Answers read_pg's reads: counts-from, the strength days, the cycle anchor
    and the cycle overrides."""
    def execute(sql, params):
        if boom_on and boom_on in sql:
            raise RuntimeError("relation does not exist")
        if "acos.system_state" in sql:
            if params == {"k": sq.COUNTS_FROM_KEY}:
                return [{"value": counts_from}] if counts_from is not None else []
            assert params == {"k": cycle_days.ANCHOR_KEY}, params
            return []
        if "cycle_day_overrides" in sql:
            return list(overrides)
        if seen is not None:
            seen.update(sql=sql, params=params)
        return [{"day": d} for d in days]
    return execute


class ReadTest(unittest.TestCase):
    def test_pg_binds_tz_as_a_parameter(self):
        seen = {}
        r = sq.read_pg(W, tz="Pacific/Auckland", today=W, execute=_fake([M, W], seen=seen))
        self.assertEqual(r["count"], 2)
        self.assertEqual(seen["params"]["tz"], "Pacific/Auckland")
        self.assertEqual((seen["params"]["week_start"], seen["params"]["week_end"]), (M, SU))
        self.assertEqual(seen["params"]["types"], list(st.STRENGTH_DAY_TYPES))
        self.assertNotIn("Auckland", seen["sql"])
        self.assertNotIn("America/", seen["sql"])

    def test_overrides_reach_the_placement(self):
        ov = [{"override_id": 1, "start_date": SU, "end_date": SU, "day_type": "travel", "location": None}]
        r = sq.read_pg(M, tz="UTC", today=M, execute=_fake([], overrides=ov))
        self.assertIn(SU.isoformat(), r["blocked_days"])
        ov = [{"override_id": 2, "start_date": SA, "end_date": SU, "day_type": "away", "location": None}]
        self.assertEqual(sq.read_pg(M, tz="UTC", today=M, execute=_fake([], overrides=ov))["status"],
                         "not_counted")

    def test_an_unreadable_override_table_is_an_unreadable_quota(self):
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(M, tz="UTC", today=M, execute=_fake([], boom_on="cycle_day_overrides"))

    def test_counts_from_comes_from_system_state(self):
        r = sq.read_pg(W, tz="UTC", today=date(2031, 3, 11), execute=_fake([], "2031-03-10"))
        self.assertEqual(r["status"], "not_counted")
        r = sq.read_pg(W, tz="UTC", today=date(2031, 3, 11), execute=_fake([], "2031-03-03"))
        self.assertEqual(r["status"], "short")

    def test_an_unset_counts_from_is_unreadable_not_counted(self):
        for v in (None, "", "2031-03-05"):
            with self.assertRaises(sq.QuotaUnreadable, msg=repr(v)):
                sq.read_pg(W, tz="UTC", today=W, execute=_fake([M], v))

    def test_fail_closed(self):
        def boom(sql, params):
            raise RuntimeError("connection refused")
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(W, tz="UTC", today=W, execute=boom)
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(W, tz="", today=W, execute=_fake([]))
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_pg(W, tz="UTC", today=W, execute=_fake(["2031-03-04"]))

    @unittest.skipUnless(HAVE_SA, "sqlalchemy not installed")
    def test_sa_matches_pg(self):
        class Res:
            def __init__(self, rows): self.rows = rows
            def mappings(self): return self
            def all(self): return self.rows

        class DB:
            def execute(self, clause, params):
                sql = str(clause)
                if "acos.system_state" in sql:
                    if params == {"k": sq.COUNTS_FROM_KEY}:
                        return Res([{"value": "2000-01-03"}])
                    return Res([])
                if "cycle_day_overrides" in sql:
                    self.ov = params
                    return Res([])
                self.sql, self.params = sql, params
                return Res([{"day": TU}])

        db = DB()
        r = sq.read_sa(db, W, tz="UTC", today=W)
        self.assertEqual(r["strength_days"], [TU.isoformat()])
        self.assertEqual(db.ov, {"start": M, "end": SU})
        # binds only: `::date` is a cast, not a parameter
        self.assertEqual(re.sub(r"(?<!:):([a-z_]\w*)", r"%(\1)s", db.sql), sq.SQL_PG)

    @unittest.skipUnless(HAVE_SA, "sqlalchemy not installed")
    def test_sa_fail_closed(self):
        class DB:
            def execute(self, *a):
                raise RuntimeError("UndefinedColumn")
        with self.assertRaises(sq.QuotaUnreadable):
            sq.read_sa(DB(), W, tz="UTC", today=W)

    def test_sql_reads_real_logs_only(self):
        self.assertIn("IS DISTINCT FROM 'inferred'", sq.SQL_PG)
        self.assertIn("is_skipped", sq.SQL_PG)
        self.assertIn("LEFT JOIN health.plan", sq.SQL_PG)   # plan_id nullable
        self.assertNotIn("dnf", sq.SQL_PG)                    # DNF rounds count


class DeployScriptTest(unittest.TestCase):
    """scripts/set_strength_quota_counts_from.py: dry run by default, a Monday
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
             mock.patch("artemis.quiet_hours.local_today", return_value=W):
            return m.main(argv), w

    def test_dry_run_writes_nothing_and_defaults_to_this_weeks_monday(self):
        rc, w = self._run([])
        self.assertEqual(rc, 0)
        w.assert_not_called()

    def test_commit_writes_this_weeks_monday_and_verifies(self):
        rc, w = self._run(["--commit"], back={"value": M.isoformat()})
        self.assertEqual(rc, 0)
        self.assertEqual(w.call_args[0][1], (sq.COUNTS_FROM_KEY, M.isoformat()))

    def test_a_bad_re_read_fails(self):
        rc, _ = self._run(["--commit"], back=None)
        self.assertEqual(rc, 1)

    def test_refuses_to_move_an_existing_value(self):
        rc, w = self._run(["--commit"], current="2031-02-23")
        self.assertEqual(rc, 1)
        w.assert_not_called()

    def test_refuses_a_non_monday(self):
        with self.assertRaises(sq.QuotaUnreadable):
            self._run(["--monday", "2031-03-05"])


if __name__ == "__main__":
    unittest.main()
