"""Tests for confirm-dispatch shadowing — confirm/approved must EXECUTE the
pending calendar action, never fall through to the LLM intent classifier.

Covers:
  - "approved"/"confirm"/"yes" with an open pending → create_event IS called,
    and _handle_intent_routed (the classifier) is NOT reached.
  - "approved" specifically is accepted (regression for the old match set that
    only had "approve").
  - Confabulation guard: create_event raises → a FAILURE message is posted, no
    "Event created" success text; pending is consumed.
  - Success path: create_event returns an id → success posted only after the
    call returns, including the id.
  - External guard: create_event is called with _user_approved_external=True;
    an expired pending never reaches create_event.
  - No pending + "confirm" → the confirm handler does not consume it (falls
    through to normal intent handling, unchanged behavior).

artemis.main pulls in flask / google / apscheduler / websocket etc. — none of
which the confirm logic needs — so those are stubbed before import.

Run:
    python tests/test_confirm_dispatch.py
"""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

# Stub heavy third-party deps so artemis.main imports without them.
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

from artemis import main  # noqa: E402


def _post(message: str, channel: str = "chanA") -> dict:
    return {"id": "post1", "channel_id": channel, "message": message, "root_id": None}


def _pending(channel: str = "chanA", ts: float | None = None) -> dict:
    return {
        "type": "calendar_create_external",
        "data": {
            "summary": "Q3 Review",
            "date": "2026-06-10",
            "start_time": "14:00",
            "end_time": "15:00",
            "description": "Quarterly review",
            "attendees": ["brad@client.com"],
        },
        "timestamp": time.time() if ts is None else ts,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        main._pending_confirms.clear()
        self._cal = MagicMock()
        self._cal.create_event.return_value = "evt_123"
        self._cal.get_meet_link.return_value = "https://meet.google.com/abc-defg-hij"
        self._mm = MagicMock()
        self._cal_patch = patch.object(main, "_calendar", self._cal)
        self._mm_patch = patch.object(main, "_mm", self._mm)
        self._cal_patch.start()
        self._mm_patch.start()

    def tearDown(self):
        self._cal_patch.stop()
        self._mm_patch.stop()
        main._pending_confirms.clear()

    def _last_post(self) -> str:
        self.assertTrue(self._mm.post_to_channel_id.called, "expected a Mattermost post")
        return self._mm.post_to_channel_id.call_args[0][1]


# ============================================================================
# Direct confirm-handler behavior
# ============================================================================

class TestCalendarConfirmExecute(_Base):
    def test_confirm_word_variants_accepted(self):
        for word in ("confirm", "approved", "yes", "approve", "send it", "go"):
            with self.subTest(word=word):
                main._pending_confirms["chanA"] = _pending()
                self._cal.reset_mock(); self._mm.reset_mock()
                self._cal.create_event.return_value = "evt_123"
                handled = main._handle_calendar_confirm(_post(word), word)
                self.assertTrue(handled, f"{word!r} should be handled")
                self.assertTrue(self._cal.create_event.called, f"{word!r} should call create_event")
                self.assertNotIn("chanA", main._pending_confirms)

    def test_approved_specifically_accepted(self):
        # Regression: the old match set had "approve" but not "approved".
        main._pending_confirms["chanA"] = _pending()
        handled = main._handle_calendar_confirm(_post("approved"), "approved")
        self.assertTrue(handled)
        self.assertTrue(self._cal.create_event.called)

    def test_success_only_after_create_returns_with_id(self):
        main._pending_confirms["chanA"] = _pending()
        main._handle_calendar_confirm(_post("confirm"), "confirm")
        self._cal.create_event.assert_called_once()
        msg = self._last_post()
        self.assertIn("evt_123", msg)
        self.assertIn("Event created", msg)
        self.assertIn("meet.google.com", msg)  # Meet link included

    def test_create_event_raises_posts_failure_no_success(self):
        main._pending_confirms["chanA"] = _pending()
        self._cal.create_event.side_effect = RuntimeError("boom")
        handled = main._handle_calendar_confirm(_post("confirm"), "confirm")
        self.assertTrue(handled)  # still consumed — never falls through
        msg = self._last_post()
        self.assertNotIn("Event created", msg)
        self.assertIn("Couldn't create", msg)
        self.assertNotIn("chanA", main._pending_confirms)

    def test_create_event_returns_none_posts_failure(self):
        main._pending_confirms["chanA"] = _pending()
        self._cal.create_event.return_value = None
        main._handle_calendar_confirm(_post("confirm"), "confirm")
        msg = self._last_post()
        self.assertNotIn("Event created", msg)
        self.assertIn("Couldn't create", msg)

    def test_external_attendee_approval_passed_on_confirm(self):
        main._pending_confirms["chanA"] = _pending()
        main._handle_calendar_confirm(_post("confirm"), "confirm")
        kwargs = self._cal.create_event.call_args.kwargs
        self.assertIs(kwargs.get("_user_approved_external"), True)
        self.assertEqual(kwargs.get("attendees"), ["brad@client.com"])

    def test_expired_pending_never_reaches_create_event(self):
        main._pending_confirms["chanA"] = _pending(ts=time.time() - 700)  # >600s
        handled = main._handle_calendar_confirm(_post("confirm"), "confirm")
        self.assertFalse(handled)
        self.assertFalse(self._cal.create_event.called)
        self.assertNotIn("chanA", main._pending_confirms)  # expired entry dropped

    def test_cancel_words_do_not_create(self):
        for word in ("cancel", "no", "deny", "discard"):
            with self.subTest(word=word):
                main._pending_confirms["chanA"] = _pending()
                self._cal.reset_mock(); self._mm.reset_mock()
                handled = main._handle_calendar_confirm(_post(word), word)
                self.assertTrue(handled)
                self.assertFalse(self._cal.create_event.called)
                self.assertNotIn("chanA", main._pending_confirms)

    def test_no_pending_falls_through(self):
        # No pending action → handler does not consume; flow falls through to
        # normal intent handling (unchanged behavior).
        self.assertNotIn("chanA", main._pending_confirms)
        handled = main._handle_calendar_confirm(_post("confirm"), "confirm")
        self.assertFalse(handled)
        self.assertFalse(self._cal.create_event.called)


# ============================================================================
# Full dispatch: control word + open pending must NOT reach the classifier
# ============================================================================

class TestDispatchShortCircuit(_Base):
    def test_control_word_never_reaches_intent_router(self):
        # _handle_calendar_confirm runs early in _handle_mention; only
        # update_last_interaction and _handle_availability_command precede it.
        for word in ("confirm", "approved", "yes"):
            with self.subTest(word=word):
                main._pending_confirms["chanA"] = _pending()
                self._cal.reset_mock(); self._mm.reset_mock()
                self._cal.create_event.return_value = "evt_123"
                with patch.object(main, "update_last_interaction", lambda *a, **k: None), \
                     patch.object(main, "_handle_availability_command", return_value=False), \
                     patch.object(main, "_handle_intent_routed") as routed:
                    main._handle_mention(_post(word), [])
                self.assertTrue(self._cal.create_event.called, f"{word!r} should execute create_event")
                routed.assert_not_called()  # classifier never reached

    def test_no_pending_reaches_intent_router(self):
        # Without a pending action, "confirm" is just a normal message: the
        # confirm handlers don't consume it and the backstop doesn't fire, so
        # the dispatcher proceeds to the intent router as before.
        self.assertNotIn("chanA", main._pending_confirms)
        with patch.object(main, "update_last_interaction", lambda *a, **k: None), \
             patch.object(main, "_handle_availability_command", return_value=False), \
             patch.object(main, "_handle_calendar_confirm", return_value=False), \
             patch.object(main, "_handle_delete_confirm", return_value=False), \
             patch.object(main, "_handle_health_conversation", return_value=False), \
             patch.object(main, "_handle_quiet_command", return_value=False), \
             patch.object(main, "_handle_inbox_command", return_value=False), \
             patch.object(main, "_handle_action_item_command", return_value=False), \
             patch.object(main, "_handle_scheduling_mention", return_value=False), \
             patch.object(main, "_handle_availability_mention", return_value=False), \
             patch.object(main, "_handle_delete_event", return_value=False), \
             patch.object(main, "_handle_convert_to_tasks", return_value=False), \
             patch.object(main, "_try_life_ops", return_value=None), \
             patch.object(main, "_handle_correction", return_value=None), \
             patch.object(main, "_handle_intent_routed", return_value=None) as routed:
            main._handle_mention(_post("confirm"), [])
        routed.assert_called_once()
        self.assertFalse(self._cal.create_event.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
