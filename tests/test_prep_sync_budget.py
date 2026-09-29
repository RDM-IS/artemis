"""PREP sync hardening — a 404 is terminal, the run is time-bounded, one at a time.

Round #25 left a sync that had appeared to run for about ten minutes. Measured on
the box (2026-09-29): a healthy full sync is **6.9 s** and a sync with three
inaccessible databases is **1.8 s**, each 404 costing 0.15 s. So the ten minutes
was not in this code — but the 00:25 job is UNATTENDED, and an unattended job that
can hang without recording why is the actual hazard. These are the guards.

PUBLIC-FIXTURES: no real Notion ids, no logged data.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD

import unittest

from artemis import prep_notion as pn
from artemis.notion_meal_plan import NotionAccessDenied, NotionUnavailable


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class TestAccessDeniedIsTerminal(unittest.TestCase):
    def test_a_404_is_its_own_exception_and_still_a_NotionUnavailable(self):
        # A subclass, so every existing `except NotionUnavailable` keeps working
        # and nothing downstream had to change.
        self.assertTrue(issubclass(NotionAccessDenied, NotionUnavailable))

    def test_404_and_403_raise_access_denied_rather_than_the_generic_error(self):
        from artemis import notion_meal_plan as nmp

        class Resp:
            def __init__(self, code):
                self.status_code = code
                self.text = "{}"

        import types
        for code in (403, 404):
            fake = types.SimpleNamespace(
                post=lambda *a, **k: Resp(code),
                RequestException=Exception)
            real = __import__("sys").modules.get("requests")
            __import__("sys").modules["requests"] = fake
            try:
                with self.assertRaises(NotionAccessDenied):
                    nmp._post("/databases/x/query", "tok", {})
            finally:
                if real is not None:
                    __import__("sys").modules["requests"] = real
                else:
                    del __import__("sys").modules["requests"]

    def test_a_500_is_NOT_access_denied_because_it_may_fix_itself(self):
        from artemis import notion_meal_plan as nmp
        import sys, types

        class Resp:
            status_code = 500
            text = "boom"

        real = sys.modules.get("requests")
        sys.modules["requests"] = types.SimpleNamespace(
            post=lambda *a, **k: Resp(), RequestException=Exception)
        try:
            with self.assertRaises(NotionUnavailable) as cm:
                nmp._post("/databases/x/query", "tok", {})
            self.assertNotIsInstance(cm.exception, NotionAccessDenied)
        finally:
            if real is not None:
                sys.modules["requests"] = real
            else:
                del sys.modules["requests"]

    def test_the_message_says_retrying_will_not_help(self):
        # The point of the distinction: a human reading the sync row should know
        # to go and click something, not to wait.
        from artemis import notion_meal_plan as nmp
        import sys, types

        class Resp:
            status_code = 404
            text = "{}"

        real = sys.modules.get("requests")
        sys.modules["requests"] = types.SimpleNamespace(
            post=lambda *a, **k: Resp(), RequestException=Exception)
        try:
            with self.assertRaises(NotionAccessDenied) as cm:
                nmp._post("/databases/x/query", "tok", {})
            self.assertIn("Share the page", str(cm.exception))
            self.assertIn("retrying will not help", str(cm.exception))
        finally:
            if real is not None:
                sys.modules["requests"] = real
            else:
                del sys.modules["requests"]


class TestDeadline(unittest.TestCase):
    def test_it_starts_full_and_drains(self):
        clock = FakeClock()
        d = pn.SyncDeadline(90.0, clock=clock)
        self.assertEqual(d.remaining(), 90.0)
        self.assertFalse(d.expired())
        clock.advance(89.0)
        self.assertAlmostEqual(d.remaining(), 1.0)
        self.assertFalse(d.expired())
        clock.advance(1.0)
        self.assertTrue(d.expired())
        self.assertEqual(d.remaining(), 0.0)

    def test_a_request_never_gets_a_zero_timeout(self):
        # A zero timeout fails instantly in a way that reads like a network error
        # rather than like a budget expiring.
        clock = FakeClock()
        d = pn.SyncDeadline(10.0, clock=clock)
        clock.advance(100.0)
        self.assertGreaterEqual(d.request_timeout(), 1.0)

    def test_a_request_is_capped_by_what_is_left(self):
        clock = FakeClock()
        d = pn.SyncDeadline(90.0, clock=clock)
        clock.advance(85.0)
        self.assertAlmostEqual(d.request_timeout(cap=15.0), 5.0)

    def test_the_cap_still_applies_when_plenty_is_left(self):
        d = pn.SyncDeadline(90.0, clock=FakeClock())
        self.assertEqual(d.request_timeout(cap=15.0), 15.0)

    def test_pagination_stops_when_the_budget_expires(self):
        """Between pages is the safe place to stop: everything yielded has been
        stored and the watermark has not moved past it."""
        clock = FakeClock()
        d = pn.SyncDeadline(10.0, clock=clock)
        clock.advance(11.0)
        report = pn.SyncReport(db_key="x")
        gen = pn.query_pages("db", "tok", since=None, bucket=pn.TokenBucket(),
                             report=report, deadline=d)
        with self.assertRaises(pn.SyncBudgetExpired):
            next(gen)
        # and it did NOT spend a request doing so
        self.assertEqual(report.requests, 0)

    def test_the_default_budget_is_far_above_a_healthy_run(self):
        # Measured on the box 2026-09-29: healthy 6.9 s, three-404 1.8 s. A budget
        # that can fire on a normal run is a budget that gets raised until it is
        # meaningless.
        self.assertGreaterEqual(pn.DEFAULT_BUDGET_SEC, 60.0)


class TestFailureKindsAreRecordedDistinctly(unittest.TestCase):
    """no_access / deadline / unavailable need different actions, so they are
    different statuses rather than one 'it did not work'."""

    def _run(self, exc):
        calls = []

        class Cur:
            def execute(self, sql, params=None):
                calls.append((sql, params))

            def fetchone(self):
                return None

            def fetchall(self):
                return []

            rowcount = 0
            description = None

        def boom(*a, **k):
            raise exc

        real_one, real_link, real_seed = pn.sync_one, pn.link_food, pn.seed_on_hand
        real_token = pn._token
        pn.sync_one = boom
        pn.link_food = lambda cur: {}
        pn.seed_on_hand = lambda cur: 0
        pn._token = lambda: "tok"
        try:
            return pn.sync_all(Cur()), calls
        finally:
            pn.sync_one, pn.link_food, pn.seed_on_hand = real_one, real_link, real_seed
            pn._token = real_token

    def _statuses(self, calls):
        return [p[3] for sql, p in calls
                if p and "prep_sync" in sql and len(p) > 3]

    def test_access_denied_records_no_access(self):
        _, calls = self._run(NotionAccessDenied("nope"))
        self.assertTrue(all(s == "no_access" for s in self._statuses(calls)),
                        self._statuses(calls))

    def test_the_budget_records_deadline(self):
        _, calls = self._run(pn.SyncBudgetExpired("out of time"))
        self.assertTrue(all(s == "deadline" for s in self._statuses(calls)),
                        self._statuses(calls))

    def test_anything_else_records_unavailable(self):
        _, calls = self._run(NotionUnavailable("timeout"))
        self.assertTrue(all(s == "unavailable" for s in self._statuses(calls)),
                        self._statuses(calls))

    def test_a_failure_never_advances_the_watermark_or_runs_the_sweep(self):
        # The fail-open shape this whole subsystem is built to avoid: a sweep off
        # the back of an outage marks every row deleted.
        _, calls = self._run(NotionAccessDenied("nope"))
        for sql, p in calls:
            if p and "prep_sync" in sql:
                self.assertIsNone(p[1], "last_edited must not move")
                self.assertFalse(p[2], "the sweep must not run")

    def test_the_report_carries_the_elapsed_time_and_the_budget(self):
        rep, _ = self._run(NotionAccessDenied("nope"))
        self.assertIn("elapsed_sec", rep)
        self.assertIn("budget_sec", rep)
        self.assertEqual(sorted(rep["failed"]), sorted(pn.ORDER))


class TestOneRunAtATime(unittest.TestCase):
    def test_refresh_does_nothing_when_another_run_holds_the_lock(self):
        from artemis import prep

        class Cur:
            def execute(self, sql, params=None):
                self.sql = sql

            def fetchone(self):
                return (False,)          # pg_try_advisory_lock said no

        out = prep.refresh(Cur(), __import__("datetime").date(2031, 3, 2))
        self.assertIn("skipped", out)
        self.assertNotIn("synced", out)

    def test_the_lock_id_is_fixed(self):
        # Advisory locks share one namespace per database, so this must not drift.
        from artemis import prep
        self.assertIsInstance(prep.REFRESH_LOCK_ID, int)
        self.assertEqual(prep.REFRESH_LOCK_ID, 2026_0929)

    def test_try_lock_reports_what_postgres_said(self):
        from artemis import prep

        class Cur:
            def __init__(self, answer):
                self.answer = answer

            def execute(self, sql, params=None):
                assert "pg_try_advisory_lock" in sql

            def fetchone(self):
                return (self.answer,)

        self.assertTrue(prep.try_lock(Cur(True)))
        self.assertFalse(prep.try_lock(Cur(False)))


if __name__ == "__main__":
    unittest.main()
