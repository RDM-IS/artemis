"""SESSION-OPEN 5a — `rebuild sessions`, the startup rebuild, and the
strength_day_types the Lambda serves for gym-display's "counts" tags.

No database, no Mattermost: session_library.write and _mm are mocked. Synthetic
2031 dates only (PUBLIC-FIXTURES)."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import logging
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _n in ("googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
           "google", "google.auth", "google.auth.transport",
           "google.auth.transport.requests", "google.oauth2",
           "google.oauth2.credentials", "google_auth_oauthlib",
           "google_auth_oauthlib.flow", "websocket", "Levenshtein", "apscheduler",
           "apscheduler.schedulers", "apscheduler.schedulers.background",
           "apscheduler.triggers", "apscheduler.triggers.cron"):
    sys.modules.setdefault(_n, MagicMock())

from artemis import main as M  # noqa: E402
from artemis import session_library  # noqa: E402

LIB = {"generated_on": "2031-03-04", "week_num": 7, "today_location_key": "synthetic_a",
       "locations": [{"key": "synthetic_a", "display": "Room A", "sessions": [{}, {}, {}]},
                     {"key": "synthetic_b", "display": "Room B", "sessions": [{}]}]}
POST = {"channel_id": "CH", "id": "P1"}


class RebuildCommandTest(unittest.TestCase):
    def _run(self, text, write=None):
        mm = MagicMock()
        write = write or MagicMock(return_value=LIB)
        with patch.object(M, "_mm", mm), patch.object(session_library, "write", write):
            handled = M._handle_rebuild_sessions(POST, text)
        return handled, write, mm

    def test_it_rebuilds_and_names_the_rooms_and_counts(self):
        for text in ("rebuild sessions", "Rebuild Sessions.", "rebuild the sessions",
                     "rebuild session library", "  rebuild sessions!  "):
            with self.subTest(text=text):
                handled, write, mm = self._run(text)
                self.assertTrue(handled)
                write.assert_called_once_with()
                reply = mm.post_message.call_args[0][1]
                self.assertIn("2031-03-04", reply)
                self.assertIn("Room A: 3 sessions", reply)
                self.assertIn("Room B: 1 session", reply)
                self.assertNotIn("Room B: 1 sessions", reply)

    def test_anything_else_falls_through_and_builds_nothing(self):
        for text in ("rebuild", "sessions", "rebuild sessions please", "yes",
                     "how many sessions did I rebuild", ""):
            with self.subTest(text=text):
                handled, write, mm = self._run(text)
                self.assertFalse(handled)
                write.assert_not_called()
                mm.post_message.assert_not_called()

    def test_a_failed_rebuild_says_so_and_never_reads_as_success(self):
        handled, _, mm = self._run("rebuild sessions", write=MagicMock(side_effect=RuntimeError("synthetic")))
        self.assertTrue(handled)
        reply = mm.post_message.call_args[0][1]
        self.assertIn("couldn't rebuild", reply)
        self.assertNotIn("rebuilt for", reply)

    def test_it_is_not_a_bare_control_word_so_confirm_arb_ignores_it(self):
        self.assertNotIn("rebuild sessions", M._CONTROL_WORDS)
        self.assertIsNone(M._arbitrate_bare_control_word("CH", "rebuild sessions"))

    def test_it_is_in_the_chain_ahead_of_the_llm_fallthrough(self):
        src = (ROOT / "artemis" / "main.py").read_text()
        chain = src[src.index("deterministic_chain = ["):src.index("for _name, _fn in deterministic_chain")]
        self.assertIn('("rebuild_sessions", _handle_rebuild_sessions)', chain)

    def test_the_mention_dispatcher_routes_it(self):
        """End to end through _handle_mention: the chain claims it, nothing
        reaches the LLM router."""
        mm = MagicMock()
        write = MagicMock(return_value=LIB)
        with patch.object(M, "_mm", mm), patch.object(session_library, "write", write), \
             patch.object(M, "update_last_interaction"), \
             patch.object(M, "_record_arbitration_outcome"), \
             patch.object(M, "_handle_intent_routed", side_effect=AssertionError("reached the LLM")):
            M._handle_mention({"channel_id": "CH", "id": "P9", "message": "@artemis rebuild sessions"}, [])
        write.assert_called_once_with()
        self.assertIn("Room A: 3 sessions", mm.post_message.call_args[0][1])

    def test_help_lists_it(self):
        from artemis import help_registry
        self.assertIn("rebuild sessions", help_registry.render_help("health"))


class StartupRebuildTest(unittest.TestCase):
    def test_a_failure_is_logged_and_does_not_raise(self):
        with patch.object(session_library, "write", side_effect=RuntimeError("synthetic: no db")), \
             self.assertLogs("artemis.main", level="ERROR") as logs:
            M._startup_rebuild_library()          # must not raise
        self.assertTrue(any("rebuild failed" in m for m in logs.output))

    def test_an_import_failure_does_not_raise_either(self):
        real_import = __import__

        def boom(name, *a, **k):
            if name == "artemis" and a and a[2] and "session_library" in a[2]:
                raise ImportError("synthetic")
            return real_import(name, *a, **k)
        with patch("builtins.__import__", side_effect=boom), \
             self.assertLogs("artemis.main", level="ERROR"):
            M._startup_rebuild_library()

    def test_success_writes_once(self):
        write = MagicMock(return_value=LIB)
        with patch.object(session_library, "write", write):
            M._startup_rebuild_library()
        write.assert_called_once_with()

    def test_main_starts_it_on_a_daemon_thread_and_adds_no_timer(self):
        src = (ROOT / "artemis" / "main.py").read_text()
        body = src[src.index("def main():"):]
        call = body.index("target=_startup_rebuild_library")
        self.assertIn("daemon=True", body[call:call + 200])
        # Before the startup post and the web server, so it runs on every start.
        self.assertLess(call, body.index("_post_startup_message("))
        sched = (ROOT / "artemis" / "scheduler.py").read_text()
        self.assertNotIn("_startup_rebuild_library", sched)
        self.assertNotIn("rebuild_sessions", sched)


class LambdaServesStrengthDayTypesTest(unittest.TestCase):
    def test_the_list_is_session_types_own_and_survives_an_unreadable_quota(self):
        from api.app.routers import health as h
        from knowledge.session_types import STRENGTH_DAY_TYPES
        ok = h.StrengthQuotaOut(count=1, required=3, days_left=4, status="on_track")
        bad = h.StrengthQuotaOut(section=h.Section(ok=False, reason="couldn't read strength days"))
        for q in (ok, bad):
            self.assertEqual(q.strength_day_types, list(STRENGTH_DAY_TYPES))
        self.assertIsNone(bad.count)              # unreadable is never a zero
        self.assertIn("strength_bw", ok.strength_day_types)
        self.assertIn("row_circuit", ok.strength_day_types)

    def test_status_carries_it(self):
        from api.app.routers import health as h
        self.assertIn("strength_day_types", h.StrengthQuotaOut.model_fields)
        self.assertIn("strength_quota", h.StatusResponse.model_fields)

    def test_unreadable_quota_through_the_reader(self):
        from api.app.routers import health as h
        from knowledge import strength_quota as sq
        db = MagicMock()
        with patch.object(sq, "read_sa", side_effect=sq.QuotaUnreadable("synthetic")), \
             self.assertLogs(h.logger.name, level="WARNING"):
            q = h._strength_quota(db, "UTC", date(2031, 3, 4))
        self.assertFalse(q.section.ok)
        self.assertIsNone(q.count)
        self.assertTrue(q.strength_day_types)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
