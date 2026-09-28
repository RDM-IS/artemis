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

from knowledge import cognition

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


# ---------------------------------------------------------------------------
# Two bind styles, one column list
# ---------------------------------------------------------------------------

class TestBothDialects(unittest.TestCase):
    """The box writes through psycopg2, the Lambda through SQLAlchemy. If the two
    statements ever disagree about columns, one of them writes the wrong thing
    into the wrong column silently — so they are generated, and this proves it."""

    @staticmethod
    def _cols(sql):
        return sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")

    def test_the_two_statements_name_the_same_columns_in_the_same_order(self):
        self.assertEqual(self._cols(cognition.SQL_PG), self._cols(cognition.SQL_SA))
        self.assertEqual(self._cols(cognition.SQL_PG), list(cognition._COLUMNS))

    def test_the_jsonb_columns_are_cast_in_both(self):
        for c in ("metadata", "assumptions", "correction"):
            with self.subTest(column=c):
                self.assertIn(f"CAST(:{c} AS jsonb)", cognition.SQL_SA)
        self.assertEqual(cognition.SQL_PG.count("%s::jsonb"), 3)

    def test_both_return_the_id(self):
        self.assertTrue(cognition.SQL_PG.rstrip().endswith("RETURNING id"))
        self.assertTrue(cognition.SQL_SA.rstrip().endswith("RETURNING id"))


class SaDb:
    """A SQLAlchemy Session fake that ENFORCES NAMED BINDS, like #213's."""

    def __init__(self, row_id="dead-beef"):
        self.captured, self._id = [], row_id

    def execute(self, stmt, params=None):
        import re
        binds = set(re.findall(r"(?<![:\w]):([a-z_][a-z0-9_]*)", str(stmt), re.I))
        missing = binds - set((params or {}).keys())
        assert not missing, f"unbound parameters {sorted(missing)}"
        extra = set((params or {}).keys()) - binds
        assert not extra, f"params with no bind in the statement: {sorted(extra)}"
        self.captured.append(dict(params or {}))
        outer = self

        class R:
            def fetchone(self_):
                return {"id": outer._id}
        return R()


class TestSaDialect(unittest.TestCase):
    def test_it_writes_the_same_row_through_sqlalchemy(self):
        db = SaDb()
        rid = cognition.log_decision_sa(
            db, agent="gym_display", action="makeup_swap", domain="health",
            outcome="executed", assumptions={"x": 1}, metadata={"y": 2})
        self.assertEqual(rid, "dead-beef")
        row = db.captured[0]
        self.assertIs(row["manual_gap"], False)
        self.assertEqual(json.loads(row["assumptions"]), {"x": 1})
        self.assertIsNone(row["correction"])

    def test_the_same_validation_applies(self):
        with self.assertRaises(TypeError):
            cognition.log_decision_sa(SaDb(), agent="a", action="b", domain="c",
                                      outcome="d", assumptions="not a dict")


# ---------------------------------------------------------------------------
# Outcome rows
# ---------------------------------------------------------------------------

class TestOutcomeRow(unittest.TestCase):
    def test_an_outcome_row_has_NO_assumptions(self):
        """Load-bearing: the job finds work with `assumptions IS NOT NULL`, so an
        outcome row carrying assumptions would be picked up as a decision needing
        an outcome, and the job would feed on its own output every night."""
        cur = Cur()
        cognition.log_outcome(cur, decides="abc-123", action="checkin_adjust",
                              domain="health", outcome="logged_as_adjusted")
        row = cur.one
        self.assertIsNone(row["assumptions"])
        self.assertIsNone(row["manual_gap"])
        self.assertEqual(row["action"], "checkin_adjust.outcome")
        self.assertEqual(json.loads(row["metadata"])["decides"], "abc-123")
        self.assertEqual(row["agent"], "cognition")

    def test_the_action_spelling_lives_in_one_place(self):
        self.assertEqual(cognition.outcome_action("repeat_week"), "repeat_week.outcome")

    def test_a_correction_rides_on_the_outcome_row_not_the_decision(self):
        cur = Cur()
        cognition.log_outcome(cur, decides="abc", action="checkin_adjust", domain="health",
                              outcome="restored_to_original",
                              correction={"what": "ran the original session"})
        self.assertEqual(json.loads(cur.one["correction"])["what"], "ran the original session")


# ---------------------------------------------------------------------------
# Site 1 — the check-in decision, including the days nothing happens
# ---------------------------------------------------------------------------

class TestCheckinAssumptions(unittest.TestCase):
    PLAN = {"plan_id": 501, "plan_date": date(2027, 3, 4), "week_num": 2,
            "session_type": "strength_a", "target_rpe": 7.0, "est_duration_min": 45,
            "blocks": {"type": "circuit", "display_name": "Test A"}}

    def _ci(self, **kw):
        from artemis.health_checkin import CheckIn
        return CheckIn(**kw)

    def test_it_records_the_parsed_checkin_and_the_plan_row(self):
        from artemis import health_checkin as hc
        a = hc.adjust_assumptions(self.PLAN, self._ci(energy=2, sleep_hrs=5.5,
                                                      soreness={"quad": 4}, pain={"knee": 2}),
                                  considered=True, reason="test")
        self.assertEqual(a["plan_id"], 501)
        self.assertEqual(a["week_num"], 2)
        self.assertEqual(a["session_type_as_written"], "strength_a")
        self.assertEqual(a["target_rpe_as_written"], 7.0)
        self.assertEqual(a["checkin"]["energy"], 2)
        self.assertEqual(a["checkin"]["soreness"], {"quad": 4})
        self.assertEqual(a["checkin"]["pain"], {"knee": 2})

    def test_considered_separates_ran_and_found_nothing_from_never_ran(self):
        from artemis import health_checkin as hc
        never = hc.adjust_assumptions(self.PLAN, self._ci(), considered=False,
                                      reason="already logged")
        self.assertFalse(never["considered"])
        self.assertIsNone(never["rules_fired"])          # the ladder did not run
        ran = hc.adjust_assumptions(self.PLAN, self._ci(),
                                    hc.Adjustment(changed=False, blocks={}, session_type="x",
                                                  target_rpe=7.0, est_duration_min=45),
                                    considered=True, reason="no rule matched")
        self.assertTrue(ran["considered"])
        self.assertEqual(ran["rules_fired"], [])         # ran, matched nothing
        self.assertNotEqual(never["rules_fired"], ran["rules_fired"])

    def test_the_payload_is_jsonb_serialisable(self):
        from artemis import health_checkin as hc
        json.dumps(hc.adjust_assumptions(self.PLAN, self._ci(energy=3),
                                         considered=True, reason="t"), default=str)


class TestCheckinWritesDecisions(unittest.TestCase):
    """Drives the real check-in flow through the existing harness, so the two
    paths that used to record NOTHING are proved to record something now."""

    def _run(self, text_in, *, adjust=True, logged=False, session_type=None):
        from datetime import datetime, timezone
        from tests import test_checkin_adjust as tca
        from artemis import health_checkin as hc
        row = tca.office_row(tca.FRI)
        if session_type is not None:
            for r in row:
                r["session_type"] = session_type
        db = tca.FakeDB(row)
        if logged:
            pid = db.plan[tca.FRI]["plan_id"]
            db.logs.append({"plan_id": pid, "logged_via": "manual",
                            "log_type": "strength_set", "exercise": "Test",
                            "weight_lbs": 100})
        hc.process_checkin(db.cursor(), text_in, tca.FRI, checkin_id="post-1",
                           now=datetime(2026, 9, 18, 10, 10, tzinfo=timezone.utc),
                           adjust=adjust)
        return [d for d in db.decisions if d["assumptions"] is not None]

    def test_a_day_the_ladder_changes_nothing_is_still_recorded(self):
        """The case that used to vanish: he checked in, nothing was wrong, and
        the ledger held no trace that the ladder had even looked."""
        rows = self._run("slept 8 energy 4 sore 0")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "checkin_adjust")
        self.assertEqual(rows[0]["outcome"], "no_change")
        a = json.loads(rows[0]["assumptions"])
        self.assertTrue(a["considered"])
        self.assertEqual(a["rules_fired"], [])
        self.assertIs(rows[0]["manual_gap"], False)

    def test_an_already_logged_session_says_the_ladder_never_ran(self):
        rows = self._run("energy 2", logged=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "no_adjust_sets_logged")
        a = json.loads(rows[0]["assumptions"])
        self.assertFalse(a["considered"])
        self.assertIsNone(a["rules_fired"])
        self.assertIn("already logged", a["reason"])

    def test_an_applied_adjustment_records_the_rule_that_fired(self):
        rows = self._run("slept 5 energy 1 sore 0")
        self.assertEqual(len(rows), 1)
        a = json.loads(rows[0]["assumptions"])
        self.assertTrue(a["considered"])
        self.assertTrue(a["rules_fired"], "a low-energy check-in should fire a rule")
        self.assertEqual(a["checkin"]["energy"], 1)
        self.assertIsNotNone(a["session_type_after"])

    def test_the_suppressed_path_records_what_it_would_have_done(self):
        rows = self._run("slept 5 energy 1 sore 0", adjust=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "checkin_adjust_suppressed")
        self.assertTrue(json.loads(rows[0]["assumptions"])["rules_fired"])

    def test_every_recorded_checkin_decision_is_jsonb_and_explicit(self):
        for label, kw in {"no change": {}, "logged": {"logged": True},
                          "adjusted": {}, "suppressed": {"adjust": False}}.items():
            with self.subTest(case=label):
                text_in = "slept 5 energy 1 sore 0" if label != "no change" else "energy 4 sore 0"
                for row in self._run(text_in, **kw):
                    json.loads(row["assumptions"])
                    self.assertIs(row["manual_gap"], False)


if __name__ == "__main__":
    unittest.main()
