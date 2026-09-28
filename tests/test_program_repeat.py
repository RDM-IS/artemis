"""REPEAT-WEEK (Ryan, 2026-09-27) — more than one session not done repeats the
week, and the block's end moves out a week. Synthetic data only."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import json
import unittest
from datetime import date, timedelta
from unittest import mock

from artemis import cycle, health_office as office, program_repeat as pr

R = office.WEEK2_START + timedelta(days=14)        # a repeat beginning in calendar week 4


def _offline():
    return (mock.patch.object(cycle, "override_for", return_value=None),
            mock.patch.object(cycle, "locations", return_value=cycle.DEFAULT_LOCATIONS),
            mock.patch.object(cycle, "anchor", return_value=cycle.DEFAULT_ANCHOR))


class TestWeekNumbers(unittest.TestCase):
    def test_no_repeats_is_unchanged(self):
        self.assertEqual(office.week_num_for(office.WEEK2_START, []), 2)
        self.assertEqual(office.week_num_for(office.OFFICE_END, []), 7)
        self.assertEqual(office.program_end([]), office.OFFICE_END)

    def test_a_repeat_holds_the_number_back_and_moves_the_end(self):
        before = R - timedelta(days=1)
        self.assertEqual(office.week_num_for(before, [R]), office.week_num_for(R, [R]))
        self.assertEqual(office.week_num_for(R + timedelta(days=7), [R]),
                         office.week_num_for(before, [R]) + 1)
        self.assertEqual(office.program_end([R]), office.OFFICE_END + timedelta(days=7))
        self.assertEqual(office.week_num_for(office.program_end([R]), [R]), 7)   # still ends on 7

    def test_repeats_fail_closed(self):
        with mock.patch("artemis.quiet_hours.get_system_value", return_value="{not json"):
            with self.assertRaises(ValueError):
                office.repeat_starts()
        with mock.patch("artemis.quiet_hours.get_system_value", return_value='{"a": 1}'):
            with self.assertRaises(ValueError):
                office.repeat_starts()


class TestScheduleWithARepeat(unittest.TestCase):
    def test_the_real_builder_and_validator_accept_a_repeat(self):
        a, b, c = _offline()
        with a, b, c:
            rows = office.build_rows([R])
            office.validate_rows(rows, end=office.program_end([R]))
        mornings = [r for r in rows if r["slot"] == "morning"]
        self.assertEqual(mornings[-1]["plan_date"], office.OFFICE_END + timedelta(days=7))
        wk = {r["plan_date"]: r["week_num"] for r in mornings}
        self.assertEqual(wk[R], wk[R - timedelta(days=1)])
        # The repeated week is built with the same progression (same exercises/loads).
        prev = [r for r in rows if r["slot"] == "morning" and R - timedelta(days=7) <= r["plan_date"] < R
                and r["session_type"].startswith("strength")]
        rep = [r for r in rows if r["slot"] == "morning" and R <= r["plan_date"] < R + timedelta(days=7)
               and r["session_type"].startswith("strength")]
        self.assertEqual([r["week_num"] for r in prev], [r["week_num"] for r in rep])

    def test_the_writer_is_slot_aware(self):
        self.assertIn("ON CONFLICT (plan_date, slot)", office._UPSERT_SQL)
        self.assertEqual(office._UPSERT_SQL.count("%s"), 11)


class TestPlanRewrite(unittest.TestCase):
    def test_logged_rows_are_kept_and_earlier_rows_untouched(self):
        built = [{"plan_date": R - timedelta(days=1), "slot": "morning"},
                 {"plan_date": R, "slot": "morning"}, {"plan_date": R, "slot": "evening"},
                 {"plan_date": R + timedelta(days=1), "slot": "morning"}]
        existing = [{"plan_date": R, "slot": "morning", "logged": True},
                    {"plan_date": R, "slot": "evening", "logged": False}]
        p = pr.plan_rewrite(existing, built, R)
        self.assertEqual([(r["plan_date"], r["slot"]) for r in p["target"]],
                         [(R, "evening"), (R + timedelta(days=1), "morning")])
        self.assertEqual(p["kept_logged"], [(R, "morning")])


class TestProposal(unittest.TestCase):
    ND = [{"plan_id": 1, "plan_date": "2027-03-08", "session_type": "strength_a",
           "display_name": "Test A", "skipped": False},
          {"plan_id": 2, "plan_date": "2027-03-10", "session_type": "cardio_z2",
           "display_name": "Test Z2", "skipped": True}]

    def _run(self, nd, repeats=()):
        store = {}
        with mock.patch.object(pr, "not_done_in", return_value=nd), \
             mock.patch.object(office, "repeat_starts", return_value=list(repeats)), \
             mock.patch("artemis.quiet_hours.get_system_value", side_effect=lambda k: store.get(k)), \
             mock.patch("artemis.quiet_hours.set_system_value",
                        side_effect=lambda k, v: store.__setitem__(k, v)):
            return pr.propose(office.WEEK2_START + timedelta(days=7)), store

    def test_two_not_done_proposes_and_stores(self):
        msg, store = self._run(self.ND)
        self.assertIn("repeat week", msg)
        self.assertIn("no repeat", msg)
        self.assertIn("(skipped)", msg)
        p = json.loads(store[pr.PENDING_KEY])
        self.assertEqual(p["repeat_start"], (office.WEEK2_START + timedelta(days=14)).isoformat())

    def test_one_or_none_proposes_nothing(self):
        self.assertEqual(self._run(self.ND[:1])[0], None)
        self.assertEqual(self._run([])[0], None)

    def test_an_already_repeated_week_is_not_proposed_again(self):
        msg, _ = self._run(self.ND, repeats=[office.WEEK2_START + timedelta(days=14)])
        self.assertIsNone(msg)


class TestRouting(unittest.TestCase):
    def test_only_qualified_words(self):
        from artemis.main import _NO_REPEAT_RE, _REPEAT_RE
        for t in ("repeat week", "Repeat the week", "yes repeat"):
            self.assertTrue(_REPEAT_RE.match(t), t)
        for t in ("no repeat", "don't repeat"):
            self.assertTrue(_NO_REPEAT_RE.match(t), t)
        for t in ("yes", "no", "repeat", "repeat week 3 please"):
            self.assertFalse(_REPEAT_RE.match(t) or _NO_REPEAT_RE.match(t), t)


if __name__ == "__main__":
    unittest.main()


class _Cur:
    """An in-memory health.plan with just the statements apply() runs."""
    def __init__(self, rows, logged=()):
        self.rows = {(r["plan_date"], r["slot"]): dict(r) for r in rows}
        self.logged, self._res, self.state = set(logged), [], {}
        self.corrupt = None                   # (date, slot) to write wrong — guard test
        self.audit = []                       # captured acos.audit_log writes

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        # Like psycopg2: every %s in the statement must be supplied. The fake used
        # to accept anything, which is how a statement and its params drifted apart
        # unnoticed in the Lambda on 2026-09-25 (#213).
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} %s placeholders but {got} params in {s[:70]!r}"
        if s.startswith("SELECT p.plan_date, p.slot, EXISTS"):
            self._res = [(d, sl, (d, sl) in self.logged) for (d, sl) in self.rows if d >= params[0]]
        elif s.startswith("SELECT plan_date, slot, week_num, session_type, blocks FROM health.plan ORDER"):
            self._res = [(d, sl, r["week_num"], r["session_type"], r["blocks"])
                         for (d, sl), r in sorted(self.rows.items())]
        elif s.startswith("INSERT INTO health.plan"):
            (d, sl, _ph, wk, st, blocks, *_rest) = params
            b = json.loads(blocks)
            if self.corrupt == (d, sl):
                b = {**b, "display_name": "WRONG"}
            self.rows[(d, sl)] = {"plan_date": d, "slot": sl, "week_num": wk, "session_type": st,
                                  "blocks": b}
        elif s.startswith("SELECT week_num, session_type, blocks FROM health.plan WHERE"):
            r = self.rows.get((params[0], params[1]))
            self._res = [(r["week_num"], r["session_type"], r["blocks"])] if r else []
        elif "INTO acos.system_state" in s:
            self.state[params[0]] = params[1]
        elif "INTO acos.audit_log" in s:
            cols = s.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            self.audit.append(dict(zip(cols, params)))
            self._res = [(9001,)]                      # RETURNING id
        self.rolled_back = getattr(self, "rolled_back", False)

    def fetchall(self):
        return self._res

    def fetchone(self):
        return self._res[0] if self._res else None


class TestApply(unittest.TestCase):
    def _run(self, logged=(), corrupt=None):
        a, b, c = _offline()
        with a, b, c:
            current = office.build_rows([])
        cur = _Cur([{"plan_date": r["plan_date"], "slot": r["slot"], "week_num": r["week_num"],
                     "session_type": r["session_type"], "blocks": r["blocks"]} for r in current],
                   logged=logged)
        cur.corrupt = corrupt
        snapshot = {k: dict(v) for k, v in cur.rows.items()}

        class Conn:
            def cursor(self_):
                return cur

            def rollback(self_):
                cur.rows = {k: dict(v) for k, v in snapshot.items()}
                cur.rolled_back = True

        from contextlib import contextmanager

        @contextmanager
        def gc():
            yield Conn()
        a, b, c = _offline()
        with a, b, c, mock.patch.object(office, "repeat_starts", return_value=[]), \
             mock.patch("knowledge.db.get_connection", gc), \
             mock.patch("artemis.quiet_hours.set_system_value"):
            msg = pr.apply(R)
        return msg, cur, snapshot

    def test_a_repeat_rebuilds_from_the_repeat_week_and_extends_the_block(self):
        msg, cur, before = self._run()
        self.assertIn("Done", msg)
        new_end = office.OFFICE_END + timedelta(days=7)
        self.assertIn((new_end, "morning"), cur.rows)
        for (d, sl), r in before.items():
            if d < R:
                self.assertEqual(cur.rows[(d, sl)], r, f"{d} {sl} before the repeat moved")
        self.assertEqual(cur.rows[(R, "morning")]["week_num"],
                         cur.rows[(R - timedelta(days=1), "morning")]["week_num"])
        self.assertEqual(json.loads(cur.state[office.REPEATS_KEY]), [R.isoformat()])

    def test_logged_rows_are_left_alone(self):
        msg, cur, before = self._run(logged=[(R, "morning")])
        self.assertEqual(cur.rows[(R, "morning")], before[(R, "morning")])
        self.assertIn("already-logged", msg)

    def test_a_row_that_doesnt_verify_rolls_everything_back(self):
        msg, cur, before = self._run(corrupt=(R + timedelta(days=2), "morning"))
        self.assertIn("didn't verify", msg)
        self.assertTrue(cur.rolled_back)
        self.assertEqual(cur.rows, before)
