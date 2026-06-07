"""Tests for the PB-009 plan_lookup intent + get_plan_lookup() handler.

Focus of the routing-bug fix:
  * detect_health_intent() classifies plan-lookup phrasings as 'plan_lookup'
    and does NOT misclassify debrief / morning / chatter.
  * get_plan_lookup() anchors date math to America/Chicago.
  * HARD GUARD: an empty DB result yields exactly "No plan seeded for <date>."
    and the plan_lookup branch never invokes the LLM general_reply path.
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from artemis import health
from artemis.health import (
    INTENT_PLAN_LOOKUP,
    detect_health_intent,
    get_plan_lookup,
)

CT = ZoneInfo("America/Chicago")


class TestPlanLookupDetection(unittest.TestCase):
    def test_plan_lookup_phrasings(self):
        for msg in [
            "what's tomorrow's workout",
            "next 7 days",
            "this week's workouts",
            "what's my workout monday",
            "show my plan",
            "what's saturday's workout",
        ]:
            self.assertEqual(detect_health_intent(msg), INTENT_PLAN_LOOKUP, msg)

    def test_does_not_swallow_debrief_or_morning(self):
        self.assertEqual(detect_health_intent("done"), "log_workout_debrief")
        self.assertEqual(detect_health_intent("workout done"), "log_workout_debrief")
        self.assertEqual(detect_health_intent("squats RPE 7, plank 30s"), "log_workout_debrief")
        self.assertEqual(detect_health_intent("morning"), "log_morning_state")
        self.assertEqual(detect_health_intent("slept 7 hours"), "log_morning_state")
        self.assertIsNone(detect_health_intent("what's the weather"))
        self.assertIsNone(detect_health_intent("hello"))


class TestGetPlanLookupHardGuard(unittest.TestCase):
    def test_empty_result_returns_exact_no_plan_string(self):
        """A requested date with no DB row → exactly 'No plan seeded for <date>.'"""
        today = datetime.now(CT).date()
        tomorrow = today + timedelta(days=1)
        with mock.patch("knowledge.db.execute_one", return_value=None) as m:
            reply = get_plan_lookup("what's tomorrow's workout")
        self.assertTrue(m.called, "should have queried the DB")
        self.assertEqual(reply, f"No plan seeded for {tomorrow.isoformat()}.")

    def test_plan_lookup_empty_result_does_not_call_general_reply(self):
        """The plan_lookup branch must short-circuit BEFORE the LLM path.

        Mirrors the main.py wiring: when detect_health_intent == 'plan_lookup',
        get_plan_lookup() handles it and the general_reply/LLM router is never
        invoked — even when the result is empty.
        """
        msg = "what's tomorrow's workout"
        general_reply = mock.MagicMock(return_value="LLM ANSWER (should never run)")

        with mock.patch("knowledge.db.execute_one", return_value=None):
            # Exact precedence from artemis/main.py:_handle_health_conversation
            if detect_health_intent(msg) == INTENT_PLAN_LOOKUP:
                reply = get_plan_lookup(msg)
            else:
                reply = general_reply(msg)

        general_reply.assert_not_called()
        self.assertTrue(reply.startswith("No plan seeded for "))

    def test_route_intent_llm_not_imported_or_called(self):
        """get_plan_lookup itself must never reach the LLM intent router."""
        with mock.patch("artemis.intent.route_intent") as route, \
             mock.patch("knowledge.db.execute_one", return_value=None):
            get_plan_lookup("show my plan")
        route.assert_not_called()


class TestGetPlanLookupFormatting(unittest.TestCase):
    def test_real_exercise_names_from_blocks(self):
        today = datetime.now(CT).date()
        row = {
            "plan_date": today,
            "session_type": "strength_a",
            "target_rpe": 7.5,
            "est_duration_min": 40,
            "is_skipped": False,
            "blocks": {
                "type": "circuit",
                "exercises": [
                    {"name": "Goblet squat", "format": "reps", "target_reps": 12},
                    {"name": "DB floor press", "format": "reps", "target_reps": 12},
                ],
                "finisher": {
                    "type": "core_circuit",
                    "exercises": [{"name": "Dead bug", "format": "reps", "target_reps": 10}],
                },
            },
        }
        with mock.patch("knowledge.db.execute_one", return_value=row):
            reply = get_plan_lookup("what's today's workout")
        self.assertIn("Goblet squat", reply)
        self.assertIn("DB floor press", reply)
        self.assertIn("finisher: Dead bug", reply)
        # Never the no-plan guard when a row exists.
        self.assertNotIn("No plan seeded", reply)


if __name__ == "__main__":
    unittest.main()
