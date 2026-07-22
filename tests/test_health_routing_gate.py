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


class TestMorningStateWritten(_Base):
    """HEALTH-1 (a): the failing check-in doesn't just ROUTE to the morning
    handler — the handler actually UPSERTs health.daily_state, CT-dated. Here the
    handler runs for real (only the LLM parse + the DB write are stubbed)."""

    def test_checkin_writes_daily_state_with_ct_date(self):
        import knowledge.db as kdb
        from datetime import datetime
        from artemis.health import CT, MorningState

        state = MorningState(sleep_hrs=7.0, energy=5, soreness={"legs": 3})
        with patch.object(health, "parse_morning_checkin", return_value=state), \
             patch.object(kdb, "execute_write") as ew:
            reply = health.handle_morning_intent("sleep 7 energy 5 legs sore 3")

        ew.assert_called_once()
        sql, params = ew.call_args[0][0], ew.call_args[0][1]
        self.assertIn("health.daily_state", sql)
        self.assertEqual(params[0], datetime.now(CT).date())   # state_date = CT today
        self.assertIn("Logged", reply)


class TestGeneralReplyCannotReroute(_Base):
    """HEALTH-1 (c): even if the LLM classifier WOULD label a detected health
    message `general_reply`, it can never re-route it — the deterministic gate
    claims the message first and the classifier is never consulted."""

    def test_general_reply_cannot_reroute_detected_health_message(self):
        general_reply = MagicMock(name="general_reply_classification")
        with patch.object(intent, "route_intent", return_value=general_reply) as route, \
             patch.object(main, "_handle_intent_routed") as routed, \
             patch.object(health, "handle_morning_intent",
                          return_value="Logged: 7h sleep, energy 5/5.") as handler:
            main._handle_mention(_post("sleep 7 energy 5 legs sore 3"), [])
        # The classifier (which would have said general_reply) is never reached…
        route.assert_not_called()
        routed.assert_not_called()
        # …and the deterministic morning handler owns the message.
        handler.assert_called_once()
        self.assertEqual(self._last_post(), "Logged: 7h sleep, energy 5/5.")


class TestSwapToRestRefusal(_Base):
    """The verbatim failing message 'swap to rest' → honest refusal, zero plan
    writes, zero audit rows, LLM never called."""

    def test_swap_to_rest_refuses_without_writes_or_llm(self):
        import knowledge.db as kdb
        with patch.object(intent, "route_intent") as route, \
             patch.object(main, "_handle_intent_routed") as routed, \
             patch.object(kdb, "execute_write") as ew, \
             patch.object(kdb, "log_audit") as la:
            main._handle_mention(_post("swap to rest"), [])
        route.assert_not_called()          # classifier never called
        routed.assert_not_called()
        ew.assert_not_called()             # zero DB writes to the plan
        la.assert_not_called()             # zero audit rows
        reply = self._last_post()
        self.assertNotIn("✅", reply)
        self.assertIn("rest", reply.lower())
        self.assertIn("aren't supported yet", reply)


class TestFabricationGate(_Base):
    """Output-side complement: an LLM draft claiming an action is suppressed and
    logged; a real deterministic-handler confirmation passes through untouched."""

    def test_fabricated_swap_claim_is_suppressed_and_logged(self):
        import knowledge.db as kdb
        fake = "✅ Swapped today's run to indoor rowing. gym.rdm.is is up to date."
        with patch.object(main, "handle_mention", return_value=fake), \
             patch.object(main, "_build_mention_context", return_value=""), \
             patch.object(main, "_try_life_ops", return_value=None), \
             patch.object(main, "_handle_intent_routed", return_value=None), \
             patch.object(kdb, "log_guardrail_violation") as glog:
            # A neutral question that falls through to the free-text LLM path;
            # the gate acts on the RESPONSE, not the question.
            main._handle_mention(_post("tell me something interesting"), [])
        posted = self._last_post()
        # The fabricated confirmation is NOT posted…
        self.assertNotIn("✅", posted)
        self.assertNotEqual(posted, fake)
        # …the honest reply is…
        self.assertIn("nothing was changed", posted.lower())
        # …and the suppressed text was logged as a guardrail violation.
        glog.assert_called_once()
        kwargs = glog.call_args.kwargs
        self.assertEqual(kwargs.get("guardrail_type"), "fabricated_action_claim")
        self.assertIn("Swapped", kwargs.get("event_summary", ""))

    def test_real_handler_confirmation_passes_through(self):
        # A deterministic handler that returns a "✅ Swapped" confirmation posts
        # it verbatim; the free-text gate never runs, nothing is logged.
        import knowledge.db as kdb
        real = "✅ Swapped to **Indoor Row — Z2 Intervals**. gym.rdm.is is up to date."
        with patch.object(health, "propose_modality_swap", return_value=real), \
             patch.object(main, "handle_mention") as llm, \
             patch.object(kdb, "log_guardrail_violation") as glog:
            main._handle_mention(_post("swap today to indoor rower"), [])
        self.assertEqual(self._last_post(), real)  # verbatim, untouched
        llm.assert_not_called()                    # never reached the LLM path
        glog.assert_not_called()                   # nothing suppressed/logged


if __name__ == "__main__":
    unittest.main(verbosity=2)
