"""CONFIRM-ARB — a bare control word with more than one pending open does nothing.

The bug: several flows consume a bare `yes`/`no`, each gating on its OWN pending
store, and those stores are independent. `deterministic_chain` resolved the tie by
list order, first-match-wins — deterministic but not intent-aware, so a `yes` meant
for the swap could execute the rule.

The rule now: >1 open → execute NOTHING, leave every pending open, and name the
qualified form for exactly the flows that are open. Exactly one open → unchanged.

FAIL-CLOSED: a store that cannot be READ counts as possibly-open. A resolver that
answers "nothing pending" because the database was down is how the wrong flow runs.

Synthetic only — no RDS, no Mattermost.

Run:
    python3.11 -m unittest tests.test_confirm_arb
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _n in ("googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
           "google", "google.auth", "google.auth.transport",
           "google.auth.transport.requests", "google.oauth2",
           "google.oauth2.credentials", "google_auth_oauthlib",
           "google_auth_oauthlib.flow", "websocket", "Levenshtein", "apscheduler",
           "apscheduler.schedulers", "apscheduler.schedulers.background",
           "apscheduler.triggers", "apscheduler.triggers.cron"):
    sys.modules.setdefault(_n, MagicMock())

from artemis import main as M  # noqa: E402

CH = "chan-1"

#: flow -> the patch that makes ONLY that flow's store report open.
CLOSED = {"debrief": None, "swap": None, "target": None, "staples": None}


def _mem(flow_type):
    """Put one value in the single in-memory slot."""
    return {"type": flow_type, "timestamp": time.time(), "spec": {}, "name": "x"}


class _Env:
    """Patches every durable store to closed, then opens the named ones."""

    def __init__(self, *open_flows, mem=None, raises=()):
        self.open = set(open_flows)
        self.mem = mem
        self.raises = set(raises)
        self.patches = []

    def __enter__(self):
        M._pending_confirms.clear()
        M._inbox_listing_state.clear()
        if self.mem:
            M._pending_confirms[CH] = _mem(self.mem)
        if "disposition" in self.open:
            M._inbox_listing_state[CH] = {"pending_dispositions": [{"id": "a"}]}

        def mk(flow):
            def f(*a, **k):
                if flow in self.raises:
                    raise RuntimeError(f"{flow} store unreadable")
                return {"x": 1} if flow in self.open else None
            return f

        targets = {"debrief": "artemis.health.load_capture_pending",
                   "swap": "artemis.health.load_swap_pending",
                   "target": "artemis.health.load_nutrition_target_pending",
                   "staples": "artemis.life_ops.load_staples_pending"}
        for flow, path in targets.items():
            pt = patch(path, side_effect=mk(flow))
            pt.start(); self.patches.append(pt)
        # the `fix` probe needs a DB; closed unless asked
        def _fix(*a, **k):
            if "fix" in self.raises:
                raise RuntimeError("fix store unreadable")
            return object() if "fix" in self.open else None
        pt = patch("artemis.nutrition.pending_correction", side_effect=_fix)
        pt.start(); self.patches.append(pt)
        pt = patch("knowledge.db.get_connection", MagicMock())
        pt.start(); self.patches.append(pt)
        return self

    def __exit__(self, *a):
        for pt in reversed(self.patches):
            pt.stop()
        M._pending_confirms.clear()
        M._inbox_listing_state.clear()
        return False


class TestTheCounter(unittest.TestCase):
    def test_nothing_open_is_zero(self):
        with _Env():
            self.assertEqual(M._open_pending_flows(CH), ([], []))

    def test_each_durable_store_is_seen(self):
        for flow in ("debrief", "swap", "target", "staples"):
            with self.subTest(flow=flow), _Env(flow):
                self.assertEqual(M._open_pending_flows(CH), ([flow], []))

    def test_the_in_memory_slot_maps_every_type(self):
        for t, flow in (("calendar_create_external", "calendar"),
                        ("calendar_delete", "delete"),
                        ("duplicate_override", "duplicate"),
                        ("bulk_convert_to_tasks", "convert"),
                        ("playbook_rule", "rule"),
                        ("dossier_set", "dossier"),
                        ("org_set", "org")):
            with self.subTest(type=t), _Env(mem=t):
                self.assertEqual(M._open_pending_flows(CH), ([flow], []))

    def test_the_disposition_batch_is_seen(self):
        with _Env("disposition"):
            self.assertEqual(M._open_pending_flows(CH), (["disposition"], []))

    def test_two_stores_open_reports_both(self):
        with _Env("swap", "target"):
            flows, bad = M._open_pending_flows(CH)
            self.assertEqual(sorted(flows), ["swap", "target"])
            self.assertEqual(bad, [])


class TestFailClosed(unittest.TestCase):
    """A store that cannot be read is possibly-open, never assumed closed."""

    def test_an_unreadable_store_is_reported_unreadable(self):
        with _Env(raises=["swap"]):
            flows, bad = M._open_pending_flows(CH)
            self.assertEqual(flows, [])
            self.assertEqual(bad, ["swap"])

    def test_one_open_plus_one_unreadable_disambiguates(self):
        with _Env("target", raises=["swap"]):
            reply = M._arbitrate_bare_control_word(CH, "yes")
            self.assertIsNotNone(reply, "unreadable must count toward the total")
            self.assertIn("yes swap", reply)
            self.assertIn("yes target", reply)
            self.assertIn("could not be read", reply)

    def test_two_unreadable_stores_disambiguate(self):
        with _Env(raises=["swap", "target"]):
            self.assertIsNotNone(M._arbitrate_bare_control_word(CH, "yes"))

    def test_a_single_unreadable_store_does_not_block(self):
        """One possibly-open flow is still one — the old path stays."""
        with _Env(raises=["swap"]):
            self.assertIsNone(M._arbitrate_bare_control_word(CH, "yes"))


class TestArbitration(unittest.TestCase):
    PAIRS = [("swap", "target"), ("swap", "debrief"), ("target", "staples"),
             ("debrief", "disposition"), ("swap", "staples"),
             ("debrief", "target"), ("disposition", "target")]

    def test_every_pair_disambiguates_and_names_both(self):
        for a, b in self.PAIRS:
            with self.subTest(pair=(a, b)), _Env(a, b):
                reply = M._arbitrate_bare_control_word(CH, "yes")
                self.assertIsNotNone(reply, f"{a}+{b} must disambiguate")
                self.assertIn(f"yes {a}", reply)
                self.assertIn(f"yes {b}", reply)

    def test_it_names_only_the_open_flows(self):
        with _Env("swap", "target"):
            reply = M._arbitrate_bare_control_word(CH, "yes")
            for absent in ("staples", "debrief", "disposition", "calendar"):
                self.assertNotIn(f"yes {absent}", reply)

    def test_a_memory_pending_plus_a_durable_one_disambiguates(self):
        with _Env("swap", mem="playbook_rule"):
            reply = M._arbitrate_bare_control_word(CH, "yes")
            self.assertIsNotNone(reply)
            self.assertIn("yes rule", reply)
            self.assertIn("yes swap", reply)

    def test_every_control_word_is_arbitrated(self):
        for w in sorted(M._CONTROL_WORDS):
            with self.subTest(word=w), _Env("swap", "target"):
                self.assertIsNotNone(M._arbitrate_bare_control_word(CH, w))

    def test_trailing_punctuation_does_not_evade_it(self):
        with _Env("swap", "target"):
            for w in ("yes.", "yes!", "  yes  ", "YES"):
                with self.subTest(word=w):
                    self.assertIsNotNone(M._arbitrate_bare_control_word(CH, w))

    def test_nothing_is_executed_and_the_pendings_stay_open(self):
        with _Env("swap", "target") as env:
            M._arbitrate_bare_control_word(CH, "yes")
            # the stores are still reporting open after arbitration
            flows, _ = M._open_pending_flows(CH)
            self.assertEqual(sorted(flows), ["swap", "target"])


class TestUnchangedWhenOneIsOpen(unittest.TestCase):
    def test_exactly_one_open_returns_none(self):
        for flow in ("debrief", "swap", "target", "staples", "disposition"):
            with self.subTest(flow=flow), _Env(flow):
                self.assertIsNone(M._arbitrate_bare_control_word(CH, "yes"))

    def test_none_open_returns_none(self):
        with _Env():
            self.assertIsNone(M._arbitrate_bare_control_word(CH, "yes"))

    def test_a_non_control_word_is_never_arbitrated(self):
        with _Env("swap", "target"):
            for t in ("swap to rowing", "what's left", "repeat week", "", "yes please really"):
                with self.subTest(text=t):
                    self.assertIsNone(M._arbitrate_bare_control_word(CH, t))


class TestQualifiedForms(unittest.TestCase):
    """A handler accepts its own qualified form however many others are open."""

    def test_the_owner_strips_its_own_suffix(self):
        for flow, word in (("swap", "yes"), ("target", "confirm"), ("rule", "approve"),
                           ("debrief", "yes"), ("disposition", "no"), ("calendar", "cancel")):
            with self.subTest(flow=flow):
                self.assertEqual(M._strip_qualifier(f"{word} {flow}", flow), word)

    def test_a_non_owner_leaves_it_alone(self):
        """`yes rule` must NOT become a bare yes for the swap handler."""
        self.assertEqual(M._strip_qualifier("yes rule", "swap"), "yes rule")
        self.assertEqual(M._strip_qualifier("yes swap", "target", "staples"), "yes swap")

    def test_a_handler_owning_two_flows_strips_either(self):
        self.assertEqual(M._strip_qualifier("yes target", "target", "staples"), "yes")
        self.assertEqual(M._strip_qualifier("yes staples", "target", "staples"), "yes")

    def test_a_bare_word_passes_through_untouched(self):
        self.assertEqual(M._strip_qualifier("yes", "swap"), "yes")

    def test_a_qualified_form_is_not_a_bare_word_so_it_is_not_arbitrated(self):
        """This is what lets a qualified reply work with several pendings open."""
        with _Env("swap", "target"):
            self.assertIsNone(M._arbitrate_bare_control_word(CH, "yes swap"))
            self.assertIsNone(M._arbitrate_bare_control_word(CH, "yes target"))

    def test_only_a_control_word_can_carry_a_qualifier(self):
        """`show swap` is not a confirm."""
        self.assertEqual(M._strip_qualifier("show swap", "swap"), "show swap")

    def test_case_and_punctuation(self):
        self.assertEqual(M._strip_qualifier("YES SWAP", "swap").lower(), "yes")
        self.assertEqual(M._strip_qualifier("yes swap.", "swap"), "yes")


if __name__ == "__main__":
    unittest.main()
