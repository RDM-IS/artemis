"""CYCLE-1 — day types, locations and the day boundaries derived from them.

The point of these tests is that there is ONE resolution: the cron registry
and the quiet_hours helpers must agree for every date.

Run:
    python3 tests/test_cycle.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import cycle  # noqa: E402
from artemis import quiet_hours as qh  # noqa: E402

CT = ZoneInfo("America/Chicago")
ANCHOR = date(2026, 9, 20)

# The three days Ryan named, plus the travel Monday.
RICHFIELD_FRI = date(2026, 9, 25)
OFFICE_TUE = date(2026, 9, 22)
MSP_HOME_SAT = date(2026, 10, 3)
TRAVEL_MON = date(2026, 9, 28)
WI_SUNDAY = date(2026, 9, 27)


def no_overrides():
    """The DB is never reached in tests; override_for returns None."""
    return patch.object(cycle, "override_for", return_value=None)


class TestDerivation(unittest.TestCase):
    def test_anchor_and_positions(self):
        self.assertEqual(cycle.DEFAULT_ANCHOR, ANCHOR)
        self.assertEqual(ANCHOR.strftime("%A"), "Sunday")
        self.assertEqual(cycle.cycle_pos(ANCHOR, ANCHOR), 0)
        self.assertEqual(cycle.cycle_pos(ANCHOR + timedelta(days=14), ANCHOR), 0)
        self.assertEqual(cycle.cycle_pos(ANCHOR - timedelta(days=1), ANCHOR), 13)

    def test_day_type_counts_per_14_days(self):
        with no_overrides():
            got = [cycle.day_type(ANCHOR + timedelta(days=i)) for i in range(14)]
        self.assertEqual(got.count("msp_work"), 8)
        self.assertEqual(got.count("msp_home"), 2)
        self.assertEqual(got.count("wi"), 3)
        self.assertEqual(got.count("travel"), 1)

    def test_position_never_drifts_across_dst(self):
        """DST is 2026-11-01. Position is derived, so the cycle steps evenly."""
        dst = date(2026, 11, 1)
        with no_overrides():
            before = cycle.day_type(dst - timedelta(days=1))
            after = cycle.day_type(dst + timedelta(days=1))
        self.assertEqual(cycle.cycle_pos(dst + timedelta(days=14)), cycle.cycle_pos(dst))
        self.assertEqual((before, after),
                         (cycle.DAY_TYPES[cycle.cycle_pos(dst - timedelta(days=1))],
                          cycle.DAY_TYPES[cycle.cycle_pos(dst + timedelta(days=1))]))

    def test_the_wi_sunday_moves_at_1700(self):
        with no_overrides():
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(9, 0)), "brown_deer")
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(16, 59)), "brown_deer")
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(17, 0)), "richfield")
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(21, 0)), "richfield")
            # the morning location drives the wake; the evening one drives quiet
            b = cycle.boundaries(WI_SUNDAY)
        self.assertEqual((b["location"], b["evening_location"]), ("brown_deer", "richfield"))
        self.assertEqual(b["wake"], time(7, 30))

    def test_wake_follows_location_not_day_type(self):
        with no_overrides():
            # the travel Monday is a Richfield morning — no special case needed
            self.assertEqual(cycle.day_type(TRAVEL_MON), "travel")
            self.assertEqual(cycle.location_at(TRAVEL_MON), "richfield")
            self.assertEqual(cycle.wake_on(TRAVEL_MON), time(6, 0))
            self.assertEqual(cycle.wake_on(RICHFIELD_FRI), time(6, 0))
            self.assertEqual(cycle.wake_on(OFFICE_TUE), time(4, 30))
            self.assertEqual(cycle.wake_on(MSP_HOME_SAT), time(7, 30))

    def test_business_hours_follow_day_type(self):
        with no_overrides():
            self.assertEqual((cycle.open_on(OFFICE_TUE), cycle.quiet_on(OFFICE_TUE)),
                             (time(6, 30), time(17, 0)))
            self.assertEqual((cycle.open_on(RICHFIELD_FRI), cycle.quiet_on(RICHFIELD_FRI)),
                             (time(8, 30), time(22, 30)))
            # travel is a work day whose morning is displaced, not a weekend
            self.assertEqual((cycle.open_on(TRAVEL_MON), cycle.quiet_on(TRAVEL_MON)),
                             (time(6, 30), time(17, 0)))


class TestOverrides(unittest.TestCase):
    def test_day_type_override_wins_and_the_cycle_resumes(self):
        ov = {"override_id": 1, "day_type": "wi", "location": None}
        with patch.object(cycle, "override_for", side_effect=lambda d: ov if d == OFFICE_TUE else None):
            self.assertEqual(cycle.day_type(OFFICE_TUE), "wi")
            self.assertEqual(cycle.open_on(OFFICE_TUE), time(8, 30))
            # the next day is back on the derived cycle — no drift
            self.assertEqual(cycle.day_type(OFFICE_TUE + timedelta(days=1)), "msp_work")

    def test_location_override_is_independent_of_day_type(self):
        """Ryan's case: working, but from Richfield."""
        ov = {"override_id": 2, "day_type": None, "location": "richfield"}
        with patch.object(cycle, "override_for", side_effect=lambda d: ov if d == OFFICE_TUE else None):
            self.assertEqual(cycle.day_type(OFFICE_TUE), "msp_work")   # still working
            self.assertEqual(cycle.location_at(OFFICE_TUE), "richfield")
            self.assertEqual(cycle.wake_on(OFFICE_TUE), time(6, 0))    # Richfield wake
            self.assertEqual(cycle.open_on(OFFICE_TUE), time(6, 30))   # office hours
            self.assertEqual(cycle.quiet_on(OFFICE_TUE), time(17, 0))

    def test_an_unknown_location_is_ignored_not_obeyed(self):
        ov = {"override_id": 3, "day_type": None, "location": "mars"}
        with patch.object(cycle, "override_for", return_value=ov):
            self.assertEqual(cycle.location_at(OFFICE_TUE), "office")

    def test_a_db_failure_means_no_override_never_an_exception(self):
        with patch("knowledge.db.execute_one", side_effect=RuntimeError("no db")):
            self.assertIsNone(cycle.override_for(OFFICE_TUE))
            self.assertEqual(cycle.day_type(OFFICE_TUE), "msp_work")


class TestOverrideTiming(unittest.TestCase):
    """Overrides take effect from the next day; a same-day boundary applies
    only if it hasn't passed, and a skip is always stated."""

    def test_a_future_override_is_next_day_only(self):
        now = datetime(2026, 9, 22, 9, 0, tzinfo=CT)
        with no_overrides():
            eff = cycle.apply_override_effect(date(2026, 9, 25), date(2026, 9, 25), now)
        self.assertEqual(eff["effective_from"], date(2026, 9, 25))
        self.assertEqual((eff["today_applies"], eff["today_skipped"]), ([], []))
        self.assertIn("In effect from Fri 9/25", cycle.describe_override_effect(eff))

    def test_todays_override_applies_to_boundaries_still_ahead(self):
        now = datetime(2026, 9, 22, 3, 0, tzinfo=CT)      # before the 04:30 wake
        with no_overrides():
            eff = cycle.apply_override_effect(now.date(), now.date(), now)
        self.assertEqual(eff["today_applies"], ["wake", "open", "quiet"])
        self.assertEqual(eff["today_skipped"], [])

    def test_a_boundary_already_passed_is_skipped_and_said_out_loud(self):
        now = datetime(2026, 9, 22, 7, 0, tzinfo=CT)      # after wake, before open
        with no_overrides():
            eff = cycle.apply_override_effect(now.date(), now.date(), now)
        self.assertEqual(eff["today_applies"], ["quiet"])
        self.assertEqual([k for k, _ in eff["today_skipped"]], ["wake", "open"])
        line = cycle.describe_override_effect(eff)
        self.assertIn("already passed today", line)
        self.assertIn("unchanged until tomorrow", line)
        self.assertIn("wake (04:30)", line)


class TestOneSourceOfTruth(unittest.TestCase):
    """The registry and the quiet_hours helpers must agree — a split source is
    how a 04:30 wake survives a 06:00 Richfield morning."""

    def test_quiet_hours_helpers_read_the_cycle(self):
        with no_overrides():
            for d in (RICHFIELD_FRI, OFFICE_TUE, MSP_HOME_SAT, TRAVEL_MON, WI_SUNDAY):
                self.assertEqual(qh.wake_time_on(d), cycle.wake_on(d), d)
                self.assertEqual(qh.open_time_on(d), cycle.open_on(d), d)
                self.assertEqual(qh.quiet_start_on(d), cycle.quiet_on(d), d)

    def test_next_wake_agrees_with_the_cycle_on_the_named_days(self):
        """Richfield Friday 06:00, office Tuesday 04:30, MSP-home Saturday 07:30."""
        cases = [(RICHFIELD_FRI, time(6, 0)), (OFFICE_TUE, time(4, 30)),
                 (MSP_HOME_SAT, time(7, 30))]
        with no_overrides():
            for d, expected in cases:
                midnight = datetime.combine(d, time(0, 1), tzinfo=CT)
                with patch.object(qh, "local_now", return_value=midnight):
                    got = qh.next_wake(midnight)
                self.assertEqual(got.timetz().replace(tzinfo=None), expected, d)
                self.assertEqual(got.date(), d, d)
                self.assertEqual(got, cycle.next_boundary("wake", midnight), d)

    def test_no_second_copy_of_the_cycle_tables(self):
        """health_office and the scheduler must READ artemis.cycle, not keep
        their own day-type or location tables."""
        root = Path(__file__).resolve().parent.parent / "artemis"
        for name in ("health_office.py", "scheduler.py", "quiet_hours.py"):
            src = (root / name).read_text()
            for marker in ("CYCLE_DAY_TYPES", "DAY_LOCATIONS = ", "DAY_TYPES = ",
                           "DAY_TYPE_HOURS = ", "CYCLE_LOCATION = "):
                self.assertNotIn(marker, src, f"{name} re-declares {marker}")
        # …and health_office's view of a date matches the cycle's
        from artemis import health_office as office
        for i in range(cycle.CYCLE_LEN):
            d = ANCHOR + timedelta(days=i)
            with no_overrides():
                want = cycle.location_at(d, time(0, 0), use_overrides=False)
                display = (cycle.DEFAULT_LOCATIONS[want] or {})["display"]
                self.assertEqual(office.day_location(d), display, d)
                self.assertEqual(office.day_type(d, use_overrides=False), cycle.day_type(d), d)

    def test_no_weekend_branching_left_in_the_schedule_path(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "quiet_hours.py").read_text()
        body = src[src.index("def wake_time_on"):src.index("def next_wake")]
        self.assertNotIn("is_weekend", body)
        self.assertNotIn("WEEKEND", body)


if __name__ == "__main__":
    unittest.main()
