"""WAKE-1 — day phases, overrides, held posts, and the wake message.

No DB: knowledge.db is mocked and the wall clock is pinned via local_now().

Run:
    python3.11 tests/test_wake_window.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import config, posting, quiet_hours  # noqa: E402

CHICAGO = "America/Chicago"
SAO_PAULO = "America/Sao_Paulo"
PARIS = "Europe/Paris"


def at(tz_name: str, y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz_name))


class PhaseCase(unittest.TestCase):
    """Pin the clock + timezone; quiet_state comes from `state`."""

    def phase_at(self, tz_name, moment, state=None):
        with patch.object(quiet_hours, "local_now", return_value=moment), \
             patch.object(quiet_hours, "get_active_timezone", return_value=tz_name), \
             patch.object(quiet_hours, "_get_quiet_row", return_value=state):
            return quiet_hours.get_phase()


class TestPhaseBoundaries(PhaseCase):
    """04:30 and 06:30 and 17:00, in three zones. Defaults: 17:00 / 04:30 / 06:30."""

    def test_chicago_boundaries(self):
        cases = [
            ((4, 29), quiet_hours.PHASE_QUIET),
            ((4, 30), quiet_hours.PHASE_WAKE),
            ((6, 29), quiet_hours.PHASE_WAKE),
            ((6, 30), quiet_hours.PHASE_OPEN),
            ((16, 59), quiet_hours.PHASE_OPEN),
            ((17, 0), quiet_hours.PHASE_QUIET),
        ]
        for (hh, mm), expected in cases:
            with self.subTest(time=f"{hh:02d}:{mm:02d}"):
                self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 21, hh, mm)), expected)

    def test_sao_paulo_and_paris_use_their_own_wall_clock(self):
        for tz in (SAO_PAULO, PARIS):
            with self.subTest(tz=tz):
                self.assertEqual(self.phase_at(tz, at(tz, 2026, 9, 21, 4, 29)), quiet_hours.PHASE_QUIET)
                self.assertEqual(self.phase_at(tz, at(tz, 2026, 9, 21, 4, 30)), quiet_hours.PHASE_WAKE)
                self.assertEqual(self.phase_at(tz, at(tz, 2026, 9, 21, 6, 30)), quiet_hours.PHASE_OPEN)
                self.assertEqual(self.phase_at(tz, at(tz, 2026, 9, 21, 17, 0)), quiet_hours.PHASE_QUIET)

    def test_us_dst_transition_day(self):
        # 2026-11-01 (a Sunday): US falls back. The weekend wake boundary
        # (07:30 local) still holds on the transition day.
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 11, 1, 7, 29)), quiet_hours.PHASE_QUIET)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 11, 1, 7, 30)), quiet_hours.PHASE_WAKE)

    def test_eu_dst_transition_day_differs_from_us(self):
        # 2026-10-25 (a Sunday): the EU falls back a week before the US — the
        # windows are local wall-clock in each zone (weekend 07:30 / 08:30).
        self.assertEqual(self.phase_at(PARIS, at(PARIS, 2026, 10, 25, 7, 30)), quiet_hours.PHASE_WAKE)
        self.assertEqual(self.phase_at(PARIS, at(PARIS, 2026, 10, 25, 8, 30)), quiet_hours.PHASE_OPEN)
        # Same instant in Chicago that morning is still the middle of the night.
        instant = at(PARIS, 2026, 10, 25, 8, 30).astimezone(ZoneInfo(CHICAGO))
        self.assertEqual(self.phase_at(CHICAGO, instant), quiet_hours.PHASE_QUIET)

    def test_is_open_and_is_quiet_track_the_phase(self):
        moment = at(CHICAGO, 2026, 9, 21, 12, 0)
        with patch.object(quiet_hours, "local_now", return_value=moment), \
             patch.object(quiet_hours, "get_active_timezone", return_value=CHICAGO), \
             patch.object(quiet_hours, "_get_quiet_row", return_value=None):
            self.assertTrue(quiet_hours.is_open())
            self.assertFalse(quiet_hours.is_quiet())


class TestWeekendSchedule(PhaseCase):
    """CYCLE-1: the day boundaries follow LOCATION (wake) and DAY TYPE (open /
    quiet), not Sat/Sun. In this cycle Sat 9/26 and Sun 9/27 are `wi` days, so
    they still read 07:30 / 08:30 / 22:30 — but now because of the cycle."""

    def test_saturday_and_sunday_boundaries(self):
        cases = [
            ((4, 30), quiet_hours.PHASE_QUIET),   # weekday wake time is still night
            ((7, 29), quiet_hours.PHASE_QUIET),
            ((7, 30), quiet_hours.PHASE_WAKE),
            ((8, 29), quiet_hours.PHASE_WAKE),
            ((8, 30), quiet_hours.PHASE_OPEN),
            ((17, 0), quiet_hours.PHASE_OPEN),    # weekday quiet time is still open
            ((22, 29), quiet_hours.PHASE_OPEN),
            ((22, 30), quiet_hours.PHASE_QUIET),
        ]
        for day in (26, 27):  # Sat 9/26, Sun 9/27
            for (hh, mm), expected in cases:
                with self.subTest(day=day, time=f"{hh:02d}:{mm:02d}"):
                    self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, day, hh, mm)), expected)

    def test_the_wi_friday_stays_open_past_17(self):
        """Fri 9/25 is the Richfield day off work: quiet starts 22:30, not 17:00.
        Before CYCLE-1 this Friday went quiet at 17:00 because it was a weekday."""
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 25, 17, 0)), quiet_hours.PHASE_OPEN)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 25, 22, 29)), quiet_hours.PHASE_OPEN)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 25, 22, 30)), quiet_hours.PHASE_QUIET)

    def test_an_office_friday_still_goes_quiet_at_17(self):
        """Fri 10/2 is an msp_work day in week 2 of the cycle."""
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 10, 2, 16, 59)), quiet_hours.PHASE_OPEN)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 10, 2, 17, 0)), quiet_hours.PHASE_QUIET)

    def test_sunday_night_quiet_to_the_travel_monday_richfield_wake(self):
        """Mon 9/28 is the travel day and a RICHFIELD morning: wake 06:00, not
        the office 04:30. This is the gap SCHEDULE-2 left open."""
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 27, 23, 0)), quiet_hours.PHASE_QUIET)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 28, 4, 30)), quiet_hours.PHASE_QUIET)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 28, 5, 59)), quiet_hours.PHASE_QUIET)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 28, 6, 0)), quiet_hours.PHASE_WAKE)
        # …and it is still a work day: open 06:30, quiet 17:00
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 28, 6, 30)), quiet_hours.PHASE_OPEN)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 28, 17, 0)), quiet_hours.PHASE_QUIET)

    def test_next_wake_and_next_open(self):
        tz = ZoneInfo(CHICAGO)
        fri_eve = at(CHICAGO, 2026, 9, 25, 20, 0).astimezone(tz)
        self.assertEqual(quiet_hours.next_wake(fri_eve), at(CHICAGO, 2026, 9, 26, 7, 30))
        self.assertEqual(quiet_hours.next_open(fri_eve), at(CHICAGO, 2026, 9, 26, 8, 30))
        sun_eve = at(CHICAGO, 2026, 9, 27, 23, 0).astimezone(tz)
        # the travel Monday wakes at Richfield's 06:00, and works from 06:30
        self.assertEqual(quiet_hours.next_wake(sun_eve), at(CHICAGO, 2026, 9, 28, 6, 0))
        self.assertEqual(quiet_hours.next_open(sun_eve), at(CHICAGO, 2026, 9, 28, 6, 30))

    def test_friday_goodnight_holds_until_saturday_wake(self):
        set_at = at(CHICAGO, 2026, 9, 25, 21, 0).astimezone(timezone.utc)
        state = {"manual_override": 1, "is_quiet": 1, "updated_at": set_at}
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 26, 7, 0), state),
                         quiet_hours.PHASE_QUIET)
        self.assertEqual(self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 26, 7, 30), state),
                         quiet_hours.PHASE_WAKE)


class TestOverrideMatrix(PhaseCase):
    def test_working_session_forces_open_even_at_night(self):
        state = {"override_active": 1, "is_quiet": 1}
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 21, 23, 0), state),
            quiet_hours.PHASE_OPEN,
        )

    def test_goodnight_holds_quiet_until_the_wake_boundary(self):
        set_at = at(CHICAGO, 2026, 9, 21, 20, 0).astimezone(timezone.utc)
        state = {"override_active": 0, "manual_override": 1, "is_quiet": 1, "updated_at": set_at}
        # 22:00 the same evening — still quiet.
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 21, 22, 0), state), quiet_hours.PHASE_QUIET)
        # 05:00 next morning — the 04:30 boundary passed, so the manual state is
        # stale and the clock wins.
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 22, 5, 0), state), quiet_hours.PHASE_WAKE)

    def test_goodnight_with_custom_wake_time(self):
        set_at = at(CHICAGO, 2026, 9, 21, 21, 0).astimezone(timezone.utc)
        state = {"override_active": 0, "manual_override": 1, "is_quiet": 1,
                 "wake_time": "06:00", "updated_at": set_at}
        # 04:45 — past the default wake, but the custom 06:00 has not arrived…
        # the default boundary still clears the manual state, and the clock says wake.
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 22, 4, 45), state), quiet_hours.PHASE_WAKE)
        # 06:15 — after the custom wake time.
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 22, 6, 15), state), quiet_hours.PHASE_WAKE)

    def test_good_morning_at_0440_is_wake_not_open(self):
        set_at = at(CHICAGO, 2026, 9, 21, 4, 40).astimezone(timezone.utc)
        state = {"override_active": 0, "manual_override": 1, "is_quiet": 0, "updated_at": set_at}
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 21, 4, 40), state), quiet_hours.PHASE_WAKE)

    def test_good_morning_at_0640_is_open(self):
        set_at = at(CHICAGO, 2026, 9, 21, 6, 40).astimezone(timezone.utc)
        state = {"override_active": 0, "manual_override": 1, "is_quiet": 0, "updated_at": set_at}
        self.assertEqual(
            self.phase_at(CHICAGO, at(CHICAGO, 2026, 9, 21, 6, 40), state), quiet_hours.PHASE_OPEN)


class FakeKV:
    """In-memory stand-in for acos.system_state."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


class TestHolds(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKV()
        self.mm = MagicMock()
        self._patches = [
            patch.object(posting, "get_system_value", self.kv.get),
            patch.object(posting, "set_system_value", self.kv.set),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_health_holds_in_quiet_posts_in_wake(self):
        with patch.object(posting, "get_phase", return_value=quiet_hours.PHASE_QUIET):
            self.assertFalse(posting.post_or_hold(self.mm, "ops", "ramp slid", "health"))
        self.mm.post_message.assert_not_called()
        self.assertEqual(posting.held_count("health"), 1)

        with patch.object(posting, "get_phase", return_value=quiet_hours.PHASE_WAKE):
            self.assertTrue(posting.post_or_hold(self.mm, "ops", "second", "health"))

    def test_business_holds_until_open(self):
        for phase in (quiet_hours.PHASE_QUIET, quiet_hours.PHASE_WAKE):
            with patch.object(posting, "get_phase", return_value=phase):
                self.assertFalse(posting.post_or_hold(self.mm, "ops", f"mail {phase}", "business"))
        self.assertEqual(posting.held_count("business"), 2)
        with patch.object(posting, "get_phase", return_value=quiet_hours.PHASE_OPEN):
            self.assertTrue(posting.post_or_hold(self.mm, "ops", "now", "business"))

    def test_flush_is_ordered_and_idempotent(self):
        with patch.object(posting, "get_phase", return_value=quiet_hours.PHASE_QUIET):
            for i in range(3):
                posting.post_or_hold(self.mm, "ops", f"msg {i}", "business")
        posted = posting.flush_holds(self.mm, "business")
        self.assertEqual(posted, 3)
        self.assertEqual([c[0][1] for c in self.mm.post_message.call_args_list],
                         ["msg 0", "msg 1", "msg 2"])
        # Nothing left, so a second flush is a no-op.
        self.assertEqual(posting.flush_holds(self.mm, "business"), 0)
        self.assertEqual(posting.held_count("business"), 0)

    def test_holds_survive_a_restart(self):
        with patch.object(posting, "get_phase", return_value=quiet_hours.PHASE_QUIET):
            posting.post_or_hold(self.mm, "ops", "overnight", "business")
        # "Restart": same durable KV, brand-new client.
        fresh_mm = MagicMock()
        self.assertEqual(posting.flush_holds(fresh_mm, "business"), 1)
        fresh_mm.post_message.assert_called_once_with("ops", "overnight")

    def test_failed_post_keeps_the_queue(self):
        with patch.object(posting, "get_phase", return_value=quiet_hours.PHASE_QUIET):
            posting.post_or_hold(self.mm, "ops", "a", "business")
            posting.post_or_hold(self.mm, "ops", "b", "business")
        self.mm.post_message.side_effect = RuntimeError("mattermost down")
        self.assertEqual(posting.flush_holds(self.mm, "business"), 0)
        self.assertEqual(posting.held_count("business"), 2)

    def test_unknown_tier_is_rejected(self):
        with self.assertRaises(ValueError):
            posting.may_post("everything")


class TestWakeMessage(unittest.TestCase):
    PLAN = {
        "plan_id": 1,
        "plan_date": date(2026, 9, 21),
        "session_type": "strength_a",
        "est_duration_min": 40,
        "blocks": {
            "type": "circuit",
            "display_name": "Office Strength A",
            "location": "office gym",
            "warmup": "5 min elliptical, easy",
            "equipment": ["leg press", "DBs"],
            "exercises": [{"name": "Leg press", "format": "reps", "target_reps": 12}],
        },
    }

    def _build(self, plan=None, held=None, checked_in=False):
        from artemis import wake as wake_mod
        cal = MagicMock()
        cal.service = True
        cal.get_today_events.return_value = [{"summary": "Standup", "start": "2026-09-21T09:00:00-05:00"}]
        with patch("artemis.health.get_today_plan", return_value=plan if plan is not None else self.PLAN), \
             patch.object(wake_mod, "_weather_line", return_value="Weather: 72°/54°F · clear · 10% precip"), \
             patch.object(wake_mod, "_depart_commitments", return_value=["· drop off dry cleaning"]), \
             patch.object(wake_mod, "get_timezone_override", return_value=None), \
             patch("artemis.quiet_hours.local_now", return_value=at(CHICAGO, 2026, 9, 21, 4, 30)), \
             patch("artemis.quiet_hours.local_today", return_value=date(2026, 9, 21)):
            return wake_mod.build_wake_message(calendar=cal, held_health=held or [],
                                              checked_in=checked_in)

    def test_contains_workout_checkin_and_departure(self):
        msg = self._build()
        self.assertIn("Office Strength A", msg)
        self.assertIn("Where: office gym", msg)
        self.assertIn("1. Leg press — 1×12", msg)
        self.assertIn("Warmup: 5 min elliptical, easy", msg)
        self.assertIn("gym.rdm.is", msg)
        self.assertIn("sleep hrs", msg.lower())
        self.assertIn("Before you leave", msg)
        self.assertIn("gym bag", msg)
        self.assertIn("First event: 9:00 AM", msg)
        self.assertIn("Weather:", msg)
        self.assertIn("dry cleaning", msg)

    def test_no_checkin_prompt_once_checked_in(self):
        msg = self._build(checked_in=True)
        self.assertNotIn("sleep hrs", msg.lower())
        self.assertNotIn("Morning check-in", msg)
        self.assertIn("Office Strength A", msg)
        self.assertIn("Before you leave", msg)

    def test_excludes_business_content(self):
        msg = self._build(held=["↔ Slid Strength A → Wed (makeup slot)."]).lower()
        for banned in ("email", "inbox", "triage", "action item", "vault", "deal", "ssl"):
            self.assertNotIn(banned, msg, f"wake post must not mention {banned}")

    def test_held_health_notice_is_included(self):
        msg = self._build(held=["↔ Slid Strength A → Wed (makeup slot)."])
        self.assertIn("Slid Strength A", msg)

    def test_rest_day_gets_one_line(self):
        plan = {"plan_id": 2, "session_type": "rest_mobility", "est_duration_min": 20,
                "blocks": {"type": "mobility", "display_name": "Rest / Mobility",
                           "notes": "20 min mobility or full rest"}}
        msg = self._build(plan=plan)
        self.assertIn("Rest / Mobility", msg)
        self.assertNotIn("Where:", msg)
        self.assertNotIn("First lift", msg)

    # ── Departure block is location-aware ──
    # The close is the real FLOW_CLOSE: since YOGA-4 every timed item carries its
    # own transition_sec, and a hand-written close without one (this fixture's
    # 9/18 shape) raised KeyError in flow_total_sec.
    from artemis.health_office import FLOW_CLOSE as _FLOW_CLOSE
    HOME_FLOW = {"plan_id": 3, "session_type": "recovery_flow", "est_duration_min": 30,
                 "blocks": {"type": "recovery_flow", "display_name": "Recovery Flow",
                            "location": "home", "rounds": 2, "total_sec": 1770,
                            "flow": [], "pre": [], "close": dict(_FLOW_CLOSE)}}

    def _depart(self, plan, *, event=True, weather=True, commitments=()):
        from artemis import wake as wake_mod
        cal = MagicMock()
        cal.service = True
        cal.get_today_events.return_value = (
            [{"summary": "Standup", "start": "2026-09-19T09:00:00-05:00"}] if event else [])
        with patch.object(wake_mod, "_weather_line",
                          return_value="Weather: 72°/54°F · clear" if weather else None), \
             patch.object(wake_mod, "_depart_commitments", return_value=list(commitments)):
            return wake_mod._departure_section(cal, plan)

    def test_office_day_shows_the_checklist(self):
        lines = self._depart(self.PLAN)
        self.assertIn("**Before you leave**", lines)
        self.assertIn("· gym bag, badge, lunch, iPad", lines)

    def test_home_saturday_has_no_checklist_but_keeps_event_and_weather(self):
        text = "\n".join(self._depart(self.HOME_FLOW))
        self.assertNotIn("gym bag", text)
        self.assertIn("**Before you leave**", text)
        self.assertIn("First event", text)
        self.assertIn("Weather:", text)

    def test_home_and_rest_days_follow_the_home_rule(self):
        home_flow = {"session_type": "recovery_flow", "blocks": {"location": "home"}}
        rest_with_office_blocks = {"session_type": "rest_mobility",
                                   "blocks": {"location": "office gym"}}
        for plan in (home_flow, rest_with_office_blocks, None):
            with self.subTest(plan=plan):
                self.assertNotIn("gym bag", "\n".join(self._depart(plan)))

    def test_depart_commitment_shows_on_a_home_day(self):
        lines = self._depart(self.HOME_FLOW, event=False, weather=False,
                             commitments=["· drop off dry cleaning"])
        self.assertEqual(lines, ["", "**Before you leave**", "· drop off dry cleaning"])

    def test_block_disappears_when_empty(self):
        self.assertEqual(self._depart(self.HOME_FLOW, event=False, weather=False), [])
        msg_plan = dict(self.HOME_FLOW)
        from artemis import wake as wake_mod
        with patch("artemis.health.get_today_plan", return_value=msg_plan), \
             patch.object(wake_mod, "_weather_line", return_value=None), \
             patch.object(wake_mod, "_depart_commitments", return_value=[]), \
             patch.object(wake_mod, "get_timezone_override", return_value=None), \
             patch("artemis.quiet_hours.local_now", return_value=at(CHICAGO, 2026, 9, 19, 7, 30)), \
             patch("artemis.quiet_hours.local_today", return_value=date(2026, 9, 19)):
            msg = wake_mod.build_wake_message(calendar=None, held_health=[])
        self.assertIn("Recovery Flow", msg)
        self.assertNotIn("Before you leave", msg)

    def test_estimate_wording_tilde_only_over_45(self):
        from artemis import wake as wake_mod
        for est, want, absent in ((44, "— 44 min", "~44"), (45, "— 45 min", "~45"),
                                  (52, "— ~52 min", None), (62, "— ~62 min", None)):
            with self.subTest(est=est):
                plan = dict(self.PLAN, est_duration_min=est)
                text = "\n".join(wake_mod._workout_section(plan))
                self.assertIn(want, text)
                if absent:
                    self.assertNotIn(absent, text)
                for word in ("over", "long", "sorry", "warning", "target"):
                    self.assertNotIn(word, text.lower())

    def test_prompt_type_comes_from_the_plan_not_the_weekday(self):
        from artemis import wake as wake_mod
        self.assertEqual(wake_mod.prompt_type_for({"session_type": "strength_b"}), "workout_am")
        self.assertEqual(wake_mod.prompt_type_for({"session_type": "cardio_z2"}), "workout_am")
        self.assertEqual(wake_mod.prompt_type_for({"session_type": "rest_mobility"}), "logging_only")
        self.assertEqual(wake_mod.prompt_type_for({"session_type": "recovery_flow"}), "logging_only")
        self.assertEqual(wake_mod.prompt_type_for(None), "logging_only")


class TestConfigDefaults(unittest.TestCase):
    def test_phase_defaults(self):
        self.assertEqual(config.QUIET_HOURS_START, "17:00")
        self.assertEqual(config.WAKE_TIME, "04:30")
        self.assertEqual(config.OPEN_TIME, "06:30")
        self.assertEqual(config.MORNING_BRIEF_TIME, "06:30")
        # Back-compat alias still resolves to the wake time.
        self.assertEqual(config.QUIET_HOURS_END, "04:30")
        self.assertIn("gym bag", config.DEPARTURE_CHECKLIST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
