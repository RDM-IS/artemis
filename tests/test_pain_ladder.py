"""PAIN-1 — pain ladder, rising-pain rule, in-session pain notes, patterns.

Runs the REAL process_* handlers and pattern code against the in-memory
FakeDB from test_checkin_adjust (seeded with the office rows). No RDS.

Run:
    python3.11 tests/test_pain_ladder.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import copy
import json
import sys
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_checkin_adjust import B_NAMES, FRI, FakeDB, names, office_row, rest_row  # noqa: E402

from artemis import health_checkin as hc  # noqa: E402
from artemis import health_patterns as hp  # noqa: E402
from artemis import health_regions as hr
from knowledge import load_config  # noqa: E402

NOW = datetime(2026, 9, 18, 10, 10, tzinfo=timezone.utc)
MON = date(2026, 9, 21)   # SCHEDULE-2: Strength A day (office)
SUN = date(2026, 9, 20)   # msp_home — Recovery Flow
ADVICE = r"\b(ice|rest it|see a|doctor|physio|advice|careful|stop if|listen to|consult)\b"


def log(plan_id, exercise, weight=None, notes=None, skipped=False, via="manual"):
    return {"plan_id": plan_id, "exercise": exercise, "log_type": "strength_set",
            "logged_via": via, "weight_lbs": weight, "notes": notes, "is_skipped": skipped}


def checkin_row(pain=None, sore=None):
    s = dict(sore or {})
    if pain is not None:
        s["pain"] = pain
    return {"weight_lbs": None, "sleep_hrs": None, "energy": None,
            "soreness": s or None, "resting_hr": None, "free_text": None}


class Base(unittest.TestCase):
    day = FRI

    def setUp(self):
        self.db = FakeDB(office_row(self.day))
        self.cur = self.db.cursor()

    def checkin(self, text, day=None):
        reply = hc.process_checkin(self.cur, text, day or self.day, checkin_id="post-1",
                                   now=NOW, adjust=True)
        self.assertNotRegex(reply.lower(), ADVICE)
        return reply

    def row(self, day=None):
        return self.db.plan[day or self.day]

    def by_name(self):
        return {e["name"]: e for e in self.row()["blocks"]["exercises"]}


# ============================================================================
# §1 Ladder
# ============================================================================

class TestLadder(Base):
    def test_p1_pain_4_day_off(self):
        reply = self.checkin("slept 8 energy 4 shoulder pain 4")
        self.assertEqual(reply, "Pain shoulder 4/5 → day off. Reply `original` to undo.")
        row = self.row()
        self.assertEqual(row["session_type"], "rest_mobility")
        self.assertEqual((row["target_rpe"], row["est_duration_min"]), (None, 0))
        b = row["blocks"]
        self.assertEqual((b["type"], b["display_name"], b["duration_min"]), ("mobility", "Day off", 0))
        self.assertNotIn("exercises", b)
        self.assertEqual(b["adjustment"]["rules_fired"], ["pain_day_off"])
        self.assertEqual(b["original"]["session_type"], "strength_b")

    def test_p1_two_regions_named_in_order(self):
        reply = self.checkin("legs pain 5, shoulder pain 4")
        self.assertEqual(reply, "Pain legs 5/5 + shoulder 4/5 → day off. Reply `original` to undo.")

    def test_p1_beats_soreness_day_swap(self):
        self.checkin("sore shoulder 4 and legs 4, knee pain 5")
        self.assertEqual(self.row()["session_type"], "rest_mobility")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["pain_day_off"])

    def test_p1_no_nudge_and_ack(self):
        self.checkin("shoulder pain 5")
        plan = hc.load_plan(self.cur, FRI)
        self.assertIsNone(hc.nudge_text(plan))
        self.assertEqual(hc.process_ack(self.cur, FRI),
                         "Got it — Day off today. Reply `original` to go back.")

    def test_p1_original_undo_and_recompute_from_original(self):
        self.checkin("shoulder pain 4")
        self.assertEqual(hc.process_original(self.cur, FRI), "Restored — run Session B as written.")
        self.assertEqual(names(self.db), B_NAMES)
        self.checkin("shoulder pain 5")
        # A newer, milder check-in recomputes from the plan as written.
        reply = self.checkin("shoulder pain 1")
        self.assertEqual(reply, "Pain shoulder 1/5 — noted.\n"
                                "Check-in logged — back to Session B as written.")
        self.assertEqual(self.row()["session_type"], "strength_b")
        self.assertEqual(names(self.db), B_NAMES)
        self.assertNotIn("adjustment", self.row()["blocks"])

    def test_p1_on_a_flow_day(self):
        """Was test_p1_on_walk_day. Sunday is no longer a walk — WALK-RETIRE
        (2026-09-22) made walking activity, never a planned session, so the
        scenario the old name described cannot occur. The live low-intensity
        day is the evening recovery flow, where only the day-off rules apply
        (health_checkin.FLOW_TYPE), so the coverage moves there."""
        sun = date(2026, 9, 20)
        self.db = FakeDB(office_row(sun, plan_id=107, slot="evening"))
        self.cur = self.db.cursor()
        reply = self.checkin("knee pain 4", day=sun)
        self.assertEqual(reply, "Pain knee 4/5 → day off. Reply `original` to undo.")
        self.assertEqual(self.row(sun)["session_type"], "rest_mobility")

    def test_rest_day_pain_changes_nothing(self):
        thu = date(2026, 9, 17)
        self.db = FakeDB(rest_row(thu))
        self.cur = self.db.cursor()
        self.assertEqual(self.checkin("shoulder pain 5", day=thu),
                         "Check-in logged — rest day as planned.")

    def test_p3_whole_day_mobility_when_half_or_more_are_primary(self):
        # Legs + shoulder are the PRIMARY region of 4 of Session B's 7
        # (goblet squat, leg extension; incline press, rear delt fly) -> whole day.
        reply = self.checkin("legs pain 3, shoulder pain 3")
        self.assertEqual(reply, "Pain legs 3/5 + shoulder 3/5 → today is Mobility / Yoga: 30 min, "
                                "Stretch Trainer + mat.\nReply `original` to undo.")
        row = self.row()
        self.assertEqual((row["session_type"], row["est_duration_min"], row["target_rpe"]),
                         ("rest_mobility", 30, None))
        b = row["blocks"]
        self.assertEqual((b["type"], b["display_name"], b["duration_min"]),
                         ("mobility", "Mobility / Yoga", 30))
        self.assertEqual(b["equipment"], ["Stretch Trainer", "mat"])
        self.assertEqual(b["mobility_focus"], ["legs", "shoulder"])
        self.assertTrue(25 <= b["duration_min"] <= 30)

    def test_p3_shoulder_on_session_b_primary_mobility_secondary_substituted(self):
        # Shoulder is PRIMARY on 2 of 7 (incline press, rear delt fly) — under
        # half, so no mobility day. Primary -> shoulder mobility block;
        # secondary-only (goblet squat, seated row) -> pool substitutes.
        reply = self.checkin("shoulder pain 3")
        row = self.row()
        self.assertEqual(row["session_type"], "strength_b")
        b = row["blocks"]
        self.assertEqual(b["adjustment"]["rules_fired"], ["pain_mobility", "pain_substitute"])
        self.assertEqual(names(self.db), ["Leg press", "Seated leg curl", "Leg extension",
                                          "Cable Pallof press", "Seated back extension"])
        by = self.by_name()
        self.assertEqual((by["Leg press"]["added_by"], by["Leg press"]["replaces"]),
                         ("checkin", "DB goblet squat"))
        self.assertEqual(by["Seated leg curl"]["replaces"], "Seated cable row")
        for sub in ("Leg press", "Seated leg curl"):
            self.assertFalse(hr.uses_any(sub, ["shoulder"]), sub)
            self.assertTrue(by[sub]["notes"].startswith("2×"), by[sub]["notes"])
        for kept in ("Leg extension", "Cable Pallof press", "Seated back extension"):
            self.assertNotIn("added_by", by[kept])
        self.assertEqual((b["mobility_focus"], b["mobility_min"]), (["shoulder"], 10))
        self.assertIn("leg press", b["equipment"])
        self.assertIn("leg curl", b["equipment"])
        self.assertEqual(b["adjustment"]["removed"],
                         ["Incline DB press", "Rear delt fly", "DB goblet squat", "Seated cable row"])
        self.assertEqual(b["adjustment"]["added"], ["Leg press", "Seated leg curl"])
        self.assertEqual(reply, "Pain shoulder 3/5 → removed incline DB press and rear delt fly. "
                                "Added 10 min shoulder mobility (Stretch Trainer + mat). "
                                "Swapped DB goblet squat → leg press, seated cable row → seated "
                                "leg curl.\nReply `original` to undo.")

    def test_p3_secondary_falls_back_to_mobility_when_no_substitute_fits(self):
        # Hip is secondary-only on B (goblet squat, back extension). With legs
        # sore too, every pool exercise touches hip or legs -> both go to the
        # hip mobility block.
        reply = self.checkin("hip pain 3, legs sore 2")
        b = self.row()["blocks"]
        self.assertEqual(b["adjustment"]["rules_fired"], ["pain_mobility", "lighten_sore"])
        self.assertEqual(names(self.db), ["Seated cable row", "Incline DB press", "Leg extension",
                                          "Rear delt fly", "Cable Pallof press"])
        self.assertFalse(any(e.get("added_by") for e in b["exercises"]))
        self.assertEqual((b["mobility_focus"], b["mobility_min"]), (["hip"], 10))
        self.assertTrue(reply.startswith("Pain hip 3/5 → removed DB goblet squat and seated back "
                                         "extension. Added 10 min hip mobility"), reply)

    def test_p3_secondary_only_region_needs_no_mobility_block(self):
        # Knee is secondary-only on B (goblet squat, leg extension); both get
        # substitutes, so there is nothing to put in a mobility block.
        reply = self.checkin("knee pain 3")
        b = self.row()["blocks"]
        self.assertEqual(b["adjustment"]["rules_fired"], ["pain_substitute"])
        self.assertNotIn("mobility_min", b)
        for ex in b["exercises"]:
            self.assertFalse(hr.uses_any(ex["name"], ["knee"]), ex["name"])
        self.assertEqual(len(b["exercises"]), 7)
        self.assertTrue(reply.startswith("Pain knee 3/5 → swapped DB goblet squat → "), reply)

    def test_p3_legs_on_a_z2_day_is_a_mobility_day(self):
        tue = date(2026, 9, 22)   # SCHEDULE-2: Z2 is the office Tuesday
        self.db = FakeDB(office_row(tue, plan_id=108))
        self.cur = self.db.cursor()
        self.checkin("legs pain 3", day=tue)
        self.assertEqual(self.row(tue)["blocks"]["display_name"], "Mobility / Yoga")

    def test_p3_beats_soreness_day_swap(self):
        self.checkin("sore legs 4 and back 4, legs pain 3, shoulder pain 3")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["pain_mobility_day"])

    def test_p3_region_mobility_and_substitute(self):
        # Low back: back extension (primary) -> mobility; goblet squat
        # (secondary) -> leg press.
        reply = self.checkin("low back pain 3")
        self.assertEqual(names(self.db), ["Leg press", "Seated cable row", "Incline DB press",
                                          "Leg extension", "Rear delt fly", "Cable Pallof press"])
        b = self.row()["blocks"]
        self.assertEqual((b["mobility_focus"], b["mobility_min"]), (["low back"], 10))
        self.assertIn("Stretch Trainer", b["equipment"])
        self.assertIn("mat", b["equipment"])
        self.assertEqual(b["mobility_notes"], hr.MOBILITY["low back"])
        self.assertEqual(reply, "Pain low back 3/5 → removed seated back extension. Added 10 min "
                                "low back mobility (Stretch Trainer + mat). Swapped DB goblet "
                                "squat → leg press.\nReply `original` to undo.")
        self.assertEqual(self.row()["session_type"], "strength_b")

    def test_p3_two_regions_is_15_minutes(self):
        self.checkin("low back pain 3, core pain 3")
        b = self.row()["blocks"]
        self.assertEqual(b["mobility_focus"], ["low back", "core"])
        self.assertEqual(b["mobility_min"], 15)
        self.assertNotIn("Cable Pallof press", names(self.db))

    def test_p3_then_soreness_replace_avoids_pain_region(self):
        # Pain-3 mobility first, then soreness 4 replace on what's left; the
        # refill never uses the painful region.
        self.checkin("low back pain 3, sore triceps 4")
        rules = self.row()["blocks"]["adjustment"]["rules_fired"]
        self.assertEqual(rules, ["pain_mobility", "pain_substitute", "replace"])
        got = names(self.db)
        self.assertNotIn("Incline DB press", got)
        for ex in self.row()["blocks"]["exercises"]:
            if ex.get("added_by") == "checkin":
                self.assertFalse(hr.uses_any(ex["name"], ["low back", "triceps"]), ex["name"])
        self.assertNotIn("Seated back extension", got, "pain-removed exercise must not come back")

    def test_p4_pain_2_lighter_from_last_logged_load(self):
        prev = date(2026, 9, 11)
        self.db.plan_dates[90] = prev
        self.db.logs += [log(90, "Incline DB press", 25), log(90, "Incline DB press", 22.5),
                         log(90, "Rear delt fly", 70), log(90, "Rear delt fly", 60)]
        reply = self.checkin("shoulder pain 2")
        by = self.by_name()
        self.assertEqual(by["Incline DB press"]["target_load_lbs"], 20.0)   # 80% of 25
        self.assertEqual(by["Incline DB press"]["load_from"], 25.0)
        self.assertEqual(by["Rear delt fly"]["target_load_lbs"], 50.0)      # 56 -> 50 (stack)
        for n in ("DB goblet squat", "Seated cable row", "Leg extension"):
            self.assertIsNone(by[n].get("target_load_lbs"), n)               # secondary / other
            self.assertNotIn("load_from", by[n])
        for ex in by.values():
            self.assertNotIn("load_pct", ex)
            self.assertNotIn("sets", ex)
            self.assertNotIn("rpe_cap", ex)
        self.assertNotIn("mobility_min", self.row()["blocks"])
        self.assertEqual(names(self.db), B_NAMES)
        self.assertEqual(reply, "Pain shoulder 2/5 → incline DB press 20 lb (last 25); "
                                "rear delt fly 50 lb (last 70).\nReply `original` to undo.")

    def test_p4_uses_most_recent_prior_session_top_set(self):
        self.db.plan_dates.update({80: date(2026, 9, 4), 90: date(2026, 9, 11)})
        self.db.logs += [log(80, "Incline DB press", 40), log(90, "Incline DB press", 30),
                         log(90, "Incline DB press", 45, skipped=True),
                         log(90, "Incline DB press", 50, via="inferred")]
        self.checkin("shoulder pain 2")
        self.assertEqual(self.by_name()["Incline DB press"]["target_load_lbs"], 20.0)  # 24 -> 20

    def test_p4_no_history_target_stays_null(self):
        reply = self.checkin("shoulder pain 2")
        by = self.by_name()
        for n in ("Incline DB press", "Rear delt fly"):
            self.assertIsNone(by[n]["target_load_lbs"], n)
            self.assertIn("go lighter than last time", by[n]["notes"])
            self.assertTrue(by[n]["notes"].startswith("2×"), by[n]["notes"])
        self.assertEqual(reply, "Pain shoulder 2/5 → incline DB press: go lighter than last time; "
                                "rear delt fly: go lighter than last time.\nReply `original` to undo.")
        lines = hc.render_plan_lines(self.row())
        self.assertIn("3. Incline DB press — 2×8-12 · RPE ≤6", lines)

    def test_p4_pain_2_with_no_primary_exercise_is_noted(self):
        reply = self.checkin("knee pain 2")
        self.assertEqual(reply, "Pain knee 2/5 — noted.\nCheck-in logged — run Session B as written.")
        self.assertNotIn("adjustment", self.row()["blocks"])

    def test_p4_seated_back_extension_is_a_machine_and_goes_lighter(self):
        self.checkin("low back pain 2")
        ext = self.by_name()["Seated back extension"]
        self.assertTrue("load_note" in ext or "load_from" in ext, ext)

    def test_p4_and_soreness_lighten_stack(self):
        reply = self.checkin("shoulder pain 2, legs sore 3")
        by = self.by_name()
        self.assertEqual(by["Leg extension"]["sets"], 1)
        self.assertIn("go lighter", by["Rear delt fly"]["notes"])
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"],
                         ["pain_lighter", "lighten_sore"])
        self.assertTrue(reply.startswith("Pain shoulder 2/5 → "), reply)

    def test_p5_pain_0_and_1_change_nothing(self):
        before = copy.deepcopy(self.row())
        self.assertEqual(self.checkin("shoulder pain 1"),
                         "Pain shoulder 1/5 — noted.\nCheck-in logged — run Session B as written.")
        self.assertEqual(self.checkin("shoulder pain 0"),
                         "Pain shoulder 0/5 — noted.\nCheck-in logged — run Session B as written.")
        self.assertEqual(self.row(), before)
        self.assertEqual(self.db.daily[FRI]["soreness"], {"pain": {"shoulder": 0}, "pain_sides": {"shoulder": "unspecified"}})

    def test_in_session_pain_notes_never_drive_todays_rules(self):
        self.db.plan_dates[90] = date(2026, 9, 17)
        self.db.logs.append(log(90, "Incline DB press", 25, notes="pain=shoulder:5"))
        before = copy.deepcopy(self.row())
        self.checkin("slept 8 energy 4 sore 0")
        self.assertEqual(self.row(), before)


# ============================================================================
# §2 Rising pain
# ============================================================================

class TestRisingPain(Base):
    def seed(self, values):
        """values: {days_before_today: pain dict or None (check-in, no pain)}."""
        for back, pain in values.items():
            self.db.daily[FRI - timedelta(days=back)] = checkin_row(pain)

    def test_1_2_3_fires_day_off_with_trend(self):
        self.seed({2: {"shoulder": 1}, 1: {"shoulder": 2}})
        reply = self.checkin("shoulder pain 3")
        self.assertEqual(reply, "Pain shoulder 1→2→3 (rising) → day off. Reply `original` to undo.")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["rising_day_off"])
        self.assertEqual(self.row()["session_type"], "rest_mobility")

    def test_1_2_does_not_fire(self):
        self.seed({1: {"shoulder": 1}})
        self.checkin("shoulder pain 2")
        self.assertNotIn("rising_day_off", self.row()["blocks"].get("adjustment", {}).get("rules_fired", []))

    def test_gap_breaks_the_chain(self):
        # 1 (D-3), 2 (D-2), no check-in D-1, 3 today: the last 3 aren't consecutive.
        self.seed({3: {"shoulder": 1}, 2: {"shoulder": 2}})
        self.checkin("low back pain 3, shoulder pain 3")
        rules = self.row()["blocks"]["adjustment"]["rules_fired"]
        self.assertNotIn("rising_day_off", rules)

    def test_consecutive_after_an_earlier_gap_fires(self):
        self.seed({5: {"shoulder": 3}, 2: {"shoulder": 1}, 1: {"shoulder": 2}})
        self.checkin("shoulder pain 3")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["rising_day_off"])

    def test_flat_then_up_does_not_fire(self):
        self.seed({2: {"shoulder": 2}, 1: {"shoulder": 2}})
        self.checkin("knee pain 3, shoulder pain 3")
        self.assertNotIn("rising_day_off", self.row()["blocks"]["adjustment"]["rules_fired"])

    def test_falling_does_not_fire(self):
        self.seed({2: {"shoulder": 3}, 1: {"shoulder": 2}})
        self.assertEqual(self.checkin("shoulder pain 1"),
                         "Pain shoulder 1/5 — noted.\nCheck-in logged — run Session B as written.")

    def test_unmentioned_region_breaks_the_chain(self):
        self.seed({2: {"knee": 1}, 1: {"shoulder": 1}})
        reply = self.checkin("shoulder pain 2")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["pain_lighter"])
        self.assertNotIn("rising", reply)
        self.seed({2: None, 1: {"shoulder": 1}})
        self.assertNotIn("rising", self.checkin("shoulder pain 2"))

    def test_0_1_2_is_pain_2_plus_a_rising_note(self):
        self.seed({2: {"shoulder": 0}, 1: {"shoulder": 1}})
        reply = self.checkin("shoulder pain 2")
        self.assertEqual(self.row()["session_type"], "strength_b")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["pain_lighter"])
        self.assertEqual(reply, "Pain shoulder 2/5 → incline DB press: go lighter than last time; "
                                "rear delt fly: go lighter than last time.\n"
                                "rising: shoulder 0→1→2\nReply `original` to undo.")
        self.assertEqual(sum("rising" in l for l in reply.splitlines()), 1)

    def test_rise_from_0_keeps_the_normal_pain_3_rule(self):
        self.seed({2: {"low back": 0}, 1: {"low back": 2}})
        reply = self.checkin("low back pain 3")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"],
                         ["pain_mobility", "pain_substitute"])
        self.assertIn("\nrising: low back 0→2→3\n", reply)

    def test_rise_from_0_with_nothing_to_lighten_is_noted(self):
        self.seed({2: {"knee": 0}, 1: {"knee": 1}})
        self.assertEqual(self.checkin("knee pain 2"),
                         "Pain knee 2/5 — noted.\nrising: knee 0→1→2\n"
                         "Check-in logged — run Session B as written.")

    def test_2_3_4_is_a_day_off_via_pain_4(self):
        self.seed({2: {"shoulder": 2}, 1: {"shoulder": 3}})
        self.checkin("shoulder pain 4")
        self.assertEqual(self.row()["blocks"]["adjustment"]["rules_fired"], ["pain_day_off"])

    def test_unscored_region_breaks_the_chain(self):
        self.seed({2: {"shoulder": None}, 1: {"shoulder": 1}})
        self.checkin("shoulder pain 2")
        self.assertNotIn("rising_day_off", self.row()["blocks"]["adjustment"]["rules_fired"])

    def test_rising_is_per_region(self):
        # knee 1→2→2 and shoulder 3→2→3: neither region rose on its own.
        self.seed({2: {"knee": 1, "shoulder": 3}, 1: {"shoulder": 2, "knee": 2}})
        self.checkin("shoulder pain 3, knee pain 2")
        self.assertNotIn("rising_day_off", self.row()["blocks"]["adjustment"]["rules_fired"])


# ============================================================================
# §3 In-session notes (the artemis side of the round trip)
# ============================================================================

class TestPainNotes(unittest.TestCase):
    def test_parse_gym_display_notes(self):
        # Exactly what gym-display's composeSetNotes writes.
        notes = "finisher; setting=7; pain=shoulder:2; pain=low back:3; felt off"
        self.assertEqual(hp.parse_pain_notes(notes), [("shoulder", 2), ("low back", 3)])

    def test_single_and_edge_cases(self):
        self.assertEqual(hp.parse_pain_notes("pain=shoulder:2"), [("shoulder", 2)])
        self.assertEqual(hp.parse_pain_notes("pain=elbow:3"), [])
        self.assertEqual(hp.parse_pain_notes("pain=knee:7"), [])
        self.assertEqual(hp.parse_pain_notes("felt off; machine taken"), [])
        self.assertEqual(hp.parse_pain_notes(None), [])
        self.assertEqual(hp.parse_pain_notes("sharp pain in shoulder"), [])

    def test_every_display_region_is_canonical(self):
        # gym-display PAIN_REGIONS mirrors health_regions.REGIONS.
        for r in hr.REGIONS:
            self.assertEqual(hp.parse_pain_notes(f"pain={r}:2"), [(r, 2)], r)

    def test_status_page_detector_still_flags_structured_notes(self):
        try:
            from api.app.routers.health import _contains_pain_keyword
        except Exception as e:  # pragma: no cover - API deps not installed
            self.skipTest(f"api deps unavailable: {e}")
        self.assertTrue(_contains_pain_keyword("pain=shoulder:2"))
        self.assertTrue(_contains_pain_keyword("setting=7; pain=low back:3"))


# ============================================================================
# §4 Patterns
# ============================================================================

D0 = date(2026, 9, 1)


def days(n, step=7):
    return [D0 + timedelta(days=i * step) for i in range(n)]


class TestTally(unittest.TestCase):
    TODAY = date(2026, 10, 20)

    def run_tally(self, sessions, checkins):
        logs = [(d, ex, notes, skipped) for d, rows in sessions.items()
                for ex, notes, skipped in rows]
        return hp.tally(logs, checkins, self.TODAY)

    def test_3_of_4_is_a_candidate(self):
        ds = days(4)
        sessions = {d: [("Incline DB press", None, False)] for d in ds}
        checkins = {d + timedelta(days=1): {"pain": {"shoulder": 2 if i < 3 else 0}}
                    for i, d in enumerate(ds)}
        t = self.run_tally(sessions, checkins)[("Incline DB press", "shoulder")]
        self.assertEqual((t.hits, t.exposures, t.qualifies), (3, 4, True))

    def test_2_of_2_is_not(self):
        ds = days(2)
        t = self.run_tally({d: [("Incline DB press", None, False)] for d in ds},
                           {d + timedelta(days=1): {"pain": {"shoulder": 3}} for d in ds})
        self.assertFalse(t[("Incline DB press", "shoulder")].qualifies)

    def test_3_of_6_is_not(self):
        ds = days(6)
        t = self.run_tally({d: [("Incline DB press", None, False)] for d in ds},
                           {d + timedelta(days=1): {"pain": {"shoulder": 2}} for d in ds[:3]})
        tt = t[("Incline DB press", "shoulder")]
        self.assertEqual((tt.hits, tt.exposures, tt.qualifies), (3, 6, False))

    def test_in_session_note_counts_without_a_checkin(self):
        ds = days(3)
        t = self.run_tally({d: [("Leg press", "pain=knee:2", False)] for d in ds}, {})
        tt = t[("Leg press", "knee")]
        self.assertEqual((tt.hits, tt.exposures, tt.qualifies), (3, 3, True))

    def test_note_below_2_is_not_a_hit(self):
        t = self.run_tally({d: [("Leg press", "pain=knee:1", False)] for d in days(3)}, {})
        self.assertNotIn(("Leg press", "knee"), t)

    def test_note_region_counts_even_if_exercise_does_not_use_it(self):
        t = self.run_tally({d: [("Leg press", "pain=neck:2", False)] for d in days(3)}, {})
        self.assertTrue(t[("Leg press", "neck")].qualifies)

    def test_one_hit_per_exposure(self):
        ds = days(3)
        sessions = {d: [("Incline DB press", "pain=shoulder:3", False),
                        ("Incline DB press", "pain=shoulder:2", False)] for d in ds}
        checkins = {d + timedelta(days=1): {"pain": {"shoulder": 4}} for d in ds}
        tt = self.run_tally(sessions, checkins)[("Incline DB press", "shoulder")]
        self.assertEqual((tt.hits, tt.exposures), (3, 3))

    def test_next_morning_region_must_be_used_by_the_exercise(self):
        ds = days(3)
        t = self.run_tally({d: [("Leg extension", None, False)] for d in ds},
                           {d + timedelta(days=1): {"pain": {"shoulder": 3}} for d in ds})
        self.assertNotIn(("Leg extension", "shoulder"), t)

    def test_family_region_reaches_members(self):
        ds = days(3)
        t = self.run_tally({d: [("Leg extension", None, False)] for d in ds},
                           {d + timedelta(days=1): {"pain": {"legs": 2}} for d in ds})
        self.assertTrue(t[("Leg extension", "legs")].qualifies)

    def test_shared_region_exercise_is_named(self):
        ds = days(4)
        sessions = {d: [("Incline DB press", None, False), ("Rear delt fly", None, False),
                        ("Leg extension", None, False)] for d in ds[:3]}
        sessions[ds[3]] = [("Incline DB press", None, False)]
        checkins = {d + timedelta(days=1): {"pain": {"shoulder": 2}} for d in ds[:3]}
        t = self.run_tally(sessions, checkins)
        inc = t[("Incline DB press", "shoulder")]
        self.assertEqual((inc.hits, inc.exposures), (3, 4))
        self.assertEqual(inc.shared, ["Rear delt fly"])
        text = hp.render({"exercise": inc.exercise, "region": inc.region, "hits": inc.hits,
                          "exposures": inc.exposures, "evidence": inc.evidence()})
        self.assertEqual(text, "Pattern: shoulder pain ≥2 after **incline DB press** — 3 of 4 "
                               "sessions (also that day: rear delt fly). What do you notice?")
        for word in ("because", "caused", "causes", "due to", "from the"):
            self.assertNotIn(word, text)

    def test_skipped_sets_alone_are_not_exposures(self):
        ds = days(4)
        sessions = {d: [("Incline DB press", None, False)] for d in ds[:3]}
        sessions[ds[3]] = [("Incline DB press", "machine taken", True)]
        checkins = {d + timedelta(days=1): {"pain": {"shoulder": 2}} for d in ds[:3]}
        self.assertEqual(self.run_tally(sessions, checkins)[("Incline DB press", "shoulder")].exposures, 3)

    def test_window_is_eight_weeks(self):
        old = [self.TODAY - timedelta(days=70 + i) for i in range(3)]
        t = self.run_tally({d: [("Leg press", "pain=knee:3", False)] for d in old}, {})
        self.assertEqual(t, {})


class PatternDB(unittest.TestCase):
    """Real recompute / lifecycle against FakeDB."""

    def setUp(self):
        self.db = FakeDB()
        self.cur = self.db.cursor()
        self.pid = 500

    def session(self, d, *exercises, notes=None):
        self.pid += 1
        self.db.plan_dates[self.pid] = d
        for ex in exercises:
            self.db.logs.append(log(self.pid, ex, 20, notes=notes))

    def morning(self, d, pain):
        self.db.daily[d] = checkin_row(pain)

    def seed_candidate(self, n_hits=3, n_exp=4, start=D0):
        ds = [start + timedelta(days=7 * i) for i in range(n_exp)]
        for i, d in enumerate(ds):
            # Rear delt fly shares the region on two of the days only (2/2 is
            # below threshold), so it is named but is not a candidate itself.
            self.session(d, *(("Incline DB press", "Rear delt fly") if i < 2
                              else ("Incline DB press",)))
            if i < n_hits:
                self.morning(d + timedelta(days=1), {"shoulder": 2})
        return ds

    def pattern(self, ex="Incline DB press", region="shoulder"):
        return next((p for p in self.db.patterns
                     if (p["exercise"], p["region"]) == (ex, region)), None)


class TestRecompute(PatternDB):
    TODAY = date(2026, 10, 1)

    def test_candidate_and_below_threshold(self):
        self.seed_candidate(3, 4)
        # Leg press: 2 of 2 note hits -> below threshold, never stored.
        for d in (D0 + timedelta(days=2), D0 + timedelta(days=9)):
            self.session(d, "Leg press", notes="pain=knee:3")
        rows = hp.recompute(self.cur, self.TODAY, NOW)
        cands = [(r["exercise"], r["region"], r["hits"], r["exposures"]) for r in rows if r["qualifies"]]
        self.assertIn(("Incline DB press", "shoulder", 3, 4), cands)
        self.assertIsNone(self.pattern("Leg press", "knee"))
        self.assertEqual(self.pattern()["evidence"]["shared"], ["Rear delt fly"])
        self.assertEqual(self.pattern()["status"], "open")

    def test_dismiss_hides_until_two_more_hits(self):
        ds = self.seed_candidate(3, 4)
        hp.recompute(self.cur, self.TODAY, NOW)
        p = self.pattern()
        hp.set_status(self.cur, [p["id"]], "dismissed", NOW)
        self.assertEqual((p["status"], p["dismissed_at_hits"]), ("dismissed", 3))

        # +1 hit: still hidden.
        d = ds[-1] + timedelta(days=7)
        self.session(d, "Incline DB press")
        self.morning(d + timedelta(days=1), {"shoulder": 2})
        rows = hp.recompute(self.cur, d + timedelta(days=1), NOW)
        self.assertEqual(self.pattern()["status"], "dismissed")
        self.assertEqual(hp.to_surface(rows), [])

        # +2 hits: back.
        d2 = d + timedelta(days=7)
        self.session(d2, "Incline DB press")
        self.morning(d2 + timedelta(days=1), {"shoulder": 3})
        rows = hp.recompute(self.cur, d2 + timedelta(days=1), NOW)
        self.assertEqual(self.pattern()["status"], "open")
        self.assertEqual([r["exercise"] for r in hp.to_surface(rows)], ["Incline DB press"])

    def test_resolved_reopens_only_with_new_data(self):
        ds = self.seed_candidate(3, 4)
        hp.recompute(self.cur, self.TODAY, NOW)
        resolved_at = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        hp.set_status(self.cur, [self.pattern()["id"]], "resolved", resolved_at)
        rows = hp.recompute(self.cur, self.TODAY, NOW)
        self.assertEqual(self.pattern()["status"], "resolved")
        self.assertEqual(hp.to_surface(rows), [])
        d = date(2026, 10, 2)
        self.session(d, "Incline DB press")
        self.morning(d + timedelta(days=1), {"shoulder": 2})
        hp.recompute(self.cur, d + timedelta(days=1), NOW)
        self.assertEqual(self.pattern()["status"], "open")

    def test_surface_only_new_or_changed(self):
        ds = self.seed_candidate(3, 4)
        rows = hp.recompute(self.cur, self.TODAY, NOW)
        todo = hp.to_surface(rows)
        self.assertEqual(len(todo), 1)
        hp.mark_surfaced(self.cur, todo[0], "post-A", NOW)
        self.assertEqual(hp.to_surface(hp.recompute(self.cur, self.TODAY, NOW)), [])
        # A new exposure changes the counts -> surfaced again.
        self.session(ds[-1] + timedelta(days=7), "Incline DB press")
        rows = hp.recompute(self.cur, ds[-1] + timedelta(days=8), NOW)
        self.assertEqual(len(hp.to_surface(rows)), 1)
        self.assertEqual(self.pattern()["post_ids"], ["post-A"])

    def test_pattern_that_stops_qualifying_is_kept_but_not_surfaced(self):
        ds = self.seed_candidate(3, 4)
        hp.recompute(self.cur, self.TODAY, NOW)
        for i in range(2):
            self.session(ds[-1] + timedelta(days=7 * (i + 1)), "Incline DB press")
        rows = hp.recompute(self.cur, ds[-1] + timedelta(days=15), NOW)
        self.assertFalse(self.pattern()["qualifies"])
        self.assertEqual((self.pattern()["hits"], self.pattern()["exposures"]), (3, 6))
        self.assertEqual(hp.to_surface(rows), [])


class TestCheckinMention(PatternDB):
    def test_the_completing_checkin_mentions_once(self):
        # Two prior hits + one exposure yesterday with no hit yet.
        prev = [date(2026, 9, 3), date(2026, 9, 10)]
        for d in prev:
            self.session(d, "Incline DB press", "Rear delt fly")
            self.morning(d + timedelta(days=1), {"shoulder": 2})
        yesterday = FRI - timedelta(days=1)
        self.session(yesterday, "Incline DB press", "Rear delt fly")
        self.db.plan[FRI] = office_row(FRI)

        reply, ids = hc.process_checkin_full(self.cur, "slept 7, shoulder pain 2",
                                             FRI, checkin_id="c1", now=NOW, adjust=True)
        self.assertEqual(ids, [self.pattern()["id"]])
        last = reply.splitlines()[-1]
        self.assertEqual(last, "Pattern: shoulder pain ≥2 after **incline DB press** — 3 of 3 "
                               "sessions (also that day: rear delt fly). What do you notice?")
        self.assertEqual(sum("Pattern:" in l for l in reply.splitlines()), 1)
        self.assertIsNotNone(self.pattern()["mentioned_at"])

        # A second check-in the same morning doesn't repeat it.
        reply2, ids2 = hc.process_checkin_full(self.cur, "shoulder pain 2", FRI,
                                               checkin_id="c2", now=NOW, adjust=True)
        self.assertEqual(ids2, [])
        self.assertNotIn("Pattern:", reply2)

    def test_no_mention_when_the_checkin_did_not_complete_it(self):
        self.seed_candidate(3, 4, start=date(2026, 8, 20))
        hp.recompute(self.cur, FRI, NOW)        # already known before today
        self.db.plan[FRI] = office_row(FRI)
        reply, ids = hc.process_checkin_full(self.cur, "shoulder pain 2", FRI,
                                             checkin_id="c1", now=NOW, adjust=True)
        self.assertEqual(ids, [])
        self.assertNotIn("Pattern:", reply)

    def test_pattern_failure_never_costs_the_checkin(self):
        self.db.plan[FRI] = office_row(FRI)
        with patch.object(hp, "recompute", side_effect=RuntimeError("boom")):
            reply = hc.process_checkin(self.cur, "shoulder pain 1", FRI, checkin_id="c",
                                       now=NOW, adjust=True)
        self.assertIn("noted", reply)
        self.assertIn(FRI, self.db.daily)


# ============================================================================
# Threads (main._handle_pattern_thread) and jobs
# ============================================================================

def _stub_main_modules():
    for n in ("flask", "requests", "websocket", "schedule", "Levenshtein",
              "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
              "apscheduler.triggers", "apscheduler.triggers.cron",
              "apscheduler.triggers.interval", "googleapiclient",
              "googleapiclient.discovery", "googleapiclient.errors", "google", "google.auth",
              "google.auth.transport", "google.auth.transport.requests", "google.oauth2",
              "google.oauth2.credentials", "google_auth_oauthlib", "google_auth_oauthlib.flow"):
        sys.modules.setdefault(n, MagicMock())


class TestPatternThread(PatternDB):
    def setUp(self):
        super().setUp()
        _stub_main_modules()
        from artemis import main
        self.main = main
        self.seed_candidate(3, 4)
        hp.recompute(self.cur, date(2026, 10, 1), NOW)
        hp.mark_surfaced(self.cur, self.pattern(), "sun-post", NOW)
        self.db.plan[MON] = office_row(MON)
        self.plan_before = copy.deepcopy(self.db.plan)
        self.kv = {}

        @contextmanager
        def conn():
            yield self.db
        self.mm = MagicMock()
        self._p = [
            patch.object(main, "_mm", self.mm),
            patch("knowledge.db.get_connection", conn),
            patch("artemis.quiet_hours.local_today", return_value=MON),
            patch("artemis.quiet_hours.get_system_value", side_effect=self.kv.get),
            patch("artemis.quiet_hours.set_system_value", side_effect=self.kv.__setitem__),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def reply(self, text, root="sun-post", pid="r1"):
        self.mm.reset_mock()
        post = {"id": pid, "channel_id": "c1", "message": text, "root_id": root}
        claimed = self.main._handle_pattern_thread(post, text)
        out = self.mm.post_to_channel_id.call_args[0][1] if self.mm.post_to_channel_id.called else None
        return claimed, out

    def test_reflection_stored_verbatim_no_plan_change(self):
        text = "I think it's the bench angle — felt it on the last rep, not before"
        claimed, out = self.reply(text)
        self.assertTrue(claimed)
        self.assertEqual(out, "Noted.")
        self.assertEqual(self.db.reflections, [{"pattern_id": self.pattern()["id"], "text": text,
                                                "post_id": "sun-post", "source_post_id": "r1"}])
        self.assertEqual(self.db.plan, self.plan_before)
        self.assertEqual(self.db.audit, [])

    def test_redelivered_post_is_stored_once(self):
        self.reply("same words", pid="r9")
        self.reply("same words", pid="r9")
        self.assertEqual(len(self.db.reflections), 1)

    def test_change_request_is_stored_then_goes_to_the_swap_flow(self):
        with patch("artemis.health.detect_modality_swap", return_value={"to": "bike"}):
            claimed, out = self.reply("swap incline press for the machine press")
        self.assertFalse(claimed)            # the dispatcher continues to the swap flow
        self.assertIsNone(out)
        self.assertEqual(len(self.db.reflections), 1)
        self.assertEqual(self.db.plan, self.plan_before)

    def test_dismiss_and_resolved(self):
        claimed, out = self.reply("dismiss")
        self.assertTrue(claimed)
        self.assertEqual(out, "Dismissed — hidden until it gets 2 more hits.")
        self.assertEqual(self.pattern()["status"], "dismissed")
        self.assertEqual(self.db.reflections, [])
        claimed, out = self.reply("resolved")
        self.assertEqual(self.pattern()["status"], "resolved")
        self.assertIsNotNone(self.pattern()["resolved_at"])

    def test_other_threads_and_top_level_are_not_claimed(self):
        self.assertEqual(self.reply("dismiss", root="some-other-thread"), (False, None))
        self.assertEqual(self.reply("dismiss", root=None), (False, None))
        self.assertEqual(self.pattern()["status"], "open")

    def test_checkin_thread_link_is_one_shot_and_same_day(self):
        self.kv[hp.checkin_thread_key("wake-thread")] = json.dumps(
            {"ids": [self.pattern()["id"]], "date": MON.isoformat()})
        claimed, _ = self.reply("only on the heavy set", root="wake-thread", pid="w1")
        self.assertTrue(claimed)
        self.assertEqual(self.db.reflections[-1]["pattern_id"], self.pattern()["id"])
        self.assertEqual(self.reply("what's the weather", root="wake-thread", pid="w2"),
                         (False, None))
        self.kv[hp.checkin_thread_key("old-thread")] = json.dumps(
            {"ids": [self.pattern()["id"]], "date": "2026-09-01"})
        self.assertEqual(self.reply("hm", root="old-thread", pid="w3"), (False, None))

    def test_lookup_failure_fails_open(self):
        with patch.object(hp, "patterns_for_post", side_effect=RuntimeError("no table")):
            self.assertEqual(self.reply("anything", root="sun-post"), (False, None))

    def test_dispatch_order(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "main.py").read_text()
        self.assertLess(src.index('("morning_flow", _handle_morning_flow)'),
                        src.index('("pattern_thread", _handle_pattern_thread)'))
        self.assertLess(src.index('("pattern_thread", _handle_pattern_thread)'),
                        src.index('("health_conversation", _handle_health_conversation)'))


class TestJobs(PatternDB):
    def make(self):
        _stub_main_modules()
        from artemis.scheduler import ArtemisScheduler
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        s.mm.post_message.side_effect = lambda ch, text: {"id": f"post-{len(self.posts)}",
                                                          "_": self.posts.append(text)}
        return s

    def setUp(self):
        super().setUp()
        self.posts = []

        @contextmanager
        def conn():
            yield self.db
        self._p = patch("knowledge.db.get_connection", conn)
        self._p.start()
        self._d = patch("artemis.scheduler._local_today", return_value=date(2026, 10, 4))
        self._d.start()

    def tearDown(self):
        self._p.stop()
        self._d.stop()

    def test_registry(self):
        s = self.make()
        by_id = {c.id: c for c in s.cron_specs()}
        rc = by_id["pain_pattern_recompute"]
        self.assertEqual((rc.hour, rc.minute, rc.day_of_week), (21, 55, None))
        hr_ = by_id["health_review"]
        self.assertEqual((hr_.hour, hr_.minute, hr_.day_of_week, hr_.tier), (8, 30, "sun", "health"))

    def test_recompute_job_is_silent(self):
        self.seed_candidate(3, 4)
        s = self.make()
        s.job_pain_pattern_recompute()
        self.assertEqual(self.posts, [])
        self.assertTrue(self.pattern()["qualifies"])

    def test_sunday_review_posts_new_only_when_open(self):
        self.seed_candidate(3, 4)
        s = self.make()
        with patch.object(s, "_is_open", return_value=False):
            s.job_health_review()
        self.assertEqual(self.posts, [])
        with patch.object(s, "_is_open", return_value=True):
            s.job_health_review()
            self.assertEqual(self.posts, [
                "Pattern: shoulder pain ≥2 after **incline DB press** — 3 of 4 sessions "
                "(also that day: rear delt fly). What do you notice?"])
            self.assertEqual(self.pattern()["post_ids"], ["post-0"])
            s.job_health_review()               # nothing changed -> nothing posted
            self.assertEqual(len(self.posts), 1)


class TestLighterLoad(unittest.TestCase):
    """LOCATION-1 (2026-09-25): the class and the gym's load config both come
    from the ROW. These cases are the office numbers the old hardcoded tables
    produced — unchanged, which is the point: the config moved, the maths did
    not. Passing neither class nor config now yields NO recommendation."""

    OFFICE = load_config.OFFICE
    RICHFIELD = load_config.RICHFIELD
    CLASS = {"Incline DB press": "dumbbell", "DB goblet squat": "dumbbell",
             "Rear delt fly": "machine", "Leg press": "machine",
             "Lat pulldown": "machine", "Cable face pull (rope)": "cable",
             "Smith squat": "smith", "Barbell bench press": "barbell",
             "Barbell row": "barbell", "Captain's chair knee raise": "bodyweight"}

    def lighter(self, name, last, cfg=None):
        return hr.lighter_load(name, last, explicit_class=self.CLASS[name],
                               load_config=cfg or self.OFFICE)

    def test_reachable_rounding_at_the_office(self):
        cases = [("Incline DB press", 25, 20), ("Incline DB press", 45, 35),
                 ("Incline DB press", 5, 5), ("DB goblet squat", 12.5, 10),
                 ("Rear delt fly", 70, 50), ("Leg press", 10, 10), ("Leg press", 200, 160),
                 ("Lat pulldown", 95, 70), ("Cable face pull (rope)", 30, 20),
                 ("Smith squat", 100, 80), ("Barbell bench press", 135, 105),
                 ("Captain's chair knee raise", 20, None)]
        for name, last, want in cases:
            with self.subTest(name=name, last=last):
                self.assertEqual(self.lighter(name, last), want)

    def test_lighter_than_last_unless_already_at_the_floor(self):
        floors = {"Incline DB press": 5, "Rear delt fly": 10, "Smith squat": 10,
                  "Barbell row": 45}
        for name, floor in floors.items():
            for last in range(5, 300, 5):
                got = self.lighter(name, last)
                with self.subTest(name=name, last=last):
                    if last > floor:
                        self.assertLess(got, last)
                    else:
                        self.assertEqual(got, floor)

    # ── the unknown states: no recommendation, never a guessed number ────────
    def test_no_class_means_no_recommendation(self):
        """The name used to imply a class. It no longer does."""
        self.assertIsNone(hr.lighter_load("Incline DB press", 45, load_config=self.OFFICE))

    def test_no_load_config_means_no_recommendation(self):
        """A row seeded before LOCATION-1. There is no office fallback."""
        self.assertIsNone(hr.lighter_load("Incline DB press", 45, explicit_class="dumbbell"))

    def test_a_class_this_gym_does_not_have_means_no_recommendation(self):
        """Richfield has no cable stack, so a cable exercise has no load there."""
        self.assertNotIn("cable", self.RICHFIELD)
        self.assertIsNone(hr.lighter_load("Cable face pull (rope)", 30,
                                          explicit_class="cable", load_config=self.RICHFIELD))

    def test_bands_and_trx_carry_no_numeric_load(self):
        for cls in ("bands", "trx"):
            with self.subTest(cls=cls):
                self.assertIsNone(hr.lighter_load("Band pull-apart", 30, explicit_class=cls,
                                                  load_config=self.RICHFIELD))

    def test_the_same_load_rounds_differently_at_the_two_gyms(self):
        """The reason the config travels: a PowerBlock is not a hex rack."""
        office = hr.lighter_load("Incline DB press", 45, explicit_class="dumbbell",
                                 load_config=self.OFFICE)
        farm = hr.lighter_load("Incline DB press", 45, explicit_class="dumbbell",
                               load_config=self.RICHFIELD)
        self.assertIsNotNone(office)
        self.assertIsNotNone(farm)
        self.assertLess(office, 45)
        self.assertLess(farm, 45)


class BodyweightOnA(Base):
    day = MON   # Mon 9/21 — Strength A carries the captain's chair (SCHEDULE-2)

    def test_p4_bodyweight_primary_is_not_given_a_load(self):
        self.checkin("core pain 2")
        knee_raise = self.by_name()["Captain's chair knee raise"]
        self.assertNotIn("load_note", knee_raise)
        self.assertNotIn("load_from", knee_raise)


if __name__ == "__main__":
    unittest.main(verbosity=2)
