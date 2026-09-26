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

    def test_every_office_day_still_wakes_at_0430_after_segments(self):
        """The trap DAY_SEGMENTS creates: an msp_work day STARTS at msp_home,
        so a wake keyed off the 00:00 segment would return 07:30. Wake reads
        the day's anchor instead."""
        a = cycle.anchor()
        with no_overrides():
            office_days = [a + timedelta(days=n) for n in range(cycle.CYCLE_LEN)
                           if cycle.day_type(a + timedelta(days=n)) == "msp_work"]
            self.assertEqual(len(office_days), 8)
            for d in office_days:
                with self.subTest(date=d):
                    self.assertEqual(cycle.anchor_location(d), "office")
                    self.assertEqual(cycle.location_at(d, time(0, 0)), "msp_home")
                    self.assertEqual(cycle.wake_on(d), time(4, 30))

    def test_no_day_anchors_on_transit(self):
        a = cycle.anchor()
        with no_overrides():
            for n in range(cycle.CYCLE_LEN):
                d = a + timedelta(days=n)
                with self.subTest(date=d):
                    self.assertFalse(cycle.is_transit(cycle.anchor_location(d)))
        self.assertTrue(cycle.is_transit("transit"))
        self.assertFalse(cycle.is_transit("office"))
        self.assertFalse(cycle.is_transit(None))

    def test_the_office_day_evening_is_at_home(self):
        """Ryan, 2026-09-22: weekday evenings are msp_home. Work ends 16:30
        with a 30 min commute."""
        with no_overrides():
            for t, want in ((time(4, 30), "msp_home"), (time(4, 59), "msp_home"),
                            (time(5, 0), "office"), (time(16, 59), "office"),
                            (time(17, 0), "msp_home"), (time(21, 0), "msp_home")):
                with self.subTest(t=t):
                    self.assertEqual(cycle.location_at(OFFICE_TUE, t), want)
            b = cycle.boundaries(OFFICE_TUE)
        # the day's location stays the anchor; the evening is where he sleeps
        self.assertEqual((b["location"], b["evening_location"]), ("office", "msp_home"))
        self.assertEqual(b["wake"], time(4, 30))

    def test_the_thursday_drive_to_the_farm(self):
        """wk 1 Thursday: office day, leaves 16:30, ~5 h, at Richfield 21:30."""
        thu = date(2026, 9, 24)
        with no_overrides():
            self.assertEqual(cycle.day_type(thu), "msp_work")
            for t, want in ((time(5, 0), "office"), (time(16, 29), "office"),
                            (time(16, 30), "transit"), (time(21, 29), "transit"),
                            (time(21, 30), "richfield"), (time(23, 0), "richfield")):
                with self.subTest(t=t):
                    self.assertEqual(cycle.location_at(thu, t), want)
            self.assertEqual(cycle.wake_on(thu), time(4, 30))
            # quiet starts 17:00 on a work day — he is on the road then
            self.assertEqual(cycle.boundaries(thu)["evening_location"], "transit")

    def test_the_travel_monday_drive_to_msp(self):
        """Leaves Richfield 11:00, ~5 h, home 16:00. Wake is still 06:00."""
        with no_overrides():
            for t, want in ((time(6, 0), "richfield"), (time(10, 59), "richfield"),
                            (time(11, 0), "transit"), (time(15, 59), "transit"),
                            (time(16, 0), "msp_home"), (time(20, 0), "msp_home")):
                with self.subTest(t=t):
                    self.assertEqual(cycle.location_at(TRAVEL_MON, t), want)
            self.assertEqual(cycle.wake_on(TRAVEL_MON), time(6, 0))
            self.assertEqual(cycle.anchor_location(TRAVEL_MON), "richfield")

    def test_segments_come_from_system_state_when_set(self):
        import json
        good = json.dumps({"2": [["msp_home", None], ["office", "05:30"],
                                 ["msp_home", "18:00"]]})
        by_key = lambda k: good if k == cycle.SEGMENTS_KEY else None   # noqa: E731
        with no_overrides(), patch("artemis.quiet_hours.get_system_value", side_effect=by_key):
            self.assertEqual(cycle.location_at(OFFICE_TUE, time(5, 0)), "msp_home")
            self.assertEqual(cycle.location_at(OFFICE_TUE, time(5, 30)), "office")
            self.assertEqual(cycle.location_at(OFFICE_TUE, time(17, 30)), "office")
            self.assertEqual(cycle.wake_on(OFFICE_TUE), time(4, 30))   # anchor, untouched

    def test_a_bad_segment_table_falls_back_rather_than_raising(self):
        import json
        for bad in ("not json",
                    json.dumps({"2": [["nowhere", None]]}),                 # unknown location
                    json.dumps({"2": [["office", "05:00"]]})):              # no midnight start
            with self.subTest(bad=bad[:24]):
                by_key = lambda k, b=bad: b if k == cycle.SEGMENTS_KEY else None  # noqa: E731
                with no_overrides(), patch("artemis.quiet_hours.get_system_value",
                                           side_effect=by_key):
                    self.assertEqual(cycle.segments(), cycle.DAY_SEGMENTS)
                    self.assertEqual(cycle.location_at(OFFICE_TUE, time(12, 0)), "office")

    def test_the_wi_friday_moves_at_1600(self):
        """Ryan, 2026-09-22: the farm day ends at Brown Deer. Wake is unchanged
        (the morning is still Richfield, 06:00)."""
        with no_overrides():
            self.assertEqual(cycle.location_at(RICHFIELD_FRI, time(6, 0)), "richfield")
            self.assertEqual(cycle.location_at(RICHFIELD_FRI, time(15, 59)), "richfield")
            self.assertEqual(cycle.location_at(RICHFIELD_FRI, time(16, 0)), "brown_deer")
            self.assertEqual(cycle.location_at(RICHFIELD_FRI, time(21, 0)), "brown_deer")
            self.assertEqual(cycle.wake_on(RICHFIELD_FRI), time(6, 0))
            b = cycle.boundaries(RICHFIELD_FRI)
        self.assertEqual((b["location"], b["evening_location"]), ("richfield", "brown_deer"))

    def test_the_wi_sunday_moves_at_1700(self):
        with no_overrides():
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(9, 0)), "brown_deer")
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(16, 59)), "brown_deer")
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(17, 0)), "richfield")
            self.assertEqual(cycle.location_at(WI_SUNDAY, time(21, 0)), "richfield")
            # the morning location drives the wake. Quiet follows the DAY TYPE,
            # so the move changes neither wake nor quiet — only where he is.
            b = cycle.boundaries(WI_SUNDAY)
        self.assertEqual((b["location"], b["evening_location"]), ("brown_deer", "richfield"))
        self.assertEqual((b["wake"], b["quiet"]), (time(7, 30), time(22, 30)))

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

    def test_a_db_failure_RAISES_and_never_answers_no_override(self):
        """FAIL-CLOSED-RESOLVERS. This test asserted the opposite until
        2026-09-26, and the behaviour it was protecting is what let eleven plan
        rows be rebuilt against the base pattern: a failed read answered "no
        override", which is a real answer that resolves and writes cleanly."""
        with patch("knowledge.db.execute_one", side_effect=RuntimeError("no db")):
            with self.assertRaises(cycle.OverrideLookupError):
                cycle.override_for(OFFICE_TUE)
            # and it propagates — no resolver quietly substitutes the base pattern
            for call in (lambda: cycle.day_type(OFFICE_TUE),
                         lambda: cycle.anchor_location(OFFICE_TUE),
                         lambda: cycle.location_at(OFFICE_TUE),
                         lambda: cycle.wake_on(OFFICE_TUE),
                         lambda: cycle.boundaries(OFFICE_TUE)):
                with self.assertRaises(cycle.OverrideLookupError):
                    call()

    def test_the_base_pattern_is_still_reachable_but_only_on_purpose(self):
        """The escape hatch is explicit, and works even with the table dead."""
        with patch("knowledge.db.execute_one", side_effect=RuntimeError("no db")):
            self.assertEqual(cycle.day_type(OFFICE_TUE, use_overrides=False), "msp_work")
            self.assertEqual(cycle.anchor_location(OFFICE_TUE, use_overrides=False), "office")


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
                # the seeded row carries the day's ANCHOR, not its 00:00
                # segment (an msp_work day starts at msp_home)
                want = cycle.anchor_location(d, use_overrides=False)
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
