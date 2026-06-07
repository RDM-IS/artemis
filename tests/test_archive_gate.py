"""Tests for the state→INBOX archive gate + NEEDS_ACTION rubric (filing engine §2/§3).

Two layers:
  1. Deterministic — should_keep_in_inbox() gives the correct keep/archive
     decision for every one of the five states. Always runs (pure function).
  2. Rubric fixtures — a handful of representative emails are run through the
     live triage classifier and we assert the rubric-assigned state. This needs
     an Anthropic API key; it SKIPS cleanly when one isn't available, and it is
     tolerant (the classifier is an LLM) — a miss is reported, not hard-failed.

Run:
    python tests/test_archive_gate.py
"""

import os
import sys
import unittest
from pathlib import Path

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block accidental AWS access (mirrors the other test modules)
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis.inbox import (  # noqa: E402
    DONE,
    NEEDS_ACTION,
    NOISE,
    SNOOZED,
    WAITING,
    should_keep_in_inbox,
)


# ============================================================================
# 1. Deterministic gate decision — the load-bearing rule
# ============================================================================

class TestShouldKeepInInbox(unittest.TestCase):
    """INBOX = a human decision is required. Only NEEDS_ACTION keeps."""

    def test_needs_action_keeps_inbox(self):
        self.assertTrue(should_keep_in_inbox(NEEDS_ACTION))

    def test_waiting_is_filed(self):
        self.assertFalse(should_keep_in_inbox(WAITING))

    def test_snoozed_is_filed_at_triage(self):
        # SNOOZED leaves the inbox now and is re-surfaced on its wake date by
        # the snooze job — the triage gate must NOT keep it.
        self.assertFalse(should_keep_in_inbox(SNOOZED))

    def test_done_is_filed(self):
        self.assertFalse(should_keep_in_inbox(DONE))

    def test_noise_is_filed(self):
        self.assertFalse(should_keep_in_inbox(NOISE))

    def test_only_needs_action_keeps(self):
        keepers = [s for s in (NEEDS_ACTION, WAITING, SNOOZED, DONE, NOISE)
                   if should_keep_in_inbox(s)]
        self.assertEqual(keepers, [NEEDS_ACTION])


# ============================================================================
# 2. Rubric fixtures — body-context classification (live LLM, skips w/o key)
# ============================================================================

def _api_available() -> bool:
    try:
        from knowledge.secrets import get_anthropic_key
        return bool(get_anthropic_key())
    except Exception:
        return False


# (description, email body, expected state)
_RUBRIC_FIXTURES = [
    ("deploy succeeded",
     "From: github@github.com\nSubject: [repo] Deployment succeeded\n\n"
     "Your deployment to production completed successfully. All checks green.",
     DONE),
    ("deploy failed",
     "From: github@github.com\nSubject: [repo] Deployment failed\n\n"
     "Your deployment to production FAILED at the build step. Action required.",
     NEEDS_ACTION),
    ("vendor invoice needing payment",
     "From: billing@vercel.com\nSubject: Invoice due\n\n"
     "Your invoice of $240.00 is due in 3 days. Please arrange payment.",
     NEEDS_ACTION),
    ("reconciled receipt",
     "From: receipts@vercel.com\nSubject: Payment received — thank you\n\n"
     "We received your payment of $240.00. No action needed. Receipt attached.",
     DONE),
    ("newsletter",
     "From: news@somenewsletter.com\nSubject: This week in tech\n\n"
     "Here are the top 10 stories in tech this week. Unsubscribe any time.",
     NOISE),
    ("person asking a direct question",
     "From: brad@client.com\nSubject: Q3 review\n\n"
     "Hi — can you let me know which days next week work for the Q3 review? "
     "Waiting to hear back before I book the room.",
     NEEDS_ACTION),
    ("github permission request",
     "From: notifications@github.com\nSubject: Access request\n\n"
     "A user has requested write access to your repository. "
     "Approve or deny in settings.",
     NEEDS_ACTION),
    ("cold marketing blast",
     "From: growth@randomsaas.io\nSubject: Boost your revenue 10x\n\n"
     "We've never met but our tool will change your business. Book a demo!",
     NOISE),
]


@unittest.skipUnless(_api_available(), "Anthropic API key not available")
class TestRubricFixtures(unittest.TestCase):
    """Tolerant: surfaces misses as a report, never hard-fails on one LLM call."""

    def test_rubric_states(self):
        from artemis.briefs import triage_emails
        from artemis.inbox import state_from_triage

        misses = []
        for desc, body, expected in _RUBRIC_FIXTURES:
            triaged = triage_emails(body)
            if not triaged:
                self.skipTest("triage returned nothing (API/parse failure)")
            state = state_from_triage(triaged[0])
            if state != expected:
                misses.append(f"  {desc}: got {state}, expected {expected}")

        if misses:
            print("\nRubric misses ({}/{}):".format(len(misses), len(_RUBRIC_FIXTURES)))
            print("\n".join(misses))
        # Report-only — the rubric is an LLM judgment, so we don't fail the build
        # on a single miss. A hard regression (most fixtures wrong) is worth a flag.
        self.assertLessEqual(
            len(misses), len(_RUBRIC_FIXTURES) // 2,
            "More than half the rubric fixtures misclassified — likely a real regression",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
