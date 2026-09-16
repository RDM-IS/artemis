"""WAKE-1 — `set timezone to <place>` parsing, expiry, and the reply.

The DB is faked (one in-memory override row), so nothing here touches RDS.
artemis.main pulls in flask / google / apscheduler — stubbed before import, as
in test_confirm_dispatch.

Run:
    python3.11 tests/test_timezone_command.py
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

_STUBS = [
    "flask", "requests", "websocket", "schedule", "Levenshtein",
    "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
    "apscheduler.triggers", "apscheduler.triggers.cron", "apscheduler.triggers.interval",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
    "google", "google.auth", "google.auth.transport", "google.auth.transport.requests",
    "google.oauth2", "google.oauth2.credentials",
    "google_auth_oauthlib", "google_auth_oauthlib.flow",
]
for _name in _STUBS:
    sys.modules.setdefault(_name, MagicMock())

from artemis import config, main, quiet_hours  # noqa: E402

TODAY = date(2026, 9, 15)


class TestPlaceResolution(unittest.TestCase):
    def test_cities_countries_zones_and_iana(self):
        cases = [
            ("brazil", "America/Sao_Paulo", True),
            ("France", "Europe/Paris", False),
            ("paris", "Europe/Paris", False),
            ("paris france", "Europe/Paris", False),
            ("Tokyo", "Asia/Tokyo", False),
            ("eastern", "America/New_York", False),
            ("pacific time", "America/Los_Angeles", False),
            ("Europe/Lisbon", "Europe/Lisbon", False),
            ("são paulo", "America/Sao_Paulo", False),
            ("the city of London", "Europe/London", False),
        ]
        for text, tz, multi in cases:
            with self.subTest(place=text):
                got_tz, _label, got_multi = quiet_hours.resolve_place_timezone(text)
                self.assertEqual(got_tz, tz)
                self.assertEqual(got_multi, multi)

    def test_home_words_resolve_to_home(self):
        for text in ("central", "chicago", "central chicago", "central time"):
            with self.subTest(place=text):
                tz, _, _ = quiet_hours.resolve_place_timezone(text)
                self.assertEqual(tz, config.HOME_TIMEZONE)

    def test_unknown_place_resolves_to_nothing(self):
        tz, _, _ = quiet_hours.resolve_place_timezone("narnia")
        self.assertIsNone(tz)


class TestDurationSplit(unittest.TestCase):
    def test_forms(self):
        cases = [
            ("Brazil through 9/23", ("Brazil", "through", "9/23")),
            ("Brazil thru Sep 23", ("Brazil", "through", "Sep 23")),
            ("Paris until 2026-10-02", ("Paris", "through", "2026-10-02")),
            ("Paris for 5 days", ("Paris", "days", 5)),
            ("Tokyo for 1 day", ("Tokyo", "days", 1)),
            ("Tokyo this week", ("Tokyo", "days", 7)),
            ("Tokyo", ("Tokyo", "none", None)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(main._split_tz_duration(text), expected)


class TestDateToken(unittest.TestCase):
    def test_formats(self):
        for token, expected in [
            ("9/23", date(2026, 9, 23)),
            ("09/23", date(2026, 9, 23)),
            ("2026-09-23", date(2026, 9, 23)),
            ("Sep 23", date(2026, 9, 23)),
            ("September 23", date(2026, 9, 23)),
            ("23 Sep", date(2026, 9, 23)),
        ]:
            with self.subTest(token=token):
                self.assertEqual(quiet_hours.parse_date_token(token, TODAY), expected)

    def test_year_less_date_rolls_forward(self):
        # 1/5 is behind us in September → next January.
        self.assertEqual(quiet_hours.parse_date_token("1/5", TODAY), date(2027, 1, 5))

    def test_garbage_is_none(self):
        for token in ("soonish", "", "13/45"):
            with self.subTest(token=token):
                self.assertIsNone(quiet_hours.parse_date_token(token, TODAY))


class TestExpiryInstant(unittest.TestCase):
    def test_through_is_midnight_of_the_next_day_in_the_away_zone(self):
        exp = quiet_hours.expires_at_for_through(date(2026, 9, 23), "America/Sao_Paulo")
        self.assertEqual(
            exp, datetime(2026, 9, 24, 0, 0, tzinfo=ZoneInfo("America/Sao_Paulo")))
        # The whole of 9/23 local-away is covered…
        end_of_day = datetime(2026, 9, 23, 23, 59, tzinfo=ZoneInfo("America/Sao_Paulo"))
        self.assertGreater(exp, end_of_day)
        # …and home takes over one second later.
        self.assertLessEqual(exp, end_of_day + timedelta(minutes=1, seconds=1))

    def test_for_n_days_counts_from_the_away_zone_today(self):
        # 3 days starting 9/15 covers 9/15-9/17; home resumes at 9/18 00:00 local-away.
        exp = quiet_hours.expires_at_for_days(3, "Europe/Paris", today=TODAY)
        self.assertEqual(
            exp, datetime(2026, 9, 18, 0, 0, tzinfo=ZoneInfo("Europe/Paris")))


class FakeOverrideDB:
    """Stands in for acos.timezone_overrides (id=1 singleton)."""

    def __init__(self):
        self.row: dict | None = None

    def set(self, tz_name, label="", expires_at=None):
        self.row = {"timezone": tz_name, "city_name": label, "expires_at": expires_at}
        return ""

    def clear(self):
        had = self.row is not None
        self.row = None
        return "Timezone reset to home (Central)." if had else "No timezone override was active."

    def get(self):
        return self.row

    def active(self):
        return self.row["timezone"] if self.row else config.HOME_TIMEZONE


class TestCommandHandling(unittest.TestCase):
    def setUp(self):
        self.db = FakeOverrideDB()
        self.mm = MagicMock()
        self.sched = MagicMock()
        self._p = [
            patch.object(main, "_mm", self.mm),
            patch.object(main, "_sched", self.sched),
            patch.object(main, "set_timezone_override", self.db.set),
            patch.object(main, "clear_timezone_override", self.db.clear),
            patch.object(main, "get_timezone_override", self.db.get),
            patch.object(main, "local_today", lambda: TODAY),
            patch.object(main, "phase_summary", lambda: "Phase: open"),
            patch.object(quiet_hours, "get_active_timezone", self.db.active),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def run_cmd(self, text) -> str | None:
        post = {"id": "p1", "channel_id": "c1", "message": text, "root_id": None}
        handled = main._handle_timezone_command(post, text)
        if not handled:
            return None
        return self.mm.post_to_channel_id.call_args[0][1]

    def test_all_accepted_forms_set_the_override(self):
        forms = [
            "set timezone to Brazil through 9/23",
            "set current timezone to Brazil",
            "set my time zone to Brazil for 5 days",
            "timezone Brazil",
            "I'm in Brazil",
            "I'm in Brazil for 3 days",
        ]
        for text in forms:
            with self.subTest(text=text):
                self.db.row = None
                reply = self.run_cmd(text)
                self.assertIsNotNone(reply, f"{text!r} was not handled")
                self.assertEqual(self.db.row["timezone"], "America/Sao_Paulo")

    def test_unrelated_message_is_not_handled(self):
        self.assertIsNone(self.run_cmd("what time is my first meeting"))

    def test_through_date_sets_the_expected_instant(self):
        self.run_cmd("set current timezone to Brazil through 9/23")
        self.assertEqual(
            self.db.row["expires_at"],
            datetime(2026, 9, 24, 0, 0, tzinfo=ZoneInfo("America/Sao_Paulo")))

    def test_reply_names_the_last_away_day_and_the_first_home_day(self):
        reply = self.run_cmd("set timezone to Brazil through 9/23")
        self.assertIn("America/Sao_Paulo", reply)
        self.assertIn("Sep 23", reply)   # last away day
        self.assertIn("Sep 24", reply)   # first Central day
        self.assertIn("Next wake", reply)
        self.assertIn("spans several zones", reply)  # Brazil is multi-zone

    def test_no_end_date_defaults_to_seven_days_and_says_so(self):
        reply = self.run_cmd("set timezone to Paris")
        self.assertIn("defaulting to 7 days", reply)
        self.assertEqual(self.db.row["timezone"], "Europe/Paris")

    def test_france_resolves_without_a_multi_zone_warning(self):
        reply = self.run_cmd("set timezone to France")
        self.assertEqual(self.db.row["timezone"], "Europe/Paris")
        self.assertNotIn("spans several zones", reply)

    def test_central_chicago_clears_the_override(self):
        self.db.set("Europe/Paris", "paris", datetime.now(ZoneInfo("Europe/Paris")))
        reply = self.run_cmd("set timezone to central chicago")
        self.assertIsNone(self.db.row)
        self.assertIn("reset", reply.lower())

    def test_reset_phrases_clear_the_override(self):
        for text in ("reset timezone", "I'm home", "i'm back home"):
            with self.subTest(text=text):
                self.db.set("Europe/Paris", "paris", None)
                self.run_cmd(text)
                self.assertIsNone(self.db.row)

    def test_past_date_is_rejected_and_changes_nothing(self):
        reply = self.run_cmd("set timezone to Brazil through 9/14/2026")
        self.assertIsNone(self.db.row)
        self.assertIn("past", reply.lower())

    def test_a_year_less_date_just_behind_today_is_refused(self):
        """Regression (found on the box): "through 9/14" typed on 9/15 rolled
        forward to 2027-09-14 and silently pinned the schedule away for a year.
        The roll-forward rule and "a past date is rejected" collide here."""
        reply = self.run_cmd("set timezone to Brazil through 9/14")
        self.assertIsNone(self.db.row, "a year-long override was set silently")
        self.assertIn("2027-09-14", reply)
        self.assertIn("with the year", reply)

    def test_a_year_less_date_inside_the_horizon_still_rolls_forward(self):
        # 1/5 from September is ~112 days out — a real trip over new year.
        self.run_cmd("set timezone to Paris through 1/5")
        self.assertEqual(
            self.db.row["expires_at"],
            datetime(2027, 1, 6, 0, 0, tzinfo=ZoneInfo("Europe/Paris")))

    def test_an_explicit_far_future_date_is_refused_too(self):
        reply = self.run_cmd("set timezone to Brazil through 2028-01-01")
        self.assertIsNone(self.db.row)
        self.assertIn("longer than I'll set a timezone for", reply)

    def test_unreadable_date_changes_nothing(self):
        reply = self.run_cmd("set timezone to Brazil through soonish")
        self.assertIsNone(self.db.row)
        self.assertIn("couldn't read", reply.lower())

    def test_unknown_place_changes_nothing(self):
        reply = self.run_cmd("set timezone to narnia")
        self.assertIsNone(self.db.row)
        self.assertIn("don't recognize", reply.lower())

    def test_replacing_an_override_is_called_out(self):
        self.db.set("Europe/Paris", "paris", None)
        reply = self.run_cmd("set timezone to Tokyo")
        self.assertEqual(self.db.row["timezone"], "Asia/Tokyo")
        self.assertIn("Replaced the active override", reply)

    def test_setting_changes_are_applied_to_the_scheduler_immediately(self):
        self.run_cmd("set timezone to Tokyo")
        self.sched.job_tz_sync.assert_called()

    def test_clearing_also_resyncs_the_scheduler(self):
        self.db.set("Asia/Tokyo", "tokyo", None)
        self.sched.reset_mock()
        self.run_cmd("reset timezone")
        self.sched.job_tz_sync.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
