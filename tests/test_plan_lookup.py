"""Tests for the PB-009 plan-read intents + handlers.

Covers two read-intents:
  * plan_lookup  — multi-day BREADTH ("next 7 days", "this week"); get_plan_lookup().
  * plan_detail  — single-day DEPTH ("detailed explanation of today's workout",
                   bare "today's workout"); get_plan_detail() renders the full block
                   and appends a constrained trainer-voice coaching note.

Shared guarantees:
  * date math anchored to America/Chicago.
  * HARD GUARD: an empty DB result yields exactly "No plan seeded for <date>."
    and NEITHER branch ever invokes the LLM (coaching or general_reply).
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from artemis import health
from artemis.health import (
    INTENT_PLAN_DETAIL,
    INTENT_PLAN_LOOKUP,
    detect_health_intent,
    get_plan_detail,
    get_plan_lookup,
)

CT = ZoneInfo("America/Chicago")


def _route(msg, general_reply):
    """Mirror artemis/main.py:_handle_health_conversation precedence exactly."""
    intent = detect_health_intent(msg)
    if intent == INTENT_PLAN_DETAIL:
        return get_plan_detail(msg)
    if intent == INTENT_PLAN_LOOKUP:
        return get_plan_lookup(msg)
    return general_reply(msg)


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class TestIntentClassification(unittest.TestCase):
    def test_plan_detail_phrasings(self):
        for msg in [
            "detailed explanation of today's workout",
            "what's the full workout",
            "break down today",
            "explain today's session",
            "give me the details for monday",
            "today's workout",          # bare single-day defaults to depth
            "what's tomorrow's workout",
            "what's my workout monday",
        ]:
            self.assertEqual(detect_health_intent(msg), INTENT_PLAN_DETAIL, msg)

    def test_plan_detail_meta_and_database_phrasings(self):
        # Detail follow-ups + meta/database asks must route to plan_detail,
        # NEVER fall through to general_reply (the confabulation source).
        for msg in [
            "tell me more about tomorrow's workout",
            "more detail on today's session",
            "expand on monday's workout",
            "full breakdown of today's workout",
            "what exactly is the cardio session",
            # meta / database flavoured
            "query the workout database",
            "deep query the workout database",
            "what's in the plan table",
            "pull from the database",
            "what data do you have on my training",
        ]:
            self.assertEqual(detect_health_intent(msg), INTENT_PLAN_DETAIL, msg)

    def test_plan_lookup_phrasings(self):
        # Multi-day BREADTH stays plan_lookup.
        for msg in ["next 7 days", "this week's workouts", "show my plan",
                    "what's my plan for the next 5 days"]:
            self.assertEqual(detect_health_intent(msg), INTENT_PLAN_LOOKUP, msg)

    def test_does_not_hijack_non_workout_topics(self):
        # Strong-fitness fallback must not grab calendar / CRM / scheduling.
        self.assertIsNone(detect_health_intent("set up a meeting with greg"))
        self.assertIsNone(detect_health_intent("query the crm database"))
        self.assertIsNone(detect_health_intent("what's on my calendar"))

    def test_does_not_swallow_debrief_or_morning(self):
        self.assertEqual(detect_health_intent("done"), "log_workout_debrief")
        self.assertEqual(detect_health_intent("workout done"), "log_workout_debrief")
        self.assertEqual(detect_health_intent("squats RPE 7, plank 30s"), "log_workout_debrief")
        self.assertEqual(detect_health_intent("morning"), "log_morning_state")
        self.assertEqual(detect_health_intent("slept 7 hours"), "log_morning_state")
        self.assertIsNone(detect_health_intent("what's the weather"))
        self.assertIsNone(detect_health_intent("hello"))


# ---------------------------------------------------------------------------
# Hard guard — empty data, no LLM, no general_reply (both handlers)
# ---------------------------------------------------------------------------

class TestHardGuard(unittest.TestCase):
    def test_lookup_empty_returns_exact_no_plan_string(self):
        # Single-date lookup path (no header) → exact guard string.
        today = datetime.now(CT).date()
        tomorrow = today + timedelta(days=1)
        with mock.patch("knowledge.db.execute_one", return_value=None) as m:
            reply = get_plan_lookup("tomorrow")
        self.assertTrue(m.called)
        self.assertEqual(reply, f"No plan seeded for {tomorrow.isoformat()}.")

    def test_detail_empty_returns_exact_no_plan_string_and_no_llm(self):
        today = datetime.now(CT).date()
        with mock.patch("knowledge.db.execute_one", return_value=None), \
             mock.patch.object(health, "_call_claude_text") as llm:
            reply = get_plan_detail("explain today's workout")
        self.assertEqual(reply, f"No plan seeded for {today.isoformat()}.")
        llm.assert_not_called()  # never reach the coaching LLM on empty data

    def test_routing_never_calls_general_reply_on_empty(self):
        """Both plan_detail and plan_lookup short-circuit BEFORE general_reply."""
        general_reply = mock.MagicMock(return_value="LLM (should never run)")
        with mock.patch("knowledge.db.execute_one", return_value=None), \
             mock.patch.object(health, "_call_claude_text", side_effect=AssertionError("no LLM")):
            r1 = _route("detailed explanation of today's workout", general_reply)  # plan_detail
            r2 = _route("show my plan", general_reply)                              # plan_lookup
        general_reply.assert_not_called()
        self.assertTrue(r1.startswith("No plan seeded for "))   # single-day, no header
        self.assertIn("No plan seeded for ", r2)                 # multi-day list w/ header


# ---------------------------------------------------------------------------
# get_plan_detail — full block render (strength + cardio)
# ---------------------------------------------------------------------------

_STRENGTH_ROW = {
    "plan_date": None, "session_type": "strength_a",
    "target_rpe": 7.5, "target_hr_zone": 3, "est_duration_min": 40, "is_skipped": False,
    "blocks": {
        "type": "circuit", "display_name": "Strength — Push/Quad", "rounds": 3,
        "warmup": "5 min easy bike + band pull-aparts",
        "cooldown": "5 min easy spin + stretch", "rest_between_rounds_sec": 120,
        "equipment": ["PowerBlocks 25-35lb"],
        "exercises": [
            {"name": "Goblet squat", "format": "reps", "target_reps": 12,
             "target_load_lbs": 30, "rest_after_sec": 60},
            {"name": "Reverse lunge", "format": "reps", "target_reps": 10,
             "rest_after_sec": 60, "notes": "10 each side"},
        ],
        "finisher": {"type": "core_circuit", "rounds": 2, "exercises": [
            {"name": "Side plank", "format": "duration", "duration_sec": 20, "notes": "20s each side"},
        ]},
    },
}

_CARDIO_ROW = {
    "plan_date": None, "session_type": "cardio_z2",
    "target_rpe": 4.5, "target_hr_zone": 2, "est_duration_min": 55, "is_skipped": False,
    "blocks": {
        "type": "steady", "display_name": "Long Z2 Bike", "duration_min": 55,
        "target_range_min": [45, 55], "intensity": "Zone 2",
        "warmup_sec": 300, "cooldown_sec": 300,
        "equipment": ["road bike + indoor trainer"],
        "setup_notes": ["Steady 45-55 min Zone 2"],
        "finisher": {"type": "core_circuit", "rounds": 2, "exercises": [
            {"name": "Dead bug", "format": "reps", "target_reps": 10, "notes": "10 each side"},
        ]},
    },
}


_RUNWALK_ROW = {
    "plan_date": None, "session_type": "cardio_z2",
    "target_rpe": 5.5, "target_hr_zone": 2, "est_duration_min": 35, "is_skipped": False,
    "blocks": {
        "type": "steady", "display_name": "Run-Walk Progression", "rounds": 6,
        "intervals_template": {"work_sec": 120, "work_settings": "easy jog",
                               "rest_sec": 120, "rest_settings": "walk"},
        "warmup_sec": 300, "warmup_settings": "walk",
        "cooldown_sec": 300, "cooldown_settings": "walk",
        "intensity": "moderate", "equipment": [],
        "setup_notes": ["Run-walk: 6 rounds of 2 min easy jog / 2 min walk"],
    },
}


class TestGetPlanDetailRender(unittest.TestCase):
    def test_strength_full_block(self):
        with mock.patch("knowledge.db.execute_one", return_value=_STRENGTH_ROW), \
             mock.patch.object(health, "_call_claude_text", return_value="Coach: brace and control the tempo."):
            reply = get_plan_detail("explain today's workout")
        # display_name (not legacy label), meta, full circuit, format-aware detail, finisher
        self.assertIn("Strength — Push/Quad", reply)
        self.assertNotIn("Strength A — Push/Legs", reply)  # legacy label not used
        self.assertIn("target RPE 7.5", reply)
        self.assertIn("Zone 3 — steady, controlled effort", reply)
        self.assertIn("Circuit** — 3 rounds (120s rest between rounds)", reply)
        self.assertIn("Goblet squat — 12 reps @ 30 lb · rest 60s", reply)
        self.assertIn("Reverse lunge — 10 reps (10 each side)", reply)
        self.assertIn("Core finisher** — 2 rounds", reply)
        self.assertIn("Side plank — 20s (20s each side)", reply)
        self.assertIn("Warmup:", reply)
        self.assertIn("Cooldown:", reply)
        self.assertTrue(reply.rstrip().endswith("Coach: brace and control the tempo."))

    def test_cardio_full_block(self):
        with mock.patch("knowledge.db.execute_one", return_value=_CARDIO_ROW), \
             mock.patch.object(health, "_call_claude_text", return_value="Coach: keep it conversational."):
            reply = get_plan_detail("detailed breakdown of today's session")
        self.assertIn("Long Z2 Bike", reply)
        self.assertNotIn("Cardio Zone 2", reply)  # legacy label not used
        self.assertIn("Zone 2 — conversational pace", reply)
        self.assertIn("Steady", reply)
        self.assertIn("45–55 min", reply)
        self.assertIn("Equipment: road bike + indoor trainer", reply)
        self.assertIn("Core finisher", reply)
        self.assertIn("Dead bug — 10 reps (10 each side)", reply)

    def test_runwalk_steady_full_block_with_zone2_cue(self):
        with mock.patch("knowledge.db.execute_one", return_value=_RUNWALK_ROW), \
             mock.patch.object(health, "_call_claude_text", return_value="Coach: keep the jogs honest."):
            reply = get_plan_detail("tell me more about tomorrow's workout")
        self.assertIn("Run-Walk Progression", reply)
        self.assertNotIn("Walk + mobility", reply)        # legacy label not used
        self.assertIn("Zone 2 — conversational pace", reply)
        # run/walk structure rendered from intervals_template
        self.assertIn("6 rounds", reply)
        self.assertIn("easy jog", reply)
        self.assertIn("walk", reply.lower())
        # Zone 2 talk-test reminder appended
        self.assertIn("Zone 2 cue", reply)
        self.assertIn("120", reply)  # ~120-140 HR cue
        self.assertNotIn("No plan seeded", reply)

    def test_coaching_path_receives_the_block(self):
        """The LLM coaching call must be passed the structured block (so it can
        only explain THIS session), not be called blind."""
        captured = {}

        def capture(system, user, max_tokens=140):
            captured["system"] = system
            captured["user"] = user
            return "Coach note."

        with mock.patch("knowledge.db.execute_one", return_value=_STRENGTH_ROW), \
             mock.patch.object(health, "_call_claude_text", side_effect=capture):
            get_plan_detail("explain today's workout")

        self.assertIn("Goblet squat", captured["user"])      # the block is in the prompt
        self.assertIn("Strength — Push/Quad", captured["user"])
        # constrained: explain only this block, no invention
        self.assertIn("do NOT invent", captured["system"])

    def test_llm_unavailable_falls_back_to_structured_render(self):
        """If the coaching LLM raises, return the structured render alone — never
        a generic reply, never an error."""
        with mock.patch("knowledge.db.execute_one", return_value=_STRENGTH_ROW), \
             mock.patch.object(health, "_call_claude_text", side_effect=RuntimeError("no api key")):
            reply = get_plan_detail("explain today's workout")
        self.assertIn("Goblet squat — 12 reps @ 30 lb", reply)  # structured render intact
        self.assertIn("Strength — Push/Quad", reply)


# ---------------------------------------------------------------------------
# get_plan_lookup — multi-day breadth formatting unchanged
# ---------------------------------------------------------------------------

class TestGetPlanLookupFormatting(unittest.TestCase):
    def test_real_exercise_names_from_blocks(self):
        row = {
            "plan_date": datetime.now(CT).date(),
            "session_type": "strength_a", "target_rpe": 7.5,
            "est_duration_min": 40, "is_skipped": False,
            "blocks": {
                "type": "circuit",
                "exercises": [
                    {"name": "Goblet squat", "format": "reps", "target_reps": 12},
                    {"name": "DB floor press", "format": "reps", "target_reps": 12},
                ],
                "finisher": {"type": "core_circuit",
                             "exercises": [{"name": "Dead bug", "format": "reps", "target_reps": 10}]},
            },
        }
        with mock.patch("knowledge.db.execute_one", return_value=row):
            reply = get_plan_lookup("show my plan for the next 1 days")
        self.assertIn("Goblet squat", reply)
        self.assertIn("DB floor press", reply)
        self.assertIn("finisher: Dead bug", reply)
        self.assertNotIn("No plan seeded", reply)


# ---------------------------------------------------------------------------
# Anti-confabulation — Artemis must NEVER deny it has a workout database
# ---------------------------------------------------------------------------

_DENIAL_MARKERS = [
    "don't have a workout database", "dont have a workout database",
    "no workout database", "no connected database",
    "shared directly in the chat", "from the chat thread",
    "don't have access to your workout", "i don't have a workout",
]


class TestAntiConfabulation(unittest.TestCase):
    def _assert_no_denial(self, text):
        low = (text or "").lower()
        for marker in _DENIAL_MARKERS:
            self.assertNotIn(marker, low, f"response contains a DB denial: {marker!r}")
        self.assertIsNone(health._DB_DENIAL_RE.search(text or ""),
                          "response matched the DB-denial regex")

    def test_deep_query_does_not_reach_general_reply_and_no_denial(self):
        """'deep query the workout database' must hit the plan handler, never
        general_reply, and the response must not deny DB access."""
        general_reply = mock.MagicMock(return_value="I don't have a workout database.")
        with mock.patch("knowledge.db.execute_one", return_value=_STRENGTH_ROW), \
             mock.patch.object(health, "_coach_note", return_value=None):
            reply = _route("deep query the workout database", general_reply)
        general_reply.assert_not_called()
        self.assertIn("Strength — Push/Quad", reply)  # real plan data, not a denial
        self._assert_no_denial(reply)

    def test_scrubber_fires_on_planted_denial(self):
        """If general_reply drafts a denial, scrub_db_denial replaces it with the
        real plan detail."""
        denial = ("I don't have a workout database — that information was shared "
                  "directly in the chat thread.")
        with mock.patch("knowledge.db.execute_one", return_value=_STRENGTH_ROW), \
             mock.patch.object(health, "_coach_note", return_value=None):
            out = health.scrub_db_denial(denial, "deep query the workout database")
        self.assertIn("Strength — Push/Quad", out)  # routed to plan_detail
        self._assert_no_denial(out)

    def test_scrubber_passes_through_clean_replies(self):
        clean = "Here are your meetings today: 10am with Greg."
        self.assertEqual(health.scrub_db_denial(clean, "what's on my calendar"), clean)

    def test_empty_result_states_range_not_denial(self):
        """Missing date → a concrete 'No plan seeded … your plan runs X to Y',
        never a claim that the database doesn't exist."""
        today = datetime.now(CT).date()
        first, last = today - timedelta(days=2), today + timedelta(days=130)

        def fake(sql, p=()):
            return {"first": first, "last": last} if "MIN(plan_date)" in " ".join(sql.split()) else None

        with mock.patch("knowledge.db.execute_one", side_effect=fake), \
             mock.patch.object(health, "_call_claude_text") as llm:
            reply = get_plan_detail("explain today's workout")
        self.assertIn(f"No plan seeded for {today.isoformat()}", reply)
        self.assertIn(f"your plan runs {first.isoformat()} to {last.isoformat()}", reply)
        llm.assert_not_called()
        self._assert_no_denial(reply)


if __name__ == "__main__":
    unittest.main()
