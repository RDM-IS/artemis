"""Tests for the ramp slide/tier engine (feat/health-ramp).

Two layers:
  * Pure-logic tests (no DB): schedule generation, week windows, makeup slots,
    week classification, proposal build (travel pinning + diff), validation.
  * Reconcile/evaluate integration via an in-memory FakeCursor that models exactly
    the SQL the engine issues — so slide/complete/evaluate/propose/confirm run
    locally without RDS. (Real CT/tz and DB behavior are re-verified on the box.)

Run per-file (unittest), never via discover:
    python3 -m unittest tests.test_health_ramp -v
"""

import contextlib
import json
import unittest
from datetime import date, timedelta
from unittest import mock

import artemis.health_ramp as hr


# ===========================================================================
# In-memory fake DB — recognizes the exact statements the engine issues.
# ===========================================================================

class FakePlanDB:
    """Models health.plan, health.session_log, and health.ramp_state."""

    def __init__(self):
        self.plan: dict[int, dict] = {}          # plan_id -> row
        self.logs: list[dict] = []               # {plan_id, logged_via, ct_date}
        self._next_id = 1
        self.ramp_state = {
            "consecutive_success_count": 0,
            "consecutive_nonsuccess_count": 0,
            "last_evaluated_end_date": None,
            "pending_proposal_id": None,
            "pending_proposal": None,
            "revisit_prompted": False,
            "ramp_complete": False,
        }

    def add_plan(self, plan_date, week_num, session_type="cardio_z2",
                 status="planned", display=None, original_date=None) -> int:
        pid = self._next_id
        self._next_id += 1
        self.plan[pid] = {
            "plan_id": pid, "plan_date": plan_date, "week_num": week_num,
            "session_type": session_type, "status": status,
            "original_date": original_date,
            "blocks": {"type": "steady", "display_name": display or session_type},
        }
        return pid

    def seed_week(self, specs):
        """specs: list of (plan_date, week_num, session_type, display)."""
        return [self.add_plan(*s[:2], session_type=s[2], display=s[3]) for s in specs]

    def add_log(self, plan_id, ct_date, logged_via="mattermost"):
        self.logs.append({"plan_id": plan_id, "logged_via": logged_via, "ct_date": ct_date})


class FakeCursor:
    def __init__(self, db: FakePlanDB):
        self.db = db
        self._rows: list = []
        self.description = None

    # Real psycopg2 cursors are context managers (`with conn.cursor() as cur`).
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _norm(sql):
        return " ".join(sql.split())

    def execute(self, sql, params=None):
        s = self._norm(sql)
        p = params or ()
        self.description = None
        self._rows = []

        if s.startswith("SELECT plan_id, plan_date, week_num, session_type, status, original_date, blocks FROM health.plan WHERE plan_date >= %s"):
            self.description = [(c,) for c in ("plan_id", "plan_date", "week_num",
                                               "session_type", "status", "original_date", "blocks")]
            rows = [r for r in self.db.plan.values() if r["plan_date"] >= p[0]]
            rows.sort(key=lambda r: r["plan_date"])
            self._rows = [(r["plan_id"], r["plan_date"], r["week_num"], r["session_type"],
                           r["status"], r["original_date"], json.dumps(r["blocks"])) for r in rows]
            return

        if s.startswith("SELECT plan_date, week_num, blocks FROM health.plan WHERE plan_date >= %s"):
            rows = [r for r in self.db.plan.values() if r["plan_date"] >= p[0]]
            rows.sort(key=lambda r: r["plan_date"])
            self._rows = [(r["plan_date"], r["week_num"], json.dumps(r["blocks"])) for r in rows]
            return

        if s.startswith("SELECT 1 FROM health.session_log"):
            plan_id, plan_date = p
            hit = any(l["plan_id"] == plan_id and l["logged_via"] != "inferred"
                      and l["ct_date"] >= plan_date for l in self.db.logs)
            self._rows = [(1,)] if hit else []
            return

        if s.startswith("SELECT count(*) FROM health.plan WHERE plan_date >= %s"):
            self._rows = [(sum(1 for r in self.db.plan.values() if r["plan_date"] >= p[0]),)]
            return

        if s.startswith("SELECT consecutive_success_count"):
            self.description = [(c,) for c in ("consecutive_success_count",
                                               "consecutive_nonsuccess_count", "last_evaluated_end_date",
                                               "pending_proposal_id", "revisit_prompted", "ramp_complete")]
            st = self.db.ramp_state
            self._rows = [(st["consecutive_success_count"], st["consecutive_nonsuccess_count"],
                           st["last_evaluated_end_date"], st["pending_proposal_id"],
                           st["revisit_prompted"], st["ramp_complete"])]
            return

        if s.startswith("SELECT pending_proposal FROM health.ramp_state"):
            self.description = [("pending_proposal",)]
            self._rows = [(self.db.ramp_state["pending_proposal"],)]
            return

        if s.startswith("UPDATE health.plan SET status = %s, plan_date = %s"):
            status, new_date, original_date, plan_id = p
            row = self.db.plan[plan_id]
            row["status"] = status
            row["plan_date"] = new_date
            if row["original_date"] is None:
                row["original_date"] = original_date
            return

        if s.startswith("UPDATE health.plan SET status = %s WHERE plan_id = %s"):
            status, plan_id = p
            self.db.plan[plan_id]["status"] = status
            return

        if s.startswith("DELETE FROM health.plan WHERE plan_date >= %s"):
            cutoff = p[0]
            for pid in [pid for pid, r in self.db.plan.items() if r["plan_date"] >= cutoff]:
                del self.db.plan[pid]
            return

        if s.startswith("INSERT INTO health.plan"):
            (plan_date, phase, week_num, session_type, blocks_json, target_rpe,
             hr_zone, est, gen_by, notes) = p
            self.db.add_plan(plan_date, week_num, session_type=session_type,
                             display=json.loads(blocks_json).get("display_name"))
            return

        if s.startswith("UPDATE health.ramp_state SET consecutive_success_count = %s"):
            sc, nsc, end, pending_id, pending_json, revisit_flag, ramp_complete = p
            self.db.ramp_state.update({
                "consecutive_success_count": sc, "consecutive_nonsuccess_count": nsc,
                "last_evaluated_end_date": end, "pending_proposal_id": pending_id,
                "pending_proposal": pending_json, "revisit_prompted": revisit_flag,
                "ramp_complete": ramp_complete})
            return

        if s.startswith("UPDATE health.ramp_state SET pending_proposal_id = NULL"):
            # Covers commit (restart/repeat) and cancel clears.
            self.db.ramp_state["pending_proposal_id"] = None
            self.db.ramp_state["pending_proposal"] = None
            if "consecutive_success_count = 0" in s:
                self.db.ramp_state["consecutive_success_count"] = 0
                self.db.ramp_state["consecutive_nonsuccess_count"] = 0
            if "revisit_prompted = false" in s:
                self.db.ramp_state["revisit_prompted"] = False
            return

        if s.startswith("INSERT INTO health.ramp_state"):
            return  # singleton already present

        raise AssertionError(f"FakeCursor: unhandled SQL: {s[:120]}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, *a, **k):
        return FakeCursor(self.db)


def fake_get_connection(db):
    @contextlib.contextmanager
    def _cm():
        conn = FakeConn(db)
        try:
            yield conn
        except Exception:
            raise  # mirrors the real pool CM (rollback then re-raise)
    return _cm


class FakeMM:
    def __init__(self):
        self.posts = []

    def get_channel_id(self, name):
        return "chan-artemis-ryan"

    def post_message(self, channel, text):
        self.posts.append((channel, text))


@contextlib.contextmanager
def engine_env(db):
    """Patch the engine's DB + audit surface for a run_nightly test. The pending
    proposal is persisted atomically into db.ramp_state['pending_proposal'] by the
    FakeCursor (no separate store), so tests assert against ramp_state directly."""
    with mock.patch("knowledge.db.get_connection", fake_get_connection(db)), \
         mock.patch.object(hr, "_audit", lambda *a, **k: "audit-id"):
        yield db.ramp_state


# ===========================================================================
# Pure-logic tests
# ===========================================================================

class TestRampSchedule(unittest.TestCase):
    def test_initial_is_35_rows_weeks_1_7_hard_stop(self):
        rows = hr.build_rows(hr.build_initial_schedule())
        self.assertEqual(len(rows), 35)
        self.assertEqual({r["week_num"] for r in rows}, set(range(1, 8)))
        self.assertEqual(min(r["plan_date"] for r in rows), hr.RAMP_START)
        self.assertEqual(max(r["plan_date"] for r in rows), hr.RAMP_END)
        # No row past the hard stop.
        self.assertTrue(all(r["plan_date"] <= hr.RAMP_END for r in rows))

    def test_week1_sat_for_sun_swap(self):
        rows = {r["plan_date"]: r for r in hr.build_rows(hr.build_initial_schedule())}
        self.assertIn(date(2026, 7, 25), rows)                 # Sat long Z2
        self.assertNotIn(date(2026, 7, 26), rows)              # no Sunday session
        self.assertEqual(rows[date(2026, 7, 25)]["blocks"]["display_name"], "Long Z2")

    def test_travel_weeks_pinned_to_calendar_dates(self):
        rows = {r["plan_date"]: r for r in hr.build_rows(hr.build_initial_schedule())}
        for d in (date(2026, 8, 18), date(2026, 8, 21), date(2026, 9, 8), date(2026, 9, 11)):
            self.assertEqual(rows[d]["session_type"], "strength_c",
                             f"{d} should be a travel circuit (strength_c)")
        for d in (date(2026, 8, 20), date(2026, 9, 10)):
            self.assertEqual(rows[d]["blocks"]["display_name"], "Travel Z2 Walk")
        # A non-travel week keeps the base Tue (Z2 Bike).
        self.assertEqual(rows[date(2026, 8, 4)]["blocks"]["display_name"], "Z2 Bike")

    def test_session_types_are_check_legal(self):
        legal = {"strength_a", "strength_b", "strength_c",
                 "cardio_intervals", "cardio_z2", "walk", "rest_mobility"}
        for r in hr.build_rows(hr.build_initial_schedule()):
            self.assertIn(r["session_type"], legal)
            self.assertEqual(r["generated_by"], "manual")
            self.assertEqual(r["phase"], 1)

    def test_validate_rejects_illegal(self):
        rows = hr.build_rows(hr.build_initial_schedule())
        rows[0]["session_type"] = "not_a_type"
        with self.assertRaises(AssertionError):
            hr.validate_ramp_rows(rows)


class TestWindows(unittest.TestCase):
    def test_windows_match_spec(self):
        dates = [r["plan_date"] for r in hr.build_rows(hr.build_initial_schedule())]
        windows = hr.compute_windows(dates)
        w = {a: win for a, win in windows.items()}
        self.assertEqual(w[date(2026, 7, 26)], (date(2026, 7, 25), date(2026, 8, 1)))   # wk1
        self.assertEqual(w[date(2026, 8, 2)], (date(2026, 8, 2), date(2026, 8, 8)))     # wk2
        self.assertEqual(w[date(2026, 9, 6)], (date(2026, 9, 6), date(2026, 9, 12)))    # wk7

    def test_makeup_slots_are_wed_and_trailing_sat(self):
        # week 1 window 7/25–8/1: makeup slots Wed 7/29 and trailing Sat 8/1.
        slots = hr._makeup_slots((date(2026, 7, 25), date(2026, 8, 1)))
        self.assertEqual(slots, [date(2026, 7, 29), date(2026, 8, 1)])


class TestClassifyWeek(unittest.TestCase):
    def test_five_of_five_success(self):
        self.assertEqual(hr.classify_week(5, prev_nonsuccess=0), "success")

    def test_four_of_five_repeat(self):
        self.assertEqual(hr.classify_week(4, prev_nonsuccess=0), "repeat")

    def test_four_of_five_after_nonsuccess_restart(self):
        # two consecutive non-successful weeks → restart even at 4/5.
        self.assertEqual(hr.classify_week(4, prev_nonsuccess=1), "restart")

    def test_three_or_fewer_restart(self):
        self.assertEqual(hr.classify_week(3, prev_nonsuccess=0), "restart")
        self.assertEqual(hr.classify_week(0, prev_nonsuccess=0), "restart")

    def test_regen_sequences(self):
        self.assertEqual(hr._regen_sequence("repeat", 3), [3, 4, 5, 6, 7])
        self.assertEqual(hr._regen_sequence("restart", 3), [1, 2, 3, 4, 5, 6, 7])


class TestBuildProposal(unittest.TestCase):
    def test_repeat_proposal_pins_travel_and_diffs(self):
        # Week 3 closed (window end 8/8) at 4/5 → repeat. Regeneration starts the
        # following Sunday (8/9). Travel must stay pinned to 8/18-21 & 9/8-11.
        current = hr.build_rows(hr.build_initial_schedule())
        prop = hr.build_proposal("repeat", week_num=3, closed_end=date(2026, 8, 8),
                                 current_future_rows=current)
        self.assertEqual(prop["start_sunday"], "2026-08-09")
        new = {date.fromisoformat(r["plan_date"]): r for r in prop["rows"]}
        # Travel windows remain travel content regardless of the shift.
        self.assertEqual(new[date(2026, 8, 18)]["session_type"], "strength_c")
        self.assertEqual(new[date(2026, 8, 21)]["session_type"], "strength_c")
        self.assertEqual(new[date(2026, 9, 8)]["session_type"], "strength_c")
        self.assertEqual(new[date(2026, 9, 11)]["blocks"]["display_name"], "Travel Circuit B")
        # First regenerated week replays program week 3 on 8/9.
        self.assertEqual(new[date(2026, 8, 9)]["week_num"], 3)
        self.assertIn("repeat this week", prop["text"])

    def test_restart_regenerates_week_one(self):
        current = hr.build_rows(hr.build_initial_schedule())
        prop = hr.build_proposal("restart", week_num=5, closed_end=date(2026, 8, 22),
                                 current_future_rows=current)
        new = {date.fromisoformat(r["plan_date"]): r for r in prop["rows"]}
        first_sunday = date(2026, 8, 23)
        self.assertEqual(new[first_sunday]["week_num"], 1)
        self.assertIn("restart at week 1", prop["text"])
        # 9/6 anchor still gets travel overlay (date-pinned) after a restart.
        self.assertEqual(new[date(2026, 9, 8)]["session_type"], "strength_c")


# ===========================================================================
# Reconcile / evaluate integration (FakeCursor)
# ===========================================================================

def _standard_week(db, week_num=1, sunday=date(2026, 8, 2)):
    """Seed one normal week (Sun/Mon/Tue/Thu/Fri) and return {role: plan_id}."""
    ids = {}
    ids["SUN"] = db.add_plan(sunday, week_num, "cardio_z2", display="Long Z2")
    ids["MON"] = db.add_plan(sunday + timedelta(days=1), week_num, "strength_a", display="Strength A")
    ids["TUE"] = db.add_plan(sunday + timedelta(days=2), week_num, "cardio_z2", display="Z2 Bike")
    ids["THU"] = db.add_plan(sunday + timedelta(days=4), week_num, "strength_b", display="Strength B")
    ids["FRI"] = db.add_plan(sunday + timedelta(days=5), week_num, "cardio_intervals", display="Intervals")
    return ids


def _complete(db, ids, roles):
    """Add a real (mattermost) session_log for each role so mark_completions
    credits it — used to isolate which session is left open to slide."""
    for role in roles:
        db.add_log(ids[role], ct_date=db.plan[ids[role]]["plan_date"])


class TestSlides(unittest.TestCase):
    def test_missed_monday_slides_to_wednesday(self):
        db = FakePlanDB()
        ids = _standard_week(db)                    # week Sun 8/2 .. Sat 8/8
        _complete(db, ids, ["SUN"])                 # Sun done; Mon left open
        # Run the nightly on Tue 8/4 (Mon 8/3 passed, uncompleted).
        with engine_env(db):
            summary = hr.run_nightly(mm=None, today=date(2026, 8, 4))
        mon = db.plan[ids["MON"]]
        self.assertEqual(mon["status"], "slid")
        self.assertEqual(mon["plan_date"], date(2026, 8, 5))       # Wed makeup slot
        self.assertEqual(mon["original_date"], date(2026, 8, 3))
        self.assertEqual(len(summary["slides"]), 1)
        self.assertEqual(summary["slides"][0]["action"], "slid")

    def test_missed_friday_slides_to_saturday(self):
        db = FakePlanDB()
        ids = _standard_week(db)
        _complete(db, ids, ["SUN", "MON", "TUE", "THU"])           # only Fri open
        # Run on Sat 8/8 (Fri 8/7 passed). Only the Sat makeup slot remains.
        with engine_env(db):
            hr.run_nightly(mm=None, today=date(2026, 8, 8))
        fri = db.plan[ids["FRI"]]
        self.assertEqual(fri["status"], "slid")
        self.assertEqual(fri["plan_date"], date(2026, 8, 8))       # trailing Sat

    def test_unslidable_becomes_missed(self):
        db = FakePlanDB()
        ids = _standard_week(db)
        _complete(db, ids, ["SUN", "MON", "TUE", "THU"])
        # Fri already slid to Sat 8/8, still not done; reconciled on Sun 8/9 (window
        # closed) → no makeup slot after Sat remains → missed.
        db.plan[ids["FRI"]]["plan_date"] = date(2026, 8, 8)
        db.plan[ids["FRI"]]["status"] = "slid"
        db.plan[ids["FRI"]]["original_date"] = date(2026, 8, 7)
        with engine_env(db):
            hr.run_nightly(mm=None, today=date(2026, 8, 9))
        self.assertEqual(db.plan[ids["FRI"]]["status"], "missed")

    def test_completed_after_slide_counts(self):
        db = FakePlanDB()
        ids = _standard_week(db)
        _complete(db, ids, ["SUN", "TUE"])                         # isolate Mon
        # Monday slid to Wed 8/5, then a real log lands tied to the Monday plan_id.
        db.plan[ids["MON"]]["plan_date"] = date(2026, 8, 5)
        db.plan[ids["MON"]]["status"] = "slid"
        db.plan[ids["MON"]]["original_date"] = date(2026, 8, 3)
        db.add_log(ids["MON"], ct_date=date(2026, 8, 5))
        with engine_env(db):
            hr.run_nightly(mm=None, today=date(2026, 8, 6))        # reconcile Wed
        self.assertEqual(db.plan[ids["MON"]]["status"], "completed")

    def test_skipped_run_still_slides_past_due(self):
        # The 00:15 job is skipped Tue; it runs Wed 8/5 with Mon 8/3 AND Tue 8/4
        # both past-due and open. Neither is forfeited: they fill the makeup slots
        # in order — Mon→Wed 8/5, Tue→Sat 8/8.
        db = FakePlanDB()
        ids = _standard_week(db)
        _complete(db, ids, ["SUN"])
        with engine_env(db):
            hr.run_nightly(mm=None, today=date(2026, 8, 5))
        self.assertEqual(db.plan[ids["MON"]]["plan_date"], date(2026, 8, 5))
        self.assertEqual(db.plan[ids["TUE"]]["plan_date"], date(2026, 8, 8))
        self.assertEqual(db.plan[ids["MON"]]["status"], "slid")
        self.assertEqual(db.plan[ids["TUE"]]["status"], "slid")

    def test_ct_boundary_late_log_counts_for_plan_date(self):
        # A session logged 10pm CT (= 3am UTC next day) resolves to the CT plan
        # date; the FakeCursor models the SQL's AT TIME ZONE result (ct_date). The
        # >= comparison must still count it as completed for that plan date.
        db = FakePlanDB()
        pid = db.add_plan(date(2026, 8, 3), 1, "strength_a", display="Strength A")
        db.add_log(pid, ct_date=date(2026, 8, 3))                  # 10pm CT on 8/3
        with engine_env(db):
            hr.run_nightly(mm=None, today=date(2026, 8, 4))
        self.assertEqual(db.plan[pid]["status"], "completed")

    def test_inferred_log_does_not_count_as_completion(self):
        db = FakePlanDB()
        ids = _standard_week(db)
        _complete(db, ids, ["SUN"])                                # isolate Mon
        db.add_log(ids["MON"], ct_date=date(2026, 8, 3), logged_via="inferred")
        with engine_env(db):
            hr.run_nightly(mm=None, today=date(2026, 8, 4))
        # Not completed by an inferred placeholder → it slides instead.
        self.assertEqual(db.plan[ids["MON"]]["status"], "slid")


class TestEvaluation(unittest.TestCase):
    def _seed_week_with_completions(self, db, completed_roles, week_num=1,
                                    sunday=date(2026, 8, 2)):
        ids = _standard_week(db, week_num=week_num, sunday=sunday)
        for role in completed_roles:
            db.plan[ids[role]]["status"] = "completed"
            db.add_log(ids[role], ct_date=db.plan[ids[role]]["plan_date"])
        return ids

    def test_five_of_five_success_no_proposal(self):
        db = FakePlanDB()
        self._seed_week_with_completions(db, ["SUN", "MON", "TUE", "THU", "FRI"])
        with engine_env(db):
            summary = hr.run_nightly(mm=FakeMM(), today=date(2026, 8, 9))   # after window close
        self.assertEqual(summary["outcome"], "success")
        self.assertIsNone(summary["proposal"])
        self.assertIsNone(db.ramp_state["pending_proposal"])   # nothing staged
        self.assertIsNone(db.ramp_state["pending_proposal_id"])
        self.assertEqual(db.ramp_state["consecutive_success_count"], 1)

    def test_four_of_five_proposes_repeat_pinned(self):
        db = FakePlanDB()
        self._seed_week_with_completions(db, ["SUN", "MON", "TUE", "THU"])  # Fri missed
        mm = FakeMM()
        with engine_env(db):
            summary = hr.run_nightly(mm=mm, today=date(2026, 8, 9))
        self.assertEqual(summary["outcome"], "repeat")
        self.assertIsNotNone(summary["proposal"])
        self.assertIn("repeat this week", summary["proposal"])
        # Pending proposal was staged atomically in ramp_state (flag + payload).
        self.assertTrue(db.ramp_state["pending_proposal_id"])
        self.assertIsNotNone(db.ramp_state["pending_proposal"])
        payload = json.loads(db.ramp_state["pending_proposal"])
        self.assertEqual(payload["channel_id"], "chan-artemis-ryan")   # channel-scoped
        # Travel stays pinned in the staged rows.
        new = {date.fromisoformat(r["plan_date"]): r for r in payload["rows"]}
        self.assertEqual(new[date(2026, 8, 18)]["session_type"], "strength_c")

    def test_three_of_five_proposes_restart(self):
        db = FakePlanDB()
        self._seed_week_with_completions(db, ["SUN", "MON", "TUE"])       # 3/5
        with engine_env(db):
            summary = hr.run_nightly(mm=FakeMM(), today=date(2026, 8, 9))
        self.assertEqual(summary["outcome"], "restart")
        self.assertIn("restart at week 1", summary["proposal"])

    def test_two_consecutive_nonsuccess_restart(self):
        # Prior week already non-successful (counter=1). This week 4/5 → restart.
        db = FakePlanDB()
        db.ramp_state["consecutive_nonsuccess_count"] = 1
        self._seed_week_with_completions(db, ["SUN", "MON", "TUE", "THU"])  # 4/5
        with engine_env(db):
            summary = hr.run_nightly(mm=FakeMM(), today=date(2026, 8, 9))
        self.assertEqual(summary["outcome"], "restart")

    def test_week2_posts_ramp_revisit_prompt(self):
        db = FakePlanDB()
        # Week 2 window 8/9–8/15; seed 5/5 and evaluate after close.
        self._seed_week_with_completions(
            db, ["SUN", "MON", "TUE", "THU", "FRI"], week_num=2, sunday=date(2026, 8, 9))
        with engine_env(db):
            summary = hr.run_nightly(mm=FakeMM(), today=date(2026, 8, 16))
        self.assertTrue(any("Ramp revisit" in n for n in summary["notices"]))

    def test_revisit_not_refired_when_already_prompted(self):
        # Finding 4: after a restart the program renumbers to week 2 again, but the
        # one-time revisit must not re-fire once revisit_prompted is set.
        db = FakePlanDB()
        db.ramp_state["revisit_prompted"] = True
        self._seed_week_with_completions(
            db, ["SUN", "MON", "TUE", "THU", "FRI"], week_num=2, sunday=date(2026, 8, 9))
        with engine_env(db):
            summary = hr.run_nightly(mm=FakeMM(), today=date(2026, 8, 16))
        self.assertFalse(any("Ramp revisit" in n for n in summary["notices"]))

    def test_open_proposal_holds_further_evaluation(self):
        db = FakePlanDB()
        db.ramp_state["pending_proposal_id"] = "ramp_repeat_wk1_2026-08-02"
        self._seed_week_with_completions(db, ["SUN", "MON"])            # would be restart
        with engine_env(db):
            summary = hr.run_nightly(mm=FakeMM(), today=date(2026, 8, 9))
        self.assertIsNone(summary["evaluated"])                        # held


class TestConfirmPath(unittest.TestCase):
    def test_confirm_writes_rows_and_renders_from_them(self):
        db = FakePlanDB()
        # Existing future rows that will be replaced from 8/9 forward.
        _standard_week(db, week_num=3, sunday=date(2026, 8, 9))
        current = hr.build_rows(hr.build_initial_schedule())
        proposal = hr.build_proposal("repeat", week_num=3, closed_end=date(2026, 8, 8),
                                     current_future_rows=current, channel_id="chan")
        # Stage it in ramp_state, as the nightly would.
        db.ramp_state["pending_proposal_id"] = proposal["proposal_id"]
        db.ramp_state["pending_proposal"] = json.dumps(proposal)
        db.ramp_state["consecutive_nonsuccess_count"] = 1

        with mock.patch("knowledge.db.get_connection", fake_get_connection(db)), \
             mock.patch.object(hr, "_audit", lambda *a, **k: "aid"), \
             mock.patch.object(hr, "load_ramp_pending",
                               lambda c=None: json.loads(db.ramp_state["pending_proposal"])
                               if db.ramp_state["pending_proposal"] else None):
            reply = hr.commit_ramp_proposal("chan")

        # Rendered from the written rows (real count + date span), not the proposal.
        n_written = sum(1 for r in db.plan.values() if r["plan_date"] >= date(2026, 8, 9))
        self.assertIn("✅ Applied", reply)
        self.assertIn(str(n_written), reply)
        # Pending cleared and counters reset atomically (no restart loop).
        self.assertIsNone(db.ramp_state["pending_proposal_id"])
        self.assertIsNone(db.ramp_state["pending_proposal"])
        self.assertEqual(db.ramp_state["consecutive_nonsuccess_count"], 0)
        # Travel still pinned in what actually landed.
        landed = {r["plan_date"]: r for r in db.plan.values()}
        self.assertEqual(landed[date(2026, 8, 18)]["session_type"], "strength_c")

    def test_confirm_with_no_pending_is_graceful(self):
        db = FakePlanDB()
        with mock.patch.object(hr, "load_ramp_pending", lambda c: None):
            reply = hr.commit_ramp_proposal("chan")
        self.assertIn("Nothing pending", reply)


if __name__ == "__main__":
    unittest.main()
