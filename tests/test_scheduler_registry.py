"""WAKE-1 — the cron registry, apply_timezone, and the tz-replay guard.

A real BackgroundScheduler is constructed but never started, so triggers can be
inspected without firing. Mattermost/Gmail/Calendar are mocks and system_state
is in-memory — nothing here touches RDS.

Run:
    python3.11 tests/test_scheduler_registry.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import os
import re
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

for _name in ("googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
              "google", "google.auth", "google.auth.transport",
              "google.auth.transport.requests", "google.oauth2",
              "google.oauth2.credentials", "google_auth_oauthlib",
              "google_auth_oauthlib.flow", "websocket", "Levenshtein"):
    sys.modules.setdefault(_name, MagicMock())

from artemis import config, quiet_hours  # noqa: E402
from artemis.scheduler import ArtemisScheduler  # noqa: E402

SAO_PAULO = "America/Sao_Paulo"


def make_scheduler() -> ArtemisScheduler:
    return ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())


class TestRegistryShape(unittest.TestCase):
    def setUp(self):
        self.s = make_scheduler()

    def test_ids_are_unique(self):
        ids = [spec.id for spec in self.s.cron_specs()]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate cron ids: {ids}")

    def test_every_spec_points_at_a_real_method(self):
        for spec in self.s.cron_specs():
            with self.subTest(job=spec.id):
                self.assertTrue(callable(getattr(self.s, spec.func_name, None)),
                                f"{spec.id} → missing method {spec.func_name}")

    def test_every_spec_declares_a_known_tier(self):
        for spec in self.s.cron_specs():
            with self.subTest(job=spec.id):
                self.assertIn(spec.tier, ("health", "business"))

    def test_phase_jobs_track_the_configured_times(self):
        by_id = {s.id: s for s in self.s.cron_specs()}
        self.assertEqual((by_id["wake"].hour, by_id["wake"].minute), (4, 30))
        self.assertEqual((by_id["open"].hour, by_id["open"].minute), (6, 30))
        self.assertEqual((by_id["quiet_hours_start"].hour,
                          by_id["quiet_hours_start"].minute), (17, 0))
        self.assertEqual((by_id["morning_brief"].hour,
                          by_id["morning_brief"].minute), (6, 30))
        # inbox_zero_morning is 5 minutes ahead of the brief.
        self.assertEqual((by_id["inbox_zero_morning"].hour,
                          by_id["inbox_zero_morning"].minute), (6, 25))
        # Silent vault ingest runs before the wake window.
        self.assertEqual((by_id["vault_sync"].hour, by_id["vault_sync"].minute), (3, 30))
        # The debrief nag must sit OUTSIDE the quiet window.
        self.assertLess(by_id["health_nag"].hour, 17)

    def test_weekend_twins_move_wake_open_brief_and_quiet(self):
        by_id = {s.id: s for s in self.s.cron_specs()}
        expect = {
            "wake_weekend": ((7, 30), "wake", "health"),
            "checkin_nudge_weekend": ((8, 15), "checkin_nudge", "health"),
            "inbox_zero_morning_weekend": ((8, 25), "inbox_zero_morning", "business"),
            "open_weekend": ((8, 30), "open", "business"),
            "morning_brief_weekend": ((8, 30), "morning_brief", "business"),
            "quiet_hours_start_weekend": ((22, 30), "quiet_hours_start", "business"),
        }
        for sid, (hm, twin, tier) in expect.items():
            with self.subTest(job=sid):
                spec = by_id[sid]
                self.assertEqual((spec.hour, spec.minute), hm)
                self.assertEqual(spec.day_of_week, "sat,sun")
                self.assertEqual(spec.guard, twin)
                self.assertEqual(spec.func_name, by_id[twin].func_name)
                self.assertEqual(spec.tier, tier)
                self.assertEqual(by_id[twin].day_of_week, "mon-fri")

    def test_weekly_eval_posts_sunday_after_the_review(self):
        spec = {s.id: s for s in self.s.cron_specs()}["weekly_eval"]
        self.assertEqual((spec.hour, spec.minute, spec.day_of_week, spec.tier),
                         (8, 35, "sun", "health"))
        self.assertTrue(callable(getattr(self.s, spec.func_name)))

    def test_sunday_health_review_runs_after_the_weekend_open(self):
        hr_ = {s.id: s for s in self.s.cron_specs()}["health_review"]
        self.assertEqual((hr_.hour, hr_.minute, hr_.day_of_week), (8, 30, "sun"))

    def test_ssl_and_domain_checks_are_weekday_only(self):
        by_id = {s.id: s for s in self.s.cron_specs()}
        for sid in ("ssl_check", "domain_check"):
            self.assertEqual(by_id[sid].day_of_week, "mon-fri")
            self.assertNotIn(f"{sid}_weekend", by_id)

    def test_wake_is_health_tier_and_business_jobs_are_not(self):
        by_id = {s.id: s for s in self.s.cron_specs()}
        self.assertEqual(by_id["wake"].tier, "health")
        self.assertEqual(by_id["health_nag"].tier, "health")
        self.assertEqual(by_id["morning_brief"].tier, "business")

    def test_no_cron_is_registered_outside_the_registry(self):
        """apply_timezone must be the ONLY add_job(..., cron, ...) path."""
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        # Strip apply_timezone's body — that is the sanctioned registration.
        start = src.index("def apply_timezone")
        end = src.index("def job_dump")
        outside = src[:start] + src[end:]
        code = "\n".join(l for l in outside.splitlines() if not l.lstrip().startswith("#"))
        offenders = re.findall(r'add_job\([^)]*["\']cron["\']', code)
        self.assertEqual(offenders, [], f"cron registered outside the registry: {offenders}")


class TestApplyTimezone(unittest.TestCase):
    def setUp(self):
        self.s = make_scheduler()
        self.kv: dict[str, str] = {}
        self._p = [
            patch.object(quiet_hours, "set_system_value", lambda k, v: self.kv.__setitem__(k, v)),
            patch.object(quiet_hours, "get_system_value", self.kv.get),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _tz_of(self, job_id):
        return str(self.s.scheduler.get_job(job_id).trigger.timezone)

    def test_every_registry_job_moves_to_the_new_zone(self):
        self.s.apply_timezone(SAO_PAULO)
        registered = {s.id for s in self.s.cron_specs()}
        self.assertTrue(registered)
        for job_id in registered:
            with self.subTest(job=job_id):
                self.assertEqual(self._tz_of(job_id), SAO_PAULO)
        self.assertEqual(self.kv.get("scheduler_tz"), SAO_PAULO)

    def test_wake_fires_at_0430_local_away(self):
        self.s.apply_timezone(SAO_PAULO)
        trigger = self.s.scheduler.get_job("wake").trigger
        nxt = trigger.get_next_fire_time(None, datetime.now(ZoneInfo(SAO_PAULO)))
        self.assertEqual((nxt.hour, nxt.minute), (4, 30))
        self.assertEqual(str(nxt.tzinfo), SAO_PAULO)

    def test_switching_back_home_reschedules_in_place(self):
        self.s.apply_timezone(SAO_PAULO)
        ids_away = {j.id for j in self.s.scheduler.get_jobs()}
        self.s.apply_timezone(config.HOME_TIMEZONE)
        ids_home = {j.id for j in self.s.scheduler.get_jobs()}
        self.assertEqual(ids_away, ids_home, "rescheduling must not add or drop jobs")
        self.assertEqual(self._tz_of("wake"), config.HOME_TIMEZONE)
        self.assertEqual(self.kv.get("scheduler_tz"), config.HOME_TIMEZONE)

    def test_an_unknown_zone_is_refused_and_the_old_one_kept(self):
        self.s.apply_timezone(SAO_PAULO)
        self.s.apply_timezone("Mars/Olympus_Mons")
        self.assertEqual(self._tz_of("wake"), SAO_PAULO)
        self.assertEqual(self.s._applied_tz, SAO_PAULO)

    def test_job_dump_lists_every_job_with_local_and_ct(self):
        self.s.apply_timezone(SAO_PAULO)
        dump = self.s.job_dump()
        self.assertEqual(len(dump), len(self.s.cron_specs()))
        self.assertTrue(all("ct=" in line for line in dump), dump)

    def test_tz_sync_reapplies_only_when_the_active_zone_changed(self):
        self.s.apply_timezone(config.HOME_TIMEZONE)
        with patch.object(quiet_hours, "check_expired_overrides", return_value=None), \
             patch.object(quiet_hours, "get_active_timezone", return_value=config.HOME_TIMEZONE), \
             patch.object(self.s, "apply_timezone") as apply_mock:
            self.s.job_tz_sync()
            apply_mock.assert_not_called()

        with patch.object(quiet_hours, "check_expired_overrides", return_value=None), \
             patch.object(quiet_hours, "get_active_timezone", return_value=SAO_PAULO), \
             patch.object(self.s, "apply_timezone") as apply_mock:
            self.s.job_tz_sync()
            apply_mock.assert_called_once_with(SAO_PAULO)


class TestDuplicateGuard(unittest.TestCase):
    """A timezone switch can replay a wall-clock time on the same local date."""

    def setUp(self):
        self.s = make_scheduler()
        self.kv: dict[str, str] = {}
        self.today = date(2026, 9, 23)
        self._p = [
            patch.object(quiet_hours, "set_system_value", lambda k, v: self.kv.__setitem__(k, v)),
            patch.object(quiet_hours, "get_system_value", self.kv.get),
            patch.object(quiet_hours, "local_today", lambda: self.today),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_second_run_on_the_same_local_day_is_skipped(self):
        self.assertTrue(self.s._once_per_local_day("quiet_hours_start"))
        self.assertFalse(self.s._once_per_local_day("quiet_hours_start"))

    def test_paris_expiry_replaying_1700_does_not_refire_quiet_hours(self):
        calls = []
        with patch.object(self.s, "job_quiet_hours_start", side_effect=lambda: calls.append(1)):
            runner = self.s._wrap_cron(
                next(s for s in self.s.cron_specs() if s.id == "quiet_hours_start"))
            runner()   # 17:00 Paris time
            runner()   # 17:00 Central, same local date, after the override expired
        self.assertEqual(len(calls), 1, "quiet_hours_start fired twice on one local day")

    def test_the_next_local_day_runs_again(self):
        self.assertTrue(self.s._once_per_local_day("wake"))
        self.today = date(2026, 9, 24)
        self.assertTrue(self.s._once_per_local_day("wake"))

    def test_weekend_twin_and_weekday_job_share_one_run_per_day(self):
        calls = []
        with patch.object(self.s, "job_wake", side_effect=lambda: calls.append(1)):
            specs = {s.id: s for s in self.s.cron_specs()}
            self.s._wrap_cron(specs["wake"])()
            self.s._wrap_cron(specs["wake_weekend"])()
        self.assertEqual(len(calls), 1, "wake fired twice on one local day")

    def test_jobs_are_guarded_independently(self):
        self.assertTrue(self.s._once_per_local_day("wake"))
        self.assertTrue(self.s._once_per_local_day("morning_brief"))


class TestPhaseGating(unittest.TestCase):
    def setUp(self):
        self.s = make_scheduler()

    def test_business_posts_route_through_the_phase_gate(self):
        with patch("artemis.posting.post_or_hold", return_value=False) as gate:
            self.s._post("ops", "hello", tier="business")
        gate.assert_called_once()
        self.assertEqual(gate.call_args[0][3], "business")

    def test_wake_posts_directly_and_folds_in_held_health(self):
        with patch("artemis.posting.take_holds", return_value=["held note"]) as take, \
             patch("artemis.wake.build_wake_message", return_value="WAKE POST") as build, \
             patch.object(quiet_hours, "exit_quiet", return_value=""), \
             patch("artemis.health.get_today_plan", return_value=None):
            self.s._do_wake()
        take.assert_called_once_with("health")
        self.assertEqual(build.call_args.kwargs["held_health"], ["held note"])
        self.s.mm.post_message.assert_called_once_with(config.CHANNEL_OPS, "WAKE POST")

    def test_wake_defers_to_the_watcher_when_a_custom_wake_time_is_set(self):
        state = {"manual_override": 1, "is_quiet": 1, "wake_time": "06:00"}
        with patch.object(quiet_hours, "get_quiet_state", return_value=state), \
             patch.object(self.s, "_do_wake") as do_wake:
            self.s.job_wake()
        do_wake.assert_not_called()

    def test_the_watcher_wakes_once_the_custom_time_arrives(self):
        state = {"manual_override": 1, "is_quiet": 1, "wake_time": "06:00"}
        late = datetime(2026, 9, 23, 6, 1, tzinfo=ZoneInfo(config.HOME_TIMEZONE))
        with patch.object(quiet_hours, "get_quiet_state", return_value=state), \
             patch.object(quiet_hours, "local_now", return_value=late), \
             patch.object(self.s, "_once_per_local_day", return_value=True), \
             patch.object(self.s, "_do_wake") as do_wake:
            self.s.job_wake_watch()
        do_wake.assert_called_once()

    def test_the_watcher_stays_quiet_before_the_custom_time(self):
        state = {"manual_override": 1, "is_quiet": 1, "wake_time": "06:00"}
        early = datetime(2026, 9, 23, 5, 15, tzinfo=ZoneInfo(config.HOME_TIMEZONE))
        with patch.object(quiet_hours, "get_quiet_state", return_value=state), \
             patch.object(quiet_hours, "local_now", return_value=early), \
             patch.object(self.s, "_do_wake") as do_wake:
            self.s.job_wake_watch()
        do_wake.assert_not_called()

    def test_open_flushes_business_holds(self):
        with patch.object(quiet_hours, "get_quiet_state", return_value={}), \
             patch("artemis.posting.flush_holds", return_value=2) as flush, \
             patch.object(self.s, "_build_overnight_summary", return_value="summary"):
            self.s.job_open()
        flush.assert_called_once_with(self.s.mm, "business")

    def test_open_holds_off_while_a_manual_goodnight_is_still_active(self):
        state = {"is_quiet": 1, "manual_override": 1}
        with patch.object(quiet_hours, "get_quiet_state", return_value=state), \
             patch("artemis.posting.flush_holds") as flush:
            self.s.job_open()
        flush.assert_not_called()



class TestWeeklyEvalJob(unittest.TestCase):
    """EVAL-1 Sunday post: Wed–Sat span, open phase only, never a DB write."""

    def _run(self, is_open=True):
        from artemis import health_eval
        from artemis import scheduler as sched
        s = make_scheduler()
        sun = date(2026, 9, 20)
        result = health_eval.evaluate([], [], [], start=date(2026, 9, 16), end=date(2026, 9, 22),
                                      today=sun)
        with patch.object(sched, "_local_today", return_value=sun), \
             patch.object(s, "_is_open", return_value=is_open), \
             patch.object(health_eval, "load", return_value=result) as load:
            s.job_weekly_eval()
        return s, load

    def test_posts_the_week_to_date_wed_to_sat(self):
        s, load = self._run()
        load.assert_called_once()
        text = s.mm.post_message.call_args[0][1]
        self.assertIn("Week 1 (Wed 9/16 – Sat 9/19, partial)", text)

    def test_nothing_outside_the_open_phase(self):
        s, load = self._run(is_open=False)
        load.assert_not_called()
        s.mm.post_message.assert_not_called()

if __name__ == "__main__":
    unittest.main(verbosity=2)
