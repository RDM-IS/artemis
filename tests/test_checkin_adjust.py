"""FRIDAY-1 — check-in-driven morning: parser, rules, flows, routing, guard.

The eight Session B scenarios from the build prompt run through the REAL
process_* handlers against an in-memory health.plan / session_log /
daily_state (FakeDB), seeded with the exact Friday 2026-09-18 row that
artemis.health_office builds. No RDS.

Run:
    python3.11 tests/test_checkin_adjust.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import copy
import json
import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import health_checkin as hc  # noqa: E402
from artemis import health_office as office  # noqa: E402
from artemis import health_regions as regions  # noqa: E402
from artemis.health_guard import Evidence, find_violations  # noqa: E402

# SCHEDULE-2: Strength B moved off Friday. This is the week-2 B day (Wed).
FRI = date(2026, 9, 23)
CT = ZoneInfo("America/Chicago")
B_NAMES = ["DB goblet squat", "Seated cable row", "Incline DB press", "Leg extension",
           "Rear delt fly", "Cable Pallof press", "Seated back extension"]


def office_row(d: date, plan_id: int = 105) -> dict:
    r = next(x for x in office.build_rows() if x["plan_date"] == d)
    return {"plan_id": plan_id, "plan_date": d, "phase": r["phase"], "week_num": r["week_num"],
            "session_type": r["session_type"], "target_rpe": r["target_rpe"],
            "target_hr_zone": r["target_hr_zone"], "est_duration_min": r["est_duration_min"],
            "blocks": copy.deepcopy(r["blocks"]), "is_skipped": False}


def rest_row(d: date, plan_id: int = 104) -> dict:
    """A plain rest_mobility day (YOGA-1 made Thu/Sat Recovery Flow days)."""
    blocks, rpe, zone, est = office._rest(1)
    return {"plan_id": plan_id, "plan_date": d, "phase": 1, "week_num": 1,
            "session_type": "rest_mobility", "target_rpe": rpe, "target_hr_zone": zone,
            "est_duration_min": est, "blocks": copy.deepcopy(blocks), "is_skipped": False}


# ----------------------------------------------------------------------------
# In-memory DB speaking exactly the SQL health_checkin uses
# ----------------------------------------------------------------------------

class FakeDB:
    def __init__(self, *rows):
        self.plan = {r["plan_date"]: copy.deepcopy(r) for r in rows}
        self.logs: list[dict] = []
        self.daily: dict[date, dict] = {}
        self.audit: list[tuple] = []
        self.patterns: list[dict] = []          # health.pain_pattern
        self.reflections: list[dict] = []       # health.reflection
        self.plan_dates: dict[int, date] = {}   # plan_id -> date for history-only rows
        self._savepoint = None

    def date_of(self, plan_id):
        for r in self.plan.values():
            if r["plan_id"] == plan_id:
                return r["plan_date"]
        return self.plan_dates.get(plan_id)

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    _PLAN_COLS = ("plan_id", "plan_date", "phase", "week_num", "session_type", "target_rpe",
                  "target_hr_zone", "est_duration_min", "blocks", "is_skipped")

    def __init__(self, db):
        self.db = db
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.description, self._rows = None, []
        if s.startswith("SELECT plan_id, plan_date, phase, week_num, session_type, target_rpe,"):
            row = self.db.plan.get(params[0])
            self.description = [(c,) for c in self._PLAN_COLS]
            if row:
                self._rows = [tuple(json.dumps(row[c]) if c == "blocks" else row[c]
                                    for c in self._PLAN_COLS)]
        elif s.startswith("SELECT count(*) FROM health.session_log"):
            n = sum(1 for l in self.db.logs if l["plan_id"] == params[0]
                    and l["logged_via"] != "inferred"
                    and l["log_type"] in ("strength_set", "cardio_block"))
            self._rows = [(n,)]
        elif s.startswith("SELECT sleep_hrs, sleep_source"):
            # TUPLES + description, like the box's psycopg2 cursor. This fake
            # used to return a dict, which hid a TypeError that broke every
            # real check-in once a daily_state row existed (2026-09-21).
            cols = ("sleep_hrs", "sleep_source", "resting_hr", "resting_hr_source",
                    "weight_lbs", "weight_source")
            row = self.db.daily.get(params[0])
            self.description = [(c,) for c in cols]
            self._rows = [] if row is None else [tuple(row.get(c) for c in cols)]
        elif s.startswith("SELECT 1 FROM health.daily_state"):
            # a check-in = anything typed, or a pre-WATCH-1 row with no markers;
            # a watch-only pre-fill row is NOT a check-in
            row = self.db.daily.get(params[0])
            marks = [row.get(m) for m in ("sleep_source", "resting_hr_source",
                                          "weight_source")] if row else []
            typed = row is not None and (
                any(row.get(k) is not None for k in ("energy", "soreness", "free_text"))
                or "manual" in marks or all(m is None for m in marks))
            self._rows = [(1,)] if typed else []
        elif s.startswith("INSERT INTO health.daily_state"):
            # WATCH-1: the three source markers ride along after free_text
            d, w, sl, en, sore, rhr, ft = params[:7]
            old = self.db.daily.get(d, {})
            new = {"weight_lbs": w, "sleep_hrs": sl, "energy": en,
                   "soreness": json.loads(sore) if sore else None, "resting_hr": rhr, "free_text": ft}
            merged = {k: (v if v is not None else old.get(k)) for k, v in new.items()}
            # a typed value marks its field `manual` and is final for the date
            for value, marker in ((sl, "sleep_source"), (rhr, "resting_hr_source"),
                                  (w, "weight_source")):
                merged[marker] = "manual" if value is not None else old.get(marker)
            self.db.daily[d] = merged
        elif s.startswith("UPDATE health.plan SET blocks"):
            blocks, st, rpe, est, pid = params
            row = next(r for r in self.db.plan.values() if r["plan_id"] == pid)
            row.update(blocks=json.loads(blocks), session_type=st, target_rpe=rpe,
                       est_duration_min=est)
        elif s.startswith("INSERT INTO acos.audit_log"):
            self.db.audit.append(params)
        elif s.startswith("SELECT exercise, log_type FROM health.session_log"):
            self._rows = [(l["exercise"], l["log_type"]) for l in self.db.logs
                          if l["plan_id"] == params[0] and l["logged_via"] != "inferred"]
        # ── PAIN-1 ──
        elif s.startswith("SELECT state_date, soreness FROM health.daily_state WHERE state_date IN"):
            self._rows = [(d, json.dumps(v["soreness"]) if v["soreness"] is not None else None)
                          for d, v in self.db.daily.items() if d in params]
        elif s.startswith("SELECT state_date, soreness FROM health.daily_state WHERE state_date BETWEEN"):
            self._rows = [(d, v["soreness"]) for d, v in sorted(self.db.daily.items())
                          if params[0] <= d <= params[1]]
        elif s.startswith("SELECT sl.weight_lbs FROM health.session_log sl"):
            name, day = params
            cands = [(self.db.date_of(l["plan_id"]), l["weight_lbs"]) for l in self.db.logs
                     if l["exercise"] == name and l["log_type"] == "strength_set"
                     and l.get("weight_lbs") and l["logged_via"] != "inferred"
                     and not l.get("is_skipped") and self.db.date_of(l["plan_id"]) < day]
            cands.sort(reverse=True)
            self._rows = [(cands[0][1],)] if cands else []
        elif s.startswith("SELECT p.plan_date, sl.exercise, sl.notes"):
            lo, hi = params
            self._rows = [(self.db.date_of(l["plan_id"]), l["exercise"], l.get("notes"),
                           bool(l.get("is_skipped")))
                          for l in self.db.logs
                          if l["log_type"] == "strength_set" and l["logged_via"] != "inferred"
                          and lo <= self.db.date_of(l["plan_id"]) <= hi]
        elif s.startswith("SELECT id, exercise, region, hits"):
            from artemis.health_patterns import _COLS
            self._rows = [tuple(copy.deepcopy(p[c]) for c in _COLS) for p in self.db.patterns]
        elif s.startswith("INSERT INTO health.pain_pattern"):
            (ex, region, hits, exposures, qual, first, last, status, dah, ev, now) = params
            row = next((p for p in self.db.patterns
                        if (p["exercise"], p["region"]) == (ex, region)), None)
            if row is None:
                row = {"id": len(self.db.patterns) + 1, "exercise": ex, "region": region,
                       "first_seen": None, "last_seen": None, "last_surfaced": None,
                       "surfaced_hits": None, "surfaced_exposures": None,
                       "mentioned_at": None, "post_ids": [], "resolved_at": None,
                       "resolved_at_hits": None}
                self.db.patterns.append(row)
            row.update(hits=hits, exposures=exposures, qualifies=qual, status=status,
                       dismissed_at_hits=dah, evidence=json.loads(ev))
            row["first_seen"] = first or row["first_seen"]
            row["last_seen"] = last or row["last_seen"]
        elif s.startswith("UPDATE health.pain_pattern SET mentioned_at"):
            self._pattern(params[1])["mentioned_at"] = params[0]
        elif s.startswith("UPDATE health.pain_pattern SET last_surfaced"):
            now, h, e, pid, _, rid = params
            row = self._pattern(rid)
            row.update(last_surfaced=now, surfaced_hits=h, surfaced_exposures=e)
            if pid is not None:
                row["post_ids"].append(pid)
        elif s.startswith("UPDATE health.pain_pattern SET status = 'dismissed'"):
            row = self._pattern(params[1])
            row.update(status="dismissed", dismissed_at_hits=row["hits"])
        elif s.startswith("UPDATE health.pain_pattern SET status = 'resolved'"):
            row = self._pattern(params[2])
            row.update(status="resolved", resolved_at=params[0], resolved_at_hits=row["hits"])
        elif s.startswith("SELECT id FROM health.pain_pattern WHERE %s = ANY(post_ids)"):
            self._rows = [(p["id"],) for p in self.db.patterns if params[0] in p["post_ids"]]
        elif s.startswith("INSERT INTO health.reflection"):
            pid, text, post_id, src = params
            if not any(r["source_post_id"] == src for r in self.db.reflections if src):
                self.db.reflections.append({"pattern_id": pid, "text": text,
                                            "post_id": post_id, "source_post_id": src})
        elif s == "SAVEPOINT pain_patterns":
            self.db._savepoint = copy.deepcopy(self.db.patterns)
        elif s == "RELEASE SAVEPOINT pain_patterns":
            self.db._savepoint = None
        elif s == "ROLLBACK TO SAVEPOINT pain_patterns":
            self.db.patterns = self.db._savepoint
        else:
            raise AssertionError(f"unhandled SQL: {s[:90]}")

    def _pattern(self, pid):
        return next(p for p in self.db.patterns if p["id"] == pid)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def names(db, d=FRI):
    return [e["name"] for e in db.plan[d]["blocks"]["exercises"]]


# ----------------------------------------------------------------------------
# Spec scenarios (Session B, Fri 9/18)
# ----------------------------------------------------------------------------

class TestSessionBScenarios(unittest.TestCase):
    """The final FRIDAY-1 spec's numbered tests (Session B unless noted)."""

    def setUp(self):
        self.db = FakeDB(office_row(FRI))
        self.cur = self.db.cursor()
        self.now = datetime(2026, 9, 18, 10, 10, tzinfo=timezone.utc)

    def checkin(self, text, **kw):
        return hc.process_checkin(self.cur, text, FRI, checkin_id="post-1", now=self.now,
                                  adjust=kw.get("adjust", True))

    def by_name(self):
        return {e["name"]: e for e in self.db.plan[FRI]["blocks"]["exercises"]}

    def test_friday_row_is_session_b(self):
        row = self.db.plan[FRI]
        self.assertEqual(row["session_type"], "strength_b")
        self.assertEqual(names(self.db), B_NAMES)
        self.assertEqual(row["blocks"]["rounds"], 2)
        self.assertEqual(row["target_rpe"], 6.0)

    def test_01_all_clear_no_change_and_zero_stored(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("slept 8 hours, energy 5, sore 0, weight 283")
        self.assertEqual(reply, "Check-in logged — run Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)
        st = self.db.daily[FRI]
        self.assertEqual(st["soreness"], {"overall": 0})
        self.assertEqual((st["sleep_hrs"], st["energy"], st["weight_lbs"]), (8.0, 5, 283.0))

    def _assert_shoulder_replace(self, reply):
        got = names(self.db)
        for gone in ("DB goblet squat", "Seated cable row", "Incline DB press", "Rear delt fly"):
            self.assertNotIn(gone, got)
        for kept in ("Leg extension", "Cable Pallof press", "Seated back extension"):
            self.assertIn(kept, got)
        added = [n for n in got if n not in B_NAMES]
        self.assertEqual(added, ["Leg press", "Seated leg curl", "Calf press",
                                 "Captain's chair knee raise"])
        self.assertEqual(len(got), 7)
        self.assertEqual(len(set(got)), 7, "duplicate exercise")
        b = self.db.plan[FRI]["blocks"]
        self.assertEqual([e["name"] for e in b["original"]["blocks"]["exercises"]], B_NAMES)
        self.assertEqual(b["adjustment"]["checkin_id"], "post-1")
        self.assertEqual(b["adjustment"]["rules_fired"], ["replace"])
        for key in ("reason", "rules_fired", "checkin_id", "at"):
            self.assertIn(key, b["adjustment"])
        for ex in b["exercises"]:
            if ex.get("added_by") == "checkin":
                self.assertTrue(ex["notes"].startswith("2×"))
                self.assertFalse(regions.uses_any(ex["name"], ["shoulder"]))
        self.assertEqual(reply,
            "Shoulder 4/5 → removed DB goblet squat, seated cable row, incline DB press, "
            "rear delt fly. Added leg press, seated leg curl, calf press, captain's chair knee "
            "raise.\nReply `original` to undo.")

    def test_02_shoulder_4_replaces(self):
        self._assert_shoulder_replace(self.checkin("slept 8, energy 5, sore shoulder 4, weight 283"))
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": 4, "sides": {"shoulder": "unspecified"}})

    def test_03_eight_of_ten_is_four(self):
        self._assert_shoulder_replace(self.checkin("sore shoulder 8/10"))
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": 4, "sides": {"shoulder": "unspecified"}})

    def test_04_legs_3_lightens_goblet_and_extension(self):
        reply = self.checkin("slept 8 energy 5 legs sore 3")
        by = self.by_name()
        for n in ("DB goblet squat", "Leg extension"):
            self.assertEqual(by[n]["sets"], 1, n)
            self.assertEqual(by[n]["rpe_cap"], 5.0, n)
            self.assertTrue(by[n]["notes"].startswith("1×"), by[n]["notes"])
        for n in ("Seated cable row", "Incline DB press", "Rear delt fly",
                  "Cable Pallof press", "Seated back extension"):
            self.assertNotIn("sets", by[n], n)
            self.assertNotIn("rpe_cap", by[n], n)
        self.assertEqual(names(self.db), B_NAMES)
        self.assertEqual(reply, "Legs 3/5 → DB goblet squat and leg extension: 1 set, RPE ≤5."
                                "\nReply `original` to undo.")

    def test_05_two_sore_regions_at_4_swap_to_z2(self):
        reply = self.checkin("sore shoulder 4 and legs 4")
        row = self.db.plan[FRI]
        self.assertEqual(row["session_type"], "cardio_z2")
        b = row["blocks"]
        self.assertEqual(b["type"], "steady")
        self.assertEqual(b["equipment"], ["recumbent bike"])
        self.assertTrue(20 <= b["duration_min"] <= 30)
        self.assertEqual(b["mobility_min"], 10)
        self.assertEqual(b["original"]["session_type"], "strength_b")
        self.assertIn("Recovery Z2 + Mobility", reply)
        self.assertEqual(b["adjustment"]["rules_fired"], ["day_swap"])

    # FRIDAY-1 tests 06 (pain 4-5 replaces) and 07 (pain 1-3: load −20% +
    # mobility) were retired by PAIN-1 — see tests/test_pain_ladder.py.

    def test_08_rating_above_5_refused_nothing_stored(self):
        before = copy.deepcopy(self.db.plan[FRI])
        for text in ("sore shoulder 6", "energy 7", "sore shoulder 7/5", "legs sore 12/10"):
            with self.subTest(text=text):
                self.assertEqual(self.checkin(text), "Ratings are 0–5.")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily, {})
        self.assertEqual(self.db.audit, [])

    def test_09_low_energy_global_recovery_only(self):
        """Energy drives recovery; the sleep in the same message plays no part."""
        reply = self.checkin("slept 8 energy 2 sore 0")
        b = self.db.plan[FRI]["blocks"]
        self.assertEqual(names(self.db), B_NAMES)
        for ex in b["exercises"]:
            self.assertEqual(ex["rpe_cap"], 5.0, ex["name"])
            self.assertLessEqual(hc.exercise_sets(ex, b), 2)
            self.assertNotIn("load_pct", ex)
        self.assertEqual(self.db.plan[FRI]["target_rpe"], 5.0)
        self.assertEqual(b["adjustment"]["rules_fired"], ["recovery"])
        self.assertTrue(reply.startswith("Energy 2/5 → RPE ≤5 on every exercise."), reply)

    def test_09c_short_sleep_alone_changes_nothing(self):
        """2026-09-21: sleep < 6 is no longer a trigger. It is recorded, never acted on."""
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("slept 4 energy 5")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["sleep_hrs"], 4.0)
        self.assertNotIn("RPE", reply)
        for text in ("slept 3", "slept 5.5 energy 3", "slept 0 energy 4 sore 0"):
            with self.subTest(text=text):
                adj = hc.compute_adjustment(office_row(FRI), hc.parse_checkin(text))
                self.assertNotIn("recovery", adj.rules_fired)
                self.assertFalse(adj.changed, text)

    def test_09d_the_rule_has_no_sleep_threshold_left(self):
        src = (Path(hc.__file__)).read_text()
        body = src[src.index("def compute_adjustment"):]
        self.assertNotRegex(body, r"sleep_hrs\s*<")

    def test_09b_recovery_stacks_with_lighten_floor_1(self):
        self.checkin("slept 5 energy 2 legs sore 3")
        by = self.by_name()
        self.assertEqual(by["DB goblet squat"]["rpe_cap"], 4.0)     # 6 −1 −1
        self.assertEqual(by["Seated cable row"]["rpe_cap"], 5.0)
        row = office_row(FRI)
        row["target_rpe"] = 1.5
        adj = hc.compute_adjustment(row, hc.parse_checkin("slept 4 energy 1 legs sore 3"))
        for ex in adj.blocks["exercises"]:
            self.assertGreaterEqual(ex["rpe_cap"], 1.0)

    def test_10_after_a_logged_set_store_only(self):
        self.db.logs.append({"plan_id": 105, "logged_via": "manual",
                             "log_type": "strength_set", "exercise": "DB goblet squat"})
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("slept 8, energy 5, sore shoulder 4, weight 283")
        self.assertEqual(reply, "Logged.")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": 4, "sides": {"shoulder": "unspecified"}})

    def test_11_original_restores(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("sore shoulder 4")
        self.assertNotEqual(self.db.plan[FRI], before)
        reply = hc.process_original(self.cur, FRI)
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(reply, "Restored — run Session B as written.")
        self.assertEqual(hc.process_original(self.cur, FRI),
                         "No adjustment to undo — today's plan is as written.")

    def test_11b_swap_then_original_restores_row_fields(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("sore shoulder 4 and legs 5")
        hc.process_original(self.cur, FRI)
        self.assertEqual(self.db.plan[FRI], before)

    def test_12_second_checkin_recomputes_from_original(self):
        self.checkin("sore shoulder 4")
        self.checkin("legs sore 3")
        b = self.db.plan[FRI]["blocks"]
        self.assertEqual(names(self.db), B_NAMES)           # shoulder change not stacked
        self.assertEqual({e["name"] for e in b["exercises"] if e.get("sets") == 1},
                         {"DB goblet squat", "Leg extension"})
        self.assertEqual([e["name"] for e in b["original"]["blocks"]["exercises"]], B_NAMES)
        self.assertEqual(b["adjustment"]["rules_fired"], ["lighten_sore"])

    def test_12b_all_clear_second_checkin_returns_to_as_written(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("sore shoulder 4")
        self.assertEqual(self.checkin("sore 0"), "Check-in logged — back to Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)

    def test_13_nudge_at_0515_then_checkin_at_0520_still_adjusts(self):
        from artemis.scheduler import ArtemisScheduler

        @contextmanager
        def conn():
            yield self.db

        sched = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        posted = []
        with patch("knowledge.db.get_connection", conn), \
             patch("artemis.scheduler._local_today", return_value=FRI), \
             patch.object(sched, "_post", side_effect=lambda ch, t, tier: posted.append((t, tier))):
            sched.job_checkin_nudge()
        self.assertEqual(posted, [("No check-in yet — run Session B as written.", "health")])

        self.now = datetime(2026, 9, 18, 10, 20, tzinfo=timezone.utc)   # 05:20 CDT
        reply = self.checkin("slept 8, energy 5, sore shoulder 4, weight 283")
        self.assertIn("removed", reply)
        self.assertIn("adjustment", self.db.plan[FRI]["blocks"])

    def test_nudge_skips_rest_and_walk_days(self):
        from artemis.scheduler import ArtemisScheduler
        for d, row in ((date(2026, 9, 17), rest_row(date(2026, 9, 17))),
                       (date(2026, 9, 28), office_row(date(2026, 9, 28), plan_id=107))):
            db = FakeDB(row)

            @contextmanager
            def conn(db=db):
                yield db

            sched = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
            with patch("knowledge.db.get_connection", conn), \
                 patch("artemis.scheduler._local_today", return_value=d), \
                 patch.object(sched, "_post") as post:
                sched.job_checkin_nudge()
            post.assert_not_called()

    # ── extra coverage ──
    def test_flag_off_stores_but_never_adjusts(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("sore shoulder 4", adjust=False)
        self.assertEqual(reply, "Check-in logged — run Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": 4, "sides": {"shoulder": "unspecified"}})
        self.assertEqual(self.db.audit[-1][2], "checkin_adjust_suppressed")

    def test_config_flag_default_on(self):
        from artemis import config
        self.assertTrue(config.CHECKIN_ADJUST)

    def test_soreness_0_and_1_change_nothing(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.assertEqual(self.checkin("sore shoulder 1, legs sore 0"),
                         "Check-in logged — run Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)

    def test_unscored_region_is_stored_and_changes_nothing(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("slept 8 energy 5 shoulder a bit sore")
        self.assertEqual(reply, "Check-in logged — run Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": None, "sides": {"shoulder": "unspecified"}})

    def test_unknown_region_is_named_and_ignored(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("slept 8 energy 5 sore elbow 4")
        self.assertIn("Didn't recognize region elbow", reply)
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"elbow": 4})

    def test_high_energy_long_sleep_never_adds_work(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("slept 10 energy 5 sore 0")
        self.assertEqual(self.db.plan[FRI], before)

    def test_rules_never_add_volume_load_or_rpe(self):
        for text in ("sore shoulder 4", "legs sore 3", "slept 8 energy 2", "shoulder pain 2",
                     "sore shoulder 4 and legs 5", "shoulder pain 5 plus legs pain 5",
                     "shoulder pain 3", "legs pain 3", "knee pain 1",
                     "slept 9 energy 5 sore 0"):
            db = FakeDB(office_row(FRI))
            hc.process_checkin(db.cursor(), text, FRI, checkin_id="x", now=self.now, adjust=True)
            b = db.plan[FRI]["blocks"]
            base = office_row(FRI)
            # A day off / mobility day has no RPE at all.
            self.assertLessEqual(db.plan[FRI]["target_rpe"] or 0, base["target_rpe"], text)
            for ex in b.get("exercises") or []:
                self.assertLessEqual(hc.exercise_sets(ex, b), 2, text)
                self.assertLessEqual(ex.get("rpe_cap", 6.0), 6.0, text)
                self.assertLessEqual(ex.get("load_pct", 100), 100, text)
            self.assertLessEqual(len(b.get("exercises") or []), 7, text)
            self.assertLessEqual(db.plan[FRI]["est_duration_min"] or 0,
                                 base["est_duration_min"], text)

    def test_rest_day_no_adjustment(self):
        thu = date(2026, 9, 17)
        db = FakeDB(rest_row(thu))
        reply = hc.process_checkin(db.cursor(), "sore shoulder 5", thu, checkin_id="x")
        self.assertEqual(reply, "Check-in logged — rest day as planned.")
        self.assertNotIn("adjustment", db.plan[thu]["blocks"])

    def test_z2_day_recovery_cuts_duration(self):
        tue = date(2026, 9, 22)   # SCHEDULE-2: Z2 is the office Tuesday
        db = FakeDB(office_row(tue, plan_id=109))
        hc.process_checkin(db.cursor(), "slept 8 energy 2", tue, checkin_id="x", adjust=True)
        self.assertEqual(db.plan[tue]["blocks"]["duration_min"], 15)   # 20 → 15

    def test_monday_c_week5_legs_heavy_drops_finisher(self):
        mon = date(2026, 10, 16)   # SCHEDULE-2: the week-5 Strength C day
        row = office_row(mon, plan_id=140)
        self.assertIn("finisher", row["blocks"])
        db = FakeDB(row)
        hc.process_checkin(db.cursor(), "legs sore 4", mon, checkin_id="x", adjust=True)
        self.assertNotIn("finisher", db.plan[mon]["blocks"])


class TestWatchPrefilledDay(unittest.TestCase):
    """A day the WATCH-1 pre-fill already wrote (every morning since 9/20).

    Regression: the check-in crashed on the real tuple cursor
    (`dict(tuple)` in _watch_source_note), and the watch-only row counted as a
    check-in, suppressing the 05:15 nudge (2026-09-21)."""

    def setUp(self):
        self.db = FakeDB(office_row(FRI))
        self.db.daily[FRI] = {"sleep_hrs": 5.7, "sleep_source": "watch",
                              "resting_hr": None, "resting_hr_source": None,
                              "weight_lbs": 282.0, "weight_source": "watch",
                              "energy": None, "soreness": None, "free_text": None}
        self.cur = self.db.cursor()
        self.now = datetime(2026, 9, 18, 10, 10, tzinfo=timezone.utc)

    def test_a_watch_only_row_is_not_a_checkin(self):
        self.assertFalse(hc.has_checkin(self.cur, FRI))

    def test_the_checkin_goes_through_and_names_the_watch_values(self):
        reply = hc.process_checkin(self.cur, "energy 4 sore 0", FRI, checkin_id="p",
                                   now=self.now, adjust=True)
        self.assertIn("From the watch: sleep 5.7h, weight 282. Not synced: resting HR.", reply)
        self.assertTrue(hc.has_checkin(self.cur, FRI))

    def test_watch_sleep_never_triggers_recovery(self):
        """5.7 h from the watch and no energy typed: the plan is untouched."""
        before = copy.deepcopy(self.db.plan[FRI])
        hc.process_checkin(self.cur, "sore 0", FRI, checkin_id="p", now=self.now, adjust=True)
        self.assertEqual(self.db.plan[FRI], before)

    def test_a_typed_value_replaces_the_watch_one_and_is_named_as_his(self):
        reply = hc.process_checkin(self.cur, "slept 7 energy 4", FRI, checkin_id="p",
                                   now=self.now, adjust=True)
        self.assertEqual(self.db.daily[FRI]["sleep_source"], "manual")
        self.assertIn("From the watch: weight 282.", reply)
        self.assertNotIn("sleep 5.7h", reply)


class TestFlows(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(office_row(FRI))
        self.cur = self.db.cursor()

    def test_done_without_sets(self):
        self.assertIn("I don't see any sets for today yet", hc.process_done(self.cur, FRI))

    def test_done_with_sets(self):
        self.db.logs += [
            {"plan_id": 105, "logged_via": "manual", "log_type": "strength_set", "exercise": "A"},
            {"plan_id": 105, "logged_via": "manual", "log_type": "strength_set", "exercise": "B"},
            {"plan_id": 105, "logged_via": "manual", "log_type": "session_summary", "exercise": "s"},
        ]
        self.assertEqual(hc.process_done(self.cur, FRI), "Session B logged: 2 sets across 2 exercises.")

    def test_ack(self):
        self.assertEqual(hc.process_ack(self.cur, FRI), "Got it — run Session B as written.")
        hc.process_checkin(self.cur, "sore shoulder 4", FRI, checkin_id="x", adjust=True)
        self.assertEqual(hc.process_ack(self.cur, FRI),
                         "Got it — run the adjusted Session B. Reply `original` to go back.")

    def test_nudge_text(self):
        self.assertEqual(hc.nudge_text(office_row(FRI)), "No check-in yet — run Session B as written.")
        self.assertIsNone(hc.nudge_text(rest_row(date(2026, 9, 17))))     # rest day
        self.assertEqual(hc.nudge_text(office_row(date(2026, 9, 25))),     # YOGA-1 flow day
                         "No check-in yet — run Recovery Flow as written.")
        self.assertIsNone(hc.nudge_text(None))


class TestParser(unittest.TestCase):
    def test_bare_weight_from_the_922_checkin(self):
        """9/22's exact reply: the bare 281.5 was dropped into free_text."""
        ci = hc.parse_checkin("Sleep 6, energy 4, soreness 0, 281.5")
        self.assertEqual((ci.sleep_hrs, ci.energy, ci.weight_lbs), (6.0, 4, 281.5))
        self.assertEqual(ci.soreness, {"overall": 0})
        self.assertIsNone(ci.free_text)
        self.assertEqual(hc.classify("Sleep 6, energy 4, soreness 0, 281.5"), "checkin")

    def test_bare_weight_is_stored_manual(self):
        cur = MagicMock()
        hc.store_checkin(cur, FRI, hc.parse_checkin("Sleep 6, energy 4, soreness 0, 281.5"))
        sql, params = cur.execute.call_args[0]
        self.assertIn("weight_source = CASE WHEN EXCLUDED.weight_lbs IS NOT NULL THEN 'manual'", sql)
        self.assertEqual(params[1], 281.5)

    def test_bare_weight_limits(self):
        cases = {
            "sleep 6, 149": None,                    # below range
            "sleep 6, 401": None,                    # above range
            "sleep 6, 150": 150.0,
            "sleep 6, 400": 400.0,
            "sleep 6, walked 200 steps": None,       # not a lone clause
            "sleep 6, 281, 283": None,               # two candidates: ambiguous
            "sleep 6, weight 283, 281": 283.0,       # labelled weight wins
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hc.parse_checkin(text).weight_lbs, want)

    def test_scale(self):
        cases = {
            "sore 0": {"overall": 0},
            "sore shoulder 4": {"shoulder": 4},
            "sore shoulder 8/10": {"shoulder": 4},
            "sore shoulder 5/10": {"shoulder": 3},
            "sore shoulder 3/5": {"shoulder": 3},
            "sore shoulder 3 out of 5": {"shoulder": 3},
            "legs sore 3": {"legs": 3},
            "sore shoulder 4 and legs 4": {"shoulder": 4, "legs": 4},
            "shoulder and neck sore 3": {"shoulder": 3, "neck": 3},
            "quads sore 2, lower back sore 4/10": {"quads": 2, "low back": 2},
            "no soreness": {"overall": 0},
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hc.parse_checkin(text).soreness, want)

    def test_energy_is_0_to_5(self):
        self.assertEqual(hc.parse_checkin("energy 0").energy, 0)
        self.assertEqual(hc.parse_checkin("energy 8/10").energy, 4)
        self.assertTrue(hc.parse_checkin("energy 6").rating_error)

    def test_out_of_range_is_an_error(self):
        for text in ("sore shoulder 6", "sore shoulder 7/5", "legs sore 11/10", "energy 9"):
            with self.subTest(text=text):
                ci = hc.parse_checkin(text)
                self.assertTrue(ci.rating_error)
                self.assertFalse(ci.has_data)

    def test_score_carries_across_and_but_not_across_commas(self):
        ci = hc.parse_checkin("sore shoulder, legs 2")
        self.assertEqual(ci.soreness, {"shoulder": None, "legs": 2})

    def test_pain_attaches_only_to_its_own_region(self):
        ci = hc.parse_checkin("sharp pain in my lower back 3, knees a little sore 2")
        self.assertEqual(ci.pain, {"low back": 3})
        self.assertEqual(ci.soreness, {"knee": 2})
        ci = hc.parse_checkin("shoulder pain 2 and legs sore 3")
        self.assertEqual((ci.pain, ci.soreness), ({"shoulder": 2}, {"legs": 3}))

    def test_every_region_word_is_recognized(self):
        for r in regions.REGIONS:
            with self.subTest(region=r):
                self.assertEqual(hc.parse_checkin(f"{r} sore 2").soreness, {r: 2})

    def test_the_real_0916_checkin(self):
        ci = hc.parse_checkin("Slept 6.5\nEnergy 5\nSore 0\nWeight 284.5")
        self.assertEqual((ci.sleep_hrs, ci.energy, ci.weight_lbs, ci.soreness),
                         (6.5, 5, 284.5, {"overall": 0}))

    def test_nothing_parsable(self):
        self.assertFalse(hc.parse_checkin("feeling ok I guess").has_data)

    def test_classify(self):
        cases = {
            "nope": "ack", "Nope": "ack", "all good": "ack", "no": "ack",
            "Workout completed.  Logged in App.": "done", "workout completed": "done",
            "done": "done", "workout done": "done",
            "original": "original", "use original": "original",
            "slept 8, energy 5, sore 0": "checkin",
            "sore shoulder 6": "checkin",          # claimed → answered "Ratings are 0–5."
            "done 1a2b3c": None, "done call Brad about the SOW": None,
            "good morning": None, "what's today's workout": None, "undo": None,
            "ok": None, "thanks": None, "sounds good": None,   # other short text: not claimed
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hc.classify(text), want)


class TestRegionMap(unittest.TestCase):
    def test_every_office_exercise_is_mapped(self):
        for lst in office._EXERCISES.values():
            for spec in lst:
                with self.subTest(exercise=spec[0]):
                    self.assertIn(spec[0], regions.EXERCISE_REGIONS)

    def test_pool_is_buildable_and_mapped(self):
        for name in regions.SUBSTITUTION_POOL:
            with self.subTest(exercise=name):
                self.assertIn(name, regions.EXERCISE_REGIONS)
                ex = hc._office_exercise(name, 2, 1)
                self.assertEqual(ex["name"], name)

    def test_spec_examples(self):
        p, s = regions.regions_for("DB goblet squat")
        self.assertIn("legs", p)
        self.assertTrue({"shoulder", "low back"} <= s)
        self.assertTrue({"chest", "shoulder", "triceps"} <= regions.regions_for("Incline DB press")[0])
        self.assertEqual(regions.regions_for("Rear delt fly")[0], frozenset({"shoulder"}))
        p, s = regions.regions_for("Seated cable row")
        self.assertEqual(p, frozenset({"back"}))
        self.assertTrue({"shoulder", "biceps"} <= s)
        self.assertIn("legs", regions.regions_for("Leg press")[0])
        self.assertEqual(regions.regions_for("Cable Pallof press")[0], frozenset({"core"}))
        self.assertEqual(regions.regions_for("Seated back extension")[0], frozenset({"low back"}))

    def test_back_extension_is_the_seated_precor_machine(self):
        # There is no 45° back extension / roman chair in the office gym.
        self.assertEqual(regions.equipment_class("Seated back extension"), "machine")
        self.assertIn("Seated back extension", regions.SUBSTITUTION_POOL)
        for r in office.build_rows():
            blob = json.dumps(r["blocks"], ensure_ascii=False)
            self.assertNotIn("45°", blob, r["plan_date"])
            for ex in r["blocks"].get("exercises") or []:
                if ex["name"] == "Seated back extension":
                    self.assertEqual(ex["equipment_class"], "machine")
                    self.assertIn(office.EQ_BACK_EXT, r["blocks"]["equipment"])

    def test_cardio_and_mobility_are_mapped(self):
        for name in ("Zone 2 Cardio", "Recovery Z2 + Mobility", "Walk", "Rest / Mobility",
                     "Stepmill or upright bike", "Recumbent bike", "Elliptical"):
            with self.subTest(name=name):
                self.assertIn(name, regions.EXERCISE_REGIONS)


class TestPlanExactRender(unittest.TestCase):
    def test_session_b_lines(self):
        lines = hc.render_plan_lines(office_row(FRI))
        self.assertEqual(lines[0], "1. DB goblet squat — 2×8-12 · RPE ≤6")
        self.assertEqual(len(lines), 7)
        self.assertIn("6. Cable Pallof press — 2×10 each side · RPE ≤6", lines)

    def test_wake_post_is_plan_exact(self):
        from artemis import wake
        row = office_row(FRI)
        with patch("artemis.health.get_today_plan", return_value=row), \
             patch.object(wake, "_weather_line", return_value=None), \
             patch.object(wake, "_depart_commitments", return_value=[]), \
             patch.object(wake, "get_timezone_override", return_value=None), \
             patch.object(wake, "local_now", return_value=datetime(2026, 9, 18, 4, 30, tzinfo=CT)), \
             patch.object(wake, "local_today", return_value=FRI):
            text = wake.build_wake_message(calendar=None, held_health=[])
        self.assertIn("Office Strength B", text)
        for i, n in enumerate(B_NAMES, 1):
            self.assertIn(f"{i}. {n} — 2×", text)
        self.assertNotIn("Calibrated plan", text)
        self.assertNotIn("workout is later", text)
        self.assertIn("Reply with: sleep hrs, energy 0–5, soreness by area 0–5 (0 = none), "
                      "weight, RHR.\nExample: `slept 7 energy 4 sore 0 weight 283`", text)

    def test_rest_day_prompt_has_no_workout_later(self):
        from artemis.health import build_morning_survey_prompt
        text = build_morning_survey_prompt(office_row(date(2026, 9, 22)), "logging_only")
        self.assertNotIn("workout is later", text)


class TestSchedulerRegistry(unittest.TestCase):
    def test_nudge_registered_calibration_gone(self):
        """CYCLE-1: the nudge rides 45 min behind the wake, which now depends on
        the day's location — so pin an office day (wake 04:30 -> nudge 05:15)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from artemis import config, cycle
        from artemis.scheduler import ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        office_day = datetime(2026, 9, 22, 0, 1, tzinfo=ZoneInfo(config.HOME_TIMEZONE))
        with patch.object(cycle, "override_for", return_value=None):
            by_id = {c.id: c for c in s.cron_specs(office_day)}
        self.assertEqual((by_id["checkin_nudge"].hour, by_id["checkin_nudge"].minute), (5, 15))
        self.assertEqual(by_id["checkin_nudge"].tier, "health")
        self.assertFalse(hasattr(s, "job_health_calibration_followup"))
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        self.assertNotIn("health_calibration", src)

    def test_wake_opens_checkin_and_schedules_nothing(self):
        from artemis.scheduler import ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        kv = {}
        with patch("artemis.posting.take_holds", return_value=[]), \
             patch("artemis.wake.checked_in_today", return_value=False), \
             patch("artemis.wake.build_wake_message", return_value="W"), \
             patch("artemis.quiet_hours.exit_quiet"), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.__setitem__), \
             patch("artemis.scheduler._local_today", return_value=FRI), \
             patch.object(s.scheduler, "add_job") as add_job:
            s._do_wake()
        self.assertEqual(kv, {f"checkin_open:{FRI}": "open"})
        add_job.assert_not_called()

    def test_wake_after_an_early_checkin_leaves_the_key_alone(self):
        """9/22: Ryan checked in at 04:04, before the 04:30 wake."""
        from artemis.scheduler import ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        kv = {}
        with patch("artemis.posting.take_holds", return_value=[]), \
             patch("artemis.wake.checked_in_today", return_value=True), \
             patch("artemis.wake.build_wake_message", return_value="W") as build, \
             patch("artemis.quiet_hours.exit_quiet"), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.__setitem__), \
             patch("artemis.scheduler._local_today", return_value=FRI):
            s._do_wake()
        self.assertEqual(kv, {})
        self.assertTrue(build.call_args.kwargs["checked_in"])
        s.mm.post_message.assert_called_once()


class TestRouting(unittest.TestCase):
    """This morning's real messages, replayed through the dispatcher."""

    def setUp(self):
        for n in ("flask", "requests", "websocket", "schedule", "Levenshtein",
                  "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
                  "apscheduler.triggers", "apscheduler.triggers.cron",
                  "apscheduler.triggers.interval", "googleapiclient",
                  "googleapiclient.discovery", "googleapiclient.errors", "google", "google.auth",
                  "google.auth.transport", "google.auth.transport.requests", "google.oauth2",
                  "google.oauth2.credentials", "google_auth_oauthlib", "google_auth_oauthlib.flow"):
            sys.modules.setdefault(n, MagicMock())
        from artemis import main
        self.main = main
        self.db = FakeDB(office_row(date(2026, 9, 21), plan_id=103))
        self.db.logs.append({"plan_id": 103, "logged_via": "manual",
                             "log_type": "strength_set", "exercise": "Leg press"})

        @contextmanager
        def conn():
            yield self.db
        self.mm = MagicMock()
        self.gmail = MagicMock()
        self._p = [
            patch.object(main, "_mm", self.mm),
            patch.object(main, "_gmail", self.gmail),
            patch("knowledge.db.get_connection", conn),
            patch("artemis.quiet_hours.local_today", return_value=date(2026, 9, 21)),
            patch.object(main, "get_phase", return_value="wake"),
            patch.object(main, "update_last_interaction"),
            patch.object(main, "_handle_nutrition", side_effect=AssertionError("nutrition reached")),
            patch.object(main, "_handle_intent_routed", side_effect=AssertionError("LLM router reached")),
            patch.object(main, "handle_mention", side_effect=AssertionError("LLM reached")),
        ]
        # Handlers that run BEFORE morning_flow read their own pending state
        # from RDS; with nothing pending they decline, so stub them to decline.
        for name in ("_handle_availability_command", "_handle_duplicate_override",
                     "_handle_calendar_confirm", "_handle_delete_confirm",
                     "_handle_debrief_confirm",
                     "_handle_swap_confirm", "_handle_nutrition_confirm",
                     "_handle_help_command", "_handle_morning_brief_command",
                     "_handle_version_command", "_handle_vault_command",
                     "_handle_dossier_command", "_handle_grocery_staples"):
            self._p.append(patch.object(main, name, return_value=False))
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def send(self, text):
        self.mm.reset_mock()
        post = {"id": f"p-{abs(hash(text))}", "channel_id": "c1", "message": text,
                "root_id": None, "user_id": "u1"}
        self.main._handle_mention(post, [])
        self.assertTrue(self.mm.post_to_channel_id.called, f"no reply for {text!r}")
        return self.mm.post_to_channel_id.call_args[0][1]

    def test_0916_messages_route_to_health_without_gmail(self):
        r1 = self.send("Slept 6.5\nEnergy 5\nSore 0\nWeight 284.5")
        self.assertEqual(r1, "Logged.")                 # sets already logged that morning
        self.assertEqual(self.db.daily[date(2026, 9, 21)]["soreness"], {"overall": 0})
        r2 = self.send("Nope")
        self.assertEqual(r2, "Got it — run Session A as written.")
        r3 = self.send("Workout completed.  Logged in App. ")
        self.assertIn("Session A logged: 1 set across 1 exercise", r3)
        self.gmail.get_recent_messages.assert_not_called()
        self.gmail.get_full_message.assert_not_called()

    def test_14_nope_and_workout_completed_route_to_health_in_any_phase(self):
        for phase in ("wake", "open"):
            with self.subTest(phase=phase), patch.object(self.main, "get_phase", return_value=phase):
                self.assertEqual(self.send("nope"), "Got it — run Session A as written.")
                self.assertIn("Session A logged", self.send("workout completed"))
        self.gmail.get_recent_messages.assert_not_called()
        self.gmail.get_full_message.assert_not_called()

    def test_rating_error_is_answered_deterministically(self):
        self.assertEqual(self.send("sore shoulder 6"), "Ratings are 0–5.")


class TestMentionContextGate(unittest.TestCase):
    def test_no_business_data_outside_open(self):
        sys.modules.setdefault("flask", MagicMock())
        from artemis import main
        gmail, cal = MagicMock(), MagicMock()
        with patch.object(main, "get_phase", return_value="wake"), \
             patch("artemis.health.build_context_slice", return_value="TRAINING"):
            ctx = main._build_mention_context({"id": "x"}, gmail, cal, question="hi")
        gmail.get_recent_messages.assert_not_called()
        gmail.get_full_message.assert_not_called()
        self.assertIn("Business data held", ctx)
        self.assertIn("TRAINING", ctx)


class TestClaimGuard(unittest.TestCase):
    EV = Evidence(
        exercises={"leg press", "db bench press", "lat pulldown", "seated leg curl",
                   "cable face pull rope", "captain's chair knee raise"},
        loads={130.0, 160.0, 30.0, 35.0, 284.5},
        logged_numbers={12.0, 15.0, 130.0, 160.0, 6.5, 7.5, 8.0, 7.0},
        real_sessions=1,
    )

    def test_15_0916_invented_plan_is_rejected(self):
        draft = ("| 5 | **Rope Pushdown** (functional trainer) | 2 × 12 | Squeeze |\n"
                 "Target RPE 7–7.5 — same range as your last two sessions, which felt right.")
        v = find_violations(draft, self.EV)
        self.assertTrue(any("rope pushdown" in x for x in v), v)
        self.assertTrue(any("2 prior sessions" in x for x in v), v)

    def test_15_invented_prior_load_is_rejected(self):
        v = find_violations("Last session you did 185 lb on the leg press for 12 reps.", self.EV)
        self.assertTrue(any("185" in x for x in v), v)

    def test_plan_exact_text_passes(self):
        text = ("Today: leg press 2×10-12 at RPE 6, then the lat pulldown and seated leg curl. "
                "Last session your leg press was 160 lb for 12 reps. Body weight 284.5 lb.")
        self.assertEqual(find_violations(text, self.EV), [])

    def test_15_equipment_words_and_greetings_are_not_exercises(self):
        text = ("Good morning! Today's workout: grab the DBs and a mat, then the leg press "
                "for 2 sets at RPE 6.")
        self.assertEqual(find_violations(text, self.EV), [])

    def test_15_original_blocks_count_as_plan(self):
        ev = Evidence(exercises=set(self.EV.exercises) | {"rear delt fly"}, loads=set(),
                      logged_numbers=set(), real_sessions=0)
        self.assertEqual(find_violations("The workout swapped out the rear delt fly.", ev), [])

    def test_non_workout_text_is_ignored(self):
        self.assertEqual(find_violations("Good morning — last week you had 3 meetings.", self.EV), [])

    def test_guard_replaces_draft_with_template(self):
        from artemis import health_guard
        with patch.object(health_guard, "gather_evidence", return_value=self.EV), \
             patch("knowledge.db.log_guardrail_violation"), \
             patch("artemis.health.get_plan_detail", return_value="PLAN TEMPLATE"):
            out = health_guard.guard_workout_reply("Do a barbell back squat workout today.", "q")
        self.assertEqual(out, "PLAN TEMPLATE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
