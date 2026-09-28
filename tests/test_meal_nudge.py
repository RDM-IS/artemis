"""MEAL-NUDGE — one evening line when nothing is logged, and only then.

Capture is the bottleneck: 3 of 14 days had any intake, and an off day is empty
unless Ryan logs it. So one nudge, once per local day, an hour before quiet.

The three rules under test:
  * HUMAN-GATED: ships OFF, only a chat command turns it on.
  * FAIL-CLOSED means SILENCE — an unreadable nutrition row posts nothing. A
    false nudge is noise to correct; a missed one costs nothing.
  * The time comes from the CYCLE, not a clock — so it follows a `set timezone`
    override and the day type.

Synthetic only — no RDS, no Mattermost.

Run:
    python3.11 -m unittest tests.test_meal_nudge
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _n in ("googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
           "google", "google.auth", "google.auth.transport",
           "google.auth.transport.requests", "google.oauth2",
           "google.oauth2.credentials", "google_auth_oauthlib",
           "google_auth_oauthlib.flow", "websocket", "Levenshtein"):
    sys.modules.setdefault(_n, MagicMock())

from artemis import config, meal_nudge  # noqa: E402
from artemis.scheduler import ArtemisScheduler  # noqa: E402

DAY = date(2026, 9, 28)


class FakeCur:
    def __init__(self, n=0, raises=False):
        self.n, self.raises, self.sql = n, raises, None

    def execute(self, sql, params=()):
        if self.raises:
            raise RuntimeError("nutrition.entry unreadable")
        self.sql = " ".join(sql.split())

    def fetchone(self):
        return (self.n,)


class TestTheFlagIsHumanGated(unittest.TestCase):
    def test_it_ships_off(self):
        with patch("artemis.quiet_hours.get_system_value", return_value=None):
            self.assertFalse(meal_nudge.enabled())

    def test_only_the_word_on_enables_it(self):
        for raw, want in (("on", True), ("ON", True), (" on ", True),
                          ("off", False), ("", False), ("true", False),
                          ("yes", False), ("1", False), (None, False)):
            with self.subTest(value=raw):
                with patch("artemis.quiet_hours.get_system_value", return_value=raw):
                    self.assertIs(meal_nudge.enabled(), want)

    def test_an_unreadable_flag_is_off(self):
        """An automation that can't confirm it was switched on does not run."""
        with patch("artemis.quiet_hours.get_system_value",
                   side_effect=RuntimeError("system_state down")):
            self.assertFalse(meal_nudge.enabled())


class TestEntryCount(unittest.TestCase):
    def test_it_counts_the_day(self):
        cur = FakeCur(n=3)
        self.assertEqual(meal_nudge.entry_count(cur, DAY), 3)
        self.assertIn("FROM nutrition.entry WHERE day_date = %s", cur.sql)

    def test_an_unreadable_row_is_none_not_zero(self):
        self.assertIsNone(meal_nudge.entry_count(FakeCur(raises=True), DAY))

    def test_zero_is_zero_and_distinguishable_from_none(self):
        self.assertEqual(meal_nudge.entry_count(FakeCur(n=0), DAY), 0)
        self.assertIsNotNone(meal_nudge.entry_count(FakeCur(n=0), DAY))


def _run_job(*, flag_on=True, count=0, raises=False, count_none=False):
    """Drive job_meal_nudge with everything faked. Returns what it posted."""
    s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
    posts = []
    cur = FakeCur(n=count, raises=raises)
    conn = MagicMock()
    conn.__enter__ = lambda self: conn
    conn.__exit__ = lambda self, *a: False
    conn.cursor = lambda: cur
    cnt = (lambda c, d: None) if count_none else meal_nudge.entry_count
    with patch.object(s, "_post", side_effect=lambda ch, t, tier="business": posts.append((ch, t))), \
         patch.object(meal_nudge, "enabled", return_value=flag_on), \
         patch.object(meal_nudge, "entry_count", side_effect=cnt), \
         patch("artemis.quiet_hours.local_today", return_value=DAY), \
         patch("knowledge.db.get_connection", return_value=conn):
        s.job_meal_nudge()
    return posts


class TestTheJob(unittest.TestCase):
    def test_zero_entries_posts_one_line(self):
        posts = _run_job(count=0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], meal_nudge.TEXT)
        self.assertIn("for dinner I had", posts[0][1])

    def test_one_or_more_entries_is_silent(self):
        for n in (1, 3, 7):
            with self.subTest(entries=n):
                self.assertEqual(_run_job(count=n), [])

    def test_the_flag_off_is_silent(self):
        self.assertEqual(_run_job(flag_on=False, count=0), [])

    def test_an_unreadable_row_is_silent(self):
        """FAIL-CLOSED: silence, never a guess that the day is empty."""
        self.assertEqual(_run_job(count_none=True), [])

    def test_a_raising_store_is_silent_and_does_not_escape(self):
        self.assertEqual(_run_job(raises=True), [])

    def test_it_says_nothing_about_what_to_eat(self):
        text = meal_nudge.TEXT.lower()
        for word in ("should", "recommend", "try ", "calorie", "protein", "instead"):
            with self.subTest(word=word):
                self.assertNotIn(word, text)

    def test_it_posts_to_one_channel_only(self):
        posts = _run_job(count=0)
        self.assertEqual({ch for ch, _ in posts}, {config.CHANNEL_OPS})


class TestTheScheduleComesFromTheCycle(unittest.TestCase):
    """Not a clock time, not a timezone — the cycle's quiet start minus the lead."""

    def test_it_is_registered_as_a_quiet_phase_job(self):
        self.assertEqual(ArtemisScheduler.LOCATION_JOBS["meal_nudge"], "quiet")

    def test_it_is_the_lead_before_quiet_on_a_work_day(self):
        from artemis import cycle
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch.object(cycle, "override_for", return_value=None), \
             patch.object(s, "_next_date_for", return_value=date(2026, 9, 22)):
            times = s.cron_times()
        quiet = times["quiet_hours_start"]
        nudge = times["meal_nudge"]
        self.assertEqual(quiet, (17, 0))                     # office day
        self.assertEqual(nudge, (16, 0))                     # 60 min earlier
        self.assertEqual(config.MEAL_NUDGE_LEAD_MIN, 60)

    def test_it_moves_with_the_day_type(self):
        from artemis import cycle
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch.object(cycle, "override_for", return_value=None), \
             patch.object(s, "_next_date_for", return_value=date(2026, 9, 26)):
            times = s.cron_times()      # a wi day: quiet 22:30
        self.assertEqual(times["quiet_hours_start"], (22, 30))
        self.assertEqual(times["meal_nudge"], (21, 30))

    def test_it_follows_a_set_timezone_override(self):
        """The cron is registered in the ACTIVE zone, so the same derived (h, m)
        lands in whatever zone apply_timezone was given — no zone is baked in."""
        from artemis import cycle
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch.object(cycle, "override_for", return_value=None), \
             patch.object(s, "_next_date_for", return_value=date(2026, 9, 22)), \
             patch("artemis.quiet_hours.set_system_value"), \
             patch("artemis.quiet_hours.get_system_value", return_value=None), \
             patch("artemis.quiet_hours.get_active_timezone", return_value="Europe/Paris"), \
             patch.object(s, "_report_location_jobs"):
            s.apply_timezone("Europe/Paris")
        ids = {sp.id: sp for sp in s._cron_specs}
        self.assertIn("meal_nudge", ids, "the job must be registered")
        self.assertEqual(s._applied_tz, "Europe/Paris")
        job = s.scheduler.get_job("meal_nudge")
        self.assertIsNotNone(job, "registered as a real cron job")
        # the zone lives on the trigger object, not in its repr
        self.assertIn("Europe/Paris", str(getattr(job.trigger, "timezone", "")))
        # and the DERIVED time is what got registered, not a hard-coded clock
        self.assertIn("hour='16'", str(job.trigger))
        self.assertIn("minute='0'", str(job.trigger))

    def test_it_is_guarded_to_once_per_local_day(self):
        from artemis import cycle
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch.object(cycle, "override_for", return_value=None):
            spec = {sp.id: sp for sp in s.cron_specs()}["meal_nudge"]
        self.assertTrue(spec.daily_guard, "must not post twice in a local day")


class TestTheToggleCommand(unittest.TestCase):
    def setUp(self):
        from artemis import main as M
        self.M = M

    def _run(self, text):
        mm, sets = MagicMock(), []
        with patch.object(self.M, "_mm", mm), \
             patch.object(meal_nudge, "set_enabled", side_effect=lambda v: sets.append(v)):
            handled = self.M._handle_meal_nudge_toggle(
                {"channel_id": "CH", "id": "P1"}, text)
        return handled, sets, mm

    def test_on_and_off_are_claimed_and_set_the_flag(self):
        for text, want in (("meal nudge on", True), ("meal nudge off", False),
                           ("Meal Nudge ON", True), ("meal nudge off.", False)):
            with self.subTest(text=text):
                handled, sets, _ = self._run(text)
                self.assertTrue(handled)
                self.assertEqual(sets, [want])

    def test_anything_else_falls_through(self):
        for text in ("meal nudge", "nudge on", "meal nudge maybe", "for dinner I had rice",
                     "meal nudge on please", ""):
            with self.subTest(text=text):
                handled, sets, _ = self._run(text)
                self.assertFalse(handled)
                self.assertEqual(sets, [])

    def test_it_is_not_a_bare_control_word_so_confirm_arb_ignores_it(self):
        self.assertNotIn("meal nudge on", self.M._CONTROL_WORDS)
        self.assertIsNone(self.M._arbitrate_bare_control_word("CH", "meal nudge on"))

    def test_a_failure_to_set_says_it_is_unchanged(self):
        mm = MagicMock()
        with patch.object(self.M, "_mm", mm), \
             patch.object(meal_nudge, "set_enabled", side_effect=RuntimeError("no db")):
            handled = self.M._handle_meal_nudge_toggle({"channel_id": "CH", "id": "P1"},
                                                      "meal nudge on")
        self.assertTrue(handled)
        self.assertIn("unchanged", mm.post_message.call_args[0][1])

    def test_the_toggle_sits_ahead_of_meal_log_in_the_chain(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "main.py").read_text()
        self.assertLess(src.index('("meal_nudge_toggle"'), src.index('("meal_log"'))


if __name__ == "__main__":
    unittest.main()
