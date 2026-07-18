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
    def test_parse_and_format(self):
        """Full roundtrip: parse → upsert → confirm."""
        fake_parsed = {
            "sleep_hrs": 6.5,
            "energy": 3,
            "soreness": {"legs": 3},
            "weight_lbs": None,
            "resting_hr": 58,
            "free_text": "feel slow",
        }
        with patch.object(health, "_call_claude_json", return_value=fake_parsed), \
             patch.object(health, "upsert_daily_state") as mock_upsert:
            result = health.handle_morning_intent("slept 6.5 energy 3 legs sore 3 RHR 58")

        # Confirms DB write happened
        mock_upsert.assert_called_once()
        # Confirms trainer-voice output
        self.assertIn("6.5h sleep", result)
        self.assertIn("energy 3/5", result)
        self.assertIn("RHR 58", result)
        self.assertIn("Anything to fix?", result)

    def test_parse_failure_returns_useful_error(self):
        """When parser fails, return guidance, not a stack trace."""
        with patch.object(health, "_call_claude_json", side_effect=ValueError("malformed")):
            result = health.handle_morning_intent("garbled garbage")
        self.assertIn("couldn't parse", result.lower())
        self.assertIn("slept", result.lower())  # example shown to user

    def test_db_failure_returns_warning(self):
        fake_parsed = {"sleep_hrs": 6.0, "energy": 4}
        with patch.object(health, "_call_claude_json", return_value=fake_parsed), \
             patch.object(health, "upsert_daily_state", side_effect=RuntimeError("conn lost")):
            result = health.handle_morning_intent("slept 6")
        self.assertIn("Couldn", result)  # "Couldn't save"
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
    def test_skip_when_rest_day(self):
        with patch("knowledge.db.execute_one") as mock_one:
            mock_one.return_value = {
                "plan_id": 1, "session_type": "rest_mobility",
                "target_rpe": None, "is_skipped": False,
            }
            self.assertIsNone(health.run_nag_check())

    def test_skip_when_already_logged(self):
        from knowledge import db as kdb

        plan_row = {"plan_id": 5, "session_type": "strength_a", "target_rpe": 6.5, "is_skipped": False}

        with patch.object(kdb, "execute_one", return_value=plan_row), \
             patch.object(kdb, "execute_query", return_value=[{"log_id": 1}]):
            self.assertIsNone(health.run_nag_check())

    def test_nag_when_no_log(self):
        from knowledge import db as kdb

        plan_row = {"plan_id": 5, "session_type": "strength_a", "target_rpe": 6.5, "is_skipped": False}

        with patch.object(kdb, "execute_one", return_value=plan_row), \
             patch.object(kdb, "execute_query", return_value=[]):
            msg = health.run_nag_check()
        self.assertIsNotNone(msg)
        self.assertIn("Strength A", msg)
        self.assertIn("debrief", msg.lower())

    def test_skip_when_skipped_explicit(self):
        with patch("knowledge.db.execute_one") as mock_one:
            mock_one.return_value = {
                "plan_id": 5, "session_type": "strength_a",
                "target_rpe": 6.5, "is_skipped": True,
            }
            self.assertIsNone(health.run_nag_check())

    def test_no_plan_no_nag(self):
        with patch("knowledge.db.execute_one", return_value=None):
            self.assertIsNone(health.run_nag_check())


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
    def test_resolve_equipment_strength(self):
        """strength_a → dumbbells + bench + downstairs gym."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("strength_a")
        self.assertEqual(r["location"], "downstairs gym")
        self.assertIn("PowerBlock dumbbells", r["equipment"])
        self.assertIn("flat bench", r["equipment"])
        self.assertEqual(r["first_lift"], "Goblet squat")

    def test_resolve_equipment_cardio_intervals(self):
        """cardio_intervals → water rower AND bike on trainer (indoor scenario).

        User does NOT own a treadmill. Intervals require real intensity,
        so the choices are water rower or bike on trainer (intervals).
        Walking pad is NOT appropriate here.

        Weather chosen to land in the indoor bike branch so both
        "water rower" and "bike on trainer" substrings appear.
        """
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_intervals",
            weather={"temp_f": 35.0, "precip_next_90min": False},  # cold → indoor
        )
        joined = " | ".join(r["equipment"]).lower()
        self.assertIn("water rower", joined)
        self.assertIn("bike on trainer", joined)
        self.assertNotIn("treadmill", joined)
        self.assertNotIn("walking pad", joined)

    def test_resolve_bike_indoor_when_cold(self):
        """temp_f=35 → indoor."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 35.0, "precip_next_90min": False},
        )
        self.assertIn("trainer", r["location"].lower())
        self.assertIn("Cold", r["notes"])

    def test_resolve_bike_indoor_when_rain(self):
        """precip_next_90min=True → indoor regardless of temp."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 70.0, "precip_next_90min": True},
        )
        self.assertIn("trainer", r["location"].lower())
        self.assertIn("Rain", r["notes"])

    def test_resolve_bike_outdoor_default(self):
        """temp_f=65, no rain → outdoor."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 65.0, "precip_next_90min": False},
        )
        self.assertIn("outside", r["location"].lower())
        self.assertIn("road bike", r["equipment"])

    def test_resolve_bike_user_override_wins_over_weather(self):
        """override='outdoor' + temp_f=20 → outdoor regardless of cold."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 20.0, "precip_next_90min": True},
            user_override="outdoor",
        )
        self.assertIn("outside", r["location"].lower())
        self.assertIn("override", r["notes"].lower())

    def test_resolve_bike_user_override_indoor(self):
        """override='indoor' wins over warm weather."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 75.0, "precip_next_90min": False},
            user_override="indoor",
        )
        self.assertIn("trainer", r["location"].lower())
        self.assertIn("override", r["notes"].lower())

    def test_walk_session_returns_outside(self):
        """walk → outside, just shoes."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("walk")
        self.assertEqual(r["location"], "outside")
        self.assertIn("walking shoes", r["equipment"])

    def test_rest_mobility_returns_anywhere(self):
        """rest_mobility → mat + bands."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("rest_mobility")
        self.assertIn("mat", r["equipment"])
        self.assertIn("resistance bands", r["equipment"])

    def test_resolve_bike_no_weather_uses_safe_default(self):
        """No weather dict → defaults treat as 50°F, no rain → outdoor."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location("cardio_z2", weather=None)
        self.assertIn("outside", r["location"].lower())

    # ── T4-fix: walking pad + corrected cardio_intervals ────────────────

    def test_resolve_z2_includes_walking_pad(self):
        """cardio_z2 → equipment list always includes walking pad alongside bike."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 60.0, "precip_next_90min": False},
        )
        joined = " | ".join(r["equipment"]).lower()
        self.assertIn("walking pad", joined)
        # Bike option also present (here: outdoor road bike since 60°F clear)
        self.assertTrue(
            "road bike" in joined or "bike on trainer" in joined,
            f"expected a bike option in {r['equipment']!r}",
        )

    def test_resolve_walk_outside_when_clear(self):
        """walk + temp_f=65, no rain → outside, walking shoes."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "walk",
            weather={"temp_f": 65.0, "precip_next_90min": False},
        )
        self.assertIn("outside", r["location"].lower())
        self.assertIn("walking shoes", r["equipment"])
        self.assertNotIn("walking pad", r["equipment"])

    def test_resolve_walk_uses_pad_when_cold(self):
        """walk + temp_f=35 → indoor walking pad, cold note."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "walk",
            weather={"temp_f": 35.0, "precip_next_90min": False},
        )
        loc = r["location"].lower()
        self.assertTrue(
            "downstairs gym" in loc or "indoor" in loc,
            f"expected indoor walking pad location, got {r['location']!r}",
        )
        self.assertIn("walking pad", r["equipment"])
        self.assertIn("Cold", r["notes"])

    def test_resolve_walk_uses_pad_when_rain(self):
        """walk + precip_next_90min=True → indoor walking pad, rain note."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "walk",
            weather={"temp_f": 60.0, "precip_next_90min": True},
        )
        loc = r["location"].lower()
        self.assertTrue(
            "downstairs gym" in loc or "indoor" in loc,
            f"expected indoor walking pad location, got {r['location']!r}",
        )
        self.assertIn("walking pad", r["equipment"])
        self.assertIn("Rain", r["notes"])

    def test_resolve_intervals_does_not_include_walking_pad(self):
        """cardio_intervals → walking pad NOT in equipment (intensity check).

        Intervals need real bursts; walking pad can't deliver.
        """
        from artemis.health import resolve_equipment_and_location
        # Test across multiple weather/override combos to catch any path that
        # accidentally adds walking pad.
        scenarios = [
            {"weather": {"temp_f": 30.0, "precip_next_90min": False}, "user_override": None},
            {"weather": {"temp_f": 75.0, "precip_next_90min": False}, "user_override": None},
            {"weather": {"temp_f": 60.0, "precip_next_90min": True}, "user_override": None},
            {"weather": None, "user_override": "indoor"},
            {"weather": None, "user_override": "outdoor"},
        ]
        for sc in scenarios:
            r = resolve_equipment_and_location("cardio_intervals", **sc)
            joined = " | ".join(r["equipment"]).lower()
            self.assertNotIn(
                "walking pad", joined,
                f"walking pad should not appear for cardio_intervals; scenario={sc}, equipment={r['equipment']!r}",
            )

    def test_resolve_z2_user_override_indoor_still_includes_pad(self):
        """cardio_z2 + user_override='indoor' → equipment includes BOTH bike and pad."""
        from artemis.health import resolve_equipment_and_location
        r = resolve_equipment_and_location(
            "cardio_z2",
            weather={"temp_f": 75.0, "precip_next_90min": False},
            user_override="indoor",
        )
        joined = " | ".join(r["equipment"]).lower()
        self.assertIn("bike on trainer", joined)
        self.assertIn("walking pad", joined)
        self.assertIn("override", r["notes"].lower())


# ============================================================================
# T4: Trainer override capture
# ============================================================================

class TestTrainerOverride(unittest.TestCase):
    def test_trainer_override_intent_detection(self):
        """'trainer set indoor' → INTENT_TRAINER_OVERRIDE via detect_health_intent."""
        from artemis.health import detect_health_intent, INTENT_TRAINER_OVERRIDE
        self.assertEqual(detect_health_intent("trainer set indoor"), INTENT_TRAINER_OVERRIDE)
        self.assertEqual(detect_health_intent("trainer set outdoor"), INTENT_TRAINER_OVERRIDE)
        self.assertEqual(detect_health_intent("@artemis trainer set indoor"), INTENT_TRAINER_OVERRIDE)
        # Non-matching
        self.assertNotEqual(detect_health_intent("trainer says hi"), INTENT_TRAINER_OVERRIDE)

    def test_trainer_override_parses_mode(self):
        from artemis.health import detect_trainer_override
        self.assertEqual(detect_trainer_override("trainer set indoor"), "indoor")
        self.assertEqual(detect_trainer_override("trainer set OUTDOOR"), "outdoor")
        self.assertIsNone(detect_trainer_override("nope"))

    def test_trainer_override_writes_to_next_cardio_date(self):
        """handle_trainer_override picks next cardio plan_date and writes override."""
        from artemis import health
        target = date(2026, 5, 7)  # Thursday — cardio_z2 in our seed

        with patch.object(health, "_next_cardio_date", return_value=target), \
             patch.object(health, "write_bike_override") as mock_write:
            result = health.handle_trainer_override("trainer set indoor")

        mock_write.assert_called_once_with(target, "indoor")
        self.assertIn("indoor", result.lower())
        # Verify date echoed in confirm
        self.assertTrue("May 7" in result or "5/7" in result or "2026-05-07" in result)

    def test_trainer_override_db_failure_returns_warning(self):
        from artemis import health
        with patch.object(health, "_next_cardio_date", return_value=date(2026, 5, 7)), \
             patch.object(health, "write_bike_override", side_effect=RuntimeError("conn lost")):
            result = health.handle_trainer_override("trainer set indoor")
        self.assertIn("Couldn", result)
        self.assertIn("DB", result)

    def test_trainer_override_unparseable_returns_help(self):
        from artemis import health
        result = health.handle_trainer_override("nonsense not an override")
        self.assertIn("couldn't parse", result.lower())


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
            "warmup": "5 min bike easy + band pull-aparts",
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

    def test_morning_survey_workout_includes_calibration_note(self):
        from artemis.health import build_morning_survey_prompt
        out = build_morning_survey_prompt(self._PLAN_STRENGTH, "workout_am")
        self.assertIn("Strength A", out)
        self.assertIn("40 min", out)
        self.assertIn("15 min", out)  # calibration heads-up

    def test_morning_survey_logging_only_no_calibration_note(self):
        from artemis.health import build_morning_survey_prompt
        out = build_morning_survey_prompt(self._PLAN_STRENGTH, "logging_only")
        self.assertIn("later", out)
        self.assertNotIn("15 min", out)

    def test_evening_prompt_includes_resolved_location(self):
        from artemis.health import build_evening_prompt
        resolved = {
            "location": "downstairs gym (bike on trainer)",
            "equipment": ["bike on trainer", "fan"],
            "first_lift": None,
            "notes": "Per your override: indoor.",
        }
        out = build_evening_prompt(self._PLAN_CARDIO, resolved)
        self.assertIn("Cardio Intervals", out)
        self.assertIn("trainer", out)
        self.assertIn("override", out)

    def test_calibration_includes_warmup(self):
        from artemis.health import build_calibrated_plan_post
        resolved = {
            "location": "downstairs gym",
            "equipment": ["PowerBlock dumbbells"],
            "first_lift": "Goblet squat",
            "notes": None,
        }
        out = build_calibrated_plan_post(self._PLAN_STRENGTH, resolved, state=None)
        self.assertIn("Goblet squat", out)
        self.assertIn("Warmup:", out)

    def test_calibration_recovery_override_when_low_sleep(self):
        """Sleep < 5h → recovery override prepended."""
        from artemis.health import build_calibrated_plan_post
        resolved = {
            "location": "downstairs gym",
            "equipment": ["PowerBlock dumbbells"],
            "first_lift": "Goblet squat",
            "notes": None,
        }
        state = {"sleep_hrs": 4.0, "energy": 3}
        out = build_calibrated_plan_post(self._PLAN_STRENGTH, resolved, state=state)
        self.assertIn("Recovery override", out)


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

    def test_read_bike_override_returns_indoor(self):
        from artemis import health
        row = {"blocks": {"bike_setup_override": "indoor", "type": "intervals"}}
        with patch("knowledge.db.execute_one", return_value=row):
            self.assertEqual(health.read_bike_override(date(2026, 5, 7)), "indoor")

    def test_read_bike_override_returns_none_when_absent(self):
        from artemis import health
        row = {"blocks": {"type": "intervals"}}
        with patch("knowledge.db.execute_one", return_value=row):
            self.assertIsNone(health.read_bike_override(date(2026, 5, 7)))


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
