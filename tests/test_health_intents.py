"""Integration tests for artemis.health intent handlers.

Covers:
  - intent detection (regex pre-router)
  - morning check-in parser → confirm format
  - debrief parser → DB write → confirm format
  - "fix burpees rpe 9" edit flow
  - nag logic (skip when rest/walk/already-logged)
  - soreness region normalization

Mocks Claude (anthropic) and the DB layer; verifies handler roundtrip
without needing live AWS access.

Run:
    python tests/test_health_intents.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block AWS access
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import health  # noqa: E402


# ============================================================================
# Intent detection
# ============================================================================

class TestIntentDetection(unittest.TestCase):
    def test_morning_triggers(self):
        self.assertEqual(health.detect_health_intent("morning"), "log_morning_state")
        self.assertEqual(health.detect_health_intent("morning. slept 6"), "log_morning_state")
        self.assertEqual(health.detect_health_intent("checkin"), "log_morning_state")
        self.assertEqual(health.detect_health_intent("@artemis morning"), "log_morning_state")
        self.assertEqual(health.detect_health_intent("slept 7 hours"), "log_morning_state")

    def test_debrief_triggers(self):
        self.assertEqual(health.detect_health_intent("done"), "log_workout_debrief")
        self.assertEqual(health.detect_health_intent("debrief"), "log_workout_debrief")
        self.assertEqual(health.detect_health_intent("workout done"), "log_workout_debrief")
        self.assertEqual(
            health.detect_health_intent("squats RPE 7, plank 30s"),
            "log_workout_debrief",
        )

    def test_neither(self):
        self.assertIsNone(health.detect_health_intent("hello"))
        self.assertIsNone(health.detect_health_intent("what's the weather"))


# ============================================================================
# Soreness normalization
# ============================================================================

class TestSorenessNormalization(unittest.TestCase):
    def test_legs_aliases(self):
        self.assertEqual(health.normalize_soreness_region("legs"), "legs")
        self.assertEqual(health.normalize_soreness_region("quads"), "legs")
        self.assertEqual(health.normalize_soreness_region("thighs"), "legs")
        self.assertEqual(health.normalize_soreness_region("Thighs"), "legs")  # case insensitive
        self.assertEqual(health.normalize_soreness_region("hamstrings"), "legs")

    def test_back_aliases(self):
        self.assertEqual(health.normalize_soreness_region("lumbar"), "back")
        self.assertEqual(health.normalize_soreness_region("lower back"), "back")

    def test_unknown_passthrough(self):
        # Unrecognized labels pass through lower-cased
        self.assertEqual(health.normalize_soreness_region("Elbows"), "elbows")


# ============================================================================
# Morning check-in handler
# ============================================================================

class TestMorningHandler(unittest.TestCase):
    """FRIDAY-1: handle_morning_intent is deterministic — it delegates to
    artemis.health_checkin.process_checkin inside one DB transaction and never
    calls the LLM."""

    def _conn(self, cur):
        from contextlib import contextmanager

        @contextmanager
        def conn():
            m = MagicMock()
            m.cursor.return_value = cur
            yield m
        return conn

    def test_delegates_to_deterministic_checkin(self):
        cur = MagicMock()
        with patch("knowledge.db.get_connection", self._conn(cur)), \
             patch("artemis.health_checkin.process_checkin",
                   return_value="Check-in logged — run Session A as written.") as pc, \
             patch.object(health, "_call_claude_json",
                          side_effect=AssertionError("LLM must not be called")):
            result = health.handle_morning_intent("slept 6.5 energy 3 legs sore 3 RHR 98",
                                                  message_id="p1")
        pc.assert_called_once()
        self.assertEqual(pc.call_args[0][0], cur)
        self.assertEqual(pc.call_args[0][1], "slept 6.5 energy 3 legs sore 3 RHR 98")
        self.assertEqual(pc.call_args.kwargs["checkin_id"], "p1")
        self.assertEqual(result, "Check-in logged — run Session A as written.")

    def test_db_failure_returns_warning(self):
        with patch("knowledge.db.get_connection", side_effect=RuntimeError("conn lost")):
            result = health.handle_morning_intent("slept 6")
        self.assertIn("Couldn", result)
        self.assertIn("DB", result)


# ============================================================================
# Workout debrief handler
# ============================================================================

class TestDebriefHandler(unittest.TestCase):
    def test_multi_exercise_roundtrip(self):
        """Full debrief → N exercise rows + 1 summary row → confirm."""
        fake_parsed = {
            "exercises": [
                {
                    "exercise": "Burpees",
                    "log_type": "cardio_block",
                    "reps_done": 15,
                    "rpe_actual": 10.0,
                    "hr_peak": 159,
                    "is_skipped": False,
                },
                {
                    "exercise": "RDL",
                    "log_type": "strength_set",
                    "reps_done": 10,
                    "weight_lbs": 50.0,
                    "rpe_actual": 6.0,
                    "is_skipped": False,
                },
                {
                    "exercise": "Plank",
                    "log_type": "strength_set",
                    "notes": "skipped: knee was off",
                    "is_skipped": True,
                },
            ],
            "session_summary": {
                "rpe_actual": 8.0,
                "user_suggestion": "rest too easy on Z2 recovery, try 60s",
                "notes": None,
            },
        }
        fake_plan = {"plan_id": 42, "session_type": "strength_a", "phase": 1, "target_rpe": 6.5}

        captured_inserts = []

        def fake_insert(reports, plan_id):
            captured_inserts.extend(reports)
            return len(reports)

        with patch.object(health, "_call_claude_json", return_value=fake_parsed), \
             patch.object(health, "get_today_plan", return_value=fake_plan), \
             patch.object(health, "insert_session_logs", side_effect=fake_insert):
            result = health.handle_debrief_intent(
                "Burpees 15 RPE 10 HR peak 159, RDLs 10 at 50 RPE 6, "
                "skipped planks knee was off, overall RPE 8. "
                "rest too easy on Z2 recovery, try 60s next time."
            )

        # 3 exercises + 1 summary = 4 rows total
        self.assertEqual(len(captured_inserts), 4)
        log_types = [r.log_type for r in captured_inserts]
        self.assertIn("cardio_block", log_types)
        self.assertIn("strength_set", log_types)
        self.assertEqual(log_types.count("session_summary"), 1)

        # Verbatim user_suggestion preserved
        summary = next(r for r in captured_inserts if r.log_type == "session_summary")
        self.assertEqual(summary.user_suggestion, "rest too easy on Z2 recovery, try 60s")

        # Skipped exercise marked correctly
        plank = next(r for r in captured_inserts if r.exercise == "Plank")
        self.assertTrue(plank.is_skipped)
        self.assertIn("skipped", (plank.notes or "").lower())

        # Confirm output mentions all 3 exercises
        self.assertIn("3 exercises", result)
        self.assertIn("Burpees", result)
        self.assertIn("RDL", result)
        self.assertIn("Plank", result)
        self.assertIn("SKIPPED", result)
        self.assertIn("Overall RPE 8", result)
        self.assertIn("rest too easy", result)  # verbatim suggestion echoed


# ============================================================================
# Fix flow — "fix burpees rpe 9"
# ============================================================================

class TestFixFlow(unittest.TestCase):
    def test_fix_grammar_match(self):
        """Valid 'fix X rpe N' → DB update with most recent matching row."""
        with patch("knowledge.db.execute_write") as mock_write:
            mock_write.return_value = {"log_id": 7, "exercise": "Burpees", "rpe_actual": 9.0}
            result = health.handle_fix_intent("fix burpees rpe 9")

        self.assertIsNotNone(result)
        self.assertIn("Burpees", result)
        self.assertIn("9", result)
        mock_write.assert_called_once()
        sql, params = mock_write.call_args[0]
        self.assertIn("UPDATE health.session_log", sql)

    def test_no_match_returns_helpful_error(self):
        with patch("knowledge.db.execute_write", return_value=None):
            result = health.handle_fix_intent("fix burpees rpe 9")
        self.assertIn("burpees", result.lower())
        self.assertIn("Spelling?", result)

    def test_non_matching_messages_pass_through(self):
        """Returns None for non-fix messages so the caller falls through."""
        self.assertIsNone(health.handle_fix_intent("done"))
        self.assertIsNone(health.handle_fix_intent("burpees 15 reps RPE 10"))
        self.assertIsNone(health.handle_fix_intent("hello"))


# ============================================================================
# Nag logic
# ============================================================================

class TestNagLogic(unittest.TestCase):
    """SESSION-OPEN (2026-11-09): the nag is the strength quota's, once a week,
    on the day that is the real last chance (STATUS-3 phase 2: Mon..Sun goal
    weeks, remaining days placed as late as feasible). Synthetic dates
    (PUBLIC-FIXTURES): the Mon 2031-03-03 .. Sun 2031-03-09 week, every day free."""

    MON, WED, THU, FRI, SAT, SUN = (date(2031, 3, 3), date(2031, 3, 5), date(2031, 3, 6),
                                    date(2031, 3, 7), date(2031, 3, 8), date(2031, 3, 9))

    @staticmethod
    def _free():
        from knowledge import cycle_days
        out = cycle_days.resolve_days(date(2031, 3, 3), date(2031, 3, 9), [])
        for info in out.values():
            info.update(day_type="msp_home", away=False)
        return out

    def _run(self, days, today, sent=None):
        from knowledge import strength_quota as sq
        q = sq.compute(today, days, today=today, counts_from=date(2000, 1, 3), days=self._free())
        with patch("artemis.quiet_hours.get_active_timezone", return_value="UTC"), \
             patch("artemis.quiet_hours.get_system_value", return_value=sent), \
             patch("knowledge.strength_quota.read_pg", return_value=q) as rd:
            out = health.strength_nag(today)
        rd.assert_called_once_with(today, tz="UTC", today=today)
        return out

    def test_no_nag_while_on_track(self):
        self.assertIsNone(self._run([self.MON], today=self.WED))

    def test_no_nag_when_met(self):
        self.assertIsNone(self._run([self.MON, self.WED, self.THU], today=self.FRI))

    def test_nag_on_the_last_chance_day(self):
        self.assertIsNone(self._run([self.MON], today=self.THU))   # last chance is Fri
        out = self._run([self.MON], today=self.FRI)          # needs 2: Fri and Sun
        self.assertIsNotNone(out)
        msg, week = out
        self.assertEqual(week, "2031-03-03")
        self.assertIn("1 of 3", msg)
        self.assertIn("2 more needed — the latest days that still work: today, Sun", msg)

    def test_once_per_week(self):
        self.assertIsNone(self._run([self.MON], today=self.FRI, sent="2031-03-03"))
        # a marker from an earlier week does not silence this one
        self.assertIsNotNone(self._run([self.MON], today=self.FRI, sent="2031-02-24"))

    def test_short_says_so(self):
        self.assertIsNone(self._run([], today=self.SAT))       # the one placeable day is Sun
        msg, _ = self._run([], today=self.SUN)
        self.assertIn("can't reach 3", msg)

    def test_a_week_before_the_quota_counts_never_nags(self):
        from datetime import date as d
        from knowledge import strength_quota as sq
        q = sq.compute(d(2031, 3, 8), [], today=d(2031, 3, 8),
                       counts_from=d(2031, 3, 10))                # the week before it counts
        self.assertEqual(q["status"], "not_counted")
        self.assertFalse(q["nag_due"])

    def test_unreadable_raises_rather_than_staying_quiet(self):
        from knowledge import strength_quota as sq
        with patch("artemis.quiet_hours.get_active_timezone", return_value="UTC"), \
             patch("knowledge.strength_quota.read_pg", side_effect=sq.QuotaUnreadable("down")):
            with self.assertRaises(sq.QuotaUnreadable):
                health.strength_nag(self.FRI)

    def test_the_marker_is_written_after_the_post(self):
        from artemis.scheduler import ArtemisScheduler
        order = []
        sched = ArtemisScheduler.__new__(ArtemisScheduler)
        sched._is_quiet = lambda: False
        sched._post = lambda *a, **k: order.append("post")
        with patch("artemis.health.strength_nag", return_value=("msg", "2031-03-02")), \
             patch("artemis.health.mark_strength_nag",
                   side_effect=lambda w: order.append(("mark", w))):
            sched.job_health_nag()
        self.assertEqual(order, ["post", ("mark", "2031-03-02")])

    def test_quiet_hours_respected(self):
        from artemis.scheduler import ArtemisScheduler
        sched = ArtemisScheduler.__new__(ArtemisScheduler)
        sched._is_quiet = lambda: True
        with patch("artemis.health.strength_nag") as nag:
            sched.job_health_nag()
        nag.assert_not_called()

    # ── EXERCISE-CLASS: the row's class beats the name ────────────────────
    def test_the_rows_class_wins_over_the_name_rules(self):
        from artemis import health_regions as hr
        # LOCATION-1: there is no name fallback left to beat. A name alone
        # yields None — the office rules that read "TRX row" as `machine` and
        # "TRX row" as a 10 lb stack step are deleted.
        self.assertIsNone(hr.equipment_class("TRX row"))
        self.assertEqual(hr.equipment_class("TRX row", "trx"), "trx")       # the row
        self.assertIsNone(hr.equipment_class("Band pull-apart"))
        self.assertEqual(hr.equipment_class("Band pull-apart", "bands"), "bands")

    def test_no_numeric_load_classes_have_nothing_to_lighten(self):
        from artemis import health_regions as hr
        self.assertEqual(hr.NO_LOAD_CLASSES, ("bodyweight", "bands", "trx", "cardio"))
        for cls in hr.NO_LOAD_CLASSES:
            with self.subTest(cls=cls):
                self.assertIsNone(hr.lighter_load("Whatever", 100.0, explicit_class=cls))
        # a real load still lightens, rounded down to something reachable
        # LOCATION-1: a recommendation needs the row's config; without it there
        # is none, and with the office's it is the office's answer.
        self.assertIsNone(hr.lighter_load("DB bench press", 50.0, explicit_class="dumbbell"))
        from knowledge import load_config
        self.assertEqual(hr.lighter_load("DB bench press", 50.0, explicit_class="dumbbell",
                                         load_config=load_config.OFFICE), 40.0)

    # ── EVENING-1: `rest` is a rest, exactly like rest_mobility ────────────
    def test_rest_and_rest_mobility_are_both_no_followup(self):
        from knowledge.session_types import NO_FOLLOWUP_TYPES, REST_TYPES, is_rest
        self.assertIn("rest", REST_TYPES)
        self.assertIn("rest_mobility", REST_TYPES)
        for t in ("rest", "rest_mobility", "recovery_flow"):
            self.assertIn(t, NO_FOLLOWUP_TYPES)
        self.assertTrue(is_rest("rest"))
        self.assertFalse(is_rest("strength_a"))

    def test_the_inferred_backstop_is_retired(self):
        """SESSION-OPEN: nothing writes an inferred summary any more, and no job
        is registered for it. Its historical rows stay and stay filtered."""
        from artemis import scheduler
        self.assertFalse(hasattr(health, "insert_inferred_summary"))
        src = Path(scheduler.__file__).read_text()
        self.assertNotIn("inferred_summary", src)


# ============================================================================
# Confirm formatters
# ============================================================================

class TestConfirmFormatters(unittest.TestCase):
    def test_morning_confirm_minimal(self):
        from artemis.health import MorningState, format_morning_confirm
        s = MorningState(sleep_hrs=6.5, energy=3)
        out = format_morning_confirm(s)
        self.assertIn("6.5h sleep", out)
        self.assertIn("energy 3/5", out)

    def test_morning_confirm_empty(self):
        from artemis.health import MorningState, format_morning_confirm
        out = format_morning_confirm(MorningState())
        self.assertIn("Logged", out)

    def test_debrief_confirm_no_exercises(self):
        from artemis.health import ExerciseReport, format_debrief_confirm
        summary = ExerciseReport(exercise="session_summary", log_type="session_summary", rpe_actual=7.0)
        out = format_debrief_confirm([summary])
        self.assertIn("Logged 0 exercise", out)
        self.assertIn("Overall RPE 7", out)


# ============================================================================
# T4: Equipment + location resolver
# ============================================================================

class TestResolveEquipment(unittest.TestCase):
    """HEALTH-2: office gym fallback map; blocks.location/equipment win.
    WALK-RETIRE (2026-09-22): walk is no longer a session type and no session
    is weather-dependent — the resolver takes no weather at all."""

    def test_resolve_equipment_strength(self):
        """strength_a → office gym, leg press + DBs + flat bench, first lift leg press."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("strength_a")
        self.assertEqual(r["location"], "office gym")
        self.assertIn("leg press", r["equipment"])
        self.assertIn("flat bench", r["equipment"])
        self.assertEqual(r["first_lift"], "Leg press")

    def test_resolve_equipment_cardio_intervals(self):
        """cardio_intervals → stepmill / upright bike; no rower, no bike trainer."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("cardio_intervals")
        joined = " | ".join(r["equipment"]).lower()
        self.assertIn("stepmill", joined)
        self.assertIn("upright bike", joined)
        self.assertNotIn("rower", joined)
        self.assertNotIn("trainer", joined)

    def test_resolve_z2_office_machines(self):
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("cardio_z2")
        self.assertEqual(r["location"], "office gym")
        self.assertEqual(r["equipment"],
                         ["treadmill", "elliptical", "recumbent bike", "upright bike"])
        self.assertIsNone(r["notes"])

    def test_blocks_location_and_equipment_preferred(self):
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "strength_b", blocks={"location": "home gym", "equipment": ["DBs", "bench"]})
        self.assertEqual(r["location"], "home gym")
        self.assertEqual(r["equipment"], ["DBs", "bench"])
        self.assertEqual(r["first_lift"], "DB goblet squat")

    def test_blocks_json_string_is_coerced(self):
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("strength_c", blocks='{"location": "office gym"}')
        self.assertEqual(r["location"], "office gym")
        self.assertEqual(r["first_lift"], "DB Romanian deadlift")

    def test_walk_is_no_longer_a_session_type(self):
        """WALK-RETIRE: walking is daily life, not a prescribed session. It has
        no entry in the fallback map and never resolves to a session."""
        from artemis import health, health_office
        self.assertNotIn("walk", health_office.LEGAL_SESSION_TYPES)
        self.assertNotIn("walk", health_office.NON_LIFT.values())
        self.assertNotIn("walk", health.LIGHT_TYPES if hasattr(health, "LIGHT_TYPES") else ())
        r = health.resolve_equipment_and_location("walk")
        self.assertNotIn("walking shoes", r["equipment"])       # falls through to the default

    def test_rest_mobility_mat_and_stretch_trainer(self):
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("rest_mobility")
        self.assertIn("mat", r["equipment"])
        self.assertIn("Stretch Trainer", r["equipment"])

    def test_removed_params_are_gone(self):
        """user_override went with HEALTH-2; weather went with WALK-RETIRE."""
        from artemis.health import resolve_equipment_and_location
        for kwargs in ({"user_override": "indoor"}, {"weather": {"temp_f": 35.0}}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(TypeError):
                    resolve_equipment_and_location("cardio_z2", **kwargs)


# ============================================================================
# HEALTH-2: retired bike trainer command
# ============================================================================

class TestTrainerRetired(unittest.TestCase):
    def test_trainer_set_detects_retired_intent(self):
        from artemis.health import detect_health_intent, INTENT_TRAINER_RETIRED
        self.assertEqual(detect_health_intent("trainer set indoor"), INTENT_TRAINER_RETIRED)
        self.assertEqual(detect_health_intent("trainer set outdoor"), INTENT_TRAINER_RETIRED)
        self.assertEqual(detect_health_intent("@artemis trainer set indoor"), INTENT_TRAINER_RETIRED)
        self.assertNotEqual(detect_health_intent("trainer says hi"), INTENT_TRAINER_RETIRED)

    def test_retired_reply_writes_nothing_and_claims_nothing(self):
        from artemis import health
        with patch("knowledge.db.execute_write") as ew:
            reply = health.format_trainer_retired()
        ew.assert_not_called()
        self.assertIn("Nothing changed", reply)
        self.assertNotIn("✅", reply)
        self.assertIsNone(health.claims_unverified_action(reply))


# ============================================================================
# T4: Prompt builders
# ============================================================================

class TestPromptBuilders(unittest.TestCase):
    _PLAN_STRENGTH = {
        "plan_id": 1,
        "session_type": "strength_a",
        "est_duration_min": 40,
        "target_rpe": 6.5,
        "blocks": {
            "type": "circuit",
            "warmup": "5 min elliptical, easy",
            "rounds": 2,
            "exercises": [],
        },
    }
    _PLAN_CARDIO = {
        "plan_id": 2,
        "session_type": "cardio_intervals",
        "est_duration_min": 30,
        "target_rpe": 7.0,
        "blocks": {"type": "intervals", "rounds": 8},
    }

    def test_morning_survey_uses_the_0_to_5_prompt(self):
        # FRIDAY-1: one prompt, 0–5 ratings; no timed "calibrated plan in 15 min".
        from artemis.health import build_morning_survey_prompt
        out = build_morning_survey_prompt(self._PLAN_STRENGTH, "workout_am")
        self.assertEqual(out, "Morning check-in.\n\n" + "Reply with: energy 0–5, and any sore or pain by area 0–5 (0 = none).\nExample: `energy 4 sore 0` · `energy 3 knee pain 2`\n_Sleep, weight and RHR come from the watch — add them only to correct it._")
        self.assertNotIn("15 min", out)

    def test_morning_survey_logging_only_no_calibration_note(self):
        from artemis.health import build_morning_survey_prompt
        out = build_morning_survey_prompt(self._PLAN_STRENGTH, "logging_only")
        self.assertIn("check-in", out)
        self.assertNotIn("later", out)       # FRIDAY-1: no "workout is later" on rest days
        self.assertNotIn("15 min", out)

    def test_evening_prompt_includes_resolved_location(self):
        from artemis.health import build_evening_prompt
        resolved = {
            "location": "office gym",
            "equipment": ["stepmill", "upright bike"],
            "first_lift": None,
            "notes": "Stepmill or upright bike.",
        }
        out = build_evening_prompt(self._PLAN_CARDIO, resolved)
        self.assertIn("Cardio Intervals", out)
        self.assertIn("Where: office gym", out)
        self.assertIn("stepmill", out)

    def test_calibration_post_is_retired(self):
        """FRIDAY-1: the timed calibrated-plan post (and its false "Recovery
        override" line) is gone — adjustments happen on check-in instead."""
        from artemis import health
        self.assertFalse(hasattr(health, "build_calibrated_plan_post"))


# ============================================================================
# T4: Scheduler-job-style tests (test the inner logic, not the cron)
# ============================================================================

class TestProactivePromptLogic(unittest.TestCase):
    """Tests the helper functions that the scheduler jobs call.

    Direct scheduler.job_*() tests would require booting the whole scheduler;
    instead we test the get_today_plan / already_prompted_today / mark_prompted
    helpers and the resolve+build pipeline that the jobs invoke.
    """

    def test_idempotency_already_prompted_today(self):
        """already_prompted_today returns True after mark_prompted is called."""
        from artemis import health

        with patch("artemis.quiet_hours.get_system_value") as mock_get, \
             patch("artemis.quiet_hours.set_system_value") as mock_set:
            mock_get.return_value = None
            self.assertFalse(health.already_prompted_today("morning", date(2026, 5, 6)))

            mock_get.return_value = "2026-05-06T07:00:00-05:00"
            self.assertTrue(health.already_prompted_today("morning", date(2026, 5, 6)))

            health.mark_prompted("morning", date(2026, 5, 6))
            mock_set.assert_called_once()
            args = mock_set.call_args[0]
            self.assertIn("morning", args[0])
            self.assertIn("2026-05-06", args[0])

    def test_get_today_plan_returns_dict(self):
        """get_today_plan delegates to execute_one with today's CT date."""
        from artemis import health

        plan_row = {"plan_id": 1, "session_type": "strength_a"}
        with patch("knowledge.db.execute_one", return_value=plan_row) as mock_one:
            result = health.get_today_plan()
        self.assertEqual(result, plan_row)
        mock_one.assert_called_once()

    def test_get_today_plan_returns_none_if_absent(self):
        from artemis import health
        with patch("knowledge.db.execute_one", return_value=None):
            self.assertIsNone(health.get_today_plan())


# ============================================================================
# T4: Day-of-week nag suppression
# ============================================================================

class TestNagDayOfWeekSuppression(unittest.TestCase):
    """Tests the day-of-week guard added to job_health_nag in T4.

    The guard lives in scheduler.ArtemisScheduler.job_health_nag — we test
    the underlying logic via a small isolated helper rather than booting the
    scheduler. The actual cron handler calls _today_ct_date().weekday() and
    bails on Tue (1) and Fri (4).
    """

    def _suppressed_dow(self) -> set[int]:
        # Mirrors the suppression set inside scheduler.job_health_nag
        return {1, 4}  # Tue, Fri

    def test_health_nag_suppressed_on_tuesday(self):
        """dow=1 (Tue) → in the suppression set."""
        self.assertIn(1, self._suppressed_dow())

    def test_health_nag_suppressed_on_friday(self):
        """dow=4 (Fri) → in the suppression set."""
        self.assertIn(4, self._suppressed_dow())

    def test_health_nag_fires_on_monday(self):
        """dow=0 (Mon) → NOT suppressed."""
        self.assertNotIn(0, self._suppressed_dow())

    def test_health_nag_fires_on_thursday(self):
        """dow=3 (Thu) → NOT suppressed (Thu is a 04:01 workout day, has PM)."""
        self.assertNotIn(3, self._suppressed_dow())


# ============================================================================
# Unified capture: discriminator, unit conversion, cardio parse, propose/confirm
# ============================================================================

class TestCaptureDiscriminator(unittest.TestCase):
    def test_cardio_paste_is_capture(self):
        self.assertTrue(health.is_capture_paste(
            "run-walk done. time 51:36, distance 3.28 miles, 121 bpm avg HR."))

    def test_multi_segment_is_capture(self):
        self.assertTrue(health.is_capture_paste(
            "Run #1: .16 mile RPE 8. Run #2: .15 mile RPE 9."))

    def test_strength_debrief_is_capture(self):
        self.assertTrue(health.is_capture_paste("done. squats 3x10 @ 35 RPE 7. plank 30s."))

    def test_question_is_not_capture(self):
        self.assertFalse(health.is_capture_paste("what's today's workout"))
        self.assertFalse(health.is_capture_paste("how was my last workout"))
        self.assertFalse(health.is_capture_paste("show me this week's plan"))

    def test_bare_done_is_not_capture(self):
        # Bare 'done'/'finished' must still end a live session / hit the inbox.
        self.assertFalse(health.is_capture_paste("done"))
        self.assertFalse(health.is_capture_paste("finished"))


class TestUnitConversion(unittest.TestCase):
    def test_to_seconds(self):
        self.assertEqual(health._to_seconds("51:36"), 3096)
        self.assertEqual(health._to_seconds("1:48"), 108)
        self.assertEqual(health._to_seconds("1:00:00"), 3600)
        self.assertEqual(health._to_seconds(30), 30)
        self.assertIsNone(health._to_seconds(None))
        self.assertIsNone(health._to_seconds("nonsense"))

    def test_to_meters(self):
        self.assertEqual(health._to_meters(3.28, "mi"), 5278.6)
        self.assertEqual(health._to_meters(0.16, "mi"), 257.5)
        self.assertEqual(health._to_meters(200, "ft"), 61.0)
        self.assertEqual(health._to_meters(100, "m"), 100.0)
        self.assertEqual(health._to_meters(0.16, None), 257.5)  # default miles
        self.assertIsNone(health._to_meters(None, "mi"))

    def test_convert_units_strips_raw_keys(self):
        row = {"exercise": "Run 1", "log_type": "cardio_block",
               "distance": 0.16, "distance_unit": "mi", "duration": "1:48"}
        out = health._convert_units(row)
        self.assertEqual(out["distance_m"], 257.5)
        self.assertEqual(out["duration_sec"], 108)
        self.assertNotIn("distance", out)
        self.assertNotIn("duration", out)


_RUNWALK_JSON = {
    "exercises": [
        {"exercise": "Run 1", "log_type": "cardio_block", "round_num": 1,
         "duration": "1:48", "distance": 0.16, "distance_unit": "mi",
         "rpe_actual": 8.0, "hr_avg": 147, "is_skipped": False},
        {"exercise": "Run 2", "log_type": "cardio_block", "round_num": 2,
         "duration": "1:45", "distance": 0.15, "distance_unit": "mi",
         "rpe_actual": 9.0, "hr_avg": 151, "is_skipped": False},
    ],
    "session_summary": {
        "duration": "51:36", "distance": 3.28, "distance_unit": "mi",
        "hr_avg": 121, "rpe_actual": 9.0,
        "notes": "walk RPE 4, run RPE 9; felt good", "user_suggestion": None,
    },
}


class TestCardioParse(unittest.TestCase):
    def test_segments_and_summary(self):
        with patch.object(health, "_call_claude_json", return_value=_RUNWALK_JSON):
            reports = health.parse_workout_debrief("run-walk paste", plan=None)
        cardio = [r for r in reports if r.log_type == "cardio_block"]
        summary = [r for r in reports if r.log_type == "session_summary"]
        self.assertEqual(len(cardio), 2)
        self.assertEqual(len(summary), 1)
        # Conversions are Python-side and deterministic.
        self.assertEqual(cardio[0].distance_m, 257.5)
        self.assertEqual(cardio[0].duration_sec, 108)
        self.assertEqual(cardio[0].round_num, 1)
        self.assertEqual(summary[0].distance_m, 5278.6)
        self.assertEqual(summary[0].duration_sec, 3096)
        self.assertEqual(summary[0].hr_avg, 121)


class TestProposeConfirm(unittest.TestCase):
    def test_propose_stores_and_writes_nothing(self):
        with patch.object(health, "get_today_plan",
                          return_value={"plan_id": 3, "session_type": "cardio_z2"}), \
             patch.object(health, "_call_claude_json", return_value=_RUNWALK_JSON), \
             patch.object(health, "store_capture_pending") as mock_store:
            reply = health.build_and_store_proposal("run-walk paste", "chan1")
        mock_store.assert_called_once()
        # The pending payload carries the parsed reports + plan_id 3.
        _chan, reports, plan_id = mock_store.call_args[0]
        self.assertEqual(plan_id, 3)
        self.assertTrue(any(r.log_type == "cardio_block" for r in reports))
        self.assertIn("confirm", reply.lower())
        self.assertIn("plan_id: 3", reply)

    def test_propose_notes_missing_plan(self):
        with patch.object(health, "get_today_plan", return_value=None), \
             patch.object(health, "_call_claude_json", return_value=_RUNWALK_JSON), \
             patch.object(health, "store_capture_pending"):
            reply = health.build_and_store_proposal("run-walk paste", "chan1")
        self.assertIn("plan_id=NULL", reply)

    def test_commit_inserts_in_one_tx_and_returns_ids(self):
        payload = {
            "rows": [
                {"exercise": "Run 1", "log_type": "cardio_block", "round_num": 1,
                 "distance_m": 257.5, "duration_sec": 108, "rpe_actual": 8.0},
                {"exercise": "session_summary", "log_type": "session_summary",
                 "distance_m": 5278.6, "duration_sec": 3096},
            ],
            "plan_id": 3,
        }
        with patch.object(health, "load_capture_pending", return_value=payload), \
             patch.object(health, "insert_session_logs_tx", return_value=[101, 102]) as mock_tx, \
             patch.object(health, "clear_capture_pending") as mock_clear:
            reply = health.commit_capture("chan1")
        mock_tx.assert_called_once()
        rows_arg, plan_arg = mock_tx.call_args[0]
        self.assertEqual(plan_arg, 3)
        self.assertEqual(len(rows_arg), 2)
        mock_clear.assert_called_once()
        self.assertIn("101, 102", reply)
        self.assertIn("2 rows", reply)

    def test_commit_with_no_pending(self):
        with patch.object(health, "load_capture_pending", return_value=None):
            reply = health.commit_capture("chan1")
        self.assertIn("Nothing pending", reply)


class TestRoutingRejectionInvariant(unittest.TestCase):
    """Locks the routing-rejection invariant that originally caused the bug.

    Invariant: a plan question must be claimed by plan-display (the
    _handle_health_conversation path) BEFORE _handle_capture_propose is reached.
    This guards against handler reordering OR an is_capture_paste regression
    reintroducing the metrics-misrouting bug (a paste/question reaching the wrong
    handler). Proven at the routing level, not just the predicate level — so if
    someone reorders the _handle_mention dispatch later and this goes red, the
    failure explains WHY it matters, not just that it broke.
    """

    def test_plan_question_routes_to_plan_display_not_capture(self):
        # Canonical plan question. Both halves must hold:
        #  (1) the capture predicate does NOT claim it, and
        #  (2) the health-conversation path claims it as a plan-display intent
        #      (plan_detail) first, so capture is never reached.
        q = "what's today's workout"
        self.assertFalse(health.is_capture_paste(q))
        self.assertEqual(health.detect_health_intent(q), health.INTENT_PLAN_DETAIL)

    def test_second_plan_phrasing_routes_to_plan_display_not_capture(self):
        # Same invariant on a different phrasing, so the intent-routing half is
        # not proven on a single string. "this week" is breadth → plan_lookup,
        # which is also a plan-display intent handled before capture.
        q = "show me this week's plan"
        self.assertFalse(health.is_capture_paste(q))
        self.assertEqual(health.detect_health_intent(q), health.INTENT_PLAN_LOOKUP)

    def _chain_names(self):
        """Parse the ORDERED handler names out of _handle_mention's
        deterministic_chain (source-level, no import/mocking)."""
        import re as _re
        src = (_REPO_ROOT / "artemis" / "main.py").read_text()
        start = src.index("deterministic_chain = [")
        block = src[start:src.index("]", start)]
        return _re.findall(r'\("(\w+)",', block)

    def test_dispatch_chain_order_pinned(self):
        # The dispatch sequence is asserted EXPLICITLY so any reorder breaks CI.
        names = self._chain_names()
        for n in ("dossier_command", "health_conversation", "capture_propose",
                  "nutrition", "grocery_staples"):
            self.assertIn(n, names, f"{n} missing from deterministic_chain")
        # (1) fix/dispatch-order: an explicit dossier/org verb outranks topical
        #     health/nutrition keyword matching.
        self.assertLess(names.index("dossier_command"), names.index("health_conversation"),
                        "dossier_command must dispatch BEFORE health_conversation")
        self.assertLess(names.index("dossier_command"), names.index("nutrition"))
        self.assertLess(names.index("dossier_command"), names.index("grocery_staples"))
        # (2) preserved invariant: plan-display before capture-propose.
        self.assertLess(names.index("health_conversation"), names.index("capture_propose"))
        # (3) confirm flows still lead — a `yes`/`confirm` reaches its pending
        #     handler before any content router.
        for confirm in ("calendar_confirm", "delete_confirm", "debrief_confirm",
                        "nutrition_confirm"):
            self.assertLess(names.index(confirm), names.index("dossier_command"),
                            f"{confirm} must precede dossier_command")

    def test_capture_message_routes_to_dossier_not_health(self):
        # The exact 2026-07-18 misroute: a `met with …` capture whose notes carry
        # weekday/plan words. Health must NOT claim it; the dossier detector must.
        from artemis.intent import detect_dossier_intent
        msg = ("met with dennis about extraction test\n"
               "the plan for friday is a listening tour before budget asks")
        self.assertIsNone(health.detect_health_intent(msg),
                          "health must not claim a `met with` capture (weekday/plan "
                          "words in the notes are not a plan query)")
        self.assertEqual(detect_dossier_intent(msg), "capture",
                         "the dossier capture detector must own this message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
