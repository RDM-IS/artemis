"""AWAY — work trips, vacations and hunting as cycle overrides."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import json
import unittest
from datetime import date

from artemis import away

START, END = date(2026, 11, 21), date(2026, 11, 29)


class Cur:
    def __init__(self, rows=(), raises=False):
        self.rows, self.raises, self._res, self.written = list(rows), raises, [], []
        self.rowcount = 0

    def execute(self, sql, params=None):
        if self.raises:
            raise RuntimeError("down")
        s = " ".join(sql.split())
        self._res = []
        if s.startswith("SELECT"):
            self._res = list(self.rows)
        else:
            self.written.append((s, params))
            self.rowcount = 1
            if "RETURNING" in s:
                self._res = [{"override_id": 7}]

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


def stay(purpose=away.WORK, lodging=None, **kw):
    return away.Stay(START, END, purpose,
                     lodging or away.DEFAULT_LODGING[purpose], **kw)


class TestOneKindNotFour(unittest.TestCase):
    def test_there_is_a_single_day_type(self):
        """work+hotel_gym and vacation+no_gym differ in what they PLAN, not in
        what kind of day they are. Four day types would have meant four
        ENUM-EXPANDs and four chances to miss a consumer."""
        self.assertEqual(away.DAY_TYPE, "away")

    def test_the_attributes_are_constrained_sets(self):
        self.assertEqual(set(away.PURPOSES), {"work", "vacation", "hunting"})
        self.assertEqual(set(away.LODGINGS), {"hotel_gym", "no_gym"})

    def test_the_defaults_match_the_spec(self):
        """A work trip is usually a hotel with a gym; a vacation usually is not."""
        self.assertEqual(away.DEFAULT_LODGING[away.WORK], away.HOTEL_GYM)
        self.assertEqual(away.DEFAULT_LODGING[away.VACATION], away.NO_GYM)


class TestPolicyPerPurposeAndLodging(unittest.TestCase):
    def test_work_with_a_hotel_gym_continues_the_program(self):
        p = away.policy_for(stay(away.WORK, away.HOTEL_GYM))
        self.assertEqual(p.strength, "hotel_gym")
        self.assertTrue(p.progression)
        self.assertTrue(p.intervals)

    def test_work_without_a_gym_falls_back_to_the_circuit_and_pauses_progression(self):
        p = away.policy_for(stay(away.WORK, away.NO_GYM))
        self.assertEqual(p.strength, "bodyweight_circuit")
        self.assertEqual(p.cardio, "walk_z2")
        self.assertFalse(p.progression)
        self.assertFalse(p.intervals)

    def test_vacation_is_maintenance_whatever_the_lodging(self):
        for lodging in (away.HOTEL_GYM, away.NO_GYM):
            p = away.policy_for(stay(away.VACATION, lodging))
            with self.subTest(lodging):
                self.assertFalse(p.progression)
                self.assertFalse(p.intervals)
                self.assertEqual(p.cardio, "walk_z2")

    def test_vacation_with_a_hotel_gym_still_uses_it(self):
        self.assertEqual(away.policy_for(stay(away.VACATION, away.HOTEL_GYM)).strength,
                         "hotel_gym")

    def test_hunting_schedules_nothing_at_all(self):
        """Planned rest. Nothing is scheduled, so nothing can be missed, and a
        walk counts only if he logs one — Artemis does not infer that he walked
        because he was in a deer stand."""
        p = away.policy_for(stay(away.HUNTING))
        self.assertIsNone(p.strength)
        self.assertIsNone(p.cardio)
        self.assertFalse(p.progression)
        self.assertIn("log walk", p.note)

    def test_NO_away_day_ever_counts_as_missed(self):
        """A trip is not a lapse. Letting one feed MAKEUP-2 or REPEAT-WEEK would
        hold the program back a week for going to a conference."""
        for purpose in away.PURPOSES:
            for lodging in away.LODGINGS:
                with self.subTest(f"{purpose}+{lodging}"):
                    self.assertFalse(away.policy_for(stay(purpose, lodging)).counts_as_missed)

    def test_every_away_day_is_a_travel_day_for_meals(self):
        for purpose in away.PURPOSES:
            with self.subTest(purpose):
                self.assertEqual(away.policy_for(stay(purpose)).meal_kind, "travel")

    def test_mobility_is_suggested_daily(self):
        for purpose in away.PURPOSES:
            with self.subTest(purpose):
                self.assertTrue(away.policy_for(stay(purpose)).mobility_suggested)


class TestTheMealsWiring(unittest.TestCase):
    def test_nutrition_treats_away_as_travel(self):
        """Falling through to KIND_OFF would have pre-filled a home menu for a
        week he is in a hotel."""
        from artemis import nutrition
        self.assertEqual(nutrition.day_kind("away"), nutrition.KIND_TRAVEL)
        self.assertEqual(nutrition.day_kind("travel"), nutrition.KIND_TRAVEL)
        self.assertEqual(nutrition.day_kind("msp_work"), nutrition.KIND_WORK)


class TestTheHotelGym(unittest.TestCase):
    def test_it_assumes_only_what_almost_every_hotel_gym_has(self):
        """Assuming a cable stack and being wrong means he walks to a machine
        that is not there."""
        self.assertEqual(away.HOTEL_GYM_INVENTORY["dumbbell"]["max"], 50)
        self.assertEqual(away.HOTEL_GYM_INVENTORY["dumbbell"]["step"], 5)
        self.assertIn("bench", away.HOTEL_GYM_INVENTORY)
        for absent in ("cable", "smith", "machine", "barbell"):
            self.assertNotIn(absent, away.HOTEL_GYM_INVENTORY)

    def test_the_bench_is_flat_only(self):
        self.assertEqual(away.HOTEL_GYM_INVENTORY["bench"]["mode"], "flat_only")


class TestStaysAndAttrs(unittest.TestCase):
    def test_covers_is_inclusive_at_both_ends(self):
        s = stay()
        self.assertTrue(s.covers(START))
        self.assertTrue(s.covers(END))
        self.assertFalse(s.covers(date(2026, 11, 20)))
        self.assertFalse(s.covers(date(2026, 11, 30)))

    def test_the_packed_kit_reads_off_the_attrs(self):
        self.assertEqual(stay(attrs={"kit": ["trx", "bands"]}).kit, {"trx", "bands"})
        self.assertEqual(stay().kit, set())

    def test_insert_writes_one_away_row(self):
        cur = Cur()
        self.assertEqual(away.insert(cur, stay()), 7)
        sql, params = cur.written[0]
        self.assertIn("INSERT INTO acos.cycle_day_overrides", sql)
        self.assertIn("away", params)

    def test_cancel_revokes_rather_than_deletes(self):
        """A trip that was planned and called off is part of why a week looks
        the way it does."""
        cur = Cur()
        away.cancel(cur, START)
        sql, _p = cur.written[0]
        self.assertIn("SET revoked_at", sql)
        self.assertNotIn("DELETE", sql)

    def test_set_attr_merges_rather_than_replaces(self):
        """`packed bands` after `packed trx` must not lose the TRX."""
        cur = Cur()
        away.set_attr(cur, START, "kit", ["trx", "bands"])
        sql, _p = cur.written[0]
        self.assertIn("attrs ||", sql)

    def test_load_stays_RAISES_rather_than_reporting_none(self):
        """FAIL-CLOSED-RESOLVERS: a caller that cannot read the overrides must
        not plan the days as if he were home — the exact mistake this feature
        exists to prevent."""
        with self.assertRaises(RuntimeError):
            away.load_stays(Cur(raises=True), START, END)

    def test_load_stays_parses_json_attrs(self):
        rows = [{"start_date": START, "end_date": END, "purpose": "work",
                 "lodging": "no_gym", "attrs": json.dumps({"kit": ["trx"]}),
                 "reason": None}]
        got = away.load_stays(Cur(rows), START, END)
        self.assertEqual(got[0].kit, {"trx"})

    def test_a_missing_lodging_falls_back_to_the_purpose_default(self):
        rows = [{"start_date": START, "end_date": END, "purpose": "vacation",
                 "lodging": None, "attrs": {}, "reason": None}]
        self.assertEqual(away.load_stays(Cur(rows), START, END)[0].lodging, away.NO_GYM)

    def test_stay_on_picks_the_covering_stay(self):
        stays = [stay(away.HUNTING)]
        self.assertIsNotNone(away.stay_on(stays, date(2026, 11, 25)))
        self.assertIsNone(away.stay_on(stays, date(2026, 12, 25)))


class TestTheCommands(unittest.TestCase):
    def test_all_three_purposes_share_one_regex(self):
        from artemis import main as m
        for line, purpose in (("trip 2026-11-21 to 2026-11-29", "trip"),
                              ("vacation 2027-06-01 to 2027-06-10 no gym", "vacation"),
                              ("hunting 2026-11-21 to 2026-11-22", "hunting")):
            with self.subTest(line):
                mm = m._AWAY_RE.match(line)
                self.assertIsNotNone(mm)
                self.assertEqual(mm.group("purpose").lower(), purpose)

    def test_the_lodging_is_optional_and_parsed(self):
        from artemis import main as m
        self.assertEqual(
            m._AWAY_RE.match("trip 2026-11-21 to 2026-11-29 hotel gym").group("lodging"),
            "hotel gym")
        self.assertIsNone(
            m._AWAY_RE.match("trip 2026-11-21 to 2026-11-29").group("lodging"))

    def test_it_does_not_match_a_bare_word_or_a_partial(self):
        from artemis import main as m
        for no in ("trip", "yes trip", "trip 2026-11-21", "vacation soon"):
            with self.subTest(no):
                self.assertIsNone(m._AWAY_RE.match(no))

    def test_the_confirm_words_are_in_the_arbitration_inventory(self):
        """A bare `yes` with an away pending open MUST count as ambiguous: the
        alternative is recording a two-week trip meant as a calendar confirm."""
        from artemis import main as m
        for word in ("trip", "vacation", "hunting"):
            with self.subTest(word):
                self.assertEqual(m._QUALIFIED_SUFFIX[word], word)
                self.assertIn(word, m.consuming_flows())

    def test_cancel_and_the_per_stay_edits_match(self):
        from artemis import main as m
        self.assertTrue(m._AWAY_CANCEL_RE.match("trip cancel 2026-11-21"))
        self.assertTrue(m._HOTEL_HAS_RE.match("hotel gym has cable stack, leg press"))
        self.assertTrue(m._PACKED_RE.match("packed trx"))
        self.assertTrue(m._PACKED_RE.match("Packed bands."))
        self.assertIsNone(m._PACKED_RE.match("packed shoes"))


if __name__ == "__main__":
    unittest.main()


class TestBlock2HonoursPlacementsAndAway(unittest.TestCase):
    """Ryan's 11/19 -> 12/4 placement. An override can carry a SESSION, not just
    a location, and the builder must honour it rather than re-deriving the day
    from the 14-day template."""

    PLACEMENTS = {
        date(2026, 11, 20): {"session": "strength_a", "location_key": "richfield"},
        date(2026, 11, 26): {"session": "bodyweight_circuit", "location_key": "brown_deer"},
        date(2026, 12, 4): {"session": "cardio_z2", "location_key": "richfield"},
    }
    STAYS = [away.Stay(date(2026, 11, 21), date(2026, 11, 22), away.HUNTING, away.NO_GYM),
             away.Stay(date(2026, 11, 27), date(2026, 11, 29), away.HUNTING, away.NO_GYM)]

    @classmethod
    def setUpClass(cls):
        from artemis import block2
        cls.block2 = block2
        cls.rows = block2.build_rows(placements=cls.PLACEMENTS, away_stays=cls.STAYS)
        cls.by_day = {}
        for r in cls.rows:
            cls.by_day.setdefault(r["plan_date"], []).append(r)

    def _morning(self, d):
        return [r for r in self.by_day.get(d, []) if r.get("slot", "morning") == "morning"][0]

    def test_a_placed_session_overrules_the_template(self):
        self.assertEqual(self._morning(date(2026, 11, 20))["session_type"], "strength_a")
        self.assertEqual(self._morning(date(2026, 12, 4))["session_type"], "cardio_z2")

    def test_a_placed_location_reaches_the_row(self):
        row = self._morning(date(2026, 11, 20))
        self.assertEqual(row["blocks"]["location_key"], "richfield")

    def test_a_placed_circuit_builds_the_circuit_not_a_rest_day(self):
        """It rendered as "Rest / Mobility" until the builder learned the type —
        a row that said one thing and did another."""
        row = self._morning(date(2026, 11, 26))
        self.assertEqual(row["session_type"], "bodyweight_circuit")
        self.assertEqual(row["blocks"]["display_name"], "Bodyweight circuit")
        self.assertEqual(row["blocks"]["type"], "recovery_flow")

    def test_hunting_days_schedule_rest_and_no_evening_flow(self):
        for d in (date(2026, 11, 21), date(2026, 11, 22),
                  date(2026, 11, 27), date(2026, 11, 28), date(2026, 11, 29)):
            with self.subTest(str(d)):
                self.assertEqual(self._morning(d)["session_type"], "rest")
                slots = [r.get("slot", "morning") for r in self.by_day[d]]
                self.assertNotIn("evening", slots)

    def test_hunting_beats_a_placement_on_the_same_day(self):
        """A Strength A placed on a day later marked hunting must not survive
        the marking — the whole point of recording the trip is that it changes
        the plan."""
        rows = self.block2.build_rows(
            placements={date(2026, 11, 21): {"session": "strength_a",
                                             "location_key": "richfield"}},
            away_stays=self.STAYS)
        row = [r for r in rows if r["plan_date"] == date(2026, 11, 21)
               and r.get("slot", "morning") == "morning"][0]
        self.assertEqual(row["session_type"], "rest")

    def test_no_away_day_in_11_19_to_11_30_is_missable(self):
        """MAKEUP-2 / REPEAT-WEEK must never see an away day as missed: a trip
        is not a lapse."""
        for stay_ in self.STAYS:
            d = stay_.start
            while d <= stay_.end:
                with self.subTest(str(d)):
                    self.assertFalse(away.policy_for(stay_).counts_as_missed)
                    self.assertEqual(self._morning(d)["session_type"], "rest")
                d += __import__("datetime").timedelta(days=1)

    def test_the_deload_is_still_week_six_and_the_block_still_ends_12_12(self):
        self.assertEqual(self.block2.DELOAD_WEEK, 6)
        self.assertEqual(self.block2.end_date(), date(2026, 12, 12))
        self.assertEqual(self.block2.week_num_for(date(2026, 12, 6)), 6)
        self.assertEqual(self.block2.week_num_for(date(2026, 12, 12)), 6)

    def test_days_outside_the_placement_keep_the_template(self):
        untouched = self._morning(date(2026, 12, 8))
        from artemis import health_office as office
        self.assertEqual(untouched["session_type"], office.session_for(date(2026, 12, 8)))
