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

class TestConsumerTable(unittest.TestCase):
    """The table is load-bearing twice over: it is how `would_have_picked` is
    computed without executing a handler, AND (since 2026-09-28) how the arbiter
    decides which open pendings can make a bare word ambiguous at all.

    Its first version was the set of handlers calling `_strip_qualifier`, which is
    NOT the same set — it missed four real consumers, two of which WRITE on a bare
    `yes`. These tests encode the correct invariant so that mistake cannot recur.
    """

    @staticmethod
    def _chain_order():
        """The chain entry names, read from the dispatch function's SOURCE. The
        list is a local inside the handler and every entry is a live handler, so
        reading the source is the only way to check the order without running it."""
        src = inspect.getsource(m)
        block = src[src.index("deterministic_chain = ["):]
        block = block[:block.index("\n    ]")]
        return re.findall(r'\(\s*"([a-z_]+)"\s*,', block)

    @staticmethod
    def _bare_word_handlers():
        """Every `_handle_*` that could consume a bare control word, by either
        signal: it accepts a qualified form (`_strip_qualifier`), or it compares
        against a control-word set. Both are needed — `swap_confirm` uses its own
        `_SWAP_YES_RE`/`_SWAP_NO_RE` and matches no word set, while
        `duplicate_override` matches a word set and never called `_strip_qualifier`
        until this round."""
        src = inspect.getsource(m)
        bounds = [(x.start(), x.group(1)) for x in re.finditer(r"\ndef (_handle_[a-z_]+)\(", src)]
        bounds.append((len(src), "<eof>"))
        out = set()
        for (a, name), (b, _n) in zip(bounds, bounds[1:]):
            body = src[a:b]
            if re.search(r"_strip_qualifier\(|_CONFIRM_WORDS|_CANCEL_WORDS|_CONTROL_WORDS",
                         body):
                out.add(name)
        out.discard("_handle_mention")        # the dispatcher, not a flow handler
        return out

    def test_every_table_entry_is_in_the_chain(self):
        order = self._chain_order()
        for entry, _flows in m._BARE_WORD_CONSUMERS:
            with self.subTest(entry=entry):
                self.assertIn(entry, order)

    def test_the_table_is_in_chain_order(self):
        """A reordered chain must fail here rather than make every recorded
        `would_have_picked` a quiet lie."""
        order = self._chain_order()
        positions = [order.index(e) for e, _ in m._BARE_WORD_CONSUMERS]
        self.assertEqual(positions, sorted(positions),
                         f"_BARE_WORD_CONSUMERS is out of chain order: {positions}")

    def test_duplicate_override_is_first_because_the_chain_puts_it_first(self):
        """The omission that mattered most: it is chain entry 1, so leaving it out
        made `would_have_picked` wrong whenever a duplicate pending was open."""
        order = self._chain_order()
        self.assertEqual(m._BARE_WORD_CONSUMERS[0][0], "duplicate_override")
        self.assertLess(order.index("duplicate_override"), order.index("swap_confirm"))

    def test_no_in_chain_bare_word_consumer_is_missing_from_the_table(self):
        """THE invariant the first version got wrong. A handler that can take a
        bare control word and is not in this table is invisible to both the
        arbiter and site 5 — which is how a bare `yes` executes the wrong flow."""
        table = {e for e, _ in m._BARE_WORD_CONSUMERS}
        chain = set(self._chain_order())
        missing = {h for h in self._bare_word_handlers()
                   if h[len("_handle_"):] in chain and h[len("_handle_"):] not in table}
        self.assertEqual(missing, set(),
                         f"in-chain bare-word consumers missing from the table: {missing}")

    def test_the_post_chain_consumer_is_accounted_for(self):
        """`convert` consumes a bare confirm word but is called after the chain, so
        it has no chain position and must be listed separately rather than dropped."""
        self.assertIn("_handle_convert_to_tasks", self._bare_word_handlers())
        self.assertNotIn("convert_to_tasks", self._chain_order())
        self.assertIn("convert", m._POST_CHAIN_CONSUMERS)
        self.assertIn("convert", m.consuming_flows())

    def test_every_non_consuming_flow_is_gated_on_a_qualified_form(self):
        """Was `fix is the only flow`. CARDIO-DETECT added a second
        (2026-09-29), so the assertion moved from a hard-coded set to the
        PROPERTY that earns membership: a flow belongs here only if a bare
        control word could never reach it.

        `fix` is gated on `fix`/`done`/a deviation-shaped line; `cardio_detect`
        on the qualified phrase `log cardio`. Neither is a control word. The
        stake is higher for cardio_detect than for fix: a bare `yes` meant for a
        calendar confirm reaching it would WRITE a training session Ryan never
        agreed to."""
        self.assertEqual(m._NON_CONSUMING_FLOWS, frozenset({"fix", "cardio_detect"}))
        for flow in m._NON_CONSUMING_FLOWS:
            with self.subTest(flow):
                self.assertNotIn(flow, m.consuming_flows())
        self.assertNotIn("_handle_nutrition_fix", self._bare_word_handlers())
        self.assertNotIn("_handle_log_cardio", self._bare_word_handlers())

    def test_log_cardio_is_qualified_and_never_matches_a_control_word(self):
        for word in ("yes", "y", "yeah", "ok", "okay", "confirm", "no", "cancel"):
            with self.subTest(word):
                self.assertIsNone(m._LOG_CARDIO_RE.match(word))

    def test_every_probed_flow_is_either_consuming_or_named_non_consuming(self):
        """`_open_pending_flows` can return 13 flow names. Each must be classified,
        or the arbiter would silently ignore one it should have counted."""
        probed = set(m._QUALIFIED_SUFFIX)
        unclassified = probed - m.consuming_flows() - m._NON_CONSUMING_FLOWS
        self.assertEqual(unclassified, set(),
                         f"flows that are neither consuming nor declared non-consuming: "
                         f"{unclassified}")

    def test_first_match_wins_is_computed_from_the_table(self):
        self.assertEqual(m._would_have_picked(["swap", "calendar"])[0], "calendar")
        self.assertEqual(m._would_have_picked(["disposition", "swap"])[0], "swap")
        self.assertEqual(m._would_have_picked(["staples", "target"])[0], "target")
        # the corrections: duplicate beats everything, dossier beats rule
        self.assertEqual(m._would_have_picked(["swap", "duplicate"])[0], "duplicate")
        self.assertEqual(m._would_have_picked(["rule", "dossier"])[0], "dossier")
        self.assertEqual(m._would_have_picked(["org", "disposition"])[0], "org")

    def test_the_post_chain_consumer_loses_to_every_chain_one(self):
        self.assertEqual(m._would_have_picked(["convert", "disposition"])[0], "disposition")
        self.assertEqual(m._would_have_picked(["convert"])[0], "convert")

    def test_only_a_non_consuming_flow_is_reported_as_unclaimable(self):
        picked, unclaimable = m._would_have_picked(["swap", "fix"])
        self.assertEqual(picked, "swap")
        self.assertEqual(unclaimable, ["fix"])
        picked, unclaimable = m._would_have_picked(["dossier", "fix"])
        self.assertEqual(picked, "dossier")      # NOT None — dossier writes on a yes
        self.assertEqual(unclaimable, ["fix"])


class TestArbiterCountsOnlyConsumers(unittest.TestCase):
    """Ryan's call, 2026-09-28: a flow that could not have claimed the bare word
    must not make it ambiguous."""

    def setUp(self):
        m._arb_decisions.clear()

    def _arbitrate(self, flows, unreadable=()):
        cur = Cur()
        with _patched(cur), \
             mock.patch.object(m, "_open_pending_flows",
                               return_value=(list(flows), list(unreadable))):
            reply = m._arbitrate_bare_control_word(CH, "yes")
        return reply, cur

    def test_a_consuming_and_a_non_consuming_pending_is_not_ambiguous(self):
        """The whole point: `fix` open alongside `swap` used to refuse a bare yes
        that only `swap` could ever have taken."""
        reply, cur = self._arbitrate(["swap", "fix"])
        self.assertIsNone(reply)
        self.assertEqual(cur.written, [])          # nothing to decide, nothing recorded

    def test_two_consuming_pendings_still_disambiguate(self):
        reply, cur = self._arbitrate(["swap", "calendar"])
        self.assertIsNotNone(reply)
        self.assertEqual(len(cur.written), 1)

    def test_an_unreadable_consumer_still_disambiguates(self):
        """FAIL-CLOSED where it matters: 'might be open' is dangerous for a flow
        that could act on the word."""
        reply, _cur = self._arbitrate(["swap"], ["calendar"])
        self.assertIsNotNone(reply)

    def test_an_unreadable_NON_consumer_does_not(self):
        """`fix` unreadable is not a competing claim, because even open it could
        not have taken the word."""
        reply, _cur = self._arbitrate(["swap"], ["fix"])
        self.assertIsNone(reply)

    def test_the_four_corrected_flows_still_count(self):
        """dossier/org/duplicate/convert DO consume a bare yes — two of them write
        — so excluding them would have reintroduced the CONFIRM-ARB bug."""
        for other in ("dossier", "org", "duplicate", "convert"):
            with self.subTest(flow=other):
                m._arb_decisions.clear()
                reply, _cur = self._arbitrate(["swap", other])
                self.assertIsNotNone(reply, f"{other} must still cause disambiguation")

    def test_only_non_consuming_flows_are_left_out_of_the_reply(self):
        reply, _cur = self._arbitrate(["swap", "calendar", "fix"])
        self.assertIn("swap", reply)
        self.assertIn("calendar", reply)
        self.assertNotIn("fix", reply)      # offering `yes fix` would be broken advice


class TestQualifiedFormsAllWork(unittest.TestCase):
    """Every flow the arbiter offers a qualified form for must have a handler that
    strips it — otherwise the disambiguation reply is advice that does nothing."""

    def test_each_consuming_flow_has_a_handler_that_strips_its_suffix(self):
        src = inspect.getsource(m)
        for flow in sorted(m.consuming_flows()):
            with self.subTest(flow=flow):
                self.assertIn(f'_strip_qualifier(question, "{flow}"', src.replace(
                    '_strip_qualifier(question, "target", "staples")',
                    '_strip_qualifier(question, "target") _strip_qualifier(question, "staples")'))


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
        """The unreadable store has to be a CONSUMING one to count at all — this
        test used `fix`, which stopped counting on 2026-09-28 and is why it is now
        `calendar`."""
        _reply, cur = self._arbitrate(["swap"], ["calendar"])
        a = json.loads(cur.written[0]["assumptions"])
        self.assertEqual(a["unreadable_flows"], ["calendar"])

    def test_dossier_and_org_are_recorded_as_real_claimants(self):
        """This test asserted the opposite before 2026-09-28, on the belief that
        neither could claim a bare `yes`. Both execute a write on one."""
        _reply, cur = self._arbitrate(["dossier", "org"])
        a = json.loads(cur.written[0]["assumptions"])
        self.assertEqual(a["would_have_picked"], "dossier")     # chain order
        self.assertIsNone(a["would_have_picked_note"])
        self.assertEqual(a["open_without_bare_claimant"], [])

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
