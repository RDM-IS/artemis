"""WEEK-AHEAD — the read-only lookahead. Synthetic only (PUBLIC-FIXTURES).

    python3.11 -m unittest tests.test_week_ahead
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from datetime import date, timedelta
from unittest import mock

from artemis import week_ahead as wa

D = date(2027, 5, 3)          # synthetic: no real plan row exists here


class Cur:
    """Serves health.plan rows and enforces binds."""

    def __init__(self, rows=None, raises=False):
        self.rows = rows if rows is not None else {}
        self.raises, self._res = raises, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} %s placeholders but {got} params"
        if self.raises:
            raise RuntimeError("RDS down")
        self._res = list(self.rows.get(params[0], []))

    def fetchall(self):
        return self._res


def _row(slot, stype, name=None):
    return (slot, stype, name)


def _patch(*, day_type="msp_work", location="office", meal=None, supported=True,
           warmup_known=True, meal_raises=False, supported_raises=False):
    from artemis import cycle, health_office as office, nutrition, session_library as sl
    from knowledge import warmup as wu
    src = meal or nutrition.MealSource("planned", day_type, "work", chosen="default")
    return (
        mock.patch.object(cycle, "day_type", return_value=day_type),
        mock.patch.object(cycle, "locations",
                          return_value={location: {"display": location.title()}}),
        mock.patch.object(office, "day_location_key", return_value=location),
        mock.patch.object(nutrition, "resolve_meal_source",
                          side_effect=RuntimeError("notion blew up") if meal_raises
                          else None, return_value=src),
        mock.patch.object(sl, "_supported",
                          side_effect=RuntimeError("cfg") if supported_raises else None,
                          return_value=supported),
        mock.patch.object(wu, "is_known", return_value=warmup_known),
    )


def _day(cur=None, **kw):
    patches = _patch(**kw)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        return wa.day_ahead(cur or Cur({D: [_row("morning", "strength_a", "Test A")]}), D)


class TestFlags(unittest.TestCase):
    def test_a_clean_day_has_no_flags(self):
        d = _day()
        self.assertEqual(d.flags, [])
        self.assertFalse(d.flagged)
        self.assertEqual(d.meal_outcome, "planned")

    def test_no_meal_source_when_the_prefill_would_record_no_plan(self):
        from artemis import nutrition
        d = _day(meal=nutrition.MealSource("no_plan", "travel", "travel"))
        self.assertIn("no_meal_source", d.flags)

    def test_no_meal_source_when_notion_is_unreachable(self):
        from artemis import nutrition
        d = _day(meal=nutrition.MealSource("unavailable", "msp_work", "work"))
        self.assertIn("no_meal_source", d.flags)
        self.assertEqual(d.meal_outcome, "unavailable")

    def test_a_day_with_a_source_is_not_flagged_for_meals(self):
        self.assertNotIn("no_meal_source", _day().flags)

    def test_session_cant_be_held(self):
        d = _day(supported=False)
        self.assertIn("session_cant_be_held", d.flags)
        self.assertEqual(d.unsupported, ["morning Test A"])

    def test_a_rest_day_is_not_flagged_for_support_or_warmup(self):
        """A rest row needs no location support and no warmup. Flagging it would
        bury the days that are real gaps."""
        cur = Cur({D: [_row("morning", "rest", "Rest")]})
        d = _day(cur, supported=False, warmup_known=False)
        self.assertNotIn("session_cant_be_held", d.flags)
        self.assertNotIn("no_warmup_configured", d.flags)

    def test_no_warmup_configured(self):
        d = _day(warmup_known=False)
        self.assertIn("no_warmup_configured", d.flags)

    def test_warmup_configured_is_not_flagged(self):
        self.assertNotIn("no_warmup_configured", _day(warmup_known=True).flags)

    def test_travel_day(self):
        from artemis import nutrition
        d = _day(day_type="travel",
                 meal=nutrition.MealSource("planned", "travel", "travel", chosen="default"))
        self.assertIn("travel_day", d.flags)

    def test_a_non_travel_day_is_not_flagged_as_one(self):
        self.assertNotIn("travel_day", _day().flags)

    def test_the_1004_shape_flags_both(self):
        """The case this exists for: a travel day whose travel default is missing."""
        from artemis import nutrition
        d = _day(day_type="travel",
                 meal=nutrition.MealSource("no_plan", "travel", "travel",
                                           detail="the default row was not found"))
        self.assertEqual(d.flags, ["no_meal_source", "travel_day"])

    def test_flags_come_back_in_display_order(self):
        from artemis import nutrition
        d = _day(day_type="travel", supported=False, warmup_known=False,
                 meal=nutrition.MealSource("no_plan", "travel", "travel"))
        self.assertEqual(d.flags, ["no_meal_source", "session_cant_be_held",
                                   "no_warmup_configured", "travel_day"])


class TestFailClosed(unittest.TestCase):
    def test_an_unreadable_meal_source_is_unknown_never_a_guess(self):
        d = _day(meal_raises=True)
        self.assertEqual(d.meal_outcome, "unknown")
        self.assertIsNone(d.meal_chosen)
        self.assertIn("unknown", d.flags)
        self.assertNotIn("no_meal_source", d.flags)   # not claimed either way

    def test_an_unreadable_day_is_unknown_and_nothing_else(self):
        d = _day(cur=Cur(raises=True))
        self.assertEqual(d.flags, ["unknown"])
        self.assertEqual(d.meal_outcome, "unknown")

    def test_an_unreadable_support_check_is_unknown_not_supported(self):
        d = _day(supported_raises=True)
        self.assertIn("unknown", d.flags)
        self.assertEqual(d.unsupported, [])       # never asserted unsupported either

    def test_unknown_never_renders_as_covered(self):
        text = wa.render([_day(meal_raises=True)])
        self.assertIn("meals: unknown", text)
        self.assertNotIn("No gaps found", text)


class TestOneCodePath(unittest.TestCase):
    """The lookahead and the pre-fill must never disagree about what a day gets."""

    def test_week_ahead_reads_the_same_resolver_the_prefill_uses(self):
        import inspect
        from artemis import nutrition
        self.assertIn("resolve_meal_source(d)", inspect.getsource(nutrition.prefill_day))
        self.assertIn("nutrition.resolve_meal_source(d)", inspect.getsource(wa.day_ahead))

    def test_they_agree_on_every_day_of_a_synthetic_week(self):
        """Drive both over one synthetic week and assert the outcomes match. This
        is the test that would have caught a re-implemented source order."""
        from artemis import nutrition
        start = date(2027, 5, 3)
        plan = {
            start + timedelta(0): nutrition.MealSource("planned", "msp_work", "work",
                                                       chosen="default"),
            start + timedelta(1): nutrition.MealSource("no_plan", "wi", "off"),
            start + timedelta(2): nutrition.MealSource("unavailable", "msp_work", "work"),
            start + timedelta(3): nutrition.MealSource("planned", "travel", "travel",
                                                       chosen="picked"),
            start + timedelta(4): nutrition.MealSource("no_plan", "travel", "travel"),
            start + timedelta(5): nutrition.MealSource("planned", "msp_work", "work",
                                                       chosen="default"),
            start + timedelta(6): nutrition.MealSource("no_plan", "msp_home", "off"),
        }

        def fake_resolve(d):
            return plan[d]

        from artemis import cycle, health_office as office, session_library as sl
        from knowledge import warmup as wu
        with mock.patch.object(nutrition, "resolve_meal_source", side_effect=fake_resolve), \
             mock.patch.object(cycle, "day_type", side_effect=lambda d: plan[d].day_type), \
             mock.patch.object(cycle, "locations",
                               return_value={"office": {"display": "Office"}}), \
             mock.patch.object(office, "day_location_key", return_value="office"), \
             mock.patch.object(sl, "_supported", return_value=True), \
             mock.patch.object(wu, "is_known", return_value=True):
            week = wa.week_ahead(Cur({}), start, 7)

        self.assertEqual(len(week), 7)
        for d in week:
            with self.subTest(day=d.day):
                # what the lookahead says == what the resolver the pre-fill uses says
                self.assertEqual(d.meal_outcome, plan[d.day].outcome)
                self.assertEqual(d.meal_chosen, plan[d.day].chosen)
                self.assertEqual("no_meal_source" in d.flags,
                                 plan[d.day].outcome in ("no_plan", "unavailable"))

    def test_support_comes_from_the_librarys_own_predicate(self):
        import inspect
        self.assertIn("sl._supported(", inspect.getsource(wa.day_ahead))


class TestRange(unittest.TestCase):
    def test_seven_days_inclusive_from_start(self):
        with mock.patch.object(wa, "day_ahead", side_effect=lambda _c, d: wa.DayAhead(day=d)):
            week = wa.week_ahead(Cur({}), D)
        self.assertEqual([d.day for d in week], [D + timedelta(n) for n in range(7)])

    def test_a_zero_or_negative_range_is_refused(self):
        for n in (0, -1):
            with self.subTest(days=n):
                with self.assertRaises(ValueError):
                    wa.week_ahead(Cur({}), D, n)


class TestRendering(unittest.TestCase):
    def _week(self, *flagsets):
        out = []
        for i, flags in enumerate(flagsets):
            d = wa.DayAhead(day=D + timedelta(i), day_type="msp_work",
                            location_key="office", location_display="Office",
                            meal_outcome="planned", meal_chosen="default")
            d.flags = list(flags)
            out.append(d)
        return out

    def test_flag_lines_are_empty_when_nothing_is_wrong(self):
        self.assertEqual(wa.flag_lines(self._week([], [])), [])

    def test_one_line_per_flagged_day_only(self):
        lines = wa.flag_lines(self._week([], ["travel_day"], ["no_meal_source"]))
        self.assertEqual(len(lines), 2)
        self.assertIn("travel day", lines[0])
        self.assertIn("pick a menu", lines[1])

    def test_the_reply_puts_flags_first(self):
        """The flag block precedes the per-day table. `rindex`, because the header
        line legitimately contains the same date as the first day row."""
        text = wa.render(self._week(["no_meal_source"], []))
        self.assertLess(text.index("need something"), text.rindex("Mon 5/3"))

    def test_a_clean_week_says_so_once(self):
        text = wa.render(self._week([], []))
        self.assertIn("No gaps found", text)
        self.assertNotIn("⚠️", text)

    def test_the_reply_carries_no_advice_beyond_the_action_the_flag_names(self):
        text = wa.render(self._week(["travel_day"]))
        for word in ("should", "recommend", "consider", "i suggest"):
            self.assertNotIn(word, text.lower())


class TestCommand(unittest.TestCase):
    def _call(self, text, week=None, raises=False):
        from artemis import main as m
        from contextlib import contextmanager

        class _Conn:
            def cursor(self_):
                return Cur({})

        @contextmanager
        def gc():
            yield _Conn()
        posted = []
        with mock.patch("knowledge.db.get_connection", gc), \
             mock.patch.object(wa, "week_ahead",
                               side_effect=RuntimeError("boom") if raises else None,
                               return_value=week or []), \
             mock.patch("artemis.quiet_hours.local_today", return_value=D), \
             mock.patch.object(m, "_mm") as mm:
            mm.post_message.side_effect = lambda ch, t, root_id=None: posted.append(t)
            handled = m._handle_week_ahead({"id": "p1", "channel_id": "c1"}, text)
        return handled, posted

    def test_it_claims_the_command(self):
        from artemis import main as m
        for t in ("week ahead", "Week Ahead", "week  ahead?"):
            with self.subTest(text=t):
                self.assertTrue(m._WEEK_AHEAD_RE.match(t), t)

    def test_it_claims_nothing_else(self):
        from artemis import main as m
        for t in ("week", "ahead", "what is the week ahead", "week ahead please",
                  "next week"):
            with self.subTest(text=t):
                self.assertFalse(m._WEEK_AHEAD_RE.match(t), t)

    def test_it_replies_with_the_rendered_week(self):
        d = wa.DayAhead(day=D, day_type="travel", location_display="Office",
                        meal_outcome="no_plan")
        d.flags = ["no_meal_source", "travel_day"]
        handled, posted = self._call("week ahead", week=[d])
        self.assertTrue(handled)
        self.assertIn("pick a menu", posted[0])

    def test_a_failure_never_reads_as_a_clear_week(self):
        _handled, posted = self._call("week ahead", raises=True)
        self.assertNotIn("No gaps", posted[0])
        self.assertIn("couldn't build", posted[0])

    def test_it_is_registered_in_the_chain(self):
        import inspect, re
        from artemis import main as m
        src = inspect.getsource(m)
        blk = src[src.index("deterministic_chain = ["):]
        blk = blk[:blk.index("\n    ]")]
        self.assertIn("week_ahead", re.findall(r'\(\s*"([a-z_]+)"\s*,', blk))


class TestSundayPost(unittest.TestCase):
    def _run(self, week):
        from artemis.scheduler import ArtemisScheduler
        from contextlib import contextmanager

        class _Conn:
            def cursor(self_):
                return Cur({})

        @contextmanager
        def gc():
            yield _Conn()
        s = ArtemisScheduler.__new__(ArtemisScheduler)
        posted = []
        with mock.patch("knowledge.db.get_connection", gc), \
             mock.patch.object(wa, "week_ahead", return_value=week), \
             mock.patch.object(ArtemisScheduler, "_post",
                              side_effect=lambda ch, t, tier="business": posted.append(t)):
            s._post_week_ahead_flags()
        return posted

    def test_flags_are_posted_when_present(self):
        d = wa.DayAhead(day=D, location_display="Office")
        d.flags = ["no_meal_source"]
        posted = self._run([d])
        self.assertEqual(len(posted), 1)
        self.assertIn("pick a menu", posted[0])
        self.assertIn("week ahead", posted[0])          # points at the full view

    def test_nothing_is_posted_when_there_are_no_flags(self):
        """A weekly "all clear" trains you to skim past the week that isn't."""
        self.assertEqual(self._run([wa.DayAhead(day=D)]), [])

    def test_only_flags_are_posted_never_the_whole_week(self):
        flagged = wa.DayAhead(day=D, location_display="Office")
        flagged.flags = ["travel_day"]
        clean = wa.DayAhead(day=D + timedelta(1), location_display="Office")
        posted = self._run([flagged, clean])
        self.assertIn("5/3", posted[0])
        self.assertNotIn("5/4", posted[0])

    def test_a_lookahead_failure_does_not_raise_into_the_review(self):
        """Isolated on purpose: a lookahead failure must not cost him the
        pain-pattern post that ran just before it."""
        from artemis.scheduler import ArtemisScheduler
        from contextlib import contextmanager

        class _Conn:
            def cursor(self_):
                return Cur({})

        @contextmanager
        def gc():
            yield _Conn()
        s = ArtemisScheduler.__new__(ArtemisScheduler)
        with mock.patch("knowledge.db.get_connection", gc), \
             mock.patch.object(wa, "week_ahead", side_effect=RuntimeError("boom")):
            s._post_week_ahead_flags()          # must not raise


if __name__ == "__main__":
    unittest.main()


class TestAllUnknownCollapses(unittest.TestCase):
    """A transient failure is ONE fact about the lookahead, not seven about the
    week. Found by test_pain_ladder's Sunday test, whose fake cursor serves no
    health.plan reads: every day came back `unknown` and the post carried seven
    identical lines."""

    def _unknown_week(self, n=7):
        out = []
        for i in range(n):
            d = wa.DayAhead(day=D + timedelta(i))
            d.flags = ["unknown"]
            out.append(d)
        return out

    def test_seven_unresolvable_days_are_one_line(self):
        lines = wa.flag_lines(self._unknown_week())
        self.assertEqual(len(lines), 1)
        self.assertIn("7 day(s) could not be resolved", lines[0])

    def test_a_real_flag_alongside_unknown_is_not_collapsed(self):
        week = self._unknown_week(2)
        week[0].flags = ["no_meal_source"]
        week[0].location_display = "Office"
        lines = wa.flag_lines(week)
        self.assertEqual(len(lines), 2)
        self.assertNotIn("could not be resolved", lines[0])

    def test_a_clean_week_is_still_no_lines(self):
        self.assertEqual(wa.flag_lines([wa.DayAhead(day=D)]), [])
