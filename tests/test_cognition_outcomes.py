"""COGNITION-1 — outcomes appended as their own rows. Synthetic only.

    python3.11 -m unittest tests.test_cognition_outcomes
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from artemis import cognition_outcomes as co

TODAY = date(2027, 3, 10)
YESTERDAY = TODAY - timedelta(days=1)
MADE_AT = datetime(2027, 3, 9, 12, 0, tzinfo=timezone.utc)


class Cur:
    """A cursor that answers the resolvers' reads, captures the writes, and
    REFUSES to UPDATE acos.audit_log — a decision row is append-only, and a test
    that only checks the happy path would not notice it being overwritten."""

    def __init__(self, *, decisions=(), logs=0, restores=0, day_status=None,
                 corrected=0, repeats_again=0, existing_outcomes=()):
        self.decisions = [dict(d) for d in decisions]
        self.logs, self.restores = logs, restores
        self.day_status, self.corrected = day_status, corrected
        self.repeats_again = repeats_again
        self.existing = list(existing_outcomes)      # (action, decides)
        self.written, self._res = [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        want = s.replace("%%", "").count("%s")
        got = 0 if params is None else len(params)
        assert want == got, f"{want} %s placeholders but {got} params in {s[:70]!r}"
        assert not (s.upper().startswith("UPDATE") and "acos.audit_log" in s), \
            "a decision row must never be UPDATEd (decision (a), 2026-09-28)"
        self._res = []
        if s.startswith("SELECT d.id, d.action"):
            actions, _limit = params
            self._res = [tuple(d[c] for c in co._COLS) for d in self.decisions
                         if d["action"] in actions]
        elif "FROM health.session_log" in s:
            self._res = [(self.logs,)]
        elif "action = 'checkin_restore'" in s:
            self._res = [(self.restores,)]
        elif "action = 'repeat_week'" in s and "created_at >" in s:
            self._res = [(self.repeats_again,)]
        elif "FROM nutrition.day" in s:
            self._res = [(self.day_status,)] if self.day_status is not None else []
        elif "FROM nutrition.entry" in s:
            self._res = [(self.corrected,)]
        elif s.startswith("SELECT 1 FROM acos.audit_log"):
            action, decides = params
            self._res = [(1,)] if (action, decides) in self.existing else []
        elif "INSERT INTO acos.audit_log" in s:
            cols = s.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            row = dict(zip(cols, params))
            self.written.append(row)
            self.existing.append((row["action"], json.loads(row["metadata"])["decides"]))
            self._res = [("new-id",)]

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


def _decision(action, *, assumptions, metadata=None, outcome="executed", did="d-1"):
    return {"id": did, "action": action, "domain": "health", "outcome": outcome,
            "assumptions": assumptions, "metadata": metadata or {}, "created_at": MADE_AT}


def _run(cur):
    return co.run(cur, today=TODAY)


# ---------------------------------------------------------------------------
# Site 1
# ---------------------------------------------------------------------------

CHECKIN = _decision("checkin_adjust", outcome="recovery",
                    assumptions={"plan_id": 77, "plan_date": YESTERDAY.isoformat()})


class TestCheckinOutcome(unittest.TestCase):
    def test_logged_as_adjusted(self):
        cur = Cur(decisions=[CHECKIN], logs=3)
        self.assertEqual(_run(cur)["wrote"], 1)
        row = cur.written[0]
        self.assertEqual(row["outcome"], "logged_as_adjusted")
        self.assertEqual(row["action"], "checkin_adjust.outcome")
        self.assertEqual(json.loads(row["metadata"])["decides"], "d-1")
        self.assertIsNone(row["assumptions"])         # an outcome is not a decision

    def test_a_restore_means_he_ran_the_original_and_is_a_correction(self):
        cur = Cur(decisions=[CHECKIN], logs=2, restores=1)
        _run(cur)
        row = cur.written[0]
        self.assertEqual(row["outcome"], "logged_as_original")
        self.assertIn("original", json.loads(row["correction"])["what"])

    def test_not_logged(self):
        cur = Cur(decisions=[CHECKIN], logs=0)
        _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "not_logged")
        self.assertIsNone(cur.written[0]["correction"])

    def test_a_no_change_decision_does_not_claim_as_adjusted(self):
        d = _decision("checkin_adjust", outcome="no_change",
                      assumptions={"plan_id": 77, "plan_date": YESTERDAY.isoformat()})
        cur = Cur(decisions=[d], logs=1)
        _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "logged")

    def test_todays_decision_is_not_yet_knowable(self):
        d = _decision("checkin_adjust",
                      assumptions={"plan_id": 77, "plan_date": TODAY.isoformat()})
        cur = Cur(decisions=[d], logs=0)
        counts = _run(cur)
        self.assertEqual((counts["not_yet"], counts["wrote"]), (1, 0))
        self.assertEqual(cur.written, [])

    def test_a_decision_missing_its_own_field_is_unreadable_not_guessed(self):
        cur = Cur(decisions=[_decision("checkin_adjust", assumptions={})])
        counts = _run(cur)
        self.assertEqual((counts["unreadable"], counts["wrote"]), (1, 0))


# ---------------------------------------------------------------------------
# Site 2
# ---------------------------------------------------------------------------

class TestMakeupOutcome(unittest.TestCase):
    D = _decision("makeup_swap", assumptions={"rule": "x"},
                  metadata={"rest_plan_id": 22, "on": YESTERDAY.isoformat()})

    def test_logged(self):
        cur = Cur(decisions=[self.D], logs=1)
        _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "logged")
        self.assertIsNone(cur.written[0]["correction"])

    def test_skipping_it_is_the_correction(self):
        cur = Cur(decisions=[self.D], logs=0)
        _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "not_logged")
        self.assertIn("skipped", json.loads(cur.written[0]["correction"])["what"])

    def test_the_rest_day_has_not_passed_yet(self):
        d = _decision("makeup_swap", assumptions={},
                      metadata={"rest_plan_id": 22, "on": TODAY.isoformat()})
        self.assertEqual(_run(Cur(decisions=[d]))["not_yet"], 1)


# ---------------------------------------------------------------------------
# Site 3
# ---------------------------------------------------------------------------

class TestRepeatOutcome(unittest.TestCase):
    def _d(self, start):
        return _decision("repeat_week", assumptions={"repeat_start": start.isoformat()})

    def test_a_clean_repeated_week(self):
        cur = Cur(decisions=[self._d(TODAY - timedelta(days=8))])
        with mock.patch("artemis.program_repeat.not_done_in", return_value=[]):
            _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "week_clean")

    def test_the_repeated_week_also_had_sessions_not_done(self):
        cur = Cur(decisions=[self._d(TODAY - timedelta(days=8))])
        nd = [{"plan_date": "2027-03-03", "display_name": "Test A"}]
        with mock.patch("artemis.program_repeat.not_done_in", return_value=nd):
            _run(cur)
        row = cur.written[0]
        self.assertEqual(row["outcome"], "week_not_done")
        self.assertEqual(json.loads(row["metadata"])["not_done_count"], 1)

    def test_a_second_repeat_of_the_same_week_is_the_correction(self):
        cur = Cur(decisions=[self._d(TODAY - timedelta(days=8))], repeats_again=1)
        with mock.patch("artemis.program_repeat.not_done_in", return_value=[]):
            _run(cur)
        self.assertIn("again", json.loads(cur.written[0]["correction"])["what"])

    def test_a_week_still_running_is_not_yet(self):
        cur = Cur(decisions=[self._d(TODAY - timedelta(days=2))])
        self.assertEqual(_run(cur)["not_yet"], 1)

    def test_an_unreadable_week_writes_nothing(self):
        cur = Cur(decisions=[self._d(TODAY - timedelta(days=8))])
        with mock.patch("artemis.program_repeat.not_done_in",
                        side_effect=RuntimeError("RDS down")):
            counts = _run(cur)
        self.assertEqual((counts["unreadable"], counts["wrote"]), (1, 0))
        self.assertEqual(cur.written, [])


# ---------------------------------------------------------------------------
# Site 4
# ---------------------------------------------------------------------------

class TestPrefillOutcome(unittest.TestCase):
    D = _decision("nutrition_prefill", outcome="planned",
                  assumptions={"day": YESTERDAY.isoformat()})

    def test_a_locked_day_closes_the_decision(self):
        cur = Cur(decisions=[self.D], day_status="locked_unconfirmed")
        _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "locked_unconfirmed")
        self.assertIsNone(cur.written[0]["correction"])

    def test_a_corrected_day_carries_what_changed(self):
        cur = Cur(decisions=[self.D], day_status="corrected", corrected=2)
        _run(cur)
        self.assertEqual(cur.written[0]["outcome"], "corrected")
        self.assertEqual(json.loads(cur.written[0]["correction"])["corrected_entries"], 2)

    def test_a_day_still_inside_its_window_is_not_yet(self):
        cur = Cur(decisions=[self.D], day_status="assumed")
        self.assertEqual(_run(cur)["not_yet"], 1)

    def test_a_missing_day_row_is_not_yet_rather_than_an_outcome(self):
        cur = Cur(decisions=[self.D], day_status=None)
        self.assertEqual(_run(cur)["not_yet"], 1)


# ---------------------------------------------------------------------------
# The runner's own guarantees
# ---------------------------------------------------------------------------

class TestRunner(unittest.TestCase):
    def test_a_decision_gets_at_most_one_outcome_row(self):
        """Idempotence. The list query is evaluated before this run's writes, so
        the per-row guard is what makes a second call in the same transaction
        safe — not the query."""
        cur = Cur(decisions=[CHECKIN], logs=1)
        self.assertEqual(_run(cur)["wrote"], 1)
        second = _run(cur)                       # same cursor, outcome now present
        self.assertEqual(second["wrote"], 0)
        self.assertEqual(len(cur.written), 1)

    def test_an_already_closed_decision_is_not_relisted(self):
        cur = Cur(decisions=[CHECKIN], logs=1,
                  existing_outcomes=[("checkin_adjust.outcome", "d-1")])
        # pending() filters in SQL on the real DB; here the guard catches it.
        self.assertEqual(_run(cur)["wrote"], 0)

    def test_only_actions_with_a_resolver_are_eligible(self):
        """A decision the job can never close must not be re-read every night
        forever, and must not get a fake outcome to silence it."""
        cur = Cur(decisions=[_decision("checkin_logged", assumptions={"plan_id": 1})],
                  logs=1)
        counts = _run(cur)
        self.assertEqual(counts, {"considered": 0, "wrote": 0, "not_yet": 0,
                                  "unreadable": 0, "raced": 0})
        self.assertEqual(cur.written, [])
        self.assertNotIn("checkin_logged", co.RESOLVERS)

    def test_the_sql_and_the_unpacking_cannot_drift(self):
        for c in co._COLS:
            self.assertIn(f"d.{c}", co._PENDING_SQL)

    def test_one_bad_decision_does_not_stop_the_others(self):
        bad = _decision("checkin_adjust", assumptions={}, did="bad")
        good = dict(CHECKIN, id="good")
        cur = Cur(decisions=[bad, good], logs=1)
        counts = _run(cur)
        self.assertEqual((counts["unreadable"], counts["wrote"]), (1, 1))
        self.assertEqual(json.loads(cur.written[0]["metadata"])["decides"], "good")

    def test_every_outcome_row_is_jsonb_serialisable(self):
        cur = Cur(decisions=[CHECKIN], logs=1, restores=1)
        _run(cur)
        for row in cur.written:
            json.loads(row["metadata"])
            if row["correction"] is not None:
                json.loads(row["correction"])

    def test_the_job_is_registered_and_silent(self):
        from artemis.scheduler import ArtemisScheduler
        spec = next(s for s in ArtemisScheduler.cron_specs(
            mock.MagicMock(spec=ArtemisScheduler), skip_location=True)
            if s.id == "cognition_outcomes")
        self.assertEqual((spec.hour, spec.minute), (22, 5))
        self.assertEqual(spec.func_name, "job_cognition_outcomes")


if __name__ == "__main__":
    unittest.main()
