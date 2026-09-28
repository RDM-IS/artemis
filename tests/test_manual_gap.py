"""COGNITION-1 — the two manual_gap rules and the `gaps` command. Synthetic only.

    python3.11 -m unittest tests.test_manual_gap
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from artemis import manual_gap as mg

TODAY = date(2027, 4, 10)


class Cur:
    def __init__(self, *, rules=(), gap_rows=(), raises=False):
        self.rules, self.gap_rows, self.raises = list(rules), list(gap_rows), raises
        self._res, self.queries = [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.queries.append((s, params))
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} %s placeholders but {got} params"
        if self.raises:
            raise RuntimeError("RDS down")
        self._res = []
        if "acos.playbook_rules" in s:
            self._res = [(1,)] if params[0] in self.rules else []
        elif "manual_gap" in s:
            self._res = list(self.gap_rows)

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


# ---------------------------------------------------------------------------
# Rule 2 — source='script', set at write time
# ---------------------------------------------------------------------------

class TestScriptRule(unittest.TestCase):
    def test_true_only_for_script(self):
        self.assertTrue(mg.is_gap_by_script("script"))
        for other in ("automation_triage", "user_directed", "automation_rule", None, ""):
            with self.subTest(source=other):
                self.assertFalse(mg.is_gap_by_script(other))

    def test_every_script_write_sets_it_at_write_time(self):
        """Rule 3: never NULL-as-maybe. The scripts already mark source='script',
        so the flag belongs in the same statement — not in a third setter."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for name in ("reseed_health_plan_v2.py", "backfill_cardio_blocks.py",
                     "apply_leave_week.py", "backfill_load_config.py"):
            text = (root / "scripts" / name).read_text()
            with self.subTest(script=name):
                self.assertEqual(text.count("'script'"), text.count("manual_gap"),
                                 f"{name}: a source='script' write with no manual_gap")


# ---------------------------------------------------------------------------
# Rule 1 — an email disposition corrected with no rule covering it
# ---------------------------------------------------------------------------

class TestEmailRule(unittest.TestCase):
    CORR = {"corrected_to_action": "archive"}

    def test_a_correction_with_no_active_rule_is_a_gap(self):
        self.assertTrue(mg.is_gap_by_email_correction(
            Cur(rules=[]), domain="email", correction=self.CORR,
            corrected_to_action="archive"))

    def test_a_correction_an_active_rule_already_covers_is_not(self):
        self.assertFalse(mg.is_gap_by_email_correction(
            Cur(rules=["archive"]), domain="email", correction=self.CORR,
            corrected_to_action="archive"))

    def test_it_never_fires_outside_email(self):
        """The whole point of the scoping: playbook_rules is an inbox table, so
        asking it about a health action returns 'no rule' for every correction
        ever made, and the gap list would contain all of them."""
        for domain in ("health", "ops", None):
            with self.subTest(domain=domain):
                self.assertFalse(mg.is_gap_by_email_correction(
                    Cur(rules=[]), domain=domain, correction=self.CORR,
                    corrected_to_action="archive"))

    def test_no_correction_is_not_a_gap(self):
        self.assertFalse(mg.is_gap_by_email_correction(
            Cur(rules=[]), domain="email", correction=None,
            corrected_to_action="archive"))

    def test_an_unreadable_rules_table_does_not_invent_a_gap(self):
        """A missed gap is recoverable; an invented one is a fact in the ledger
        that was never established."""
        self.assertFalse(mg.is_gap_by_email_correction(
            Cur(raises=True), domain="email", correction=self.CORR,
            corrected_to_action="archive"))

    def test_an_unknown_corrected_to_action_is_not_a_gap(self):
        self.assertFalse(mg.is_gap_by_email_correction(
            Cur(rules=[]), domain="email", correction=self.CORR,
            corrected_to_action=None))


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------

def _row(action, n, days_ago=0):
    return (action, n, datetime(2027, 4, 10, 9, 0, tzinfo=timezone.utc)
            - timedelta(days=days_ago))


class TestGapsListing(unittest.TestCase):
    def _gaps(self, rows, period="week"):
        cur = Cur(gap_rows=rows)
        with mock.patch.object(mg, "_tz", return_value="America/Chicago"):
            out = mg.gaps(cur, period, today=TODAY)
        return out, cur

    def test_grouped_by_action_with_counts_and_the_latest_date(self):
        out, _cur = self._gaps([_row("leave_week_overrides", 8),
                                _row("load_config_backfill", 1, days_ago=3)])
        self.assertEqual([r["action"] for r in out],
                         ["leave_week_overrides", "load_config_backfill"])
        self.assertEqual(out[0]["count"], 8)
        self.assertEqual(out[1]["latest"].date(), date(2027, 4, 7))

    def test_the_window_is_trailing_and_anchored_to_the_active_timezone(self):
        _out, cur = self._gaps([], "week")
        sql, params = cur.queries[-1]
        self.assertIn("AT TIME ZONE %s", sql)          # never bare current_date
        self.assertEqual(params[0], "America/Chicago")  # passed, never interpolated
        self.assertEqual(params[1], TODAY - timedelta(days=6))

    def test_month_is_thirty_trailing_days(self):
        _out, cur = self._gaps([], "month")
        self.assertEqual(cur.queries[-1][1][1], TODAY - timedelta(days=29))

    def test_an_unknown_period_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            mg.gaps(Cur(), "fortnight", today=TODAY)

    def test_an_empty_period_says_so(self):
        self.assertEqual(mg.render([], "week"),
                         "No manual gaps recorded in the last 7 days.")
        self.assertEqual(mg.render([], "month"),
                         "No manual gaps recorded in the last 30 days.")

    def test_the_reply_lists_each_action_once(self):
        text = mg.render([{"action": "leave_week_overrides", "count": 8,
                           "latest": datetime(2027, 4, 9, tzinfo=timezone.utc)},
                          {"action": "load_config_backfill", "count": 1,
                           "latest": datetime(2027, 4, 7, tzinfo=timezone.utc)}], "week")
        self.assertIn("9 rows", text)
        self.assertIn("`leave_week_overrides` — 8, most recent 2027-04-09", text)
        self.assertIn("`load_config_backfill` — 1, most recent 2027-04-07", text)

    def test_the_reply_carries_no_advice(self):
        text = mg.render([{"action": "x", "count": 1,
                           "latest": datetime(2027, 4, 9, tzinfo=timezone.utc)}], "week")
        for word in ("should", "recommend", "consider", "suggest", "you could"):
            self.assertNotIn(word, text.lower())


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

class TestGapsCommand(unittest.TestCase):
    def _call(self, text, rows=(), raises=False):
        from artemis import main as m
        from contextlib import contextmanager

        class _Conn:
            def cursor(self_):
                return Cur(gap_rows=list(rows), raises=raises)

        @contextmanager
        def gc():
            yield _Conn()
        posted = []
        with mock.patch("knowledge.db.get_connection", gc), \
             mock.patch.object(mg, "_tz", return_value="America/Chicago"), \
             mock.patch.object(mg, "gaps", wraps=mg.gaps), \
             mock.patch.object(m, "_mm") as mm:
            mm.post_message.side_effect = lambda ch, t, root_id=None: posted.append(t)
            handled = m._handle_gaps_command({"id": "p1", "channel_id": "c1"}, text)
        return handled, posted

    def test_it_claims_the_command_and_its_periods(self):
        from artemis import main as m
        for text in ("gaps", "gaps week", "gaps month", "Gaps Week", "gaps?"):
            with self.subTest(text=text):
                self.assertTrue(m._GAPS_RE.match(text), text)

    def test_it_does_not_claim_anything_else(self):
        from artemis import main as m
        for text in ("gaps in my training", "what gaps", "gaps fortnight",
                     "mind the gaps", "gap"):
            with self.subTest(text=text):
                self.assertFalse(m._GAPS_RE.match(text), text)

    def test_an_empty_period_replies_that_it_is_empty(self):
        handled, posted = self._call("gaps")
        self.assertTrue(handled)
        self.assertEqual(posted, ["No manual gaps recorded in the last 7 days."])

    def test_it_groups_what_it_finds(self):
        _handled, posted = self._call("gaps month", rows=[_row("leave_week_overrides", 8)])
        self.assertIn("leave_week_overrides` — 8", posted[0])
        self.assertIn("last 30 days", posted[0])

    def test_a_broken_query_never_reads_as_a_clean_week(self):
        """"No gaps" and "I could not look" must not be the same sentence."""
        _handled, posted = self._call("gaps", raises=True)
        self.assertNotIn("No manual gaps", posted[0])
        self.assertIn("couldn't read", posted[0])

    def test_it_is_registered_in_the_deterministic_chain(self):
        import inspect, re as _re
        from artemis import main as m
        src = inspect.getsource(m)
        block = src[src.index("deterministic_chain = ["):]
        block = block[:block.index("\n    ]")]
        self.assertIn("gaps_command", _re.findall(r'\(\s*"([a-z_]+)"\s*,', block))


if __name__ == "__main__":
    unittest.main()
