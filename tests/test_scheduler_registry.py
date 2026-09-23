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
from datetime import date, datetime, time
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
        """CYCLE-1: the phase times now depend on the day, so pin an office
        Tuesday (wake 04:30 · open 06:30 · quiet 17:00)."""
        from artemis import cycle
        office_day = datetime(2026, 9, 22, 0, 1, tzinfo=ZoneInfo(config.HOME_TIMEZONE))
        with patch.object(cycle, "override_for", return_value=None):
            by_id = {s.id: s for s in self.s.cron_specs(office_day)}
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

    def test_no_weekend_twins_remain(self):
        """CYCLE-1 collapsed them: one job per function, timed by location."""
        ids = [s.id for s in self.s.cron_specs()]
        self.assertEqual([i for i in ids if i.endswith("_weekend")], [])
        for job in ("wake", "checkin_nudge", "inbox_zero_morning", "open",
                    "morning_brief", "quiet_hours_start"):
            with self.subTest(job=job):
                self.assertEqual(ids.count(job), 1)
                spec = next(s for s in self.s.cron_specs() if s.id == job)
                self.assertIsNone(spec.day_of_week, "location jobs run every day")

    def test_location_jobs_take_their_time_from_the_cycle(self):
        """Richfield Friday 06:00 · office Tuesday 04:30 · MSP-home Saturday 07:30."""
        from datetime import datetime
        from artemis import cycle
        cases = {
            date(2026, 9, 25): (6, 0),      # wi, Richfield
            date(2026, 9, 22): (4, 30),     # msp_work, office
            date(2026, 10, 3): (7, 30),     # msp_home
            date(2026, 9, 28): (6, 0),      # travel, a Richfield morning
        }
        with patch.object(cycle, "override_for", return_value=None):
            for d, hm in cases.items():
                with self.subTest(day=d):
                    # just before midnight the day before → the job is for `d`
                    now = datetime.combine(d, time(0, 1), tzinfo=ZoneInfo(config.HOME_TIMEZONE))
                    times = self.s.cron_times(now)
                    self.assertEqual(times["wake"], hm)
                    # the nudge rides 45 min behind the wake
                    self.assertEqual(times["checkin_nudge"],
                                     ((hm[0] * 60 + hm[1] + config.CHECKIN_NUDGE_OFFSET_MIN) // 60 % 24,
                                      (hm[1] + config.CHECKIN_NUDGE_OFFSET_MIN) % 60))

    def test_business_hours_follow_the_day_type(self):
        from datetime import datetime
        from artemis import cycle
        tz = ZoneInfo(config.HOME_TIMEZONE)
        with patch.object(cycle, "override_for", return_value=None):
            work = self.s.cron_times(datetime(2026, 9, 22, 0, 1, tzinfo=tz))
            off = self.s.cron_times(datetime(2026, 9, 25, 0, 1, tzinfo=tz))
        self.assertEqual((work["open"], work["quiet_hours_start"]), ((6, 30), (17, 0)))
        self.assertEqual((off["open"], off["quiet_hours_start"]), ((8, 30), (22, 30)))

    def test_sunday_review_and_eval_are_pinned_to_fixed_hours(self):
        """Ryan, 2026-09-19: 08:30 / 08:35 whatever the location says."""
        by_id = {s.id: s for s in self.s.cron_specs()}
        self.assertEqual((by_id["health_review"].hour, by_id["health_review"].minute), (8, 30))
        self.assertEqual((by_id["weekly_eval"].hour, by_id["weekly_eval"].minute), (8, 35))
        for job in ("health_review", "weekly_eval"):
            self.assertEqual(by_id[job].day_of_week, "sun")
            self.assertNotIn(job, self.s.LOCATION_JOBS)

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

    def test_wake_fires_at_the_local_wall_clock_away(self):
        """A timezone move keeps the wall-clock time; CYCLE-1 decides what that
        time IS (here: an office day, 04:30)."""
        from artemis import cycle
        office_day = datetime(2026, 9, 22, 0, 1, tzinfo=ZoneInfo(config.HOME_TIMEZONE))
        with patch.object(cycle, "override_for", return_value=None), \
             patch.object(self.s, "cron_specs",
                          side_effect=lambda now=None: ArtemisScheduler.cron_specs(self.s, office_day)):
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

    def test_a_rescheduled_job_does_not_fire_twice_on_one_local_day(self):
        """CYCLE-1: moving wake 04:30 -> 06:00 mid-day must not re-fire it.
        The twins used to share a guard key; now there is one job, one key."""
        calls = []
        with patch.object(self.s, "job_wake", side_effect=lambda: calls.append(1)):
            spec = next(s for s in self.s.cron_specs() if s.id == "wake")
            self.s._wrap_cron(spec)()          # 04:30 firing
            self.s._wrap_cron(spec)()          # after a recompute moved it
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
        result = health_eval.evaluate([], [], [], start=date(2026, 9, 16), end=date(2026, 9, 19),
                                      today=sun)
        with patch.object(sched, "_local_today", return_value=sun), \
             patch.object(s, "_is_open", return_value=is_open), \
             patch.object(health_eval, "load", return_value=result) as load:
            s.job_weekly_eval()
        return s, load

    def test_posts_the_week_that_just_ended(self):
        """SCHEDULE-2 moved program weeks to Sun..Sat, so the Sunday 08:35 post
        covers the COMPLETE week that ended yesterday, not a partial."""
        s, load = self._run()
        load.assert_called_once()
        start, end = load.call_args[0][0], load.call_args[0][1]
        self.assertEqual((start, end), (date(2026, 9, 16), date(2026, 9, 19)))
        text = s.mm.post_message.call_args[0][1]
        self.assertIn("Week 1 (Wed 9/16 – Sat 9/19)", text)
        self.assertNotIn("partial", text)

    def test_nothing_outside_the_open_phase(self):
        s, load = self._run(is_open=False)
        load.assert_not_called()
        s.mm.post_message.assert_not_called()

class TestLiveRegistryMatchesTheCode(unittest.TestCase):
    """The job list is self-verifying: start() must register exactly the specs
    declared in code (CronSpec + IntervalSpec), so no report has to compare the
    live scheduler against a count someone wrote down a week ago."""

    def _start(self, *, scopes_ok=True):
        """Run start() against a fake APScheduler and return the ids it added."""
        from artemis.scheduler import ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        fake = MagicMock()
        fake.get_job.return_value = None
        s.scheduler = fake
        with patch("artemis.scheduler.check_billing_scopes",
                   return_value=(scopes_ok, [] if scopes_ok else ["gmail.readonly"])), \
             patch("artemis.scheduler.load_playbooks"), \
             patch("artemis.quiet_hours.get_active_timezone", return_value="America/Chicago"), \
             patch("artemis.quiet_hours.set_system_value"):
            s.start()
        added = [c.kwargs.get("id") or c.args[2] for c in fake.add_job.call_args_list]
        return s, added

    def test_start_registers_exactly_the_declared_specs(self):
        from artemis.scheduler import INTERVAL_SPECS
        s, added = self._start()
        expected = {spec.id for spec in INTERVAL_SPECS} | {c.id for c in s.cron_specs()}
        self.assertEqual(sorted(added), sorted(set(added)), f"duplicate job ids: {added}")
        self.assertEqual(set(added), expected)
        # and the count follows from the code, not from anyone's memory
        self.assertEqual(len(added), len(INTERVAL_SPECS) + len(s.cron_specs()))

    def test_billing_intake_is_the_only_conditional_job(self):
        from artemis.scheduler import INTERVAL_SPECS
        _, with_scopes = self._start(scopes_ok=True)
        _, without = self._start(scopes_ok=False)
        self.assertEqual(set(with_scopes) - set(without), {"billing_intake"})
        self.assertEqual([spec.id for spec in INTERVAL_SPECS if spec.needs_billing_scopes],
                         ["billing_intake"])

    def test_every_spec_names_a_real_job_method(self):
        from artemis.scheduler import INTERVAL_SPECS, ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        for spec in INTERVAL_SPECS:
            with self.subTest(job=spec.id):
                self.assertTrue(callable(getattr(s, spec.func_name, None)), spec.func_name)
                self.assertTrue(spec.every, "an interval job needs a period")
        for c in s.cron_specs():
            with self.subTest(job=c.id):
                self.assertTrue(callable(getattr(s, c.func_name, None)), c.func_name)

    def test_no_interval_job_is_registered_outside_the_registry(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        body = src[src.index("    def start(self):"):src.index("    def stop(self):")]
        self.assertEqual(body.count("self.scheduler.add_job("), 1,
                         "start() must add jobs only from INTERVAL_SPECS")
        self.assertIn("for spec in INTERVAL_SPECS:", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
