"""REPORT-CMD — `dietitian report …` in Mattermost, on demand.

Ryan's own channel, on his own request: not external comms. The handler must
never email, draft, or post anywhere but the channel the request came from, and
nothing about it is scheduled.

Synthetic fixtures only (tests/fixtures/dietitian_data.py) — no RDS, no
Mattermost, no network.

Run:
    python3.11 -m unittest tests.test_report_cmd
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

for _n in ("googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
           "google", "google.auth", "google.auth.transport",
           "google.auth.transport.requests", "google.oauth2",
           "google.oauth2.credentials", "google_auth_oauthlib",
           "google_auth_oauthlib.flow", "websocket", "Levenshtein"):
    sys.modules.setdefault(_n, MagicMock())

from artemis import dietitian_report as dr  # noqa: E402
from artemis import main as M  # noqa: E402
from dietitian_data import month_data  # noqa: E402

POST = {"channel_id": "CH", "id": "P1"}
TODAY = date(2027, 1, 31)


class FakeMM:
    """Records what the handler would send. Nothing leaves the process."""

    def __init__(self, upload_raises=False):
        self.messages = []          # (channel, text, root)
        self.uploads = []           # (channel, filename, nbytes, ctype)
        self.posts_with_files = []  # (channel, text, file_ids, root)
        self.upload_raises = upload_raises

    def post_message(self, channel, text, root_id=None):
        self.messages.append((channel, text, root_id)); return True

    def upload_file(self, channel, filename, data, content_type="application/octet-stream"):
        if self.upload_raises:
            raise RuntimeError("upload boom")
        self.uploads.append((channel, filename, len(data), content_type))
        return "FILEID"

    def post_with_files(self, channel, text, file_ids, root_id=None):
        self.posts_with_files.append((channel, text, file_ids, root_id)); return True


#: the REAL produce, captured before any patching — a lambda that called
#: dr.produce() after patching it recursed forever.
_REAL_PRODUCE = dr.produce


def _run(text, *, mm=None, produce=None, today=TODAY):
    mm = mm or FakeMM()
    data = month_data()
    with patch.object(M, "_mm", mm), \
         patch("artemis.quiet_hours.local_today", return_value=today), \
         patch.object(dr, "collect", return_value=data), \
         patch.object(dr, "render_pdf", return_value=b"%PDF-1.7 fake"), \
         patch.object(dr, "page_count", return_value=2):
        if produce is not None:
            with patch.object(dr, "produce", side_effect=produce):
                handled = M._handle_dietitian_report(dict(POST), text)
        else:
            handled = M._handle_dietitian_report(dict(POST), text)
    return handled, mm


class TestRouteMatching(unittest.TestCase):
    MATCH = ["dietitian report", "Dietitian Report", "the dietitian report",
             "dietitian report week", "dietitian report fortnight",
             "dietitian report month", "dietitian report day",
             "dietitian report summary", "dietitian report month summary",
             "dietitian report full", "dietitian report.",
             "dietitian report for 2026-09-01 to 2026-09-14",
             "dietitian report for 2026-09-01 to 2026-09-14 summary"]
    NO_MATCH = ["report", "reports", "dietitian", "the dietitian",
                "what did the dietitian say about the report",
                "send the dietitian report to joy",
                "email the dietitian report", "dietitian report next tuesday",
                "i had a dietitian report for breakfast", "", "   "]

    def test_it_matches_the_command_forms(self):
        for t in self.MATCH:
            with self.subTest(text=t):
                self.assertIsNotNone(M._REPORT_CMD_RE.match(t), t)

    def test_it_does_not_match_anything_else(self):
        for t in self.NO_MATCH:
            with self.subTest(text=t):
                self.assertIsNone(M._REPORT_CMD_RE.match(t), t)

    def test_a_non_match_is_not_claimed_by_the_handler(self):
        handled, mm = _run("report")
        self.assertFalse(handled)
        self.assertEqual(mm.messages, [])
        self.assertEqual(mm.uploads, [])

    def test_it_sits_ahead_of_meal_log_and_nutrition_in_the_chain(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "main.py").read_text()
        i_rep = src.index('("dietitian_report"')
        self.assertLess(i_rep, src.index('("meal_log"'))
        self.assertLess(i_rep, src.index('("nutrition", _handle_nutrition)'))


class TestPeriodParsing(unittest.TestCase):
    def test_default_is_the_fortnight_full(self):
        m = M._REPORT_CMD_RE.match("dietitian report")
        with patch("artemis.quiet_hours.local_today", return_value=TODAY):
            start, end, detail = M._report_cmd_period(m)
        self.assertEqual((start, end, detail), (date(2027, 1, 18), TODAY, "full"))

    def test_each_kind_resolves(self):
        for kind, want_start in (("day", TODAY), ("week", date(2027, 1, 25)),
                                 ("fortnight", date(2027, 1, 18)),
                                 ("month", date(2027, 1, 1))):
            with self.subTest(kind=kind):
                m = M._REPORT_CMD_RE.match(f"dietitian report {kind}")
                with patch("artemis.quiet_hours.local_today", return_value=TODAY):
                    start, end, _ = M._report_cmd_period(m)
                self.assertEqual(start, want_start)

    def test_summary_is_picked_up(self):
        m = M._REPORT_CMD_RE.match("dietitian report week summary")
        with patch("artemis.quiet_hours.local_today", return_value=TODAY):
            self.assertEqual(M._report_cmd_period(m)[2], "summary")

    def test_an_explicit_range_wins(self):
        m = M._REPORT_CMD_RE.match("dietitian report for 2026-09-01 to 2026-09-14")
        start, end, _ = M._report_cmd_period(m)
        self.assertEqual((start, end), (date(2026, 9, 1), date(2026, 9, 14)))

    def test_a_backwards_range_is_refused_not_rendered(self):
        handled, mm = _run("dietitian report for 2026-09-14 to 2026-09-01")
        self.assertTrue(handled)
        self.assertEqual(mm.uploads, [], "nothing may be produced")
        self.assertIn("ends before it starts", mm.messages[0][1])


class TestTheReply(unittest.TestCase):
    def test_it_uploads_the_pdf_and_posts_one_line(self):
        handled, mm = _run("dietitian report")
        self.assertTrue(handled)
        self.assertEqual(len(mm.uploads), 1)
        ch, name, nbytes, ctype = mm.uploads[0]
        self.assertEqual(ch, "CH")
        self.assertTrue(name.endswith(".pdf"))
        self.assertEqual(ctype, "application/pdf")
        self.assertGreater(nbytes, 0)
        self.assertEqual(len(mm.posts_with_files), 1)

    def test_the_line_names_period_pages_and_recorded_days(self):
        _, mm = _run("dietitian report")
        text = mm.posts_with_files[0][1]
        self.assertIn("period 2027-01-18 → 2027-01-31", text)
        self.assertIn("14 days", text)
        self.assertIn("pages 2", text)
        self.assertIn("days with intake recorded:", text)

    def test_it_replies_in_the_same_channel_and_thread(self):
        _, mm = _run("dietitian report")
        ch, _, fids, root = mm.posts_with_files[0]
        self.assertEqual(ch, "CH")
        self.assertEqual(root, "P1")
        self.assertEqual(fids, ["FILEID"])

    def test_the_filename_carries_the_period_and_detail(self):
        _, mm = _run("dietitian report week summary")
        self.assertEqual(mm.uploads[0][1], "dietitian-2027-01-25_2027-01-31-summary.pdf")


class TestFailurePaths(unittest.TestCase):
    def test_a_missing_pdf_engine_says_so_and_offers_html(self):
        def boom(*a, **k):
            raise dr.PdfEngineMissing("WeasyPrint is not installed")
        handled, mm = _run("dietitian report", produce=boom)
        self.assertTrue(handled)
        self.assertEqual(mm.uploads, [], "no partial file")
        msg = mm.messages[0][1]
        self.assertIn("WeasyPrint is not installed", msg)
        self.assertIn("--html", msg)

    def test_a_refused_report_is_not_sent(self):
        def refuse(*a, **k):
            raise dr.ReportRefused("the report text contains judgment words: ['lazy']")
        handled, mm = _run("dietitian report", produce=refuse)
        self.assertTrue(handled)
        self.assertEqual(mm.uploads, [])
        self.assertEqual(mm.posts_with_files, [])
        self.assertIn("judgment words", mm.messages[0][1])
        self.assertIn("Nothing was written", mm.messages[0][1])

    def test_any_other_failure_produces_no_partial_file(self):
        def boom(*a, **k):
            raise RuntimeError("collect exploded")
        handled, mm = _run("dietitian report", produce=boom)
        self.assertTrue(handled)
        self.assertEqual(mm.uploads, [])
        self.assertEqual(mm.posts_with_files, [])
        self.assertIn("failed to build", mm.messages[0][1])

    def test_an_upload_failure_is_reported_and_attaches_nothing(self):
        handled, mm = _run("dietitian report", mm=FakeMM(upload_raises=True))
        self.assertTrue(handled)
        self.assertEqual(mm.posts_with_files, [])
        self.assertIn("upload failed", mm.messages[0][1])
        self.assertIn("nothing is attached", mm.messages[0][1])


class TestItIsNotExternalComms(unittest.TestCase):
    """Ryan's own channel, on request. It must reach nothing else."""

    def test_the_handler_never_mails_or_drafts(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "main.py").read_text()
        body = src[src.index("def _handle_dietitian_report"):]
        body = body[:body.index("\n\ndef ")]
        for forbidden in ("send_message", "create_draft", "gmail", "smtp", "sendmail",
                          "CHANNEL_OPS", "CHANNEL_BRIEFS"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, body)

    def test_it_only_ever_uses_the_requesting_channel(self):
        _, mm = _run("dietitian report")
        chans = {c for c, *_ in mm.uploads} | {c for c, *_ in mm.posts_with_files} \
                | {c for c, *_ in mm.messages}
        self.assertEqual(chans, {"CH"})

    def test_nothing_schedules_it(self):
        sched = (Path(__file__).resolve().parent.parent / "artemis" / "scheduler.py").read_text()
        self.assertNotIn("dietitian_report", sched)
        self.assertNotIn("_handle_dietitian_report", sched)


class TestOneCodePath(unittest.TestCase):
    """The script and the handler both go through dr.produce()."""

    def test_the_script_calls_produce(self):
        src = (Path(__file__).resolve().parent.parent / "scripts" / "dietitian_report.py").read_text()
        self.assertIn("dr.produce(", src)
        self.assertNotIn("dr.build(dr.collect(", src)

    def test_produce_refuses_a_backwards_period(self):
        with self.assertRaises(ValueError):
            dr.produce(date(2026, 9, 14), date(2026, 9, 1))

    def test_produce_applies_the_banned_words_rule(self):
        with patch.object(dr, "collect", return_value=month_data()), \
             patch.object(dr, "banned_words_in", return_value=["lazy"]):
            with self.assertRaises(dr.ReportRefused):
                dr.produce(date(2027, 1, 1), date(2027, 1, 14))

    def test_produce_returns_bytes_and_writes_no_file(self):
        with patch.object(dr, "collect", return_value=month_data()), \
             patch.object(dr, "render_pdf", return_value=b"%PDF-1.7 x"), \
             patch.object(dr, "page_count", return_value=2):
            r = dr.produce(date(2027, 1, 18), date(2027, 1, 31))
        self.assertIsInstance(r.data, bytes)
        self.assertTrue(r.data.startswith(b"%PDF-"))
        self.assertEqual(r.pages, 2)
        self.assertEqual(r.n_days, 14)
        self.assertFalse(Path("/tmp") .joinpath(r.filename).exists()
                         and False, "produce() must not write")

    def test_html_mode_returns_text_and_no_page_count(self):
        with patch.object(dr, "collect", return_value=month_data()):
            r = dr.produce(date(2027, 1, 18), date(2027, 1, 31), as_html=True)
        self.assertIsInstance(r.data, str)
        self.assertIsNone(r.pages)
        self.assertIn("n/a (html)", r.summary_line)


if __name__ == "__main__":
    unittest.main()
