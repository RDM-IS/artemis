"""POLISH-1 regression tests — one per observed first-weekend defect (P1–P9)
plus the generated `help` registry.

Two tiers (mirrors test_dossier.py):
  * Tier 1 (always run): pure/mocked tests of the logic that lives in locally
    importable modules — utils, dossier.todos, commitments, morning_brief,
    help_registry.
  * Tier 2 (skip unless importable): tests of routing in artemis.main (pulls in
    flask) and vault.morning_brief_section (pulls in requests).

Run:
    python3 -m artemis.test_polish1
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import commitments, dossier, help_registry, morning_brief  # noqa: E402
from artemis import utils  # noqa: E402

# Sun Jul 19 2026 — the day the defects were observed. Jul 22 is that week's Wed.
SUN_JUL_19 = date(2026, 7, 19)
WED_JUL_22 = date(2026, 7, 22)

try:
    import artemis.main as main  # noqa: E402
    _MAIN_OK = True
except Exception:  # pragma: no cover
    main = None
    _MAIN_OK = False


# ===========================================================================
# P2 — relative dates always carry absolutes
# ===========================================================================
class TestP2DescribeDue(unittest.TestCase):
    def setUp(self):
        self.assertEqual(SUN_JUL_19.weekday(), 6, "fixture must be a Sunday")

    def test_absolute_always_present(self):
        self.assertEqual(utils.describe_due(SUN_JUL_19, SUN_JUL_19), "today (Jul 19)")
        self.assertEqual(utils.describe_due(date(2026, 7, 20), SUN_JUL_19), "tomorrow (Jul 20)")
        self.assertEqual(utils.abbr_date(WED_JUL_22), "Jul 22")

    def test_this_wednesday_from_sunday(self):
        # The prompt's own example: from Sun Jul 19, Jul 22 reads as "this Wednesday".
        self.assertEqual(utils.describe_due(WED_JUL_22, SUN_JUL_19), "this Wednesday (Jul 22)")

    def test_far_out_uses_in_n_days(self):
        self.assertEqual(utils.describe_due(date(2026, 7, 30), SUN_JUL_19), "in 11 days (Jul 30)")

    def test_none_is_no_date(self):
        self.assertEqual(utils.describe_due(None, SUN_JUL_19), "no date set")

    def test_end_of_this_week_sunday_extends(self):
        # On a Sunday, "this week" runs through the FOLLOWING Sunday (7 days),
        # so a mid-week due date is inside the window.
        self.assertEqual(utils.end_of_this_week(SUN_JUL_19), date(2026, 7, 26))
        self.assertLessEqual(WED_JUL_22, utils.end_of_this_week(SUN_JUL_19))
        # On a weekday, it runs to the coming Sunday.
        self.assertEqual(utils.end_of_this_week(WED_JUL_22), date(2026, 7, 26))


# ===========================================================================
# P4 — todos "this week" window includes a mid-week due date
# ===========================================================================
class TestP4TodosWindow(unittest.TestCase):
    def _rows(self):
        return [
            {"id": 5, "title": "overdue task", "due_date": date(2026, 7, 15),
             "status": "active", "person": None},
            {"id": 6, "title": "renewal deck", "due_date": WED_JUL_22,
             "status": "active", "person": None},
            {"id": 7, "title": "undated task", "due_date": None,
             "status": "active", "person": None},
        ]

    def test_wednesday_shows_and_has_id(self):
        with patch.object(dossier, "_ct_today", return_value=SUN_JUL_19), \
             patch.object(dossier, "execute_query", return_value=self._rows()):
            out = dossier.todos("week")
        # The bug: Jul 22 was pushed to "next week" and dropped. It must show now,
        # with its id (P5) and absolute date (P2).
        self.assertIn("renewal deck (#6)", out)
        self.assertIn("Jul 22", out)
        self.assertIn("This week", out)
        # Undated items still show; overdue still shows.
        self.assertIn("undated task (#7)", out)
        self.assertIn("overdue task (#5)", out)


# ===========================================================================
# P5 — commitment ids + close by id
# ===========================================================================
class TestP5CloseById(unittest.TestCase):
    def test_close_by_id_closes_active(self):
        row = {"id": 6, "title": "renewal deck", "status": "active"}
        with patch.object(commitments, "get_commitment", return_value=row), \
             patch.object(commitments, "execute_write") as ew:
            result = commitments.close_commitment_by_id(6)
        self.assertEqual(result, {"status": "closed", "title": "renewal deck", "id": 6})
        ew.assert_called_once()

    def test_close_by_id_missing_is_not_found(self):
        with patch.object(commitments, "get_commitment", return_value=None), \
             patch.object(commitments, "list_commitments", return_value=[]):
            result = commitments.close_commitment_by_id(99)
        self.assertEqual(result["status"], "not_found")

    def test_close_by_id_already_closed_is_not_found(self):
        row = {"id": 6, "title": "x", "status": "closed"}
        with patch.object(commitments, "get_commitment", return_value=row), \
             patch.object(commitments, "list_commitments", return_value=[]):
            result = commitments.close_commitment_by_id(6)
        self.assertEqual(result["status"], "not_found")

    def test_list_and_close_result_carry_id(self):
        rows = [{"id": 6, "title": "renewal deck", "client": "Acme",
                 "due_date": WED_JUL_22, "created_at": "2026-07-10"}]
        listing = commitments.format_commitments_list(rows)
        self.assertIn("(#6)", listing)
        self.assertIn("close #<id>", listing)
        closed = commitments.format_close_result({"status": "closed", "title": "renewal deck", "id": 6})
        self.assertIn("(#6)", closed)


# ===========================================================================
# P1/P2/P3 — deterministic morning brief
# ===========================================================================
class TestP1MorningBriefRender(unittest.TestCase):
    def test_header_equals_relative_anchor(self):
        # P1 invariant: the header date and the anchor for every relative line are
        # the SAME today. Compose with a due-soon commitment and assert the line's
        # relative phrase matches describe_due computed from the header's today.
        rows = [{"id": 6, "title": "renewal deck", "due_date": WED_JUL_22, "client": ""}]
        out = morning_brief.render_brief(
            SUN_JUL_19, meeting_lines=[], commitment_rows=rows, inbox_count=3,
        )
        self.assertIn("Morning Brief — Sunday, Jul 19", out)
        self.assertIn(utils.describe_due(WED_JUL_22, SUN_JUL_19), out)  # "this Wednesday (Jul 22)"
        self.assertIn("(#6)", out)

    def test_inbox_line_live_and_omittable(self):
        # A concrete count renders; zero → inbox zero; None → the line is omitted
        # entirely (P3: never a stale number).
        self.assertIn("3 threads need", morning_brief.render_brief(
            SUN_JUL_19, meeting_lines=[], commitment_rows=[], inbox_count=3))
        self.assertIn("inbox zero", morning_brief.render_brief(
            SUN_JUL_19, meeting_lines=[], commitment_rows=[], inbox_count=0))
        omitted = morning_brief.render_brief(
            SUN_JUL_19, meeting_lines=[], commitment_rows=[], inbox_count=None)
        self.assertNotIn("Inbox", omitted)

    def test_stale_line(self):
        items = [{"id": "abcdef1234", "title": "Schedule 60min with Google Cloud"}]
        line = morning_brief.render_stale_line(items)
        self.assertIn("Stale items (1)", line)
        self.assertIn("Schedule 60min with Google Cloud", line)
        self.assertEqual(morning_brief.render_stale_line([]), "")


# ===========================================================================
# P7 — reminder backoff + caps (pure decision)
# ===========================================================================
class TestP7ReminderDue(unittest.TestCase):
    def _now(self):
        return datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    def test_first_reminder_fires(self):
        self.assertTrue(morning_brief.reminder_due(
            sent_count=0, reminders_today=0, last_reminded_at=None, now=self._now()))

    def test_daily_cap(self):
        self.assertFalse(morning_brief.reminder_due(
            sent_count=1, reminders_today=morning_brief.MAX_REMINDERS_PER_DAY,
            last_reminded_at=self._now() - timedelta(hours=48), now=self._now()))

    def test_backoff_interval_holds(self):
        # 1 sent → next only after 4h; 3h elapsed → hold.
        self.assertFalse(morning_brief.reminder_due(
            sent_count=1, reminders_today=0,
            last_reminded_at=self._now() - timedelta(hours=3), now=self._now()))
        # 5h elapsed → fire.
        self.assertTrue(morning_brief.reminder_due(
            sent_count=1, reminders_today=0,
            last_reminded_at=self._now() - timedelta(hours=5), now=self._now()))

    def test_escalation_widens(self):
        # 3 sent → daily (24h) spacing; 10h elapsed → hold.
        self.assertFalse(morning_brief.reminder_due(
            sent_count=3, reminders_today=0,
            last_reminded_at=self._now() - timedelta(hours=10), now=self._now()))

    def test_stops_after_max(self):
        # Exhausted budget → never pings again (demoted to the brief instead).
        self.assertFalse(morning_brief.reminder_due(
            sent_count=morning_brief.MAX_REMINDERS, reminders_today=0,
            last_reminded_at=self._now() - timedelta(days=30), now=self._now()))


# ===========================================================================
# help — generated registry
# ===========================================================================
class TestHelpRegistry(unittest.TestCase):
    def test_every_command_renders(self):
        # Introspective: a command cannot be routable without appearing in help.
        out = help_registry.render_help()
        for cmd in help_registry.COMMANDS:
            self.assertIn(cmd.phrase, out, f"{cmd.phrase} missing from help output")

    def test_previously_undiscoverable_commands_present(self):
        out = help_registry.render_help()
        for needle in ("close #<id>", "close <title>", "done <words>", "morning brief"):
            self.assertIn(needle, out)

    def test_filter(self):
        out = help_registry.render_help("email")
        self.assertIn("inbox", out)
        self.assertNotIn("vault sync", out)

    def test_filter_miss(self):
        out = help_registry.render_help("zzzznotacommand")
        self.assertIn("No commands match", out)


# ===========================================================================
# Tier 2 — artemis.main routing (skips if flask absent)
# ===========================================================================
@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestP8DoneRouting(unittest.TestCase):
    def test_classify_shape(self):
        self.assertEqual(main._classify_done_arg("18c9f0a2b3d4"), "thread")
        self.assertEqual(main._classify_done_arg("#18c9f0a2b3d4"), "thread")
        self.assertEqual(main._classify_done_arg("follow up with jennifer"), "commitment")
        self.assertEqual(main._classify_done_arg("pay the vendor"), "commitment")
        self.assertEqual(main._classify_done_arg(""), "empty")

    def test_hex_routes_to_thread(self):
        post = {"channel_id": "c1", "id": "p1"}
        with patch.object(main, "_mm", MagicMock()) as mm, \
             patch.object(main, "resolve_thread_id", return_value="fullthreadid") as rt, \
             patch.object(main, "mark_done") as md:
            handled = main._handle_done_command(post, "done 18c9f0a2b3d4")
        self.assertTrue(handled)
        rt.assert_called_once()
        md.assert_called_once_with("fullthreadid")
        self.assertIn("DONE", mm.post_to_channel_id.call_args[0][1])

    def test_words_route_to_commitment(self):
        post = {"channel_id": "c1", "id": "p1"}
        with patch.object(main, "_mm", MagicMock()), \
             patch.object(main, "close_commitment",
                          return_value={"status": "closed", "title": "follow up with jennifer", "id": 8}) as cc, \
             patch.object(main, "format_close_result", return_value="closed!"):
            handled = main._handle_done_command(post, "done follow up with jennifer")
        self.assertTrue(handled)
        cc.assert_called_once_with("follow up with jennifer")

    def test_empty_and_nomatch_show_both_usages(self):
        post = {"channel_id": "c1", "id": "p1"}
        with patch.object(main, "_mm", MagicMock()) as mm:
            main._handle_done_command(post, "done")
            reply = mm.post_to_channel_id.call_args[0][1]
        self.assertIn("thread-id", reply)
        self.assertIn("commitment", reply)
        with patch.object(main, "_mm", MagicMock()) as mm, \
             patch.object(main, "resolve_thread_id", return_value=None):
            main._handle_done_command(post, "done deadbeef1234")
            reply = mm.post_to_channel_id.call_args[0][1]
        self.assertIn("No inbox thread matches", reply)


@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestP6DispositionEcho(unittest.TestCase):
    EIGHT_LINES = (
        "archive 1\narchive 2\narchive 3\n"
        "file 5 as billing\ndelete 7\nspam 9\n"
        "14 founder loan\n20 vc intro"
    )

    def test_batch_parse_accounts_for_every_line(self):
        groups, unrecognized = main._parse_disposition_batch(self.EIGHT_LINES)
        self.assertEqual(len(groups), 6)
        self.assertEqual(unrecognized, ["14 founder loan", "20 vc intro"])

    def test_handler_echoes_unrecognized_before_confirm(self):
        # This batch contains delete/spam → propose-then-confirm path (no Gmail).
        ch = "c1"
        with patch.object(main, "_mm", MagicMock()) as mm, \
             patch.dict(main._inbox_listing_state,
                        {ch: {"offset": 0, "total": 30,
                              "mapping": {i: f"m{i}" for i in range(1, 31)}}}, clear=True):
            handled = main._handle_disposition_command(
                {"channel_id": ch, "id": "p1"}, self.EIGHT_LINES)
            reply = mm.post_to_channel_id.call_args[0][1]
        self.assertTrue(handled)
        self.assertIn("Didn't understand", reply)
        self.assertIn("14 founder loan", reply)
        self.assertIn("20 vc intro", reply)
        self.assertIn("Reply `yes`", reply)


@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestHelpAndBriefHandlers(unittest.TestCase):
    def test_help_matches_and_filters(self):
        post = {"channel_id": "c1", "id": "p1"}
        with patch.object(main, "_mm", MagicMock()) as mm:
            self.assertTrue(main._handle_help_command(post, "help"))
            self.assertIn("Artemis commands", mm.post_to_channel_id.call_args[0][1])
        with patch.object(main, "_mm", MagicMock()) as mm:
            self.assertTrue(main._handle_help_command(post, "help email"))
        # A longer phrase is NOT a help command.
        with patch.object(main, "_mm", MagicMock()):
            self.assertFalse(main._handle_help_command(post, "help me draft an email"))

    def test_morning_brief_routes_to_composer(self):
        post = {"channel_id": "c1", "id": "p1"}
        sched = MagicMock()
        sched.compose_morning_brief.return_value = "THE BRIEF"
        with patch.object(main, "_mm", MagicMock()) as mm, \
             patch.object(main, "_sched", sched):
            self.assertTrue(main._handle_morning_brief_command(post, "morning brief"))
            self.assertEqual(mm.post_to_channel_id.call_args[0][1], "THE BRIEF")
        # `brief jeremy` is a dossier meeting package, not the morning brief.
        with patch.object(main, "_mm", MagicMock()), patch.object(main, "_sched", sched):
            self.assertFalse(main._handle_morning_brief_command(post, "brief jeremy"))


# ===========================================================================
# P9 — empty-digest render (skips if vault/requests absent)
# ===========================================================================
try:
    from artemis import vault  # noqa: E402
    _VAULT_OK = True
except Exception:  # pragma: no cover
    vault = None
    _VAULT_OK = False


@unittest.skipUnless(_VAULT_OK, "artemis.vault (requests) unavailable")
class TestP9EmptyDigest(unittest.TestCase):
    def test_no_prompt_line_when_empty(self):
        with patch.object(vault, "render_digest",
                          return_value=("✅ No pending proposals. `vault sync` to pull new notes.", {})), \
             patch.object(vault, "_pat_expiry_warning", return_value=None), \
             patch.object(vault, "_journal_diff_section", return_value=""), \
             patch.object(vault, "_state_get", return_value=None):
            section = vault.morning_brief_section(mirror="/tmp/x")
        self.assertNotIn("Say `digest` to approve/reject", section)

    def test_prompt_line_present_when_pending(self):
        with patch.object(vault, "render_digest",
                          return_value=("📝 Pending proposals (2 pending): …", {1: {}, 2: {}})), \
             patch.object(vault, "_pat_expiry_warning", return_value=None), \
             patch.object(vault, "_journal_diff_section", return_value=""), \
             patch.object(vault, "_state_get", return_value=None):
            section = vault.morning_brief_section(mirror="/tmp/x")
        self.assertIn("Say `digest` to approve/reject", section)


if __name__ == "__main__":
    unittest.main(verbosity=2)
