"""STATUS-1 — GET /api/health/overview (the Status page).

Mocks the DB session; no AWS or RDS.

Run:
    python tests/api/test_overview.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

VALID_KEY = "test-health-api-key-xyz"
ANCHOR = date(2026, 9, 16)
TODAY = date(2026, 9, 18)          # Fri, week 1


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


def circuit(name, exercises, **extra):
    b = {"type": "circuit", "display_name": name, "location": "office gym", "rounds": 2,
         "exercises": [{"name": n, "format": "reps", "notes": "2×8-12"} for n in exercises]}
    b.update(extra)
    return b


def plan(pid, d, st, blocks, phase=1, week=1, **extra):
    row = {"plan_id": pid, "plan_date": d, "phase": phase, "week_num": week, "session_type": st,
           "blocks": blocks, "target_rpe": 6.0, "est_duration_min": 40, "is_skipped": False,
           "status": "planned"}
    row.update(extra)
    return row


def log(pid, exercise, weight=25.0, reps=10, log_type="strength_set", **extra):
    row = {"plan_id": pid, "log_type": log_type, "exercise": exercise, "set_num": 1,
           "reps_done": reps, "weight_lbs": weight, "duration_sec": None, "rpe_actual": None,
           "notes": None, "is_skipped": False, "logged_via": "manual", "log_id": 0}
    row.update(extra)
    return row


A = ["Leg press", "DB bench press"]
B = ["DB goblet squat", "Incline DB press"]
FLOW = {"type": "recovery_flow", "display_name": "Recovery Flow", "location": "office gym",
        "total_sec": 2250, "rounds": 2, "flow": []}

PLANS = [
    # retired home program — must never appear
    plan(1, date(2026, 9, 11), "cardio_z2", {"type": "steady", "display_name": "Long Z2 Bike",
                                             "duration_min": 60}, phase=3, week=14),
    plan(2, date(2026, 9, 12), "strength_a", circuit("Home A", ["Leg press"]), phase=3, week=14),
    plan(3, date(2026, 9, 13), "walk", {"type": "steady", "display_name": "Run-Walk"}, phase=3, week=15),
    # current program week 1 (Wed 9/16 – Tue 9/22)
    plan(10, date(2026, 9, 16), "strength_a", circuit("Office Strength A", A)),
    plan(11, date(2026, 9, 17), "rest_mobility", FLOW, est_duration_min=38),
    plan(12, TODAY, "strength_b", circuit("Office Strength B", B, adjustment={
        "summary": ["Pain shoulder 2/5 → incline DB press 20 lb (last 25)."],
        "rules_fired": ["pain_lighter"]})),
    plan(13, date(2026, 9, 19), "rest_mobility", {"type": "mobility", "display_name": "Rest / Mobility"}),
    plan(14, date(2026, 9, 20), "walk", {"type": "steady", "display_name": "Walk", "duration_min": 30},
         est_duration_min=30),
    plan(15, date(2026, 9, 21), "strength_c", circuit("Office Strength C", ["Pec fly"])),
    plan(16, date(2026, 9, 22), "cardio_z2", {"type": "steady", "display_name": "Zone 2 Cardio"},
         est_duration_min=20),
]

LOGS = [
    # old program: a heavy leg press that must not count
    log(2, "Leg press", 300, 12, rpe_actual=9, notes="right knee tweak"),
    # 9/16 done: 4 of 4 sets, one RPE over cap, a machine setting
    log(10, "Leg press", 160, 12, notes="setting=4"), log(10, "Leg press", 170, 10, rpe_actual=8),
    log(10, "DB bench press", 30, 10), log(10, "DB bench press", 30, 9),
    # 9/17 flow partial
    log(11, None, None, None, log_type="session_summary", duration_sec=840,
        notes="recovery_flow: partial 14 of 38 min"),
    # today: 1 set so far, with a pain chip
    log(12, "Incline DB press", 20, 10, notes="setting=2; pain=shoulder:2"),
]

DAILY = {
    date(2026, 9, 12): {"weight_lbs": 290.0, "sleep_hrs": 7.0, "energy": 3,
                        "soreness": {"pain": {"knee": 3}}},                      # pre-anchor
    date(2026, 9, 16): {"weight_lbs": 286.0, "sleep_hrs": 6.5, "energy": 5, "soreness": {"overall": 0}},
    date(2026, 9, 17): {"weight_lbs": None, "sleep_hrs": 8.0, "energy": 4,
                        "soreness": {"legs": 3, "pain": {"shoulder": 1}}},
    TODAY: {"weight_lbs": 284.5, "sleep_hrs": 7.5, "energy": 4, "resting_hr": 58,
            "soreness": {"quads": 2, "pain": {"shoulder": 2}}},
}


class FakeSession:
    def __init__(self, plans=PLANS, logs=LOGS, daily=DAILY, program_state=None,
                 patterns=None, reflections=None):
        self.plans, self.logs, self.daily = plans, logs, daily
        self.state, self.patterns, self.reflections = program_state, patterns, reflections

    def execute(self, clause, params=None):
        sql = " ".join(str(clause).split())
        p = params or {}
        if "acos.timezone_overrides" in sql:
            return _Result([])
        if "acos.system_state" in sql:
            return _Result([{"value": json.dumps(self.state)}] if self.state else [])
        if "to_regclass" in sql:
            has = self.patterns is not None
            return _Result([{"t": "health.pain_pattern" if has else None,
                             "r": "health.reflection" if has else None}])
        if "FROM health.pain_pattern" in sql:
            rows = []
            for r in self.patterns or []:
                refl = [x["created_at"] for x in self.reflections or [] if x["pattern_id"] == r["id"]]
                rows.append({**r, "last_reflection_at": max(refl) if refl else None})
            return _Result([r for r in rows if r["status"] == "open" and r["qualifies"]])
        if "FROM health.phase_config" in sql:
            return _Result([{"phase_name": "Foundation"}] if p["p"] == 1 else [{"phase_name": "Peak"}])
        if sql.startswith("SELECT phase, week_num, plan_date FROM health.plan"):
            rows = sorted([x for x in self.plans if x["plan_date"] <= p["t"]], key=lambda x: x["plan_date"])
            return _Result(rows[-1:][::-1])
        if "min(plan_date) AS anchor" in sql:
            ds = [x["plan_date"] for x in self.plans if x["phase"] == p["p"] and x["week_num"] == 1
                  and p["lo"] < x["plan_date"] <= p["t"]]
            return _Result([{"anchor": min(ds) if ds else None}])
        if "max(week_num) AS weeks" in sql:
            ws = [x["week_num"] for x in self.plans if x["phase"] == p["p"] and x["plan_date"] >= p["a"]]
            return _Result([{"weeks": max(ws) if ws else None}])
        if "max(plan_date) AS d FROM health.plan" in sql:
            ds = [x["plan_date"] for x in self.plans if x["plan_date"] < p["a"]]
            return _Result([{"d": max(ds) if ds else None}])
        if "FROM health.plan WHERE plan_date BETWEEN" in sql:
            return _Result(sorted([x for x in self.plans if p["s"] <= x["plan_date"] <= p["e"]],
                                  key=lambda x: x["plan_date"]))
        if "FROM health.session_log sl JOIN health.plan p" in sql:
            dates = {x["plan_id"]: x["plan_date"] for x in self.plans}
            return _Result([{"exercise": l["exercise"], "plan_date": dates[l["plan_id"]],
                             "weight_lbs": l["weight_lbs"], "reps_done": l["reps_done"], "notes": l["notes"]}
                            for l in self.logs
                            if l["log_type"] == "strength_set" and l["logged_via"] != "inferred"
                            and not l["is_skipped"] and l["exercise"] in p["names"]
                            and p["s"] <= dates[l["plan_id"]] <= p["t"]])
        if "FROM health.session_log" in sql:
            return _Result([l for l in self.logs if l["plan_id"] in p["pids"]])
        if "FROM health.daily_state WHERE state_date = :d" in sql:
            r = self.daily.get(p["d"])
            return _Result([{"state_date": p["d"], "resting_hr": None, **r}] if r else [])
        if "weight_lbs AS v" in sql:
            return _Result([{"d": d, "v": r["weight_lbs"]} for d, r in sorted(self.daily.items())
                            if p["s"] <= d <= p["t"] and r.get("weight_lbs") is not None])
        if "FROM health.daily_state WHERE state_date BETWEEN" in sql:
            return _Result([{"state_date": d, "resting_hr": None, **r} for d, r in sorted(self.daily.items())
                            if p["s"] <= d <= p["t"]])
        raise AssertionError(f"unexpected SQL: {sql[:90]}")


class Base(unittest.TestCase):
    def overview(self, session=None, now=None):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            raise unittest.SkipTest("fastapi not installed")
        from api.app.database import get_db
        from api.app.main import app
        from api.app.routers import health as h
        self.session = session or FakeSession()

        def _db():
            yield self.session
        app.dependency_overrides[get_db] = _db
        h._HEALTH_API_KEY = VALID_KEY
        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(setattr, h, "_HEALTH_API_KEY", None)
        real = datetime
        fixed = now or real(2026, 9, 18, 17, 0, tzinfo=timezone.utc)

        class _DT(real):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        p = patch.object(h, "datetime", _DT)
        p.start()
        self.addCleanup(p.stop)
        client = TestClient(app)
        self.client = client
        r = client.get("/api/health/overview", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestProgram(Base):
    def test_401_without_key(self):
        self.overview()
        self.assertEqual(self.client.get("/api/health/overview").status_code, 401)

    def test_program_header_derived_from_plan_rows(self):
        prog = self.overview()["program"]
        self.assertEqual(prog["source"], "derived")
        self.assertEqual((prog["name"], prog["phase"], prog["week"], prog["weeks_total"]),
                         ("Foundation", 1, 1, 1))
        self.assertEqual((prog["anchor"], prog["week_start"], prog["week_end"]),
                         ("2026-09-16", "2026-09-16", "2026-09-22"))
        # Sessions exclude rest days; the flow day counts (partial, not done).
        self.assertEqual((prog["sessions_done"], prog["sessions_planned"]), (1, 6))

    def test_program_state_wins(self):
        state = {"name": "Foundation", "phase": 1, "anchor": "2026-09-16", "weeks_total": 7,
                 "deload_week": 7}
        prog = self.overview(FakeSession(program_state=state))["program"]
        self.assertEqual((prog["source"], prog["weeks_total"], prog["deload_week"], prog["weeks_to_deload"]),
                         ("state", 7, 7, 6))
        later = self.overview(FakeSession(program_state=state),
                              now=datetime(2026, 10, 30, 17, 0, tzinfo=timezone.utc))["program"]
        self.assertEqual((later["week"], later["weeks_to_deload"], later["week_start"]),
                         (7, 0, "2026-10-28"))

    def test_scoping_excludes_the_old_program(self):
        body = self.overview()
        dates = [d["plan_date"] for d in body["week_days"]]
        self.assertEqual(dates[0], "2026-09-16")
        self.assertEqual(len(dates), 7)
        for f in body["flags"]:
            self.assertGreaterEqual(f["date"], "2026-09-16", f)
            self.assertNotIn("tweak", f["text"])
        self.assertTrue(all(c["date"] >= "2026-09-16" for c in body["checkins_14d"]))
        lp = next(r for r in body["strength_progress"] if r["exercise"] == "Leg press")
        self.assertEqual((lp["best"]["weight_lbs"], lp["best"]["reps"]), (160.0, 12),
                         "the old 300 lb set is out of scope")
        self.assertEqual(body["previous_program_end"], "2026-09-13")
        # Body weight is NOT scoped.
        self.assertEqual(body["weight_30d"][0], {"date": "2026-09-12", "value": 290.0})


class TestToday(Base):
    def test_today_progress_checkin_adjustment(self):
        t = self.overview()["today"]
        self.assertEqual(t["date"], "2026-09-18")
        self.assertEqual(t["day"]["display_name"], "Office Strength B")
        self.assertEqual(t["day"]["status"], "partial")
        self.assertEqual(t["progress"], {"unit": "sets", "done": 1, "planned": 4})
        self.assertEqual(t["checkin"], {"date": "2026-09-18", "sleep_hrs": 7.5, "energy": 4,
                                        "weight_lbs": 284.5, "resting_hr": 58,
                                        "soreness": {"quads": 2}, "pain": {"shoulder": 2}})
        self.assertEqual(t["adjustment"]["rules_fired"], ["pain_lighter"])

    def test_no_checkin_yet(self):
        daily = {k: v for k, v in DAILY.items() if k != TODAY}
        self.assertIsNone(self.overview(FakeSession(daily=daily))["today"]["checkin"])

    def test_progress_unit_per_session_type(self):
        from api.app.routers.health import PlanDay, progress_for

        def day(st, blocks, est=40):
            return PlanDay(plan_id=1, plan_date=TODAY, session_type=st, phase=1, week_num=1,
                           est_duration_min=est, status="today", blocks=blocks)
        strength = day("strength_b", circuit("B", B))
        self.assertEqual(progress_for(strength, [log(1, "DB goblet squat"), log(1, "X", is_skipped=True),
                                                 log(1, "Y", logged_via="inferred")]).model_dump(),
                         {"unit": "sets", "done": 1, "planned": 4})
        flow = day("recovery_flow", FLOW, 38)
        self.assertEqual(progress_for(flow, [log(1, None, log_type="session_summary", duration_sec=2250,
                                                 notes="recovery_flow: complete 37 min")]).model_dump(),
                         {"unit": "minutes", "done": 38, "planned": 38})
        self.assertEqual(progress_for(flow, []).model_dump(), {"unit": "minutes", "done": 0, "planned": 38})
        walk = day("walk", {"type": "steady", "duration_min": 30}, 30)
        self.assertEqual(progress_for(walk, [log(1, "Walk", log_type="cardio_block", duration_sec=1260)])
                         .model_dump(), {"unit": "minutes", "done": 21, "planned": 30})
        z2 = day("cardio_z2", {"type": "steady"}, 20)
        self.assertEqual(progress_for(z2, []).model_dump()["unit"], "minutes")
        rest = day("rest_mobility", {"type": "mobility"})
        self.assertEqual(progress_for(rest, []).model_dump(), {"unit": "rest", "done": None, "planned": None})
        day_off = day("rest_mobility", {"type": "mobility", "display_name": "Day off", "duration_min": 0})
        self.assertEqual(progress_for(day_off, []).unit, "rest")


class TestStrengthProgress(Base):
    def test_rows_last_previous_best_setting(self):
        rows = {r["exercise"]: r for r in self.overview()["strength_progress"]}
        # Program week's exercises, as written, in plan order.
        self.assertEqual(list(rows), ["Leg press", "DB bench press", "DB goblet squat",
                                      "Incline DB press", "Pec fly"])
        lp = rows["Leg press"]
        # Top set by load × reps: 160 × 12 (1920) beats 170 × 10 (1700).
        self.assertEqual(lp["last"], {"date": "2026-09-16", "weight_lbs": 160.0, "reps": 12, "score": 1920.0})
        self.assertIsNone(lp["previous"])
        self.assertIsNone(lp["trend"])
        self.assertEqual(lp["setting"], 4.0)
        self.assertEqual(rows["Incline DB press"]["setting"], 2.0)
        self.assertEqual(rows["Pec fly"]["sessions"], 0)
        self.assertIsNone(rows["Pec fly"]["last"])

    def test_trend_calculation(self):
        from api.app.routers.health import TopSet, strength_progress, trend_of

        def ts(score):
            return TopSet(date=TODAY, score=score)
        self.assertEqual(trend_of(ts(1100), ts(1000)), "up")
        self.assertEqual(trend_of(ts(1015), ts(1000)), "flat")      # within 2%
        self.assertEqual(trend_of(ts(985), ts(1000)), "flat")
        self.assertEqual(trend_of(ts(900), ts(1000)), "down")
        self.assertIsNone(trend_of(ts(900), None))
        self.assertEqual(trend_of(ts(10), ts(0)), "up")
        d1, d2, d3 = date(2026, 9, 16), date(2026, 9, 23), date(2026, 9, 30)
        rows = [
            {"exercise": "Leg press", "plan_date": d1, "weight_lbs": 160, "reps_done": 12, "notes": None},
            {"exercise": "Leg press", "plan_date": d2, "weight_lbs": 180, "reps_done": 12, "notes": "setting=5"},
            {"exercise": "Leg press", "plan_date": d2, "weight_lbs": 190, "reps_done": 6, "notes": None},
            {"exercise": "Leg press", "plan_date": d3, "weight_lbs": 170, "reps_done": 10, "notes": None},
            # bodyweight: reps only
            {"exercise": "Captain's chair knee raise", "plan_date": d1, "weight_lbs": None, "reps_done": 10, "notes": None},
            {"exercise": "Captain's chair knee raise", "plan_date": d2, "weight_lbs": None, "reps_done": 12, "notes": None},
        ]
        out = {r.exercise: r for r in strength_progress(["Leg press", "Captain's chair knee raise"], rows)}
        lp = out["Leg press"]
        self.assertEqual((lp.last.date, lp.last.score), (d3, 1700.0))
        self.assertEqual((lp.previous.date, lp.previous.weight_lbs, lp.previous.reps), (d2, 180.0, 12))
        self.assertEqual(lp.trend, "down")
        self.assertEqual((lp.best.date, lp.best.score), (d2, 2160.0))
        self.assertEqual(lp.setting, 5.0)
        self.assertEqual(lp.sessions, 3)
        self.assertEqual(out["Captain's chair knee raise"].trend, "up")


class TestPatternsFlagsWeight(Base):
    def test_patterns_tolerate_a_missing_table(self):
        self.assertEqual(self.overview(FakeSession(patterns=None))["patterns"], [])

    def test_open_patterns_with_reflection_date(self):
        pats = [
            {"id": 1, "exercise": "Incline DB press", "region": "shoulder", "hits": 3, "exposures": 4,
             "status": "open", "qualifies": True, "evidence": {"shared": ["Rear delt fly"]}},
            {"id": 2, "exercise": "Leg press", "region": "knee", "hits": 3, "exposures": 3,
             "status": "dismissed", "qualifies": True, "evidence": {}},
        ]
        refl = [{"pattern_id": 1, "created_at": datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)}]
        out = self.overview(FakeSession(patterns=pats, reflections=refl))["patterns"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "shoulder pain ≥2 after Incline DB press — 3 of 4 sessions "
                                         "(also that day: Rear delt fly)")
        self.assertTrue(out[0]["last_reflection_at"].startswith("2026-09-20T13:00"))
        self.assertEqual(self.overview(FakeSession(patterns=[]))["patterns"], [])

    def test_flags_are_plain_language_and_current_program(self):
        flags = self.overview()["flags"]
        texts = [f["text"] for f in flags]
        self.assertEqual(texts, [
            "Pain chip: shoulder 2 on Incline DB press (9/18)",
            "Partial: Recovery Flow (9/17) — 14 of 38 min",
            "RPE 8 on Leg press (9/16), cap was 6",
        ])
        self.assertEqual([f["kind"] for f in flags], ["pain", "partial", "rpe"])

    def test_missed_and_keyword_pain_flags(self):
        plans = PLANS + [plan(17, date(2026, 9, 15), "strength_b", circuit("Old", B), phase=3, week=15)]
        logs = LOGS + [log(10, "DB bench press", 30, 8, notes="left shoulder hurt a bit")]
        now = datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc)   # week 2; 9/18–9/22 now past
        body = self.overview(FakeSession(plans=plans, logs=logs), now=now)
        texts = [f["text"] for f in body["flags"]]
        self.assertIn("Missed: Walk (9/20)", texts)
        self.assertIn("Missed: Office Strength C (9/21)", texts)
        self.assertIn("Partial: Office Strength B (9/18) — 1 of 4 sets", texts)
        self.assertIn("Pain note on DB bench press (9/16): “left shoulder hurt a bit”", texts)
        self.assertNotIn("Missed: Rest / Mobility (9/19)", texts)
        self.assertFalse([t for t in texts if "Old" in t])

    def test_weight_with_zero_one_and_many_points(self):
        from api.app.routers.health import TrendPoint, weight_summary
        self.assertIsNone(weight_summary([]))
        one = weight_summary([TrendPoint(date=TODAY, value=284.5)])
        self.assertEqual((one.first.value, one.latest.value, one.change), (284.5, 284.5, 0.0))
        many = self.overview()
        self.assertEqual([p["value"] for p in many["weight_30d"]], [290.0, 286.0, 284.5])
        self.assertEqual(many["weight_summary"]["change"], -5.5)
        none = self.overview(FakeSession(daily={}))
        self.assertEqual((none["weight_30d"], none["weight_summary"]), ([], None))
        self.assertIsNone(none["today"]["checkin"])
        self.assertEqual(none["checkins_14d"], [])

    def test_checkins_carry_soreness_and_pain_apart(self):
        cis = {c["date"]: c for c in self.overview()["checkins_14d"]}
        self.assertEqual(list(cis), ["2026-09-16", "2026-09-17", "2026-09-18"])
        self.assertEqual((cis["2026-09-17"]["soreness"], cis["2026-09-17"]["pain"]),
                         ({"legs": 3}, {"shoulder": 1}))
        self.assertEqual(cis["2026-09-16"]["soreness"], {"overall": 0})


class TestNoProgram(Base):
    def test_empty_database(self):
        body = self.overview(FakeSession(plans=[], logs=[], daily={}))
        self.assertIsNone(body["program"])
        self.assertEqual(body["week_days"], [])
        self.assertIsNone(body["today"]["day"])
        self.assertEqual(body["flags"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
