"""Tests for PB-010 — Meeting Intelligence / Colleague Dossiers.

Two tiers (mirrors test_guardrails.py):

  * MOCKED unit tests (always run) — pure parsing/resolution/routing logic plus
    targeted-mock tests of the write paths (capture immutability, malformed-LLM
    safety, commitment origin, CT-anchored to-do windows, deterministic intent).
    No AWS/RDS needed.

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migrations 001+020+024 apply clean, and the real dossier.py functions run
    their real SQL against a throwaway DB: capture end-to-end + immutability,
    unknown→stub, the draft/approve/edit/drop state machine (incl. "a draft never
    closes a loop"), brief rendering, and commitment integration.

Run:
    python3 -m artemis.test_dossier
    python3 artemis/test_dossier.py
"""

import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block AWS/RDS access at import (pool/secrets are lazy, but be defensive).
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import commitments, dossier  # noqa: E402
from artemis import intent as intent_mod  # noqa: E402

# artemis.main pulls in flask (present on the box/CI, sometimes not locally).
# Tests that exercise the mention-router wiring skip when it can't be imported.
try:
    import artemis.main as artemis_main  # noqa: E402
    _MAIN_OK = True
except Exception as _main_exc:  # pragma: no cover
    artemis_main = None
    _MAIN_OK = False

_MIG_001 = (_REPO_ROOT / "migrations" / "001_create_acos_schema.sql").read_text()
_MIG_020 = (_REPO_ROOT / "migrations" / "020_commitments.sql").read_text()
_MIG_024 = (_REPO_ROOT / "migrations" / "024_dossier.sql").read_text()
_MIG_025 = (_REPO_ROOT / "migrations" / "025_dossier_approve.sql").read_text()


# ============================================================================
# MOCKED — pure logic (always run)
# ============================================================================

class TestNoSqlite(unittest.TestCase):
    def test_module_has_no_sqlite(self):
        self.assertFalse(hasattr(dossier, "sqlite3"))

    def test_source_has_no_sqlite(self):
        # No sqlite import/driver usage (the docstring may mention SQLite in prose).
        src = Path(dossier.__file__).read_text().lower()
        self.assertNotIn("import sqlite3", src)
        self.assertNotIn("sqlite3.", src)


class TestDetectDossierIntent(unittest.TestCase):
    def test_each_trigger_routes(self):
        cases = {
            "met with dennis about logistics": "capture",
            "brief jennifer about databricks": "brief",
            "prepare a meeting package for jeremy": "brief",
            "i'm meeting with jeremy about x": "brief",
            "remind me to follow up friday": "remind",
            "dossier review": "dossier",
            "dossier show jennifer": "dossier",
            "dossier new sarah": "dossier",
            "dossier set jennifer needs: x": "dossier",
            "approve all": "approve",
            "approve 1-4": "approve",
            "drop 4": "drop",
            "edit 2: new text": "edit",
            "what's on the to dos today": "todos",
            "whats on the todos this week": "todos",
            # §3.5 mid-sentence "remind me to" (interaction + reminder in one line)
            "I emailed Jennifer about the connector, remind me to follow up friday": "remind",
        }
        for text, expected in cases.items():
            self.assertEqual(dossier_detect(text), expected, f"{text!r} → {expected}")

    def test_non_triggers_return_none(self):
        for text in ["what's the weather", "add greg as a lead", "sleep 7 energy 5",
                     "archive 3", "remember to buy milk", "dropbox link",
                     "to do the thing properly", "let me tell you about databricks"]:
            self.assertIsNone(dossier_detect(text), text)


def dossier_detect(text):
    return intent_mod.detect_dossier_intent(text)


@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestDeterministicNotOverridable(unittest.TestCase):
    """A positive dossier match must reach the dossier handler and NEVER the LLM
    classifier — even when route_intent would return general_reply."""

    def test_met_with_never_reaches_router(self):
        main = artemis_main
        post = {"channel_id": "c1", "id": "p1", "message": "met with dennis about logistics"}
        with patch.object(main, "_mm", MagicMock()), \
             patch("artemis.dossier.capture_meeting", return_value="captured") as cap, \
             patch("artemis.intent.route_intent") as router:
            handled = main._handle_dossier_command(post, post["message"])
        self.assertTrue(handled)
        cap.assert_called_once()
        router.assert_not_called()

    def test_brief_never_reaches_router(self):
        main = artemis_main
        post = {"channel_id": "c1", "id": "p1", "message": "brief jeremy about x"}
        with patch.object(main, "_mm", MagicMock()), \
             patch("artemis.dossier.brief", return_value="pkg") as br, \
             patch("artemis.intent.route_intent") as router:
            handled = main._handle_dossier_command(post, post["message"])
        self.assertTrue(handled)
        br.assert_called_once()
        router.assert_not_called()

    def test_approve_without_review_falls_through(self):
        """`approve 1` with no pending review must NOT be handled here (returns
        False so it can reach the LLM) — the review-context gate."""
        main = artemis_main
        main._dossier_review_state.pop("c1", None)
        post = {"channel_id": "c1", "id": "p1", "message": "approve 1"}
        with patch.object(main, "_mm", MagicMock()):
            handled = main._handle_dossier_command(post, "approve 1")
        self.assertFalse(handled)


class TestAttendeeResolution(unittest.TestCase):
    def _rows(self, *rows):
        return list(rows)

    def test_exact_slug(self):
        with patch.object(dossier, "_find_dossiers",
                          return_value=[{"slug": "jennifer", "full_name": "Jennifer Xu", "active": True}]):
            kind, val = dossier.resolve_attendee("jennifer")
        self.assertEqual(kind, "resolved")
        self.assertEqual(val["slug"], "jennifer")

    def test_first_name_unique(self):
        with patch.object(dossier, "_find_dossiers",
                          return_value=[{"slug": "dennis-r", "full_name": "Dennis Rowe", "active": True}]):
            kind, val = dossier.resolve_attendee("dennis")
        self.assertEqual(kind, "resolved")

    def test_ambiguous(self):
        rows = [
            {"slug": "chris-a", "full_name": "Chris Adams", "active": True},
            {"slug": "chris-b", "full_name": "Chris Boone", "active": True},
        ]
        with patch.object(dossier, "_find_dossiers", return_value=rows):
            kind, val = dossier.resolve_attendee("chris")
        self.assertEqual(kind, "ambiguous")
        self.assertEqual(len(val), 2)

    def test_unknown(self):
        with patch.object(dossier, "_find_dossiers", return_value=[]):
            kind, val = dossier.resolve_attendee("zoe")
        self.assertEqual(kind, "unknown")
        self.assertEqual(val, "zoe")


class TestCaptureParsing(unittest.TestCase):
    def test_names_topic(self):
        p = dossier._parse_capture_directive("met with jeremy & dennis about week-one logistics")
        self.assertEqual(p["names"], ["jeremy", "dennis"])
        self.assertEqual(p["topic"], "week-one logistics")
        self.assertIsNone(p["date"])

    def test_split_names_variants(self):
        self.assertEqual(dossier._split_names("a, b and c"), ["a", "b", "c"])
        self.assertEqual(dossier._split_names("jeremy & dennis"), ["jeremy", "dennis"])

    def test_not_met_with(self):
        self.assertIsNone(dossier._parse_capture_directive("hello there"))

    # ── B2: parser hardening ──
    def test_date_token_ends_attendee_segment(self):
        # The spec's exact regression: garbage date/time tokens must NOT become
        # attendee stubs. Only [dennis]; occurred_on parsed from the stated date.
        with patch.object(dossier, "_ct_today", return_value=date(2026, 7, 17)):
            p = dossier._parse_capture_directive(
                "met with dennis today Friday, July 17, 2026 @ 1400 cst about parser test")
        self.assertEqual(p["names"], ["dennis"])
        self.assertEqual(p["date"], date(2026, 7, 17))
        self.assertEqual(p["topic"], "parser test")

    def test_reject_tokens_with_digits_and_at(self):
        p = dossier._parse_capture_directive("met with dennis 1400 about x")
        self.assertEqual(p["names"], ["dennis"])

    def test_stated_relative_date_yesterday(self):
        with patch.object(dossier, "_ct_today", return_value=date(2026, 7, 17)):
            p = dossier._parse_capture_directive("met with jennifer yesterday about pricing")
        self.assertEqual(p["date"], date(2026, 7, 16))
        self.assertEqual(p["names"], ["jennifer"])

    def test_no_stated_date_is_none(self):
        p = dossier._parse_capture_directive("met with jeremy about the roadmap")
        self.assertIsNone(p["date"])

    def test_topic_clamped_at_dash_and_length(self):
        self.assertEqual(dossier._clamp_topic("pricing - and other stuff"), "pricing")
        self.assertEqual(dossier._clamp_topic("x" * 200), "x" * 80)
        self.assertEqual(dossier._clamp_topic("line one\nline two"), "line one")


class TestFmtDate(unittest.TestCase):
    """B4: dates are code-rendered with the weekday computed in code."""

    def test_weekday_correctness(self):
        self.assertEqual(dossier.fmt_date(date(2026, 7, 22)), "Wednesday, Jul 22")
        self.assertEqual(dossier.fmt_date(date(2026, 7, 17)), "Friday, Jul 17")

    def test_accepts_iso_string(self):
        self.assertEqual(dossier.fmt_date("2026-07-22"), "Wednesday, Jul 22")

    def test_none_is_empty(self):
        self.assertEqual(dossier.fmt_date(None), "")


class TestAttachmentPolicy(unittest.TestCase):
    def test_text_extension_accepted(self):
        self.assertEqual(
            dossier._extract_attachment_text({"filename": "n.txt", "ext": "txt", "content": b"hello"}),
            "hello",
        )

    def test_binary_extension_rejected(self):
        self.assertIsNone(
            dossier._extract_attachment_text({"filename": "n.pdf", "ext": "pdf", "content": b"%PDF"}),
        )

    def test_undecodable_bytes_rejected(self):
        self.assertIsNone(
            dossier._extract_attachment_text({"filename": "n.txt", "ext": "txt", "content": b"\xff\xfe\x00\x01"}),
        )

    def test_predecoded_text_path(self):
        self.assertEqual(
            dossier._extract_attachment_text({"filename": "n.md", "ext": "md", "text": "notes"}),
            "notes",
        )


class TestCaptureImmutability(unittest.TestCase):
    """raw_notes must be stored byte-identical to the input; no path mutates it."""

    def test_raw_notes_byte_identical(self):
        captured = {}

        def fake_one(sql, params=None):
            if "INSERT INTO acos.dossier_meeting" in sql:
                captured["raw_notes"] = params[2]
                return {"meeting_id": 1, "occurred_on": params[0], "topic": params[1],
                        "raw_notes": params[2], "source_filename": params[3]}
            if "SELECT * FROM acos.dossier_meeting" in sql:
                return {"meeting_id": 1, "occurred_on": date(2026, 7, 17), "topic": "x",
                        "raw_notes": captured.get("raw_notes", ""), "source_filename": None}
            return None

        def fake_query(sql, params=None):
            if "dossier_meeting_attendee a" in sql:
                return [{"full_name": "Jennifer Xu"}]
            return []

        notes = "LINE ONE\n  indented two\nLINE three — em-dash & stuff"
        full_message = f"met with jennifer about x\n{notes}"
        with patch.object(dossier, "_find_dossiers",
                          return_value=[{"dossier_id": 1, "slug": "jennifer",
                                         "full_name": "Jennifer Xu", "active": True}]), \
             patch.object(dossier, "execute_one", side_effect=fake_one), \
             patch.object(dossier, "execute_query", side_effect=fake_query), \
             patch.object(dossier, "execute_write", MagicMock()), \
             patch.object(dossier, "_audit", MagicMock()), \
             patch.object(dossier, "draft_extraction", return_value="drafted."):
            reply = dossier.capture_meeting(full_message)
        # B2 rule 1: raw_notes is the ENTIRE original message (never subtracted).
        self.assertEqual(captured["raw_notes"], full_message)
        self.assertIn("Captured meeting #1", reply)


class TestMalformedExtraction(unittest.TestCase):
    """A malformed LLM output yields NO partial writes and surfaces the failure."""

    def test_none_extraction_no_writes(self):
        meeting = {"meeting_id": 1, "occurred_on": date(2026, 7, 17), "raw_notes": "notes"}
        writes = MagicMock()
        with patch.object(dossier, "execute_one", return_value=meeting), \
             patch.object(dossier, "execute_query",
                          return_value=[{"dossier_id": 1, "slug": "j", "full_name": "Jennifer Xu"}]), \
             patch.object(dossier, "_build_extraction_context", return_value=""), \
             patch.object(dossier, "execute_write", writes), \
             patch.object(dossier, "_llm_extract", return_value=None), \
             patch.object(commitments, "add_commitment", MagicMock()) as add, \
             patch.object(dossier, "_audit", MagicMock()):
            msg = dossier.draft_extraction(1)
        writes.assert_not_called()
        add.assert_not_called()
        self.assertIn("failed", msg.lower())


class TestCommitmentOrigin(unittest.TestCase):
    """dossier_id attachment + draft-vs-immediate by origin."""

    def test_direct_commitment_is_immediate_active(self):
        d = {"dossier_id": 7, "slug": "jennifer", "full_name": "Jennifer Xu"}
        with patch.object(dossier, "_find_dossier_in_text", return_value=d), \
             patch.object(commitments, "add_commitment", return_value=42) as add, \
             patch.object(commitments, "get_commitment",
                          return_value={"title": "follow up with jennifer", "due_date": date(2026, 7, 22),
                                        "status": "active"}), \
             patch.object(dossier, "_audit", MagicMock()):
            reply = dossier.direct_commitment("remind me to follow up with jennifer wednesday")
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs["status"], "active")
        self.assertEqual(kwargs["dossier_id"], 7)
        self.assertIn("Reminder logged", reply)

    def test_interaction_phrasing_also_drafts_touch(self):
        d = {"dossier_id": 7, "slug": "jennifer", "full_name": "Jennifer Xu"}
        entries = []

        def fake_one(sql, params=None):
            if "INSERT INTO acos.dossier_entry" in sql:
                entries.append(params)
                return {"entry_id": 99}
            return None

        with patch.object(dossier, "_find_dossier_in_text", return_value=d), \
             patch.object(commitments, "add_commitment", return_value=42), \
             patch.object(commitments, "get_commitment",
                          return_value={"title": "follow up", "due_date": None, "status": "active"}), \
             patch.object(dossier, "execute_one", side_effect=fake_one), \
             patch.object(dossier, "_audit", MagicMock()):
            reply = dossier.direct_commitment(
                "I emailed Jennifer about the connector, remind me to follow up friday")
        self.assertEqual(len(entries), 1)                      # one draft touch entry
        self.assertIn("(inferred)", entries[0][2])             # entry_text carries the inferred tag
        self.assertIn("drafted a log touch", reply)

    def test_apply_draft_action_item_is_draft(self):
        meeting = {"meeting_id": 5, "occurred_on": date(2026, 7, 17)}
        d = {"dossier_id": 3, "slug": "dennis", "full_name": "Dennis Rowe"}
        extraction = {"log_entry": "Met Dennis; discussed onboarding.",
                      "action_items": [{"text": "send onboarding doc", "due_date": "2026-07-20"}]}
        with patch.object(dossier, "execute_one", return_value={"entry_id": 1}), \
             patch.object(dossier, "execute_write", MagicMock()), \
             patch.object(commitments, "add_commitment", return_value=1) as add, \
             patch.object(dossier, "_audit", MagicMock()):
            dossier._apply_draft(meeting, d, extraction)
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs["status"], "draft")
        self.assertEqual(kwargs["dossier_id"], 3)
        self.assertEqual(kwargs["meeting_id"], 5)
        self.assertEqual(kwargs["due_date"], date(2026, 7, 20))


class TestTodoWindows(unittest.TestCase):
    """CT-anchored grouping. Freezing _ct_today (the CT seam) makes the 7pm-UTC
    boundary deterministic: 'today' is the CT date, never the UTC date."""

    def test_grouping(self):
        today = date(2026, 7, 15)  # Wednesday
        eow = today + timedelta(days=(6 - today.weekday()))
        rows = [
            {"id": 1, "title": "OVERDUE", "due_date": today - timedelta(days=2), "status": "active", "person": None},
            {"id": 2, "title": "TODAY", "due_date": today, "status": "active", "person": "Jennifer Xu"},
            {"id": 3, "title": "WEEK", "due_date": eow, "status": "active", "person": None},
            {"id": 4, "title": "UNDATED", "due_date": None, "status": "active", "person": None},
            {"id": 5, "title": "DRAFTITEM", "due_date": today, "status": "draft", "person": "Jeremy"},
        ]
        with patch.object(dossier, "_ct_today", return_value=today), \
             patch.object(dossier, "execute_query", return_value=rows):
            out = dossier.todos("week")
        self.assertIn("Overdue", out)
        self.assertIn("OVERDUE", out)
        self.assertIn("TODAY", out)
        self.assertIn("· Jennifer Xu", out)          # dossier-linked item shows the person
        self.assertIn("WEEK", out)
        self.assertIn("UNDATED", out)
        self.assertIn("pending review (1)", out)      # draft listed separately
        self.assertNotIn("DRAFTITEM", out.split("pending review")[0])  # not in active groups

    def test_today_window_excludes_rest_of_week(self):
        today = date(2026, 7, 15)
        rows = [
            {"id": 1, "title": "TODAY", "due_date": today, "status": "active", "person": None},
            {"id": 2, "title": "LATERWEEK", "due_date": today + timedelta(days=2), "status": "active", "person": None},
        ]
        with patch.object(dossier, "_ct_today", return_value=today), \
             patch.object(dossier, "execute_query", return_value=rows):
            out = dossier.todos("today")
        self.assertIn("TODAY", out)
        self.assertNotIn("LATERWEEK", out)

    def test_next_week_window_boundaries(self):
        # Wed 2026-07-15 → next week is Mon 2026-07-20 .. Sun 2026-07-26.
        today = date(2026, 7, 15)
        rows = [
            {"id": 1, "title": "THISWEEK", "due_date": date(2026, 7, 16), "status": "active", "person": None},
            {"id": 2, "title": "NEXTMON", "due_date": date(2026, 7, 20), "status": "active", "person": None},
            {"id": 3, "title": "NEXTSUN", "due_date": date(2026, 7, 26), "status": "active", "person": None},
            {"id": 4, "title": "WEEKAFTER", "due_date": date(2026, 7, 27), "status": "active", "person": None},
        ]
        with patch.object(dossier, "_ct_today", return_value=today), \
             patch.object(dossier, "execute_query", return_value=rows):
            out = dossier.todos("next_week")
        self.assertIn("NEXTMON", out)     # Mon boundary included
        self.assertIn("NEXTSUN", out)     # Sun boundary included
        self.assertNotIn("THISWEEK", out)
        self.assertNotIn("WEEKAFTER", out)
        self.assertIn("Wednesday, Jul 22", "Wednesday, Jul 22")  # sanity: fmt weekday

    def test_tomorrow_window(self):
        today = date(2026, 7, 15)
        rows = [
            {"id": 1, "title": "TMR", "due_date": date(2026, 7, 16), "status": "active", "person": None},
            {"id": 2, "title": "TODAYITEM", "due_date": today, "status": "active", "person": None},
        ]
        with patch.object(dossier, "_ct_today", return_value=today), \
             patch.object(dossier, "execute_query", return_value=rows):
            out = dossier.todos("tomorrow")
        self.assertIn("TMR", out)
        self.assertNotIn("TODAYITEM", out)


class TestExtractionPromptFraming(unittest.TestCase):
    """B3: the extraction prompt forbids adjudicating whether the meeting happened
    ('no direct interaction' for a meeting WITH the person was the live bug)."""

    def test_prompt_states_meeting_occurred(self):
        sys_prompt = dossier._EXTRACT_SYSTEM.lower()
        self.assertIn("occurred", sys_prompt)
        self.assertIn("future-tense", sys_prompt)
        self.assertIn("no interaction", sys_prompt)  # the thing it must NOT write

    def test_future_tense_notes_still_write_an_entry(self):
        # Mock LLM returns a proper entry (as the hardened prompt intends) — the
        # apply path records that the meeting happened, not a non-occurrence.
        meeting = {"meeting_id": 9, "occurred_on": date(2026, 7, 17)}
        d = {"dossier_id": 3, "slug": "dennis", "full_name": "Dennis Shields"}
        extraction = {"log_entry": "Met Dennis; agreed he will send the org chart next week."}
        entries = []

        def fake_one(sql, params=None):
            if "INSERT INTO acos.dossier_entry" in sql:
                entries.append(params)
                return {"entry_id": 1}
            return None

        with patch.object(dossier, "execute_one", side_effect=fake_one), \
             patch.object(dossier, "execute_write", MagicMock()), \
             patch.object(dossier, "_audit", MagicMock()):
            counts = dossier._apply_draft(meeting, d, extraction)
        self.assertEqual(counts["entries"], 1)
        self.assertIn("will send the org chart", entries[0][3])  # entry_text recorded


class TestWhenParsing(unittest.TestCase):
    def test_iso_date(self):
        task, due = dossier._parse_when("submit the report 2026-08-01")
        self.assertEqual(due, date(2026, 8, 1))
        self.assertNotIn("2026", task)

    def test_in_n_days(self):
        with patch.object(dossier, "_ct_today", return_value=date(2026, 7, 15)):
            _, due = dossier._parse_when("ping them in 3 days")
        self.assertEqual(due, date(2026, 7, 18))

    def test_weekday_next_occurrence(self):
        # Wednesday 2026-07-15; "friday" → 2026-07-17
        with patch.object(dossier, "_ct_today", return_value=date(2026, 7, 15)):
            _, due = dossier._parse_when("follow up friday")
        self.assertEqual(due, date(2026, 7, 17))

    def test_bare_weekday_same_day_rolls_forward(self):
        # Wednesday; "wednesday" → next Wednesday (+7), never today
        with patch.object(dossier, "_ct_today", return_value=date(2026, 7, 15)):
            _, due = dossier._parse_when("do it wednesday")
        self.assertEqual(due, date(2026, 7, 22))

    def test_no_date(self):
        task, due = dossier._parse_when("just a plain task")
        self.assertIsNone(due)
        self.assertEqual(task, "just a plain task")


@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestParseNumbersReuse(unittest.TestCase):
    """approve number parsing reuses main._parse_numbers (the E3 range/& parser)."""

    def test_ranges_and_amp(self):
        _parse_numbers = artemis_main._parse_numbers
        self.assertEqual(_parse_numbers("1-4"), [1, 2, 3, 4])
        self.assertEqual(_parse_numbers("1 & 3"), [1, 3])
        self.assertEqual(_parse_numbers("1 and 3"), [1, 3])
        self.assertEqual(_parse_numbers("2, 5, 9"), [2, 5, 9])
        self.assertEqual(_parse_numbers("1-3, 7"), [1, 2, 3, 7])


@unittest.skipUnless(_MAIN_OK, "artemis.main (flask) unavailable")
class TestBriefArgParsing(unittest.TestCase):
    def test_variants(self):
        _parse_brief_args = artemis_main._parse_brief_args
        self.assertEqual(_parse_brief_args("brief jeremy about databricks"), ("jeremy", "databricks"))
        self.assertEqual(_parse_brief_args("prepare a meeting package for jeremy"), ("jeremy", None))
        self.assertEqual(
            _parse_brief_args("i'm meeting with jeremy about databricks, prepare a meeting package"),
            ("jeremy", "databricks"),
        )
        self.assertEqual(_parse_brief_args("brief jeremy & dennis"), ("jeremy & dennis", None))


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

_TEST_DB = "artemis_dossier_test"
_LIVE = False
_CONN = None


def _admin_connect():
    return psycopg2.connect(dbname="postgres", connect_timeout=2)


def _q(sql, params=None):
    with _CONN.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description:
            return [dict(r) for r in cur.fetchall()]
        return []


def _one(sql, params=None):
    rows = _q(sql, params)
    return dict(rows[0]) if rows else None


def _w(sql, params=None):
    with _CONN.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description:
            r = cur.fetchone()
            return dict(r) if r else None
        return None


def setUpModule():
    global _LIVE, _CONN
    try:
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB}")
            cur.execute(f"CREATE DATABASE {_TEST_DB}")
        admin.close()
        _CONN = psycopg2.connect(dbname=_TEST_DB)
        _CONN.autocommit = True
        with _CONN.cursor() as cur:
            cur.execute(_MIG_001)
            cur.execute(_MIG_020)
            cur.execute(_MIG_024)
            cur.execute(_MIG_025)
        _LIVE = True
    except Exception as e:
        sys.stderr.write(f"[test_dossier] live PG unavailable, skipping: {e}\n")
        _LIVE = False


def tearDownModule():
    global _CONN
    if _CONN:
        _CONN.close()
        _CONN = None
    if not _LIVE:
        return
    try:
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB}")
        admin.close()
    except Exception:
        pass


class LiveBase(unittest.TestCase):
    def setUp(self):
        if not _LIVE:
            self.skipTest("no local Postgres")
        # Point the module DB helpers at the throwaway DB; no-op the audit writer.
        self._orig = {
            "d_q": dossier.execute_query, "d_one": dossier.execute_one, "d_w": dossier.execute_write,
            "d_audit": dossier.log_audit,
            "c_q": commitments.execute_query, "c_one": commitments.execute_one, "c_w": commitments.execute_write,
        }
        dossier.execute_query, dossier.execute_one, dossier.execute_write = _q, _one, _w
        dossier.log_audit = lambda *a, **k: ""
        commitments.execute_query, commitments.execute_one, commitments.execute_write = _q, _one, _w
        for t in ("dossier_idea", "dossier_loop", "dossier_entry",
                  "dossier_meeting_attendee", "dossier_meeting", "dossier"):
            with _CONN.cursor() as cur:
                cur.execute(f"TRUNCATE acos.{t} RESTART IDENTITY CASCADE")
        with _CONN.cursor() as cur:
            cur.execute("TRUNCATE acos.commitments RESTART IDENTITY CASCADE")

    def tearDown(self):
        dossier.execute_query = self._orig["d_q"]
        dossier.execute_one = self._orig["d_one"]
        dossier.execute_write = self._orig["d_w"]
        dossier.log_audit = self._orig["d_audit"]
        commitments.execute_query = self._orig["c_q"]
        commitments.execute_one = self._orig["c_one"]
        commitments.execute_write = self._orig["c_w"]

    def _mk(self, slug, name, active=True, position=None, needs=None):
        return _one(
            "INSERT INTO acos.dossier (slug, full_name, active, position_terrain, needs_from_me) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (slug, name, active, position, needs),
        )


class TestLiveSchema(LiveBase):
    def test_tables_exist(self):
        rows = _q("SELECT table_name FROM information_schema.tables WHERE table_schema='acos'")
        names = {r["table_name"] for r in rows}
        for t in ("dossier", "dossier_meeting", "dossier_meeting_attendee",
                  "dossier_entry", "dossier_loop", "dossier_idea"):
            self.assertIn(t, names)

    def test_commitments_extended(self):
        cols = {r["column_name"] for r in _q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='acos' AND table_name='commitments'")}
        self.assertIn("dossier_id", cols)
        self.assertIn("meeting_id", cols)
        # due_date is now nullable
        nn = _one("SELECT is_nullable FROM information_schema.columns "
                  "WHERE table_schema='acos' AND table_name='commitments' AND column_name='due_date'")
        self.assertEqual(nn["is_nullable"], "YES")

    def test_migration_025_approve_rename(self):
        # B1: 025 renamed blessed_at→approved_at and the status check is draft/approved.
        cols = {r["column_name"] for r in _q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='acos' AND table_name='dossier_entry'")}
        self.assertIn("approved_at", cols)
        self.assertNotIn("blessed_at", cols)
        # the CHECK accepts 'approved' and rejects 'blessed'
        did = self._mk("z", "Z Person")["dossier_id"]
        _w("INSERT INTO acos.dossier_entry (dossier_id, entry_date, entry_text, status) "
           "VALUES (%s, %s, 'x', 'approved')", (did, date(2026, 7, 1)))
        with self.assertRaises(Exception):
            _w("INSERT INTO acos.dossier_entry (dossier_id, entry_date, entry_text, status) "
               "VALUES (%s, %s, 'x', 'blessed')", (did, date(2026, 7, 1)))

    def test_one_line_capture_keeps_full_raw_notes(self):
        # B2: a one-line note is NOT swallowed into topic — raw_notes = full message.
        self._mk("dennis", "Dennis Shields")
        msg = "met with dennis about parser test\nquick one-liner note"
        with patch.object(dossier, "_llm_extract", return_value={"log_entry": None}):
            dossier.capture_meeting(msg)
        m = _one("SELECT raw_notes, topic FROM acos.dossier_meeting ORDER BY meeting_id DESC LIMIT 1")
        self.assertEqual(m["raw_notes"], msg)          # entire message, verbatim
        self.assertEqual(m["topic"], "parser test")    # topic labeled, not subtracted
        self.assertGreater(len(m["raw_notes"].split()), 3)  # NOT 0-word (the bug)


class TestLiveCapture(LiveBase):
    def test_capture_end_to_end_and_immutability(self):
        self._mk("jennifer", "Jennifer Xu")
        notes = "She wants the connector demo.\nAsked about pricing — em-dash — & ampersand."
        extraction = {"log_entry": "Met Jennifer; discussed the connector demo.",
                      "open_loops": ["send pricing sheet"],
                      "ideas": [{"text": "co-author a case study", "cross_pollinate_slug": None}],
                      "action_items": [{"text": "email the demo link", "due_date": None}]}
        full_message = f"met with jennifer about connector\n{notes}"
        with patch.object(dossier, "_llm_extract", return_value=extraction):
            reply = dossier.capture_meeting(full_message)
        self.assertIn("Captured meeting #1", reply)
        m = _one("SELECT * FROM acos.dossier_meeting WHERE meeting_id=1")
        self.assertEqual(m["raw_notes"], full_message)         # B2: entire message, verbatim
        self.assertEqual(m["topic"], "connector")
        att = _q("SELECT * FROM acos.dossier_meeting_attendee WHERE meeting_id=1")
        self.assertEqual(len(att), 1)
        # drafts written
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_entry WHERE status='draft'")["c"], 1)
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_loop WHERE status='proposed'")["c"], 1)
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_idea WHERE status='proposed'")["c"], 1)
        self.assertEqual(_one("SELECT count(*) c FROM acos.commitments WHERE status='draft'")["c"], 1)

    def test_unknown_attendee_creates_inactive_stub(self):
        with patch.object(dossier, "_llm_extract", return_value={"log_entry": None}):
            reply = dossier.capture_meeting("met with zoe about intro\nquick sync")
        stub = _one("SELECT * FROM acos.dossier WHERE lower(full_name)='zoe'")
        self.assertIsNotNone(stub)
        self.assertFalse(stub["active"])
        self.assertIn("No dossier for zoe", reply)


class TestLiveStateMachine(LiveBase):
    def _seed_meeting(self, did):
        m = _one("INSERT INTO acos.dossier_meeting (occurred_on, topic, raw_notes) "
                 "VALUES (%s,'sync','notes') RETURNING *", (date(2026, 7, 17),))
        _w("INSERT INTO acos.dossier_meeting_attendee (meeting_id, dossier_id) VALUES (%s,%s)",
           (m["meeting_id"], did))
        return m

    def test_draft_never_closes_a_loop_until_approved(self):
        d = self._mk("dennis", "Dennis Rowe")
        did = d["dossier_id"]
        # an existing approved OPEN loop
        loop = _one("INSERT INTO acos.dossier_loop (dossier_id, loop_text, status) "
                    "VALUES (%s,'ship the POC','open') RETURNING *", (did,))
        m = self._seed_meeting(did)
        dossier._apply_draft(m, d, {"log_entry": "POC shipped.", "close_loops": [loop["loop_id"]]})
        # proposal only: loop still OPEN, closed_at NULL, closed_entry_id set
        row = _one("SELECT * FROM acos.dossier_loop WHERE loop_id=%s", (loop["loop_id"],))
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["closed_at"])
        self.assertIsNotNone(row["closed_entry_id"])
        # it surfaces as a pending 'loop_close' review item
        items = dossier.pending_items()
        self.assertTrue(any(it["type"] == "loop_close" and it["id"] == loop["loop_id"] for it in items))

    def test_approve_transitions(self):
        d = self._mk("dennis", "Dennis Rowe")
        did = d["dossier_id"]
        m = self._seed_meeting(did)
        dossier._apply_draft(m, d, {
            "log_entry": "Discussed onboarding.",
            "open_loops": ["confirm start date"],
            "ideas": [{"text": "intro to design team", "cross_pollinate_slug": None}],
            "action_items": [{"text": "send welcome packet", "due_date": None}],
        })
        _reply, mapping = dossier.render_review()
        self.assertTrue(mapping)
        dossier.approve_all(mapping)
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_entry WHERE status='approved'")["c"], 1)
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_loop WHERE status='open'")["c"], 1)
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_idea WHERE status='active'")["c"], 1)
        self.assertEqual(_one("SELECT count(*) c FROM acos.commitments WHERE status='active'")["c"], 1)
        # nothing left pending
        self.assertEqual(dossier.pending_items(), [])

    def test_draft_invisible_to_approved_surface(self):
        d = self._mk("dennis", "Dennis Rowe")
        did = d["dossier_id"]
        m = self._seed_meeting(did)
        dossier._apply_draft(m, d, {"log_entry": "Draft-only note."})
        # show() approved-only excludes the draft entry; --drafts includes it
        self.assertNotIn("Draft-only note", dossier.show("dennis"))
        self.assertIn("Draft-only note", dossier.show("dennis", include_drafts=True))

    def test_edit_replaces_and_approves(self):
        d = self._mk("dennis", "Dennis Rowe")
        did = d["dossier_id"]
        m = self._seed_meeting(did)
        dossier._apply_draft(m, d, {"log_entry": "original text"})
        _r, mapping = dossier.render_review()
        num = next(n for n, it in mapping.items() if it["type"] == "entry")
        dossier.edit_item(num, "corrected text", mapping)
        row = _one("SELECT * FROM acos.dossier_entry WHERE dossier_id=%s", (did,))
        self.assertEqual(row["entry_text"], "corrected text")
        self.assertEqual(row["status"], "approved")

    def test_drop_deletes_draft(self):
        d = self._mk("dennis", "Dennis Rowe")
        did = d["dossier_id"]
        m = self._seed_meeting(did)
        dossier._apply_draft(m, d, {"log_entry": "throwaway", "open_loops": ["temp loop"]})
        _r, mapping = dossier.render_review()
        loop_num = next(n for n, it in mapping.items() if it["type"] == "loop_open")
        dossier.drop_item(loop_num, mapping)
        self.assertEqual(_one("SELECT count(*) c FROM acos.dossier_loop WHERE status='proposed'")["c"], 0)


class TestLiveBrief(LiveBase):
    def test_brief_render(self):
        d = self._mk("jennifer", "Jennifer Xu", needs="Wants a warm intro to Databricks.")
        did = d["dossier_id"]
        _w("INSERT INTO acos.dossier_loop (dossier_id, loop_text, status) VALUES (%s,'send pricing','open')", (did,))
        _w("INSERT INTO acos.dossier_idea (dossier_id, idea_text, status) VALUES (%s,'co-author a case study','active')", (did,))
        _w("INSERT INTO acos.dossier_entry (dossier_id, entry_date, entry_text, status, approved_at) "
           "VALUES (%s,%s,'First intro call went well.','approved', now())", (did, date(2026, 7, 1)))
        out = dossier.brief("jennifer", "databricks")
        self.assertIn("Open loops", out)
        self.assertIn("send pricing", out)
        self.assertIn("Wants a warm intro", out)
        self.assertIn("co-author a case study", out)
        self.assertIn("First intro call", out)

    def test_empty_idea_bank_flag(self):
        self._mk("dennis", "Dennis Rowe", needs="Clarity on scope.")
        out = dossier.brief("dennis")
        self.assertIn("No ideas banked", out)

    def test_multi_person_dedup_shared_loop(self):
        a = self._mk("jeremy", "Jeremy Vale")
        b = self._mk("dennis", "Dennis Rowe")
        for did in (a["dossier_id"], b["dossier_id"]):
            _w("INSERT INTO acos.dossier_loop (dossier_id, loop_text, status) VALUES (%s,'align on the joint roadmap','open')", (did,))
        out = dossier.brief("jeremy & dennis")
        self.assertEqual(out.lower().count("align on the joint roadmap"), 1)  # shared loop shown once

    def test_draft_commitment_tagged_in_open_loops(self):
        d = self._mk("jennifer", "Jennifer Xu")
        commitments.add_commitment("approved task", date(2026, 7, 20), status="active", dossier_id=d["dossier_id"])
        commitments.add_commitment("draft task", None, status="draft", dossier_id=d["dossier_id"])
        out = dossier.brief("jennifer")
        self.assertIn("approved task", out)
        self.assertIn("[draft] draft task", out)


class TestLiveCommitmentIntegration(LiveBase):
    def test_dossier_id_attached_and_windowed(self):
        d = self._mk("jennifer", "Jennifer Xu")
        today = date(2026, 7, 15)
        commitments.add_commitment("overdue thing", today - timedelta(days=1),
                                   status="active", dossier_id=d["dossier_id"])
        commitments.add_commitment("today thing", today, status="active", dossier_id=d["dossier_id"])
        with patch.object(dossier, "_ct_today", return_value=today):
            out = dossier.todos("week")
        self.assertIn("overdue thing", out)
        self.assertIn("today thing", out)
        self.assertIn("· Jennifer Xu", out)
        # provenance persisted
        row = _one("SELECT dossier_id FROM acos.commitments WHERE title='today thing'")
        self.assertEqual(row["dossier_id"], d["dossier_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
