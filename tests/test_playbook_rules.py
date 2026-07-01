"""Rule engine (feature #1) — pure-logic tests: spec parsing, matching, describe.

The parser and matcher are pure; the DB layer (execute_*) is stubbed so these run
without RDS. Run:  python tests/test_playbook_rules.py
"""

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

# Stub knowledge.db so importing the engine needs no real DB.
_dbstub = types.ModuleType("knowledge.db")
_dbstub.execute_one = lambda *a, **k: None
_dbstub.execute_query = lambda *a, **k: []
_dbstub.execute_write = lambda *a, **k: None
sys.modules["knowledge.db"] = _dbstub
# knowledge package parent
if "knowledge" not in sys.modules:
    pkg = types.ModuleType("knowledge")
    pkg.__path__ = []  # namespace-ish
    sys.modules["knowledge"] = pkg

import importlib  # noqa: E402
pr = importlib.import_module("artemis.playbook_rules")


class TestParseRuleSpec(unittest.TestCase):
    def test_cloudflare_body_rule(self):
        spec = pr.parse_rule_spec('archive from:cloudflare-workers-and-pages body:"Deploy successful"')
        self.assertEqual(spec, {
            "action": "archive", "action_label": None,
            "match_sender": "cloudflare-workers-and-pages",
            "match_subject": None, "match_body": "Deploy successful",
        })

    def test_file_with_label_and_conditions(self):
        spec = pr.parse_rule_spec("file as founder-loan from:vercel.com subject:receipt")
        self.assertEqual(spec["action"], "file")
        self.assertEqual(spec["action_label"], "founder-loan")
        self.assertEqual(spec["match_sender"], "vercel.com")
        self.assertEqual(spec["match_subject"], "receipt")

    def test_spam_sender_only(self):
        spec = pr.parse_rule_spec("spam from:etsy.com")
        self.assertEqual(spec["action"], "spam")
        self.assertEqual(spec["match_sender"], "etsy.com")

    def test_quoted_values_with_spaces(self):
        spec = pr.parse_rule_spec('archive subject:"Cloudflare Access login code"')
        self.assertEqual(spec["match_subject"], "Cloudflare Access login code")

    # --- error paths ---
    def test_no_action(self):
        with self.assertRaises(pr.RuleSpecError):
            pr.parse_rule_spec("from:etsy.com")

    def test_delete_rejected(self):
        with self.assertRaises(pr.RuleSpecError):
            pr.parse_rule_spec("delete from:etsy.com")

    def test_file_without_label(self):
        with self.assertRaises(pr.RuleSpecError):
            pr.parse_rule_spec("file from:vercel.com")

    def test_reserved_label_rejected(self):
        with self.assertRaises(pr.RuleSpecError):
            pr.parse_rule_spec("file as spam from:x.com")

    def test_no_conditions(self):
        with self.assertRaises(pr.RuleSpecError):
            pr.parse_rule_spec("archive")


class TestMatches(unittest.TestCase):
    def test_sender_substring_case_insensitive(self):
        rule = {"match_sender": "vercel.com"}
        self.assertTrue(pr.matches(rule, "billing@VERCEL.com", "Receipt"))
        self.assertFalse(pr.matches(rule, "billing@stripe.com", "Receipt"))

    def test_all_conditions_must_hold(self):
        rule = {"match_sender": "cloudflare", "match_body": "Deploy successful"}
        self.assertTrue(pr.matches(rule, "bot@cloudflare.com", "Re: PR", "…Deploy successful! at https://…"))
        self.assertFalse(pr.matches(rule, "bot@cloudflare.com", "Re: PR", "Deploy FAILED"))
        self.assertFalse(pr.matches(rule, "someone@else.com", "Re: PR", "Deploy successful!"))

    def test_empty_rule_never_matches(self):
        self.assertFalse(pr.matches({}, "a@b.com", "hi", "body"))

    def test_subject_condition(self):
        rule = {"match_subject": "login code"}
        self.assertTrue(pr.matches(rule, "x@cloudflare.com", "Cloudflare Access login code for gym"))
        self.assertFalse(pr.matches(rule, "x@cloudflare.com", "Your invoice"))


class TestNeedsBodyAndDescribe(unittest.TestCase):
    def test_needs_body(self):
        self.assertTrue(pr.needs_body({"match_body": "x"}))
        self.assertFalse(pr.needs_body({"match_sender": "x"}))

    def test_describe_file_rule(self):
        d = pr.describe_rule({"action": "file", "action_label": "founder-loan",
                              "match_sender": "vercel.com"})
        self.assertIn("@artemis/founder-loan", d)
        self.assertIn("vercel.com", d)

    def test_describe_archive_rule(self):
        d = pr.describe_rule({"action": "archive", "match_body": "Deploy successful"})
        self.assertIn("archive", d)
        self.assertIn("Deploy successful", d)


class TestMatchMessageLazyBody(unittest.TestCase):
    """match_message should fetch body only when a candidate rule needs it, and
    only after cheap sender/subject pre-filtering."""

    def test_body_fetched_only_after_prefilter(self):
        rules = [{"id": 1, "match_sender": "cloudflare", "match_subject": None,
                  "match_body": "Deploy successful", "action": "archive",
                  "action_label": None, "active": True}]
        pr.list_rules = lambda active_only=True: rules  # type: ignore

        calls = {"n": 0}
        def fetch():
            calls["n"] += 1
            return "…Deploy successful! ✅"

        # Non-matching sender: pre-filter fails, body NEVER fetched.
        self.assertIsNone(pr.match_message("bot@github.com", "Re: PR", fetch))
        self.assertEqual(calls["n"], 0)

        # Matching sender: body fetched once, rule matches.
        m = pr.match_message("bot@cloudflare.com", "Re: PR", fetch)
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], 1)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
