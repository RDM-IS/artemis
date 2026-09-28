"""COGNITION-1 site 5 — the arbiter records what it disambiguated, and what he
answered. Synthetic only.

    python3.11 -m unittest tests.test_cognition_site5
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import inspect
import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from artemis import main as m

CH = "chan-test"


class Cur:
    """Captures audit writes and enforces binds."""

    def __init__(self, row_id="arb-1"):
        self.written, self._res, self._id = [], [], row_id

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} %s placeholders but {got} params"
        assert not (s.upper().startswith("UPDATE") and "acos.audit_log" in s), \
            "a decision row must never be UPDATEd"
        self._res = []
        if "INSERT INTO acos.audit_log" in s:
            cols = s.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            self.written.append(dict(zip(cols, params)))
            self._res = [(self._id,)]

    def fetchone(self):
        return self._res[0] if self._res else None


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _patched(cur):
    from contextlib import contextmanager

    @contextmanager
    def gc():
        yield _Conn(cur)
    return mock.patch("knowledge.db.get_connection", gc)


# ---------------------------------------------------------------------------
# The claimant table is the load-bearing part: it is how "would have picked" is
# computed without executing a live handler.
# ---------------------------------------------------------------------------

class TestClaimantTable(unittest.TestCase):
    @staticmethod
    def _chain_order():
        """The chain entry names, read from the dispatch function's SOURCE. The
        list is a local inside the handler, and every entry is a live handler, so
        reading the source is the only way to check the order without running it."""
        src = inspect.getsource(m)
        block = src[src.index("deterministic_chain = ["):]
        block = block[:block.index("\n    ]")]
        return re.findall(r'\(\s*"([a-z_]+)"\s*,', block)

    def test_every_claimant_is_in_the_chain(self):
        order = self._chain_order()
        for entry, _flows in m._BARE_WORD_CLAIMANTS:
            with self.subTest(entry=entry):
                self.assertIn(entry, order)

    def test_the_table_is_in_chain_order(self):
        """A reordered chain must fail here rather than make every recorded
        `would_have_picked` a quiet lie."""
        order = self._chain_order()
        positions = [order.index(e) for e, _ in m._BARE_WORD_CLAIMANTS]
        self.assertEqual(positions, sorted(positions),
                         f"_BARE_WORD_CLAIMANTS is out of chain order: {positions}")

    def test_the_table_is_exactly_the_handlers_that_consume_a_bare_word(self):
        """`_strip_qualifier` is called by precisely the arbitrated handlers. A new
        one that forgets this table would otherwise be invisible to it."""
        src = inspect.getsource(m)
        callers = set()
        for match in re.finditer(r"_strip_qualifier\(", src):
            head = src[:match.start()]
            fn = re.findall(r"\ndef (_handle_[a-z_]+)\(", head)
            if fn:
                callers.add(fn[-1])
        callers.discard("_handle_strip_qualifier")
        expected = {f"_handle_{e}" for e, _ in m._BARE_WORD_CLAIMANTS}
        self.assertEqual(callers, expected,
                         "the handlers calling _strip_qualifier and "
                         "_BARE_WORD_CLAIMANTS have diverged")

    def test_first_match_wins_is_computed_from_the_table(self):
        self.assertEqual(m._would_have_picked(["swap", "calendar"])[0], "calendar")
        self.assertEqual(m._would_have_picked(["disposition", "swap"])[0], "swap")
        self.assertEqual(m._would_have_picked(["staples", "target"])[0], "target")

    def test_open_flows_no_handler_would_have_claimed_are_reported_apart(self):
        """dossier/org/duplicate/convert/fix make a bare word ambiguous without
        any handler ever having been able to claim it."""
        picked, unclaimable = m._would_have_picked(["dossier", "fix"])
        self.assertIsNone(picked)
        self.assertEqual(unclaimable, ["dossier", "fix"])
        picked, unclaimable = m._would_have_picked(["swap", "dossier"])
        self.assertEqual(picked, "swap")
        self.assertEqual(unclaimable, ["dossier"])


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

class TestArbitrationDecision(unittest.TestCase):
    def setUp(self):
        m._arb_decisions.clear()

    def _arbitrate(self, flows, unreadable=()):
        cur = Cur()
        with _patched(cur), \
             mock.patch.object(m, "_open_pending_flows",
                               return_value=(list(flows), list(unreadable))):
            reply = m._arbitrate_bare_control_word(CH, "yes")
        return reply, cur

    def test_it_records_what_it_refused_to_execute(self):
        reply, cur = self._arbitrate(["swap", "calendar"])
        self.assertIsNotNone(reply)
        row = cur.written[0]
        self.assertEqual(row["action"], "confirm_arbitration")
        self.assertEqual(row["outcome"], "disambiguated")
        self.assertIs(row["manual_gap"], False)
        a = json.loads(row["assumptions"])
        self.assertEqual(a["word"], "yes")
        self.assertEqual(a["open_flows"], ["calendar", "swap"])
        self.assertEqual(a["would_have_picked"], "calendar")
        self.assertIn("no handler was executed", a["would_have_picked_from"])
        self.assertIsNone(a["would_have_picked_note"])

    def test_unreadable_stores_are_recorded_as_such(self):
        _reply, cur = self._arbitrate(["swap"], ["fix"])
        a = json.loads(cur.written[0]["assumptions"])
        self.assertEqual(a["unreadable_flows"], ["fix"])

    def test_when_nothing_could_have_claimed_it_the_row_says_why(self):
        _reply, cur = self._arbitrate(["dossier", "org"])
        a = json.loads(cur.written[0]["assumptions"])
        self.assertIsNone(a["would_have_picked"])
        self.assertIn("would have executed nothing", a["would_have_picked_note"])
        self.assertEqual(a["open_without_bare_claimant"], ["dossier", "org"])

    def test_one_pending_records_nothing_and_changes_nothing(self):
        reply, cur = self._arbitrate(["swap"])
        self.assertIsNone(reply)                 # first-match-wins, untouched
        self.assertEqual(cur.written, [])

    def test_a_non_control_word_records_nothing(self):
        cur = Cur()
        with _patched(cur):
            self.assertIsNone(m._arbitrate_bare_control_word(CH, "how is my week"))
        self.assertEqual(cur.written, [])

    def test_a_failed_write_must_not_cost_the_safety_reply(self):
        """Everywhere else a decision that cannot be recorded should not stand.
        Here the decision IS 'execute nothing' — the safe outcome — so the reply
        that keeps every pending open must survive a broken ledger."""
        class Broken(Cur):
            def execute(self, *a, **k):
                raise RuntimeError("RDS down")
        cur = Broken()
        with _patched(cur), \
             mock.patch.object(m, "_open_pending_flows",
                               return_value=(["swap", "calendar"], [])):
            reply = m._arbitrate_bare_control_word(CH, "yes")
        self.assertIsNotNone(reply)
        self.assertIn("ambiguous", reply)
        self.assertNotIn(CH, m._arb_decisions)


# ---------------------------------------------------------------------------
# The outcome
# ---------------------------------------------------------------------------

class TestArbitrationOutcome(unittest.TestCase):
    def setUp(self):
        m._arb_decisions.clear()

    def _open(self, picked="calendar", minutes_ago=0):
        m._arb_decisions[CH] = {
            "id": "arb-1", "would_have_picked": picked, "word": "yes",
            "at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)}

    def _answer(self, text):
        cur = Cur()
        with _patched(cur):
            m._record_arbitration_outcome(CH, text)
        return cur

    def test_the_answer_that_matches_first_match_wins(self):
        self._open("calendar")
        cur = self._answer("yes calendar")
        row = cur.written[0]
        self.assertEqual(row["action"], "confirm_arbitration.outcome")
        self.assertIsNone(row["assumptions"])         # an outcome is not a decision
        md = json.loads(row["metadata"])
        self.assertEqual(md["decides"], "arb-1")
        self.assertIs(md["matched_first_match_wins"], True)
        self.assertEqual(md["chose"], "calendar")

    def test_the_answer_that_proves_first_match_wins_would_have_been_wrong(self):
        self._open("calendar")
        md = json.loads(self._answer("yes swap").written[0]["metadata"])
        self.assertIs(md["matched_first_match_wins"], False)
        self.assertEqual(md["chose"], "swap")
        self.assertEqual(md["would_have_picked"], "calendar")

    def test_any_control_word_qualifies_not_just_yes(self):
        for word in ("no", "cancel", "confirm"):
            with self.subTest(word=word):
                m._arb_decisions.clear()
                self._open("swap")
                cur = self._answer(f"{word} swap")
                self.assertEqual(len(cur.written), 1)

    def test_it_is_recorded_once_and_the_slot_clears(self):
        self._open("calendar")
        self.assertEqual(len(self._answer("yes calendar").written), 1)
        self.assertEqual(self._answer("yes calendar").written, [])   # nothing left

    def test_a_reply_after_the_window_is_not_its_answer(self):
        self._open("calendar", minutes_ago=m._ARB_OUTCOME_WINDOW_MIN + 1)
        self.assertEqual(self._answer("yes calendar").written, [])
        self.assertNotIn(CH, m._arb_decisions)

    def test_an_unqualified_or_unrelated_message_leaves_it_open(self):
        for text in ("yes", "how is my week", "yes please"):
            with self.subTest(text=text):
                m._arb_decisions.clear()
                self._open("calendar")
                self.assertEqual(self._answer(text).written, [])
                self.assertIn(CH, m._arb_decisions)    # still waiting

    def test_nothing_pending_records_nothing(self):
        self.assertEqual(self._answer("yes calendar").written, [])

    def test_a_failed_outcome_write_clears_the_slot_rather_than_retrying_forever(self):
        self._open("calendar")

        class Broken(Cur):
            def execute(self, *a, **k):
                raise RuntimeError("RDS down")
        cur = Broken()
        with _patched(cur):
            m._record_arbitration_outcome(CH, "yes calendar")
        self.assertNotIn(CH, m._arb_decisions)

    def test_the_action_is_absent_from_the_nightly_job(self):
        """Mattermost posts are not persisted, so the reply cannot be recovered
        after the fact — the nightly job must not try."""
        from artemis import cognition_outcomes as co
        self.assertNotIn("confirm_arbitration", co.RESOLVERS)


if __name__ == "__main__":
    unittest.main()
