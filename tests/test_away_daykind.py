"""AWAY-DAYKIND — an away day is a TRAVEL day for meals, and the schedule is untouched.

The defect this covers is a mapping that could never fire. `nutrition.day_kind`
has mapped `away` to KIND_TRAVEL since the AWAY round, but every caller asked
`cycle.day_type()`, which CANNOT return `away` — its valid set is built from the
14-day pattern tuple and `away` only exists as an override. So the 11/19–12/04
hunting stays would have been pre-filled with a work-day menu by code that looked
like it prevented exactly that.

Two readers of one fact, on purpose: the SCHEDULE keeps `cycle.day_type()`,
because wake and quiet hours are about the clock and not the bed; anything
deciding about FOOD asks `nutrition.meal_day_type()`. These tests pin both halves
— the meal answer changes, and the schedule answer does not.

PUBLIC-FIXTURES: synthetic dates in 2031 for the unit cases. Ryan's real November
dates appear only as *dates* in the regression case, which is the point of that
case — no logged data, no macros, no measurements.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD

import ast
import unittest
from datetime import date

from artemis import away, cycle, nutrition


def hunting(start, end):
    return away.Stay(start=start, end=end, purpose=away.HUNTING,
                     lodging=away.NO_GYM)


class TestMealDayTypeSeesAway(unittest.TestCase):
    def test_an_away_day_is_away_for_meals(self):
        stays = [hunting(date(2031, 4, 10), date(2031, 4, 14))]
        self.assertEqual(
            nutrition.meal_day_type(date(2031, 4, 12), stays=stays), "away")

    def test_and_therefore_a_TRAVEL_day_for_the_menu(self):
        # The whole point: travel kind means no pre-fill and logging only, rather
        # than a work-day menu for a week in a hotel.
        stays = [hunting(date(2031, 4, 10), date(2031, 4, 14))]
        kind = nutrition.day_kind(
            nutrition.meal_day_type(date(2031, 4, 12), stays=stays))
        self.assertEqual(kind, nutrition.KIND_TRAVEL)

    def test_the_travel_kind_has_no_default_row_beyond_the_travel_one(self):
        # A travel day's only default row is the travel row, which does not exist
        # in Notion yet — so the day records no_plan rather than borrowing the
        # work-day menu.
        self.assertEqual(nutrition._default_row_for(nutrition.KIND_TRAVEL),
                         "default day — travel day")

    def test_a_day_outside_the_stay_is_unchanged(self):
        stays = [hunting(date(2031, 4, 10), date(2031, 4, 14))]
        for d in (date(2031, 4, 9), date(2031, 4, 15)):
            self.assertEqual(nutrition.meal_day_type(d, stays=stays),
                             cycle.day_type(d, use_overrides=False),
                             d)

    def test_with_no_stays_at_all_it_is_the_cycles_answer(self):
        for d in (date(2031, 4, 1), date(2031, 4, 2), date(2031, 4, 6)):
            self.assertEqual(nutrition.meal_day_type(d, stays=[]),
                             cycle.day_type(d, use_overrides=False), d)

    def test_the_boundary_days_of_a_stay_are_included(self):
        stays = [hunting(date(2031, 4, 10), date(2031, 4, 14))]
        for d in (date(2031, 4, 10), date(2031, 4, 14)):
            self.assertEqual(nutrition.meal_day_type(d, stays=stays), "away", d)


class TestTheScheduleIsUntouched(unittest.TestCase):
    """cycle.day_type() must NOT learn about away. Widening it would mean
    DAY_TYPE_HOURS['away'], i.e. a KeyError in open_on / quiet_on / boundaries,
    and it would move quiet hours during a hunting stay."""

    def test_cycle_day_type_still_cannot_return_away(self):
        self.assertNotIn("away", cycle._VALID_DAY_TYPES)
        self.assertNotIn("away", cycle.DAY_TYPES)

    def test_every_day_type_the_cycle_can_return_has_business_hours(self):
        # The KeyError that widening day_type() would have caused, asserted as
        # the invariant it really is.
        for t in cycle._VALID_DAY_TYPES:
            self.assertIn(t, cycle.DAY_TYPE_HOURS, t)

    def test_wake_and_quiet_on_a_hunting_day_are_the_patterns(self):
        # 2026-11-21 is inside Ryan's 11/19–12/04 hunting window. The schedule
        # must be exactly what it was before AWAY-DAYKIND: derived from the
        # pattern, with no away-shaped exception.
        d = date(2026, 11, 21)
        base = cycle.day_type(d, use_overrides=False)
        self.assertEqual(cycle.wake_on(d, use_overrides=False),
                         cycle.wake_on(d, override=None, use_overrides=False))
        self.assertIn(base, cycle.DAY_TYPE_HOURS)
        # and the hours come from the base type, not from 'away'
        self.assertEqual(
            cycle.quiet_on(d, use_overrides=False),
            cycle._parse_hhmm(cycle.DAY_TYPE_HOURS[base][1]))


class TestFailClosed(unittest.TestCase):
    def test_a_failed_override_read_raises_instead_of_saying_not_away(self):
        """Answering "not away" from a failed read is what plans a home menu for
        a week in a hotel, and it writes perfectly cleanly."""
        import knowledge.db as kdb
        real = kdb.get_connection

        def boom():
            raise RuntimeError("connection refused")

        kdb.get_connection = boom
        try:
            with self.assertRaises(away.AwayLookupError):
                away.covers(date(2031, 4, 12))
        finally:
            kdb.get_connection = real

    def test_the_only_swallowed_error_is_the_test_guard(self):
        # Under ARTEMIS_TEST_NO_DB the guard raises RealDbInTestError, which means
        # "there is no table here", not "the read failed". That one is answered
        # None; everything else raises (above).
        self.assertIsNone(away.covers(date(2031, 4, 12)))

    def test_covers_narrows_to_one_exception_type(self):
        # Widening this except clause re-creates the 2026-09-26 fail-open defect.
        import inspect
        src = inspect.getsource(away.covers)
        self.assertIn("except RealDbInTestError:", src)
        self.assertNotIn("except Exception:\n        return None", src)


class TestOneResolverOnly(unittest.TestCase):
    """Only `meal_day_type` may call `cycle.day_type` to answer a meal question.

    The duplicate is what caused this bug: `prep.day_type_map` had its own away
    lookup while `resolve_meal_source` still asked `cycle.day_type()`, so the same
    fortnight was correctly excluded from a stay AND pre-filled with a work-day
    menu. This walks the AST for real Call nodes rather than grepping text —
    mentioning the function in a docstring is fine and wanted, calling it is not.
    """

    #: Functions permitted to call cycle.day_type directly, and why.
    ALLOWED = {
        # the resolver itself: this is where the fallback belongs
        ("artemis/nutrition.py", "meal_day_type"),
    }

    def _calls_of(self, path):
        """[(enclosing function name, lineno)] for every cycle.day_type(...) call."""
        tree = ast.parse(open(path).read())
        out = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "day_type"
                        and isinstance(f.value, ast.Name)
                        and f.value.id in ("cycle", "_cycle")):
                    out.append((fn.name, node.lineno))
        return out

    def test_only_the_resolver_calls_cycle_day_type_for_meals(self):
        offenders = []
        for path in ("artemis/nutrition.py", "artemis/prep.py"):
            for name, lineno in self._calls_of(path):
                if (path, name) not in self.ALLOWED:
                    offenders.append(f"{path}:{lineno} in {name}()")
        self.assertEqual(offenders, [], "\n".join(
            ["call nutrition.meal_day_type instead of cycle.day_type:"] + offenders))

    def test_the_resolver_really_does_call_it(self):
        # The guard above would also pass if the fallback disappeared entirely,
        # which would silently make every non-away day resolve to nothing.
        calls = self._calls_of("artemis/nutrition.py")
        self.assertIn("meal_day_type", [name for name, _ in calls])

    def test_prep_no_longer_holds_its_own_away_lookup(self):
        src = open("artemis/prep.py").read()
        self.assertNotIn("_away_dates", src)
        self.assertIn("nutrition.meal_day_type(", src)


if __name__ == "__main__":
    unittest.main()
