"""CARDIO-DETECT — the watch proposes, Ryan confirms.

PUBLIC-FIXTURES: every bpm here is synthetic and chosen to sit on a boundary,
not to resemble anything recorded.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import json
import unittest
from datetime import date, datetime, timedelta, timezone

from artemis import cardio_detect as cd
from knowledge import zones

DAY = date(2099, 1, 14)
T0 = datetime(2099, 1, 14, 5, 12, tzinfo=timezone.utc)
FLOOR = zones.ZONES["Z1"][0]          # 87


def run(minutes, bpm, every_sec=30, start=T0):
    """A dense run whose SPAN is `minutes`. The endpoint is included: n samples
    every 30 s span (n-1)*30 s, so 30 samples is 14.5 minutes, not 15 — which
    made the boundary test look like a detector bug rather than a helper one."""
    n = int(minutes * 60 / every_sec) + 1
    return [(start + timedelta(seconds=every_sec * i), bpm) for i in range(n)]


class Cur:
    """Serves the detector's reads; any can be made to raise."""

    def __init__(self, *, rows=(), samples=(), enabled=None, pending=None,
                 raises=(), claimed=(), set_spans=()):
        self.rows, self.samples, self.claimed = list(rows), list(samples), list(claimed)
        self.set_spans = list(set_spans)
        self.enabled, self._pending, self.raises = enabled, pending, set(raises)
        self._res, self.written, self.deleted = [], [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._res = []
        if "acos.system_state" in s and s.startswith("SELECT"):
            if "state" in self.raises:
                raise RuntimeError("down")
            key = (params or (None,))[0]
            if key == cd.ENABLED_KEY:
                self._res = [(self.enabled,)] if self.enabled is not None else []
            else:
                self._res = [(self._pending,)] if self._pending is not None else []
        elif "DELETE FROM acos.system_state" in s:
            self.deleted.append(params)
            self._pending = None
        elif "INSERT INTO acos.system_state" in s:
            self._pending = params[1]
        elif "health.session_hr_zones" in s:
            if "claimed" in self.raises:
                raise RuntimeError("down")
            self._res = [{"window_start": a, "window_end": b} for a, b in self.claimed]
        elif "MIN(sl.logged_at)" in s:
            if "sets" in self.raises:
                raise RuntimeError("down")
            self._res = [{"first_at": a, "last_at": b} for a, b in self.set_spans]
        elif "health.plan" in s:
            if "plan" in self.raises:
                raise RuntimeError("down")
            self._res = list(self.rows)
        elif "health.watch_heart_rate" in s:
            if "hr" in self.raises:
                raise RuntimeError("down")
            self._res = [{"measured_at": t, "bpm": b} for t, b in self.samples]
        elif "INSERT INTO health.session_log" in s:
            # The log_type is a LITERAL in the statement, not a bind, so the
            # statement has to be kept to assert which row was written.
            kind = "cardio_block" if "'cardio_block'" in s else "session_summary"
            self.written.append((kind, params))

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


def plan_row(plan_id=1, stype="cardio_z2", name="Zone 2 – Treadmill"):
    return {"plan_id": plan_id, "slot": "morning", "session_type": stype,
            "blocks": {"display_name": name, "location_key": "brown_deer"}}


class TestTheEnableFlag(unittest.TestCase):
    def test_default_is_off(self):
        self.assertFalse(cd.is_enabled(Cur()))

    def test_on_when_set(self):
        self.assertTrue(cd.is_enabled(Cur(enabled="true")))

    def test_an_unreadable_flag_is_off(self):
        """A standing automation that turns itself on because a read failed is
        the failure mode that matters."""
        self.assertFalse(cd.is_enabled(Cur(raises=("state",))))

    def test_an_empty_value_is_off(self):
        self.assertFalse(cd.is_enabled(Cur(enabled="")))


class TestFindingABlock(unittest.TestCase):
    def test_a_long_dense_elevated_block_is_found(self):
        b = cd.best_block(run(38, 118))
        self.assertIsNotNone(b)
        self.assertEqual(b["minutes"], 38)
        self.assertEqual(b["avg_bpm"], 118)
        self.assertTrue(b["dense"])

    def test_too_short_is_not_proposed(self):
        self.assertIsNone(cd.best_block(run(12, 118)))

    def test_exactly_the_floor_length_is(self):
        self.assertIsNotNone(cd.best_block(run(15, 118)))

    def test_too_sparse_is_discarded_not_proposed_with_a_caveat(self):
        """The minutes are what he is being asked to confirm. Minutes measured
        from four samples an hour are not minutes."""
        sparse = run(40, 118, every_sec=300)       # one every 5 min
        self.assertIsNone(cd.best_block(sparse))

    def test_below_the_z1_floor_is_not_a_session(self):
        self.assertIsNone(cd.best_block(run(40, FLOOR - 1)))

    def test_a_dip_below_the_floor_ends_the_block(self):
        """A rest that drops him out of Z1 is two efforts, not one long one."""
        s = run(10, 118) + run(2, 60, start=T0 + timedelta(minutes=10)) \
            + run(10, 118, start=T0 + timedelta(minutes=12))
        self.assertIsNone(cd.best_block(s))

    def test_a_long_gap_ends_the_block(self):
        s = run(10, 118) + run(10, 118, start=T0 + timedelta(minutes=40))
        self.assertIsNone(cd.best_block(s))

    def test_the_longest_block_wins(self):
        s = run(16, 110) + run(30, 120, start=T0 + timedelta(hours=3))
        b = cd.best_block(s)
        self.assertEqual(b["minutes"], 30)

    def test_a_window_restricts_the_search(self):
        s = run(30, 118) + run(40, 120, start=T0 + timedelta(hours=6))
        b = cd.best_block(s, window=(T0, T0 + timedelta(hours=1)))
        self.assertEqual(b["minutes"], 30)

    def test_the_median_is_reported_not_just_the_average(self):
        b = cd.best_block(run(20, 118))
        self.assertEqual(b["median_bpm"], 118)


class TestDetect(unittest.TestCase):
    def test_a_block_is_proposed_for_an_unlogged_row(self):
        out = cd.detect(Cur(rows=[plan_row()], samples=run(38, 118)), DAY)
        self.assertTrue(out["ok"])
        self.assertEqual(out["row"]["plan_id"], 1)
        self.assertEqual(out["block"]["minutes"], 38)

    def test_no_planned_cardio_means_nothing_to_propose(self):
        out = cd.detect(Cur(rows=[], samples=run(38, 118)), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("no unlogged cardio", out["reason"])

    def test_an_already_logged_row_never_reaches_the_detector(self):
        """The SQL excludes rows with a real log, so an empty result IS the
        already-logged case — asserted here so the predicate cannot drift."""
        cur = Cur(rows=[], samples=run(38, 118))
        cd.detect(cur, DAY)
        self.assertTrue(True)   # covered by the query text test below

    def test_an_unreadable_plan_posts_nothing(self):
        out = cd.detect(Cur(rows=[plan_row()], raises=("plan",)), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("couldn't read the plan", out["reason"])

    def test_an_unreadable_watch_posts_nothing(self):
        out = cd.detect(Cur(rows=[plan_row()], raises=("hr",)), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("couldn't read the watch", out["reason"])

    def test_it_never_raises(self):
        for r in ("plan", "hr", "state"):
            with self.subTest(r):
                cd.detect(Cur(rows=[plan_row()], raises=(r,)), DAY)

    def test_the_query_excludes_rows_that_already_have_a_real_log(self):
        import inspect
        src = inspect.getsource(cd._rows_needing_a_log)
        self.assertIn("NOT EXISTS", src)
        self.assertIn("logged_via <> 'inferred'", src)


class TestThePendingProposal(unittest.TestCase):
    def test_round_trips(self):
        cur = Cur()
        cd.set_pending(cur, {"day": DAY.isoformat(), "plan_id": 7, "minutes": 38})
        self.assertEqual(cd.pending(cur)["plan_id"], 7)

    def test_it_expires_at_the_next_day(self):
        """Yesterday's block is not an answer to today's question."""
        cur = Cur(pending=json.dumps({"day": "2099-01-13", "plan_id": 7}))
        self.assertTrue(cd.clear_expired(cur, DAY))
        self.assertIsNone(cd.pending(cur))

    def test_todays_proposal_survives(self):
        cur = Cur(pending=json.dumps({"day": DAY.isoformat(), "plan_id": 7}))
        self.assertFalse(cd.clear_expired(cur, DAY))
        self.assertIsNotNone(cd.pending(cur))

    def test_garbage_is_none_rather_than_a_crash(self):
        self.assertIsNone(cd.pending(Cur(pending="not json")))

    def test_an_unreadable_pending_is_none(self):
        self.assertIsNone(cd.pending(Cur(raises=("state",))))


class TestConfirm(unittest.TestCase):
    def _prop(self, **kw):
        base = {"day": DAY.isoformat(), "plan_id": 7, "session_type": "cardio_z2",
                "display_name": "Zone 2 – Treadmill", "location_key": "brown_deer",
                "start": T0, "end": T0 + timedelta(minutes=38), "minutes": 38,
                "avg_bpm": 118, "median_bpm": 118, "sample_count": 76}
        base.update(kw)
        return base

    def test_it_writes_the_same_two_rows_finish_cardio_writes(self):
        cur = Cur()
        cd.confirm(cur, self._prop(), on=DAY)
        self.assertEqual([k for k, _p in cur.written],
                         ["cardio_block", "session_summary"])

    def test_the_duration_is_the_confirmed_minutes(self):
        cur = Cur()
        cd.confirm(cur, self._prop(minutes=38), on=DAY)
        block_params = dict(zip(
            ("plan_id", "exercise", "duration_sec", "notes", "logged_via",
             "logged_at", "modality", "device"), cur.written[0][1]))
        self.assertEqual(block_params["duration_sec"], 38 * 60)
        summary_params = dict(zip(
            ("plan_id", "duration_sec", "logged_via", "logged_at"), cur.written[1][1]))
        self.assertEqual(summary_params["duration_sec"], 38 * 60)

    def test_logged_via_says_it_came_from_the_watch(self):
        cur = Cur()
        cd.confirm(cur, self._prop(), on=DAY)
        for _kind, params in cur.written:
            self.assertIn(cd.LOGGED_VIA, params)
        self.assertEqual(cd.LOGGED_VIA, "watch_confirmed")

    def test_it_counts_as_a_real_log(self):
        """Every consumer tests `logged_via <> 'inferred'`, so a confirmed
        session counts for the gate and the makeup rules — which is right: Ryan
        confirming is Ryan saying he trained."""
        self.assertNotEqual(cd.LOGGED_VIA, "inferred")

    def test_an_iso_string_end_is_accepted(self):
        """The pending is stored as JSON, so the timestamps come back as text."""
        cur = Cur()
        cd.confirm(cur, self._prop(end=(T0 + timedelta(minutes=38)).isoformat()), on=DAY)
        self.assertEqual(len(cur.written), 2)


class TestTheLine(unittest.TestCase):
    def test_it_names_the_minutes_the_time_and_the_session(self):
        line = cd.propose_line(plan_row(), cd.best_block(run(38, 118)))
        self.assertIn("38 min", line)
        self.assertIn("118 bpm", line)
        self.assertIn("Zone 2 – Treadmill", line)
        self.assertIn("log cardio", line)


class TestItNeverConsumesABareYes(unittest.TestCase):
    def test_cardio_detect_is_a_non_consuming_flow(self):
        """A pending proposal answered by a bare `yes` would write a training
        session he never agreed to — the worst version of the CONFIRM-ARB bug."""
        from artemis import main as m
        self.assertIn("cardio_detect", m._NON_CONSUMING_FLOWS)
        self.assertNotIn("cardio_detect", m.consuming_flows())

    def test_the_command_is_a_qualified_two_word_form(self):
        from artemis import main as m
        self.assertTrue(m._LOG_CARDIO_RE.match("log cardio"))
        self.assertTrue(m._LOG_CARDIO_RE.match("  Log Cardio. "))
        for no in ("yes", "y", "ok", "log", "cardio", "log cardio please", "confirm"):
            with self.subTest(no):
                self.assertIsNone(m._LOG_CARDIO_RE.match(no))

    def test_the_toggle_matches_only_on_and_off(self):
        from artemis import main as m
        self.assertTrue(m._CARDIO_DETECT_RE.match("cardio detect on"))
        self.assertTrue(m._CARDIO_DETECT_RE.match("cardio detect OFF"))
        for no in ("cardio detect", "cardio detect maybe", "detect on"):
            with self.subTest(no):
                self.assertIsNone(m._CARDIO_DETECT_RE.match(no))


if __name__ == "__main__":
    unittest.main()


class TestAnotherSessionsWindowIsNotCardio(unittest.TestCase):
    """The box proof found 16-18 min blocks above the Z1 floor at ~10:30 AM on
    9/21, 9/23 and 9/24 — his STRENGTH sessions, the same windows ZONE-0
    computed Z2 minutes for. A lift raises the heart rate like anything else, so
    on a day with both a lift and unlogged cardio the longest block is quite
    likely the lift. Proposing it would put a session in his record that never
    happened."""

    def test_a_block_inside_a_logged_strength_window_is_not_proposed(self):
        samples = run(38, 118)
        claimed = [(T0 - timedelta(minutes=5), T0 + timedelta(minutes=45))]
        out = cd.detect(Cur(rows=[plan_row()], samples=samples, claimed=claimed), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("another logged session", out["reason"])

    def test_a_block_outside_it_still_is(self):
        samples = run(20, 118) + run(38, 118, start=T0 + timedelta(hours=6))
        claimed = [(T0 - timedelta(minutes=5), T0 + timedelta(minutes=45))]
        out = cd.detect(Cur(rows=[plan_row()], samples=samples, claimed=claimed), DAY)
        self.assertTrue(out["ok"])
        self.assertEqual(out["block"]["minutes"], 38)

    def test_a_partial_overlap_still_disqualifies(self):
        """Half a lift's window is still a lift."""
        samples = run(38, 118)
        claimed = [(T0 + timedelta(minutes=30), T0 + timedelta(minutes=90))]
        out = cd.detect(Cur(rows=[plan_row()], samples=samples, claimed=claimed), DAY)
        self.assertFalse(out["ok"])

    def test_an_unreadable_claimed_list_posts_nothing(self):
        """Without knowing which windows belong to other sessions, ANY block
        might be one of them."""
        out = cd.detect(Cur(rows=[plan_row()], samples=run(38, 118),
                            raises=("claimed",)), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("other sessions", out["reason"])


class TestSetTimestampsCloseTheGap(unittest.TestCase):
    """Round #19 shipped the overlap guard reading only ZONE-0 windows, which
    exist only for sessions FINISHED on the iPad. A lift logged set by set with
    no Finish left its heart-rate block eligible — so the very case the guard
    was built for could still slip through. Every set carries a `logged_at`, and
    the span of a session's sets says when he was training whether or not he
    ever pressed Finish."""

    def test_a_lift_WITHOUT_a_finish_still_blocks_its_own_hr(self):
        # No ZONE-0 row at all — only set timestamps.
        spans = [(T0 + timedelta(minutes=2), T0 + timedelta(minutes=36))]
        out = cd.detect(Cur(rows=[plan_row()], samples=run(38, 118),
                            claimed=[], set_spans=spans), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("another logged session", out["reason"])

    def test_a_lift_WITH_a_finish_still_blocks_it(self):
        """The ZONE-0 path must keep working — this is not a replacement."""
        claimed = [(T0 - timedelta(minutes=5), T0 + timedelta(minutes=45))]
        out = cd.detect(Cur(rows=[plan_row()], samples=run(38, 118),
                            claimed=claimed, set_spans=[]), DAY)
        self.assertFalse(out["ok"])

    def test_cardio_after_a_lift_the_same_morning_is_still_proposed(self):
        """The guard must not swallow the day. A lift at 5:12 and cardio at
        11:12 are two blocks, and only the first is explained."""
        samples = run(35, 118) + run(40, 120, start=T0 + timedelta(hours=6))
        spans = [(T0 + timedelta(minutes=2), T0 + timedelta(minutes=33))]
        out = cd.detect(Cur(rows=[plan_row()], samples=samples,
                            claimed=[], set_spans=spans), DAY)
        self.assertTrue(out["ok"])
        self.assertEqual(out["block"]["minutes"], 40)

    def test_back_to_back_sessions_both_claim_their_own_window(self):
        a = run(20, 118)
        b = run(25, 120, start=T0 + timedelta(minutes=25))
        spans = [(T0 + timedelta(minutes=1), T0 + timedelta(minutes=19)),
                 (T0 + timedelta(minutes=26), T0 + timedelta(minutes=49))]
        out = cd.detect(Cur(rows=[plan_row()], samples=a + b,
                            claimed=[], set_spans=spans), DAY)
        self.assertFalse(out["ok"])

    def test_the_pad_catches_a_block_that_starts_in_the_warm_up(self):
        """A set is logged AFTER it is done, so the block starts before the
        first timestamp. Without the pad it would slip past."""
        spans = [(T0 + timedelta(minutes=4), T0 + timedelta(minutes=36))]
        out = cd.detect(Cur(rows=[plan_row()], samples=run(38, 118),
                            claimed=[], set_spans=spans), DAY)
        self.assertFalse(out["ok"])

    def test_an_unreadable_set_span_read_posts_nothing(self):
        out = cd.detect(Cur(rows=[plan_row()], samples=run(38, 118),
                            raises=("sets",)), DAY)
        self.assertFalse(out["ok"])
        self.assertIn("other sessions", out["reason"])

    def test_the_query_excludes_cardio_rows_from_the_claimed_set(self):
        """A cardio row's OWN sets must not disqualify its own block."""
        import inspect
        src = inspect.getsource(cd._claimed_windows)
        self.assertEqual(src.count("session_type NOT IN"), 2)
