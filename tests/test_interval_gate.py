"""PROGRAM-2 interval GATE — three conditions, fail-closed. Synthetic only."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import json
import unittest
from datetime import date, timedelta
from unittest import mock

from artemis import interval_gate as ig

TODAY = date(2027, 7, 15)


class Cur:
    """Serves the gate's three reads; any of them can be made to raise."""

    def __init__(self, *, cleared="true", cardio=(), pain=(), raises=(),
                 resolved=None, logged=False, row_exists=True):
        self.cleared, self.cardio, self.pain = cleared, list(cardio), list(pain)
        self.raises, self._res, self.written = set(raises), [], []
        self.resolved, self.logged, self.row_exists = resolved, logged, row_exists
        self.marked, self.upserted = [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} placeholders but {got} params"
        self._res = []
        if "acos.system_state" in s and "SELECT" in s:
            # Two different reads hit this table: the cleared FLAG and the
            # "this day is already resolved" MARKER. Keyed by the param, because
            # answering the marker with the flag's value would make every resolve
            # look already-done.
            key = (params or (None,))[0]
            if str(key).startswith(ig.RESOLVED_PREFIX):
                if "marker" in self.raises:
                    raise RuntimeError("down")
                self._res = [(self.resolved,)] if self.resolved is not None else []
            else:
                if "state" in self.raises:
                    raise RuntimeError("down")
                self._res = [(self.cleared,)] if self.cleared is not None else []
        elif "INSERT INTO acos.system_state" in s:
            self.marked.append(params)
        elif s.startswith("SELECT 1 FROM health.session_log"):
            # Anchored at the START: the recent-cardio query also mentions
            # health.session_log, in an EXISTS subquery, so a substring match
            # here swallowed it and every gate saw "0 of the last 0".
            self._res = [(1,)] if self.logged else []
        elif s.startswith("SELECT plan_id FROM health.plan"):
            self._res = [(41,)] if self.row_exists else []
        elif "INSERT INTO health.plan" in s:
            self.upserted.append(params)
        elif "health.plan" in s and "session_type IN" in s:   # recent cardio
            if "cardio" in self.raises:
                raise RuntimeError("down")
            self._res = list(self.cardio)
        elif "health.daily_state" in s:
            if "pain" in self.raises:
                raise RuntimeError("down")
            self._res = list(self.pain)
        elif "INSERT INTO acos.audit_log" in s:
            cols = s.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            self.written.append(dict(zip(cols, params)))
            self._res = [("id-1",)]

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


def _cardio(n_rows=6, n_logged=6):
    return [(TODAY - timedelta(days=i + 1), i < n_logged) for i in range(n_rows)]


def _pain(score, region="knee", days_ago=1):
    return [(TODAY - timedelta(days=days_ago), json.dumps({"pain": {region: score}}))]


class TestAllThreePass(unittest.TestCase):
    def test_cleared_training_and_pain_free_runs_intervals(self):
        r = ig.evaluate(Cur(cardio=_cardio(), pain=_pain(1)), TODAY)
        self.assertTrue(r.ok)
        self.assertIsNone(r.reason)
        self.assertEqual(r.conditions, {"cleared": True, "training": True, "no_pain": True})


class TestEachConditionBlocks(unittest.TestCase):
    def test_a_not_cleared_flag_blocks(self):
        r = ig.evaluate(Cur(cleared="", cardio=_cardio(), pain=_pain(0)), TODAY)
        self.assertFalse(r.ok)
        self.assertIn("intervals cleared", r.reason)

    def test_a_missing_flag_blocks(self):
        """Never sent is not cleared."""
        r = ig.evaluate(Cur(cleared=None, cardio=_cardio(), pain=_pain(0)), TODAY)
        self.assertFalse(r.ok)
        self.assertIs(r.conditions["cleared"], False)

    def test_too_few_logged_cardio_blocks(self):
        r = ig.evaluate(Cur(cardio=_cardio(6, 3), pain=_pain(0)), TODAY)
        self.assertFalse(r.ok)
        self.assertIn("3 of the last 6", r.reason)

    def test_exactly_four_of_six_passes(self):
        r = ig.evaluate(Cur(cardio=_cardio(6, 4), pain=_pain(0)), TODAY)
        self.assertTrue(r.ok)

    def test_pain_at_the_threshold_blocks(self):
        r = ig.evaluate(Cur(cardio=_cardio(), pain=_pain(3)), TODAY)
        self.assertFalse(r.ok)
        self.assertIn("pain 3", r.reason)

    def test_pain_below_the_threshold_does_not(self):
        self.assertTrue(ig.evaluate(Cur(cardio=_cardio(), pain=_pain(2)), TODAY).ok)


class TestFailClosed(unittest.TestCase):
    """"I could not check" and "you are cleared" must never look the same."""

    def test_an_unreadable_flag_blocks_and_says_so(self):
        r = ig.evaluate(Cur(cardio=_cardio(), pain=_pain(0), raises=["state"]), TODAY)
        self.assertFalse(r.ok)
        self.assertIsNone(r.conditions["cleared"])
        self.assertIn("couldn't check", r.reason)

    def test_unreadable_cardio_blocks(self):
        r = ig.evaluate(Cur(pain=_pain(0), raises=["cardio"]), TODAY)
        self.assertFalse(r.ok)
        self.assertIsNone(r.conditions["training"])

    def test_unreadable_checkins_block(self):
        r = ig.evaluate(Cur(cardio=_cardio(), raises=["pain"]), TODAY)
        self.assertFalse(r.ok)
        self.assertIsNone(r.conditions["no_pain"])

    def test_everything_unreadable_blocks_and_names_all_three(self):
        r = ig.evaluate(Cur(raises=["state", "cardio", "pain"]), TODAY)
        self.assertFalse(r.ok)
        for phrase in ("cleared", "cardio", "check-ins"):
            self.assertIn(phrase, r.reason)

    def test_it_never_raises(self):
        class Broken(Cur):
            def execute(self, *a, **k):
                raise RuntimeError("everything is down")
        self.assertFalse(ig.evaluate(Broken(), TODAY).ok)


class TestTheDecisionIsRecorded(unittest.TestCase):
    def test_a_blocked_gate_records_why(self):
        cur = Cur(cleared="", cardio=_cardio(), pain=_pain(0))
        r = ig.evaluate(cur, TODAY)
        ig.record(cur, r, TODAY, "msp_home")
        a = json.loads(cur.written[0]["assumptions"])
        self.assertIs(a["cleared"], False)
        self.assertIs(a["training"], True)
        self.assertIn("intervals cleared", a["reason"])
        self.assertEqual(cur.written[0]["outcome"], "z2_variant")

    def test_a_passing_gate_is_recorded_too(self):
        """"It ran Z2 and I don't know why" is the question this answers, and
        that needs the passing days on the record as well."""
        cur = Cur(cardio=_cardio(), pain=_pain(0))
        ig.record(cur, ig.evaluate(cur, TODAY), TODAY, "msp_home")
        self.assertEqual(cur.written[0]["outcome"], "intervals")


class TestArtemisNeverSetsIt(unittest.TestCase):
    def test_only_the_chat_command_writes_the_flag(self):
        """Brad Spaits, applied to his body. Nothing but the handler may set it."""
        import pathlib, re
        root = pathlib.Path(__file__).resolve().parents[1]
        setters = []
        for p in list(root.glob("artemis/*.py")) + list(root.glob("knowledge/*.py")):
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"set_system_value\(\s*CLEARED_KEY|"
                             r'set_system_value\(\s*["\']intervals_cleared', line):
                    setters.append(f"{p.name}:{n}")
        self.assertEqual(len(setters), 1, f"expected one setter, found {setters}")
        self.assertTrue(setters[0].startswith("main.py"), setters)

    def test_the_command_matches_both_forms_and_nothing_else(self):
        from artemis import main as m
        for t in ("intervals cleared", "Intervals Cleared", "intervals not cleared"):
            self.assertTrue(m._INTERVALS_CLEARED_RE.match(t), t)
        for t in ("intervals", "cleared", "am i cleared for intervals", "clear intervals"):
            self.assertFalse(m._INTERVALS_CLEARED_RE.match(t), t)


if __name__ == "__main__":
    unittest.main()


# ── Where the gate is APPLIED ───────────────────────────────────────────────
# The gate was correct and unwired for a round: `build_row` passed `gate=None`
# and `_intervals` read an absent gate as a pass, so 10/17 and 10/18 built as
# intervals whatever the gate would have said. These tests are about the wiring,
# not the conditions.

INTERVAL_DAY = date(2026, 10, 17)


def _spec(day=INTERVAL_DAY, slot="morning"):
    return {"plan_date": day, "slot": slot, "session_type": "cardio_intervals",
            "week_num": 5, "location": "MSP home", "location_key": "msp_home",
            "day_type": "msp_home", "pos": 13, "wk0": False}


class TestAnAbsentGateIsAFail(unittest.TestCase):
    """The defect itself: no gate answer must never build intervals."""

    def _blocks(self, gate):
        from artemis import health_office as office
        return office._intervals(5, location="MSP home", location_key="msp_home", gate=gate)

    def test_no_gate_builds_the_z2_variant(self):
        blocks, _rpe, zone, _est = self._blocks(None)
        self.assertEqual(blocks["type"], "steady")
        self.assertEqual(blocks["ran_as"], "z2_variant")
        self.assertEqual(zone, 2)
        self.assertIn("not been evaluated", blocks["z2_variant_reason"])

    def test_a_blocked_gate_builds_the_z2_variant_and_names_the_condition(self):
        blocks, _rpe, zone, _est = self._blocks({"ok": False, "reason": "you haven't sent x"})
        self.assertEqual(blocks["ran_as"], "z2_variant")
        self.assertEqual(zone, 2)
        self.assertEqual(blocks["z2_variant_reason"], "you haven't sent x")

    def test_only_a_passing_gate_builds_intervals(self):
        blocks, _rpe, zone, _est = self._blocks({"ok": True})
        self.assertEqual(blocks["type"], "intervals")
        self.assertEqual(blocks["ran_as"], "intervals")
        self.assertEqual(zone, 4)

    def test_a_seeded_row_with_no_gate_is_zone_two(self):
        """build_row passes no gate, so every seeded interval row is Z2 until
        the morning resolves it. Fail-closed by construction, not by luck."""
        from artemis import health_office as office
        row = office.build_row(_spec())
        self.assertEqual(row["session_type"], "cardio_intervals")
        self.assertEqual(row["blocks"]["type"], "steady")
        self.assertEqual(row["target_hr_zone"], 2)


class TestResolveDay(unittest.TestCase):
    def _cur(self, **kw):
        kw.setdefault("cardio", _cardio())
        kw.setdefault("pain", _pain(1))
        return Cur(**kw)

    def _resolve(self, cur, day=INTERVAL_DAY):
        with mock.patch("artemis.health_office.build_schedule", return_value=[_spec(day)]):
            return ig.resolve_day(cur, day)

    def test_a_passing_gate_writes_the_interval_row(self):
        cur = self._cur()
        out = self._resolve(cur)
        self.assertTrue(out["resolved"])
        self.assertEqual(out["outcome"], "intervals")
        self.assertEqual(len(cur.upserted), 1)
        blocks = json.loads(cur.upserted[0][5])
        self.assertEqual(blocks["type"], "intervals")
        self.assertEqual(cur.upserted[0][7], 4)          # target_hr_zone

    def test_a_blocked_gate_writes_the_z2_row_with_the_reason(self):
        cur = self._cur(cleared="")
        out = self._resolve(cur)
        self.assertEqual(out["outcome"], "z2_variant")
        self.assertIn("intervals cleared", out["reason"])
        blocks = json.loads(cur.upserted[0][5])
        self.assertEqual(blocks["type"], "steady")
        self.assertIn("intervals cleared", blocks["z2_variant_reason"])
        self.assertEqual(cur.upserted[0][7], 2)

    def test_an_unreadable_condition_writes_the_z2_row(self):
        cur = self._cur(raises=("state",))
        out = self._resolve(cur)
        self.assertEqual(out["outcome"], "z2_variant")
        self.assertIn("couldn't check", out["reason"])
        self.assertEqual(json.loads(cur.upserted[0][5])["type"], "steady")

    def test_one_decision_row_per_day_and_the_day_is_marked(self):
        cur = self._cur()
        self._resolve(cur)
        gate_rows = [w for w in cur.written if w.get("action") == "interval_gate"]
        self.assertEqual(len(gate_rows), 1)
        self.assertEqual(len(cur.marked), 1)
        self.assertEqual(cur.marked[0][0], f"{ig.RESOLVED_PREFIX}{INTERVAL_DAY.isoformat()}")

    def test_a_second_call_on_the_same_day_does_nothing(self):
        """Not per fetch, per evaluation day. A wake that runs twice must not
        write a second decision row or re-evaluate against a changed read."""
        cur = self._cur(resolved="intervals")
        out = self._resolve(cur)
        self.assertFalse(out["resolved"])
        self.assertEqual(cur.upserted, [])
        self.assertEqual([w for w in cur.written if w.get("action") == "interval_gate"], [])

    def test_a_day_with_no_interval_row_is_left_alone(self):
        cur = self._cur()
        with mock.patch("artemis.health_office.build_schedule",
                        return_value=[{**_spec(), "session_type": "strength_a"}]):
            out = ig.resolve_day(cur, INTERVAL_DAY)
        self.assertFalse(out["resolved"])
        self.assertEqual(cur.upserted, [])

    def test_a_logged_session_is_never_rewritten(self):
        """He already trained it. Rewriting would replace what he did with what
        the gate now thinks."""
        cur = self._cur(logged=True)
        out = self._resolve(cur)
        self.assertTrue(out["resolved"])
        self.assertEqual(cur.upserted, [])
        self.assertEqual(out["skipped"], [f"{INTERVAL_DAY} morning (already logged)"])

    def test_an_unreadable_marker_raises_rather_than_double_evaluating(self):
        cur = self._cur(raises=("marker",))
        with self.assertRaises(RuntimeError):
            self._resolve(cur)
        self.assertEqual(cur.upserted, [])


class TestTheCardAndThePostAgree(unittest.TestCase):
    """Both read the row the gate wrote. There is no second evaluation to
    disagree with -- which is the reason the gate resolves the ROW rather than
    being consulted at card fetch."""

    def _resolved_blocks(self, **kw):
        kw.setdefault("cardio", _cardio())
        kw.setdefault("pain", _pain(1))
        cur = Cur(**kw)
        with mock.patch("artemis.health_office.build_schedule", return_value=[_spec()]):
            ig.resolve_day(cur, INTERVAL_DAY)
        return json.loads(cur.upserted[0][5])

    def test_the_post_names_the_same_reason_the_card_carries(self):
        from artemis import wake
        blocks = self._resolved_blocks(cleared="")
        plan = {"session_type": "cardio_intervals", "blocks": blocks,
                "est_duration_min": 55, "plan_date": INTERVAL_DAY}
        lines = wake._workout_section(plan)
        text = "\n".join(lines)
        self.assertIn("Not intervals today:", text)
        # The card reads blocks["z2_variant_reason"]; the post must quote it, not
        # compose its own version of why.
        self.assertIn(blocks["z2_variant_reason"], text)

    def test_a_passing_gate_adds_no_such_line(self):
        from artemis import wake
        blocks = self._resolved_blocks()
        self.assertEqual(blocks["ran_as"], "intervals")
        lines = wake._workout_section({"session_type": "cardio_intervals", "blocks": blocks,
                                       "est_duration_min": 33, "plan_date": INTERVAL_DAY})
        self.assertNotIn("Not intervals today:", "\n".join(lines))


class TestEveryBlockIsPlainJson(unittest.TestCase):
    """`upsert_params` calls json.dumps WITHOUT default=str, so a non-serialisable
    value raises instead of silently becoming a string in a card field."""

    def test_every_built_row_serialises_without_a_default(self):
        from artemis import health_office as office
        for row in office.build_rows():
            with self.subTest(str(row["plan_date"])):
                json.dumps(row["blocks"])

    def test_upsert_params_is_in_the_sql_column_order(self):
        from artemis import health_office as office
        row = office.build_row(_spec())
        params = office.upsert_params(row)
        cols = (office._UPSERT_SQL.split("(", 1)[1].split(")", 1)[0]
                .replace("\n", " ").replace(" ", "").split(","))
        self.assertEqual(len(params), len(cols))
        self.assertEqual(cols[1], "slot")
        self.assertEqual(params[1], "morning")
        self.assertEqual(cols[0], "plan_date")
        self.assertEqual(params[0], INTERVAL_DAY)
