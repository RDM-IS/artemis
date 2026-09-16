"""FRIDAY-1 — check-in-driven morning: parser, rules, flows, routing, guard.

The eight Session B scenarios from the build prompt run through the REAL
process_* handlers against an in-memory health.plan / session_log /
daily_state (FakeDB), seeded with the exact Friday 2026-09-18 row that
artemis.health_office builds. No RDS.

Run:
    python3.11 tests/test_checkin_adjust.py
"""

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

FRI = date(2026, 9, 18)
CT = ZoneInfo("America/Chicago")
B_NAMES = ["DB goblet squat", "Seated cable row", "Incline DB press", "Leg extension",
           "Rear delt fly", "Cable Pallof press", "45° back extension"]


def office_row(d: date, plan_id: int = 105) -> dict:
    r = next(x for x in office.build_rows() if x["plan_date"] == d)
    return {"plan_id": plan_id, "plan_date": d, "phase": r["phase"], "week_num": r["week_num"],
            "session_type": r["session_type"], "target_rpe": r["target_rpe"],
            "target_hr_zone": r["target_hr_zone"], "est_duration_min": r["est_duration_min"],
            "blocks": copy.deepcopy(r["blocks"]), "is_skipped": False}


# ----------------------------------------------------------------------------
# In-memory DB speaking exactly the SQL health_checkin uses
# ----------------------------------------------------------------------------

class FakeDB:
    def __init__(self, *rows):
        self.plan = {r["plan_date"]: copy.deepcopy(r) for r in rows}
        self.logs: list[dict] = []
        self.daily: dict[date, dict] = {}
        self.audit: list[tuple] = []

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
        elif s.startswith("SELECT 1 FROM health.daily_state"):
            self._rows = [(1,)] if params[0] in self.db.daily else []
        elif s.startswith("INSERT INTO health.daily_state"):
            d, w, sl, en, sore, rhr, ft = params
            old = self.db.daily.get(d, {})
            new = {"weight_lbs": w, "sleep_hrs": sl, "energy": en,
                   "soreness": json.loads(sore) if sore else None, "resting_hr": rhr, "free_text": ft}
            self.db.daily[d] = {k: (v if v is not None else old.get(k)) for k, v in new.items()}
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
        else:
            raise AssertionError(f"unhandled SQL: {s[:90]}")

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
    def setUp(self):
        self.db = FakeDB(office_row(FRI))
        self.cur = self.db.cursor()
        self.now = datetime(2026, 9, 18, 10, 10, tzinfo=timezone.utc)

    def checkin(self, text, **kw):
        return hc.process_checkin(self.cur, text, FRI, checkin_id="post-1", now=self.now,
                                  adjust=kw.get("adjust", True))

    def test_friday_row_is_session_b(self):
        row = self.db.plan[FRI]
        self.assertEqual(row["session_type"], "strength_b")
        self.assertEqual(names(self.db), B_NAMES)
        self.assertEqual(row["blocks"]["rounds"], 2)
        self.assertEqual(row["target_rpe"], 6.0)

    def test_1_all_clear_no_change_and_zero_stored(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("Slept 8 hours, energy 5, sore 0, weight 283")
        self.assertEqual(reply, "Check-in logged — run Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)
        st = self.db.daily[FRI]
        self.assertEqual(st["soreness"], {"overall": 0})
        self.assertEqual((st["sleep_hrs"], st["energy"], st["weight_lbs"]), (8.0, 5, 283.0))

    def test_2_shoulder_8_replaces_four_keeps_three_adds_four(self):
        reply = self.checkin("Slept 8, energy 5, sore shoulder 8/10, weight 283")
        got = names(self.db)
        for gone in ("DB goblet squat", "Seated cable row", "Incline DB press", "Rear delt fly"):
            self.assertNotIn(gone, got)
        for kept in ("Leg extension", "Cable Pallof press", "45° back extension"):
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
        # substitutes carry the week's sets and no shoulder work
        for ex in b["exercises"]:
            if ex.get("added_by") == "checkin":
                self.assertTrue(ex["notes"].startswith("2×"))
                self.assertFalse(regions.uses_any(ex["name"], ["shoulder"]))
        self.assertIn("Shoulder 8/10 → removed DB goblet squat, seated cable row, "
                      "incline DB press, rear delt fly. Added leg press, seated leg curl, "
                      "calf press, captain's chair knee raise.", reply)
        self.assertIn("Reply `original` to undo.", reply)
        self.assertNotRegex(reply.lower(), r"\b(ice|rest it|see a|doctor|physio|advice)\b")

    def test_3_legs_6_eases_goblet_and_extension(self):
        reply = self.checkin("slept 8 energy 5 legs sore 3")
        b = self.db.plan[FRI]["blocks"]
        by = {e["name"]: e for e in b["exercises"]}
        for n in ("DB goblet squat", "Leg extension"):
            self.assertEqual(by[n]["sets"], 1, n)
            self.assertEqual(by[n]["rpe_cap"], 5.0, n)
            self.assertTrue(by[n]["notes"].startswith("1×"), by[n]["notes"])
        for n in ("Seated cable row", "Incline DB press", "Rear delt fly",
                  "Cable Pallof press", "45° back extension"):
            self.assertNotIn("sets", by[n], n)
            self.assertNotIn("rpe_cap", by[n], n)
        self.assertEqual(names(self.db), B_NAMES)
        self.assertIn("Legs 6/10 → DB goblet squat and leg extension: 1 set, RPE ≤5.", reply)

    def test_4_two_heavy_regions_swap_to_z2(self):
        reply = self.checkin("slept 8 energy 4 sore shoulder 8 and legs 7")
        row = self.db.plan[FRI]
        self.assertEqual(row["session_type"], "cardio_z2")
        b = row["blocks"]
        self.assertEqual(b["type"], "steady")
        self.assertEqual(b["equipment"], ["recumbent bike"])
        self.assertTrue(20 <= b["duration_min"] <= 30)
        self.assertEqual(b["mobility_min"], 10)
        self.assertEqual(b["original"]["session_type"], "strength_b")
        self.assertIn("Recovery Z2 + Mobility", reply)

    def test_5_poor_sleep_lowers_rpe_only(self):
        self.checkin("slept 5 energy 2 sore 0")
        b = self.db.plan[FRI]["blocks"]
        self.assertEqual(names(self.db), B_NAMES)
        for ex in b["exercises"]:
            self.assertEqual(ex["rpe_cap"], 5.0, ex["name"])
            self.assertLessEqual(hc.exercise_sets(ex, b), 2)
        self.assertEqual(self.db.plan[FRI]["target_rpe"], 5.0)
        self.assertEqual(b["adjustment"]["rules_fired"], ["recovery"])

    def test_6_after_a_logged_set_store_only(self):
        self.db.logs.append({"plan_id": 105, "logged_via": "manual",
                             "log_type": "strength_set", "exercise": "DB goblet squat"})
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("Slept 8, energy 5, sore shoulder 8/10, weight 283")
        self.assertEqual(reply, "Logged.")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": 8})

    def test_7_original_restores(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("Slept 8, energy 5, sore shoulder 8/10, weight 283")
        self.assertNotEqual(self.db.plan[FRI], before)
        reply = hc.process_original(self.cur, FRI)
        self.assertEqual(self.db.plan[FRI]["blocks"], before["blocks"])
        self.assertEqual(self.db.plan[FRI]["session_type"], "strength_b")
        self.assertEqual(self.db.plan[FRI]["target_rpe"], 6.0)
        self.assertTrue(reply.startswith("Restored — run Session B as written."))
        self.assertEqual(hc.process_original(self.cur, FRI),
                         "No adjustment to undo — today's plan is as written.")

    def test_7b_swap_then_original_restores_row_fields(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("sore shoulder 8 and legs 7")
        hc.process_original(self.cur, FRI)
        self.assertEqual(self.db.plan[FRI], before)

    def test_8_nudge_at_0515_then_checkin_at_0520_still_adjusts(self):
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
        reply = self.checkin("Slept 8, energy 5, sore shoulder 8/10, weight 283")
        self.assertIn("removed", reply)
        self.assertIn("adjustment", self.db.plan[FRI]["blocks"])

    # ── extra coverage ──
    def test_recheckin_recomputes_from_original_not_stacked(self):
        self.checkin("sore shoulder 8/10")
        self.checkin("legs sore 3")
        b = self.db.plan[FRI]["blocks"]
        self.assertEqual(names(self.db), B_NAMES)       # shoulder change undone
        self.assertEqual({e["name"] for e in b["exercises"] if e.get("sets") == 1},
                         {"DB goblet squat", "Leg extension"})
        self.assertEqual([e["name"] for e in b["original"]["blocks"]["exercises"]], B_NAMES)

    def test_recheckin_all_clear_returns_to_as_written(self):
        before = copy.deepcopy(self.db.plan[FRI])
        self.checkin("sore shoulder 8/10")
        reply = self.checkin("sore 0")
        self.assertEqual(reply, "Check-in logged — back to Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)

    def test_flag_off_stores_but_never_adjusts(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("sore shoulder 8/10", adjust=False)
        self.assertEqual(reply, "Check-in logged — run Session B as written.")
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"shoulder": 8})
        self.assertEqual(self.db.audit[-1][2], "checkin_adjust_suppressed")

    def test_config_flag_default_on(self):
        from artemis import config
        self.assertTrue(config.CHECKIN_ADJUST)

    def test_pain_below_7_reduces_load_and_adds_mobility(self):
        reply = self.checkin("shoulder pain 5")
        b = self.db.plan[FRI]["blocks"]
        self.assertEqual(names(self.db), B_NAMES)
        by = {e["name"]: e for e in b["exercises"]}
        for n in ("DB goblet squat", "Seated cable row", "Incline DB press", "Rear delt fly"):
            self.assertEqual(by[n]["load_pct"], 80, n)
            self.assertEqual(by[n]["rpe_cap"], 5.0, n)
        self.assertNotIn("load_pct", by["Leg extension"])
        self.assertEqual(b["mobility_focus"], ["shoulder"])
        self.assertIn("load −20%", reply)
        self.assertIn("shoulder mobility", reply)

    def test_pain_7_or_more_replaces(self):
        self.checkin("tweaked my shoulder, pain 8")
        self.assertNotIn("Incline DB press", names(self.db))

    def test_unknown_region_is_flagged_and_ignored(self):
        before = copy.deepcopy(self.db.plan[FRI])
        reply = self.checkin("slept 8 energy 5 sore elbow 8")
        self.assertIn("Didn't recognize region elbow", reply)
        self.assertEqual(self.db.plan[FRI], before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"elbow": 8})

    def test_rules_never_add_volume_load_or_rpe(self):
        for text in ("sore shoulder 8/10", "legs sore 3", "slept 5 energy 2",
                     "shoulder pain 5", "sore shoulder 8 and legs 7", "slept 9 energy 5 sore 0"):
            db = FakeDB(office_row(FRI))
            hc.process_checkin(db.cursor(), text, FRI, checkin_id="x", now=self.now, adjust=True)
            b = db.plan[FRI]["blocks"]
            base = office_row(FRI)
            self.assertLessEqual(db.plan[FRI]["target_rpe"], base["target_rpe"], text)
            for ex in b.get("exercises") or []:
                self.assertLessEqual(hc.exercise_sets(ex, b), 2, text)
                self.assertLessEqual(ex.get("rpe_cap", 6.0), 6.0, text)
                self.assertLessEqual(ex.get("load_pct", 100), 100, text)
            self.assertLessEqual(db.plan[FRI]["est_duration_min"] or 0,
                                 base["est_duration_min"], text)

    def test_rest_day_no_adjustment(self):
        thu = date(2026, 9, 17)
        db = FakeDB(office_row(thu, plan_id=104))
        reply = hc.process_checkin(db.cursor(), "sore shoulder 9/10", thu, checkin_id="x")
        self.assertEqual(reply, "Check-in logged — rest day as planned.")
        self.assertNotIn("adjustment", db.plan[thu]["blocks"])

    def test_z2_day_recovery_cuts_duration(self):
        tue = date(2026, 9, 22)
        db = FakeDB(office_row(tue, plan_id=109))
        hc.process_checkin(db.cursor(), "slept 4 energy 3", tue, checkin_id="x", adjust=True)
        self.assertEqual(db.plan[tue]["blocks"]["duration_min"], 15)   # 20 → 15

    def test_monday_c_week5_legs_heavy_drops_finisher(self):
        mon = date(2026, 10, 19)
        row = office_row(mon, plan_id=140)
        self.assertIn("finisher", row["blocks"])
        db = FakeDB(row)
        hc.process_checkin(db.cursor(), "legs sore 8/10", mon, checkin_id="x", adjust=True)
        self.assertNotIn("finisher", db.plan[mon]["blocks"])


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
        hc.process_checkin(self.cur, "sore shoulder 8/10", FRI, checkin_id="x", adjust=True)
        self.assertEqual(hc.process_ack(self.cur, FRI),
                         "Got it — run the adjusted Session B. Reply `original` to go back.")

    def test_nudge_text(self):
        self.assertEqual(hc.nudge_text(office_row(FRI)), "No check-in yet — run Session B as written.")
        self.assertIsNone(hc.nudge_text(office_row(date(2026, 9, 17))))   # rest day
        self.assertIsNone(hc.nudge_text(None))


class TestParser(unittest.TestCase):
    def test_forms(self):
        cases = {
            "sore 0": {"overall": 0},
            "sore shoulder 8/10": {"shoulder": 8},
            "legs sore 3": {"legs": 6},
            "sore shoulder 8 and legs 7": {"shoulder": 8, "legs": 7},
            "shoulder and neck sore 6": {"shoulder": 6, "neck": 6},
            "quads sore 2, lower back sore 4/10": {"quads": 4, "low back": 4},
            "sore hip 3 out of 5": {"hip": 6},
            "no soreness": {"overall": 0},
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hc.parse_checkin(text).soreness, want)

    def test_pain_flag(self):
        ci = hc.parse_checkin("sharp pain in my lower back, knees a little sore 2")
        self.assertEqual(ci.pain, {"low back"})
        self.assertEqual(ci.soreness, {"low back": 7, "knee": 4})

    def test_every_region_word_is_recognized(self):
        for r in regions.REGIONS:
            with self.subTest(region=r):
                self.assertEqual(hc.parse_checkin(f"{r} sore 4/10").soreness, {r: 4})

    def test_the_real_0916_checkin(self):
        ci = hc.parse_checkin("Slept 6.5\nEnergy 5\nSore 0\nWeight 284.5")
        self.assertEqual((ci.sleep_hrs, ci.energy, ci.weight_lbs, ci.soreness),
                         (6.5, 5, 284.5, {"overall": 0}))

    def test_nothing_parsable(self):
        self.assertFalse(hc.parse_checkin("feeling ok I guess").has_data)

    def test_classify(self):
        cases = {
            "Nope": "ack", "all good": "ack", "no": "ack",
            "Workout completed.  Logged in App.": "done", "done": "done", "workout done": "done",
            "original": "original", "use original": "original",
            "Slept 8, energy 5, sore 0": "checkin",
            "done 1a2b3c": None, "done call Brad about the SOW": None,
            "good morning": None, "what's today's workout": None, "undo": None,
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
        self.assertTrue({"chest", "shoulder"} <= regions.regions_for("Incline DB press")[0])
        self.assertEqual(regions.regions_for("Cable Pallof press")[0], frozenset({"core"}))


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

    def test_rest_day_prompt_has_no_workout_later(self):
        from artemis.health import build_morning_survey_prompt
        text = build_morning_survey_prompt(office_row(date(2026, 9, 17)), "logging_only")
        self.assertNotIn("workout is later", text)


class TestSchedulerRegistry(unittest.TestCase):
    def test_nudge_registered_calibration_gone(self):
        from artemis.scheduler import ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        by_id = {c.id: c for c in s.cron_specs()}
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
             patch("artemis.wake.build_wake_message", return_value="W"), \
             patch("artemis.quiet_hours.exit_quiet"), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.__setitem__), \
             patch("artemis.scheduler._local_today", return_value=FRI), \
             patch.object(s.scheduler, "add_job") as add_job:
            s._do_wake()
        self.assertEqual(kv, {"checkin_open:2026-09-18": "open"})
        add_job.assert_not_called()


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
        self.db = FakeDB(office_row(date(2026, 9, 16), plan_id=103))
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
            patch("artemis.quiet_hours.local_today", return_value=date(2026, 9, 16)),
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
                     "_handle_ramp_confirm", "_handle_debrief_confirm",
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
        self.assertEqual(self.db.daily[date(2026, 9, 16)]["soreness"], {"overall": 0})
        r2 = self.send("Nope")
        self.assertEqual(r2, "Got it — run Session A as written.")
        r3 = self.send("Workout completed.  Logged in App. ")
        self.assertIn("Session A logged: 1 set across 1 exercise", r3)
        self.gmail.get_recent_messages.assert_not_called()
        self.gmail.get_full_message.assert_not_called()

    def test_ack_outside_the_window_is_not_claimed(self):
        from artemis import main
        with patch.object(main, "get_phase", return_value="open"), \
             patch("artemis.quiet_hours.get_system_value", return_value=None):
            self.assertFalse(main._handle_morning_flow(
                {"id": "x", "channel_id": "c1", "root_id": None}, "Nope"))


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

    def test_0916_invented_plan_is_rejected(self):
        draft = ("| 5 | **Rope Pushdown** (functional trainer) | 2 × 12 | Squeeze |\n"
                 "Target RPE 7–7.5 — same range as your last two sessions, which felt right.")
        v = find_violations(draft, self.EV)
        self.assertTrue(any("rope pushdown" in x for x in v), v)
        self.assertTrue(any("2 prior sessions" in x for x in v), v)

    def test_invented_prior_load_is_rejected(self):
        v = find_violations("Last session you did 185 lb on the leg press for 12 reps.", self.EV)
        self.assertTrue(any("185" in x for x in v), v)

    def test_plan_exact_text_passes(self):
        text = ("Today: leg press 2×10-12 at RPE 6, then the lat pulldown and seated leg curl. "
                "Last session your leg press was 160 lb for 12 reps. Body weight 284.5 lb.")
        self.assertEqual(find_violations(text, self.EV), [])

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
