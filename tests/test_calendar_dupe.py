"""Tests for restored Brad-incident guards: duplicate detection (block + distinct
`override duplicate`) and calendar-write audit logging.

Layers:
  1. Pure classifier — guardrails.check_duplicate_event (title / attendee / unrelated).
  2. Both create paths — direct (_process_calendar_events) and confirm-execute
     (_handle_calendar_confirm) — block confident dups and audit successful writes.
  3. Override routing — only `override duplicate` (NOT confirm words) gets past a
     block; the external-attendee guard still applies on override.

artemis.main's heavy deps (flask/google/apscheduler/websocket) are stubbed.

Run:
    python tests/test_calendar_dupe.py
"""

import json
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

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

from artemis import main  # noqa: E402
from artemis import guardrails  # noqa: E402

_TZ = ZoneInfo("America/Chicago")


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=_TZ)


def _existing(summary, start_iso, attendees=None, eid="ev_existing"):
    return {
        "id": eid,
        "summary": summary,
        "start": start_iso,
        "attendees": [{"email": e, "name": "", "self": False} for e in (attendees or [])],
    }


# ============================================================================
# 1. Pure classifier
# ============================================================================

class TestDuplicateClassifier(unittest.TestCase):
    def test_similar_title_within_window_is_duplicate(self):
        v = guardrails.check_duplicate_event(
            "Q3 Review", _dt("2026-06-10 14:00"), [],
            [_existing("Q3 Review", "2026-06-10T14:00:00-05:00")],
        )
        self.assertTrue(v["duplicate"])
        self.assertEqual(v["reason"], "title")

    def test_shared_attendee_within_window_is_duplicate(self):
        v = guardrails.check_duplicate_event(
            "Sync", _dt("2026-06-10 14:00"), ["brad@spaits.com"],
            [_existing("Totally different name", "2026-06-10T15:00:00-05:00",
                       attendees=["brad@spaits.com"])],
        )
        self.assertTrue(v["duplicate"])
        self.assertEqual(v["reason"], "attendee")

    def test_unrelated_overlap_is_not_duplicate_but_soft_note(self):
        v = guardrails.check_duplicate_event(
            "Q3 Review", _dt("2026-06-10 14:00"), [],
            [_existing("Dentist appointment", "2026-06-10T14:30:00-05:00")],
        )
        self.assertFalse(v["duplicate"])
        self.assertIsNotNone(v["soft_note"])
        self.assertIn("Dentist", v["soft_note"])

    def test_no_nearby_events(self):
        v = guardrails.check_duplicate_event("Q3 Review", _dt("2026-06-10 14:00"), [], [])
        self.assertFalse(v["duplicate"])
        self.assertIsNone(v["soft_note"])

    def test_same_title_outside_window_not_duplicate(self):
        v = guardrails.check_duplicate_event(
            "Q3 Review", _dt("2026-06-10 14:00"), [],
            [_existing("Q3 Review", "2026-06-10T19:00:00-05:00")],  # 5h away
        )
        self.assertFalse(v["duplicate"])


# ============================================================================
# Shared harness for main-path tests
# ============================================================================

class _MainBase(unittest.TestCase):
    def setUp(self):
        main._pending_confirms.clear()
        self._cal = MagicMock()
        self._cal.create_event.return_value = "evt_new"
        self._cal.get_meet_link.return_value = "https://meet.google.com/x"
        self._cal.get_events_around.return_value = []
        self._mm = MagicMock()
        self._audit = MagicMock(return_value="1")
        self._p = [
            patch.object(main, "_calendar", self._cal),
            patch.object(main, "_mm", self._mm),
            patch.object(main, "log_calendar_action", lambda *a, **k: None),
            patch("knowledge.db.log_calendar_audit", self._audit),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        main._pending_confirms.clear()

    def _post(self, message, channel="chanA"):
        return {"id": "p1", "channel_id": channel, "message": message, "root_id": None}

    def _ext_pending(self, channel="chanA", attendees=("brad@spaits.com",)):
        return {
            "type": "calendar_create_external",
            "data": {
                "summary": "Q3 Review", "date": "2026-06-10",
                "start_time": "14:00", "end_time": "15:00",
                "attendees": list(attendees),
            },
            "timestamp": time.time(),
        }

    def _last_msg(self):
        return self._mm.post_to_channel_id.call_args[0][1]


# ============================================================================
# 2a. Confirm-execute path
# ============================================================================

class TestConfirmPathDuplicate(_MainBase):
    def test_dup_blocks_confirm_and_stores_override_pending(self):
        self._cal.get_events_around.return_value = [
            _existing("Q3 Review", "2026-06-10T14:00:00-05:00", attendees=["brad@spaits.com"])
        ]
        main._pending_confirms["chanA"] = self._ext_pending()
        handled = main._handle_calendar_confirm(self._post("confirm"), "confirm")
        self.assertTrue(handled)
        self._cal.create_event.assert_not_called()
        self.assertIn("Blocked", self._last_msg())
        self.assertIn("override duplicate", self._last_msg())
        self.assertEqual(main._pending_confirms["chanA"]["type"], "duplicate_override")
        self.assertTrue(main._pending_confirms["chanA"]["user_approved_external"])

    def test_override_after_block_creates_with_dup_override_audit(self):
        self._cal.get_events_around.return_value = [
            _existing("Q3 Review", "2026-06-10T14:00:00-05:00", attendees=["brad@spaits.com"])
        ]
        main._pending_confirms["chanA"] = self._ext_pending()
        main._handle_calendar_confirm(self._post("confirm"), "confirm")  # → block
        self._cal.get_events_around.return_value = []  # not needed past override
        handled = main._handle_duplicate_override(self._post("override duplicate"), "override duplicate")
        self.assertTrue(handled)
        self._cal.create_event.assert_called_once()
        # external approval preserved through the override
        self.assertIs(self._cal.create_event.call_args.kwargs["_user_approved_external"], True)
        self.assertTrue(self._audit.called)
        self.assertTrue(self._audit.call_args.kwargs["dup_override"])
        self.assertEqual(self._audit.call_args.kwargs["action"], "create")

    def test_confirm_words_do_not_bypass_block(self):
        main._pending_confirms["chanA"] = {
            "type": "duplicate_override",
            "data": self._ext_pending()["data"],
            "user_approved_external": True, "match": {}, "timestamp": time.time(),
        }
        for word in ("confirm", "approved", "yes"):
            with self.subTest(word=word):
                self._cal.reset_mock()
                handled = main._handle_duplicate_override(self._post(word), word)
                self.assertTrue(handled)  # consumed, never reaches classifier
                self._cal.create_event.assert_not_called()  # but does NOT create
                self.assertIn("override duplicate", self._last_msg())

    def test_cancel_discards_block(self):
        main._pending_confirms["chanA"] = {
            "type": "duplicate_override", "data": self._ext_pending()["data"],
            "user_approved_external": True, "match": {}, "timestamp": time.time(),
        }
        handled = main._handle_duplicate_override(self._post("cancel"), "cancel")
        self.assertTrue(handled)
        self._cal.create_event.assert_not_called()
        self.assertNotIn("chanA", main._pending_confirms)


# ============================================================================
# 2b. Direct path
# ============================================================================

_CAL_BLOCK = (
    "Scheduling that.\n```calendar_event\n"
    + json.dumps({"summary": "Q3 Review", "date": "2026-06-10",
                  "start_time": "14:00", "end_time": "15:00"})
    + "\n```\n"
)


class TestDirectPathDuplicate(_MainBase):
    def test_dup_blocks_direct_create(self):
        self._cal.get_events_around.return_value = [
            _existing("Q3 Review", "2026-06-10T14:00:00-05:00")
        ]
        out = main._process_calendar_events(_CAL_BLOCK, "chanA")
        self._cal.create_event.assert_not_called()
        self.assertIn("Blocked", out)
        self.assertIn("override duplicate", out)
        self.assertEqual(main._pending_confirms["chanA"]["type"], "duplicate_override")

    def test_unrelated_overlap_creates_and_audits(self):
        self._cal.get_events_around.return_value = [
            _existing("Dentist", "2026-06-10T14:30:00-05:00")
        ]
        out = main._process_calendar_events(_CAL_BLOCK, "chanA")
        self._cal.create_event.assert_called_once()
        self.assertIn("Event created", out)
        self.assertTrue(self._audit.called)
        kw = self._audit.call_args.kwargs
        self.assertEqual(kw["action"], "create")
        self.assertFalse(kw["has_external"])
        self.assertIsNone(kw["approved_by"])
        self.assertFalse(kw["dup_override"])

    def test_clean_create_audits(self):
        self._cal.get_events_around.return_value = []
        main._process_calendar_events(_CAL_BLOCK, "chanA")
        self._cal.create_event.assert_called_once()
        self.assertTrue(self._audit.called)


# ============================================================================
# 3. External guard survives override; dispatch routing
# ============================================================================

class TestExternalGuardAndRouting(_MainBase):
    def test_override_does_not_grant_external_approval(self):
        # duplicate_override carrying external attendees but NO prior approval.
        main._pending_confirms["chanA"] = {
            "type": "duplicate_override",
            "data": {"summary": "Ext", "date": "2026-06-10", "start_time": "14:00",
                     "end_time": "15:00", "attendees": ["outsider@external.com"]},
            "user_approved_external": False, "match": {}, "timestamp": time.time(),
        }
        main._handle_duplicate_override(self._post("override duplicate"), "override duplicate")
        # create_event is called, but WITHOUT external approval — the hard guard
        # inside create_event still applies (override ≠ attendee approval).
        self.assertIs(self._cal.create_event.call_args.kwargs["_user_approved_external"], False)

    def test_override_phrase_routed_before_classifier(self):
        main._pending_confirms["chanA"] = {
            "type": "duplicate_override",
            "data": {"summary": "Q3 Review", "date": "2026-06-10", "start_time": "14:00",
                     "end_time": "15:00", "attendees": []},
            "user_approved_external": False, "match": {}, "timestamp": time.time(),
        }
        with patch.object(main, "update_last_interaction", lambda *a, **k: None), \
             patch.object(main, "_handle_availability_command", return_value=False), \
             patch.object(main, "_handle_intent_routed") as routed:
            main._handle_mention(self._post("override duplicate"), [])
        self._cal.create_event.assert_called_once()
        routed.assert_not_called()

    def test_confirm_after_block_never_reaches_classifier(self):
        main._pending_confirms["chanA"] = {
            "type": "duplicate_override",
            "data": {"summary": "Q3 Review", "date": "2026-06-10", "start_time": "14:00",
                     "end_time": "15:00", "attendees": []},
            "user_approved_external": False, "match": {}, "timestamp": time.time(),
        }
        with patch.object(main, "update_last_interaction", lambda *a, **k: None), \
             patch.object(main, "_handle_availability_command", return_value=False), \
             patch.object(main, "_handle_intent_routed") as routed:
            main._handle_mention(self._post("confirm"), [])
        routed.assert_not_called()
        self._cal.create_event.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
