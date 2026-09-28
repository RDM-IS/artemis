"""COGNITION-1 bronze — the decision-row write, and the two sites that use it.

Synthetic only (PUBLIC-FIXTURES): no real session, dates outside any real plan,
no real Notion ids.

    python3.11 -m unittest tests.test_cognition_bronze
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import json
import unittest
from datetime import date, timedelta
from unittest import mock

from artemis import cognition

D = date(2027, 3, 4)          # synthetic: no real plan row exists here


class Cur:
    """A psycopg2-like cursor that ENFORCES BINDS and captures audit writes.

    The bind check is the point: a fake that accepts any (sql, params) pair is
    how a statement and its parameters drifted apart unnoticed in the Lambda on
    2026-09-25 (#213). Three new columns on a shared INSERT is exactly that
    shape of change, so the fake refuses it here.
    """

    def __init__(self, row_id=4242, entries=0):
        self.captured, self._res, self._id, self.entries = [], [], row_id, entries

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} %s placeholders but {got} params in {s[:70]!r}"
        if "INTO acos.audit_log" in s:
            cols = s.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            self.captured.append(dict(zip(cols, params)))
            self._res = [(self._id,)]
        elif "count(*) FROM nutrition.entry" in s:
            self._res = [(self.entries,)]
        else:
            self._res = []

    def fetchone(self):
        return self._res[0] if self._res else None

    @property
    def one(self):
        assert len(self.captured) == 1, f"expected 1 audit row, got {len(self.captured)}"
        return self.captured[0]


class TestHelper(unittest.TestCase):
    def _log(self, cur=None, **kw):
        kw.setdefault("agent", "test")
        kw.setdefault("action", "unit_test")
        kw.setdefault("domain", "health")
        kw.setdefault("outcome", "executed")
        kw.setdefault("assumptions", {"a": 1})
        return cognition.log_decision(cur or Cur(), **kw)

    def test_the_statement_and_its_params_agree(self):
        cur = Cur()
        self._log(cur)
        row = cur.one
        self.assertEqual(row["action"], "unit_test")
        self.assertEqual(json.loads(row["assumptions"]), {"a": 1})
        self.assertEqual(json.loads(row["metadata"]), {})

    def test_it_returns_the_new_row_id(self):
        self.assertEqual(self._log(Cur(row_id=77)), 77)

    def test_a_dict_cursor_works_too(self):
        class DictCur(Cur):
            def fetchone(self):
                return {"id": 91} if self._res else None
        self.assertEqual(self._log(DictCur()), 91)

    def test_no_returning_row_is_none_not_a_crash(self):
        class Silent(Cur):
            def fetchone(self):
                return None
        self.assertIsNone(self._log(Silent()))

    def test_manual_gap_is_written_explicitly_and_false_by_default(self):
        self.assertIs(Cur() and self._log(Cur()) is not None, True)
        cur = Cur()
        self._log(cur)
        self.assertIs(cur.one["manual_gap"], False)     # never NULL-as-maybe

    def test_manual_gap_must_be_a_real_bool(self):
        for bad in (None, 0, 1, "false"):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    self._log(manual_gap=bad)

    def test_assumptions_must_be_a_dict(self):
        for bad in (None, "{}", [], 3):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    self._log(assumptions=bad)

    def test_dates_and_other_objects_serialise_rather_than_raising(self):
        cur = Cur()
        self._log(cur, assumptions={"day": D, "nested": {"when": D}})
        self.assertEqual(json.loads(cur.one["assumptions"])["day"], "2027-03-04")

    def test_a_failed_write_is_not_swallowed(self):
        """A decision that could not be recorded must not stand: the exception
        reaches the caller, whose transaction then rolls back."""
        class Broken(Cur):
            def execute(self, *a, **k):
                raise RuntimeError("no connection")
        with self.assertRaises(RuntimeError):
            self._log(Broken())


# ---------------------------------------------------------------------------
# Site 3 — program_repeat
# ---------------------------------------------------------------------------

class _Office:
    def week_num_for(self, d, reps=None):
        return 3

    def program_end(self, reps):
        return date(2027, 5, 2) + timedelta(days=7 * len(reps))


class TestRepeatAssumptions(unittest.TestCase):
    PROPOSAL = {"week_start": "2027-02-21", "repeat_start": "2027-02-28", "week_num": 3,
                "proposed_at": "2027-02-28T14:35:00+00:00",
                "not_done": [{"plan_date": "2027-02-23", "display_name": "Test Strength A"},
                             {"plan_date": "2027-02-25", "display_name": "Test Mobility",
                              "skipped": True}]}

    def _build(self, pending_value=PROPOSAL, raises=False):
        from artemis import program_repeat as pr
        plan = {"kept_logged": [(date(2027, 3, 1), "morning")]}
        side = RuntimeError("RDS unreachable") if raises else None
        with mock.patch.object(pr, "pending", side_effect=side,
                               return_value=pending_value):
            return pr._assumptions(date(2027, 2, 28), [], [date(2027, 2, 28)], plan,
                                   ["k1", "k2", "k3"], "md5-before", _Office())

    def test_it_records_which_sessions_were_not_done(self):
        a = self._build()
        self.assertEqual(a["trigger"]["not_done_count"], 2)
        self.assertEqual(a["trigger"]["source"], "proposal")
        self.assertIn("2027-02-23 Test Strength A", a["trigger"]["not_done"])
        self.assertIn("(skipped)", a["trigger"]["not_done"][1])

    def test_it_carries_the_hash_and_the_block_move(self):
        a = self._build()
        self.assertEqual(a["md5_untouched_before"], "md5-before")
        self.assertEqual(a["rows_to_rewrite"], 3)
        self.assertEqual(a["rows_kept_because_logged"], ["2027-03-01 morning"])
        self.assertNotEqual(a["program_end_before"], a["program_end_after"])

    def test_an_unreadable_proposal_is_unknown_not_empty(self):
        """Absence and silence are different answers. An empty not_done list
        would claim the rule fired on nothing at all."""
        a = self._build(raises=True)
        self.assertIsNone(a["trigger"]["not_done"])
        self.assertIsNone(a["trigger"]["not_done_count"])
        self.assertIn("unreadable", a["trigger"]["source"])

    def test_no_live_proposal_says_so(self):
        a = self._build(pending_value=None)
        self.assertIsNone(a["trigger"]["not_done"])
        self.assertIn("absent", a["trigger"]["source"])

    def test_the_whole_payload_is_jsonb_serialisable(self):
        for case in (self._build(), self._build(raises=True), self._build(None)):
            json.dumps(case)          # raises if not


class TestRepeatWritesTheDecision(unittest.TestCase):
    def test_apply_writes_assumptions_with_the_not_done_list(self):
        from tests import test_program_repeat as tpr
        t = tpr.TestApply("test_a_repeat_rebuilds_from_the_repeat_week_and_extends_the_block")
        with mock.patch("artemis.program_repeat.pending",
                        return_value=TestRepeatAssumptions.PROPOSAL):
            _msg, cur, _before = t._run()
        rows = [r for r in cur.audit if r.get("action") == "repeat_week"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIs(row["manual_gap"], False)
        a = json.loads(row["assumptions"])
        self.assertEqual(a["trigger"]["not_done_count"], 2)
        self.assertIn("rule", a)
        # metadata still carries what the write DID; assumptions is not a copy.
        self.assertIn("md5_untouched", json.loads(row["metadata"]))


# ---------------------------------------------------------------------------
# Site 4 — nutrition.prefill_day
# ---------------------------------------------------------------------------

class TestPrefillDecision(unittest.TestCase):
    """Every outcome of "which meal source won" is a recorded decision — the
    NONE answers most of all, because a day with no intake is either "he didn't
    log" or "nothing was ever planned", and nothing else distinguishes them."""

    def _run(self, day_type, *, dated=False, default_raises=None, foods=True,
             dated_raises=None):
        from artemis import notion_meal_plan as nmp
        from artemis import nutrition
        from artemis.notion_meal_plan import DefaultDay, PlannedFood
        slots = ({"breakfast": [PlannedFood("Test oats", "p-test", kcal=300, protein_g=20)]}
                 if foods else {})
        plan = DefaultDay("page-test", "Test day", slots=slots)
        cur = Cur()
        with mock.patch("artemis.cycle.day_type", return_value=day_type), \
             mock.patch.object(nutrition, "get_day", return_value=None), \
             mock.patch.object(nutrition, "upsert_food"), \
             mock.patch.object(nutrition, "_upsert_day"), \
             mock.patch.object(nmp, "fetch_dated_day", side_effect=dated_raises,
                               return_value=plan if dated else None), \
             mock.patch.object(nmp, "fetch_default_day", side_effect=default_raises,
                               return_value=plan):
            result = nutrition.prefill_day(cur, D)
        return result, cur

    def _assumptions(self, cur):
        return json.loads(cur.one["assumptions"])

    def test_a_dated_pick_records_that_the_pick_won(self):
        result, cur = self._run("wi", dated=True)
        self.assertEqual(result.outcome, "planned")
        a = self._assumptions(cur)
        self.assertEqual(a["chosen"], "picked")
        self.assertIs(a["dated_pick_found"], True)
        self.assertIs(cur.one["manual_gap"], False)

    def test_a_work_day_default_records_which_row_it_used(self):
        result, cur = self._run("msp_work")
        self.assertEqual(result.outcome, "planned")
        a = self._assumptions(cur)
        self.assertEqual(a["chosen"], "default")
        self.assertIs(a["dated_pick_found"], False)
        self.assertIsNotNone(a["default_row_for_kind"])

    def test_an_off_day_with_no_pick_is_a_recorded_decision(self):
        result, cur = self._run("msp_home")
        self.assertEqual(result.outcome, "no_plan")
        a = self._assumptions(cur)
        self.assertIsNone(a["chosen"])
        self.assertIsNone(a["default_row_for_kind"])
        self.assertIn("off day", a["detail"])
        self.assertEqual(a["day_kind"], "off")

    def test_notion_down_records_that_neither_lookup_RAN(self):
        from artemis import notion_meal_plan as nmp
        result, cur = self._run("msp_work",
                                dated_raises=nmp.NotionUnavailable("test outage"))
        self.assertEqual(result.outcome, "unavailable")
        a = self._assumptions(cur)
        self.assertIsNone(a["dated_pick_found"])      # unknown, NOT False
        self.assertIsNone(a["chosen"])

    def test_a_missing_default_row_records_the_pick_as_unknown(self):
        """The LookupError can come from either lookup, so `False` would be a
        guess when the first one is what raised."""
        result, cur = self._run("msp_work", dated_raises=LookupError("no such row"))
        self.assertEqual(result.outcome, "no_plan")
        self.assertIsNone(self._assumptions(cur)["dated_pick_found"])

    def test_a_default_row_with_no_usable_recipes(self):
        result, cur = self._run("msp_work", foods=False)
        self.assertEqual(result.outcome, "no_plan")
        a = self._assumptions(cur)
        self.assertEqual(a["chosen"], "default")
        self.assertIn("usable macros", a["detail"])

    def test_every_outcome_writes_exactly_one_decision_row(self):
        from artemis import notion_meal_plan as nmp
        cases = {
            "planned/picked": dict(day_type="wi", dated=True),
            "planned/default": dict(day_type="msp_work"),
            "no_plan/off": dict(day_type="msp_home"),
            "no_plan/norecipes": dict(day_type="msp_work", foods=False),
            "no_plan/lookup": dict(day_type="msp_work", dated_raises=LookupError("x")),
            "unavailable": dict(day_type="msp_work",
                                dated_raises=nmp.NotionUnavailable("x")),
        }
        for label, kw in cases.items():
            with self.subTest(case=label):
                _r, cur = self._run(**kw)
                self.assertEqual(len(cur.captured), 1, label)
                a = json.loads(cur.one["assumptions"])
                self.assertEqual(a["day"], D.isoformat())
                self.assertIn("rule", a)
                self.assertIs(cur.one["manual_gap"], False)

    def test_an_already_prefilled_day_is_not_a_decision(self):
        """Idempotent re-runs are not choices, and a row per re-run would drown
        the ones that are."""
        from artemis import nutrition
        cur = Cur(entries=3)
        with mock.patch.object(nutrition, "get_day", return_value=None):
            self.assertEqual(nutrition.prefill_day(cur, D).outcome, "already")
        self.assertEqual(cur.captured, [])


if __name__ == "__main__":
    unittest.main()
