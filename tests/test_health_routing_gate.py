"""HEALTH-1 Part A — deterministic health intents short-circuit the LLM
classifier, and no live route reaches add_note or the 'learning' re-route.

artemis.main pulls flask / google / apscheduler / websocket etc. — none needed
here — so they're stubbed before import (same pattern as test_confirm_dispatch).

Run:
    python tests/test_health_routing_gate.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from artemis import health, intent, main  # noqa: E402


def _post(message: str, channel: str = "chanA") -> dict:
    return {"id": "post1", "channel_id": channel, "message": message, "root_id": None}


class _Base(unittest.TestCase):
    def setUp(self):
        main._pending_confirms.clear()
        self._mm = MagicMock()
        self._mm_patch = patch.object(main, "_mm", self._mm)
        self._mm_patch.start()
        self._ii = patch.object(main, "update_last_interaction", lambda *a, **k: None)
        self._ii.start()

    def tearDown(self):
        self._mm_patch.stop()
        self._ii.stop()
        main._pending_confirms.clear()

    def _last_post(self):
        self.assertTrue(self._mm.post_to_channel_id.called, "expected a Mattermost post")
        return self._mm.post_to_channel_id.call_args[0][1]


class TestClassifierBypass(_Base):
    """Every deterministic health detector must be handled before the LLM
    classifier — route_intent is never called for a matching message."""

    def _run_expecting_health(self, message, health_attr, canned):
        with patch.object(intent, "route_intent") as route, \
             patch.object(main, "_handle_intent_routed") as routed, \
             patch.object(health, health_attr, return_value=canned) as handler:
            main._handle_mention(_post(message), [])
        route.assert_not_called()
        routed.assert_not_called()
        handler.assert_called()
        self.assertEqual(self._last_post(), canned)

    def test_morning_checkin_bypasses_classifier(self):
        # A4: the HEALTH-1 morning check-in routes straight to the morning
        # handler (which writes daily_state and replies "Logged: …").
        self._run_expecting_health(
            "sleep 7 energy 5 legs sore 3", "handle_morning_intent",
            "Logged: 7h sleep, energy 5/5. Anything to fix?")

    def test_trainer_override_bypasses_classifier(self):
        self._run_expecting_health(
            "trainer set indoor", "handle_trainer_override",
            "Got it — bike on trainer set indoor for Sun Jul 20.")

    def test_modality_swap_bypasses_classifier(self):
        self._run_expecting_health(
            "swap today to indoor rower", "propose_modality_swap",
            "**Swap today's session:** … reply `yes`")

    def test_swap_revert_bypasses_classifier(self):
        self._run_expecting_health(
            "swap revert", "propose_swap_revert",
            "**Revert today's swap:** … reply `yes`")


class TestNoConfabulationRoutes(_Base):
    def test_add_note_not_a_classifier_action(self):
        # A2: add_note is gone from the classifier's action set, so the LLM can
        # never emit it and no route reaches the note-writing stub.
        self.assertNotIn("add_note", intent.VALID_ACTIONS)

    def test_router_has_no_add_note_branch(self):
        import inspect
        src = inspect.getsource(main._handle_intent_routed)
        self.assertNotIn('primary_action == "add_note"', src)
        self.assertNotIn("data_vault_satellites", src)

    def test_no_health_actions_in_classifier(self):
        for a in ("log_morning_state", "log_workout_debrief", "trainer_override",
                  "modality_swap"):
            self.assertNotIn(a, intent.VALID_ACTIONS)

    def test_correction_learning_reroute_not_reached(self):
        # A1/A2: the "I've learned…" correction re-route must not run in live
        # routing. A plain message that falls through to the LLM path must never
        # invoke _handle_correction.
        with patch.object(main, "_handle_correction") as corr, \
             patch.object(main, "_handle_intent_routed", return_value="ok") as routed:
            main._handle_mention(_post("just some general chatter"), [])
        corr.assert_not_called()
        routed.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
