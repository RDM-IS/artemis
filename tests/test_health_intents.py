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


if __name__ == "__main__":
    unittest.main(verbosity=2)
