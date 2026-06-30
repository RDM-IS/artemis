"""Unit tests for inbox disposition parsing: multi-word `file as <category>`,
compound multi-group batches (numbers either side of the verb, parentheticals
stripped), and the disposition-shape guard that stops a malformed inbox command
from falling through to the financial/LLM classifier.

Regression anchor: `file 1 as founder loan` and the compound batch
`1-4 archive 5-7 file as founder loans ...` both previously mis-routed to the
financial summary because the single-token category regex choked on the space
in "founder loan(s)". See docs/EMAIL_MODEL.md.

The parser functions are pure; main.py's heavy third-party imports are stubbed
so the test needs no anthropic/AWS/flask. Run:
    python tests/test_compound_dispositions.py
"""

import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")


def _import_main_with_stubs():
    """Import artemis.main, stubbing any missing heavy third-party module on
    demand so the pure parser functions are reachable without real deps."""
    for _ in range(50):
        try:
            return importlib.import_module("artemis.main")
        except ModuleNotFoundError as e:
            missing = e.name
            if missing and missing.split(".")[0] in {"artemis", "knowledge"}:
                raise  # never stub first-party packages
            stub = types.ModuleType(missing)
            stub.__getattr__ = lambda name: MagicMock()  # type: ignore[attr-defined]
            sys.modules[missing] = stub
    raise RuntimeError("could not import artemis.main after stubbing")


main = _import_main_with_stubs()


class TestSingleParserMultiWord(unittest.TestCase):
    def test_multiword_category_slugified(self):
        self.assertEqual(
            main._parse_disposition_command("file 1 as founder loan"),
            ("file", [1], "founder-loan"),
        )

    def test_multiword_plural_with_range_and_list(self):
        self.assertEqual(
            main._parse_disposition_command("file 5-7, 13 as founder loans"),
            ("file", [5, 6, 7, 13], "founder-loans"),
        )

    def test_single_token_category_still_works(self):
        self.assertEqual(
            main._parse_disposition_command("file 3 as billing"),
            ("file", [3], "billing"),
        )

    def test_plain_verbs_unaffected(self):
        self.assertEqual(main._parse_disposition_command("archive 1-4"),
                         ("archive", [1, 2, 3, 4], None))
        self.assertEqual(main._parse_disposition_command("delete 2, 5"),
                         ("delete", [2, 5], None))

    def test_non_disposition_rejected(self):
        self.assertIsNone(main._parse_disposition_command("what is my balance"))
        self.assertIsNone(main._parse_disposition_command("show me founder loans"))


class TestCompoundParser(unittest.TestCase):
    def test_full_original_batch(self):
        cmd = ("1-4 archive 5-7 file as founder loans 8-12 archive 13 file as "
               "founder loans 14 delete (was a test) 15 archive 16 delete "
               "17 delete 18 delete 19 delete 20 delete")
        groups = main._parse_compound_dispositions(cmd)
        self.assertEqual(groups, [
            ("archive", [1, 2, 3, 4], None),
            ("file", [5, 6, 7], "founder-loans"),
            ("archive", [8, 9, 10, 11, 12], None),
            ("file", [13], "founder-loans"),
            ("delete", [14], None),
            ("archive", [15], None),
            ("delete", [16], None),
            ("delete", [17], None),
            ("delete", [18], None),
            ("delete", [19], None),
            ("delete", [20], None),
        ])

    def test_verb_first_and_nums_first_mix(self):
        # Lookahead: 5-7 is followed by `file`, so it binds forward, not to archive.
        groups = main._parse_compound_dispositions(
            "archive 1-2 5-7 file as billing delete 4")
        self.assertEqual(groups, [
            ("archive", [1, 2], None),
            ("file", [5, 6, 7], "billing"),
            ("delete", [4], None),
        ])

    def test_verb_first_consumes_trailing_numbers_when_no_verb_follows(self):
        # No trailing verb → archive keeps all its operands.
        self.assertEqual(
            main._parse_compound_dispositions("archive 1 2 3"),
            [("archive", [1, 2, 3], None)],
        )

    def test_parentheticals_stripped(self):
        groups = main._parse_compound_dispositions("14 delete (was a test) 15 archive")
        self.assertEqual(groups, [("delete", [14], None), ("archive", [15], None)])

    def test_file_without_category_dropped(self):
        # `file 9` with no category is unusable and dropped; siblings survive.
        groups = main._parse_compound_dispositions("archive 1 file 9")
        self.assertEqual(groups, [("archive", [1], None)])

    def test_single_command_is_not_compound_only(self):
        # A lone command still parses as a (single-element) batch — caller
        # prefers the single parser, but compound must not crash on it.
        self.assertEqual(
            main._parse_compound_dispositions("archive 1-4"),
            [("archive", [1, 2, 3, 4], None)],
        )

    def test_non_disposition_returns_none(self):
        self.assertIsNone(main._parse_compound_dispositions("what is my balance"))
        self.assertIsNone(main._parse_compound_dispositions("show me founder loans"))


class TestDispositionShapeGuard(unittest.TestCase):
    """_looks_dispositional gates the in-context refusal: disposition-shaped
    lines are caught; genuine financial/prose queries pass through."""

    def test_shaped_lines_caught(self):
        self.assertTrue(main._looks_dispositional("file 1 as founder loan"))
        self.assertTrue(main._looks_dispositional("1-4 archive"))
        self.assertTrue(main._looks_dispositional("delete 14, 16-20"))

    def test_genuine_financial_queries_pass(self):
        # Must NOT be swallowed by the guard — these legitimately want the report.
        self.assertFalse(main._looks_dispositional("show me founder loans"))
        self.assertFalse(main._looks_dispositional("what is my balance"))
        self.assertFalse(main._looks_dispositional("how much have I loaned"))

    def test_verb_without_number_not_shaped(self):
        # The confabulated help question carried no number → not disposition-shaped.
        self.assertFalse(
            main._looks_dispositional("what do you do when i give the file as founder loan command"))


class TestConsequentialClassification(unittest.TestCase):
    def test_delete_and_spam_are_consequential(self):
        self.assertIn("delete", main._CONSEQUENTIAL_VERBS)
        self.assertIn("spam", main._CONSEQUENTIAL_VERBS)

    def test_archive_and_file_are_reversible(self):
        self.assertNotIn("archive", main._CONSEQUENTIAL_VERBS)
        self.assertNotIn("file", main._CONSEQUENTIAL_VERBS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
