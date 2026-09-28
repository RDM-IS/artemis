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

    def __init__(self, *, cleared="true", cardio=(), pain=(), raises=()):
        self.cleared, self.cardio, self.pain = cleared, list(cardio), list(pain)
        self.raises, self._res, self.written = set(raises), [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} placeholders but {got} params"
        self._res = []
        if "acos.system_state" in s and "SELECT" in s:
            if "state" in self.raises:
                raise RuntimeError("down")
            self._res = [(self.cleared,)] if self.cleared is not None else []
        elif "health.plan" in s:
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
