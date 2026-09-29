"""PREP-1 — a Minneapolis stay derived from the cycle.

`find_stays` is pure: it takes {date: day_type} and returns runs. That is why it
is testable without a database, and why the away-day and override behaviour can be
checked here rather than only against RDS.

PUBLIC-FIXTURES: the dates are in 2031 and the day types are the cycle's own
vocabulary. Nothing here is a real date of Ryan's.
"""

import unittest
from datetime import date, timedelta

from artemis import prep


def types(spec: str, start=date(2031, 3, 1)) -> dict:
    """Build a {date: day_type} map from a compact string.

    w = msp_work · h = msp_home · i = wi (Wisconsin) · t = travel · a = away
    """
    letters = {"w": "msp_work", "h": "msp_home", "i": "wi", "t": "travel",
               "a": "away"}
    return {start + timedelta(days=n): letters[c] for n, c in enumerate(spec)}


class TestFindStays(unittest.TestCase):
    def test_a_run_of_msp_days_is_one_stay(self):
        stays = prep.find_stays(types("hwwwwii"))
        self.assertEqual(stays, [(date(2031, 3, 1), date(2031, 3, 5))])

    def test_the_arrival_travel_day_opens_the_stay(self):
        # The Monday drive lands at 16:00: he cooks that evening, so the travel
        # day is part of the stay and the list has to feed it.
        stays = prep.find_stays(types("itwwww"))
        self.assertEqual(stays, [(date(2031, 3, 2), date(2031, 3, 6))])

    def test_a_departure_travel_day_belongs_to_no_stay(self):
        # A travel day NOT followed by an MSP day is him leaving. Counting it
        # would buy a day of food for a day he spends in the car.
        stays = prep.find_stays(types("wwtii"))
        self.assertEqual(stays, [(date(2031, 3, 1), date(2031, 3, 2))])

    def test_two_stays_in_a_fortnight_are_found_separately(self):
        stays = prep.find_stays(types("hwwwwiiitwwwwh"))
        self.assertEqual(stays, [(date(2031, 3, 1), date(2031, 3, 5)),
                                 (date(2031, 3, 9), date(2031, 3, 14))])

    def test_an_away_stay_breaks_the_run_it_falls_inside(self):
        # THE case that makes this worth a function: `cycle.day_type()` cannot
        # return 'away' (it falls back to the 14-day pattern), so an away week
        # reads as msp_work unless the overrides are consulted separately. If it
        # did not break the run, a fortnight's shopping would be bought for a
        # week spent in a hotel.
        stays = prep.find_stays(types("wwaaaww"))
        self.assertEqual(stays, [(date(2031, 3, 1), date(2031, 3, 2)),
                                 (date(2031, 3, 6), date(2031, 3, 7))])

    def test_a_lone_travel_day_at_the_window_edge_is_not_a_stay(self):
        self.assertEqual(prep.find_stays(types("iit")), [])

    def test_a_stay_of_one_day_is_still_a_stay(self):
        self.assertEqual(prep.find_stays(types("ihi")),
                         [(date(2031, 3, 2), date(2031, 3, 2))])

    def test_wisconsin_days_are_never_part_of_a_stay(self):
        stays = prep.find_stays(types("iiii"))
        self.assertEqual(stays, [])

    def test_the_real_pattern_shape_yields_the_expected_two_stays(self):
        # The 14-day pattern from cycle.DAY_TYPES, spelled out: this is the shape
        # the list is actually built against, so it is asserted rather than
        # assumed from the smaller cases above.
        from artemis import cycle
        letters = {"msp_work": "w", "msp_home": "h", "wi": "i", "travel": "t"}
        spec = "".join(letters[t] for t in cycle.DAY_TYPES)
        self.assertEqual(spec, "hwwwwiiitwwwwh")
        stays = prep.find_stays(types(spec))
        self.assertEqual(len(stays), 2)
        # The second stay opens on the travel day and runs to the msp_home Sunday.
        self.assertEqual(stays[1][0], date(2031, 3, 9))


if __name__ == "__main__":
    unittest.main()
