"""YOGA-1 — Recovery Flow: builder, side validator, reseed diff, rules,
nudge, nag, wake post. No RDS.

Run:
    python3.11 tests/test_recovery_flow.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import copy
import json
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

from test_checkin_adjust import FakeDB, office_row, rest_row  # noqa: E402

from artemis import health_checkin as hc  # noqa: E402
from artemis import health_office as office  # noqa: E402

# SCHEDULE-2 (Ryan, 2026-09-19): the flows travel — Richfield and msp_home,
# mat only. No office flow is scheduled any more, so the Stretch Trainer
# variant is built directly (OFFICE_FLOW) rather than read off a plan row.
THU = date(2026, 9, 25)   # Fri, Richfield — mat flow (name kept to limit churn)
SAT = date(2026, 10, 3)   # Sat, msp_home — mat flow
NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
ROWS = {r["plan_date"]: r for r in office.build_rows()}
# The office (Stretch Trainer) variant, still supported by the builder.
OFFICE_FLOW = office._recovery_flow(office.LOCATION)[0]


def _reseed():
    spec = importlib.util.spec_from_file_location(
        "reseed_v2", _HERE.parent / "scripts" / "reseed_health_plan_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def flow(**over) -> dict:
    b = copy.deepcopy(ROWS[SAT]["blocks"])
    b.update(over)
    return b


class TestBuilder(unittest.TestCase):
    def test_flow_variants(self):
        thu, sat = ROWS[THU], ROWS[SAT]
        for r in (thu, sat):
            self.assertEqual(r["session_type"], "recovery_flow")
            self.assertEqual(r["blocks"]["type"], "recovery_flow")
            self.assertEqual(r["target_rpe"], 2.0)
            self.assertEqual(r["blocks"]["rounds"], 2)
            self.assertEqual(len(r["blocks"]["flow"]), 20)
            self.assertEqual(r["blocks"]["close"]["duration_sec"], 180)
        self.assertEqual(thu["blocks"]["location"], "Richfield")
        self.assertEqual(sat["blocks"]["location"], "home")
        # Both seeded flows are the mat variant; no Stretch Trainer travels.
        for r in (thu, sat):
            self.assertEqual(r["blocks"]["pre"], [office.FLOW_MEDITATION])
            self.assertNotIn("Stretch Trainer", r["blocks"]["equipment"])
        # …but the office variant still builds, for a flow run at the office.
        self.assertEqual(OFFICE_FLOW["pre"], [office.FLOW_MEDITATION,
                                              {"name": "Stretch Trainer", "side": None,
                                               "duration_sec": 480, "transition_sec": 5,
                                               "posture": "standing",
                                               "cue": "Follow the 8 placard stretches"}])
        self.assertIn("Stretch Trainer", OFFICE_FLOW["equipment"])

    def test_flow_days_follow_the_cycle(self):
        """SCHEDULE-2: flows land on the office non-lift day (Stretch Trainer
        variant) and the msp_home Saturday / Sunday (mat variant)."""
        for d, r in ROWS.items():
            is_flow = r["session_type"] == "recovery_flow"
            self.assertEqual(is_flow, office.session_for(d) == "recovery_flow", d)
            if is_flow:
                loc = office.day_location(d)
                self.assertEqual(r["blocks"]["location"], loc)
                # only the office has the Stretch Trainer
                pre = [p["name"] for p in r["blocks"]["pre"]]
                self.assertEqual("Stretch Trainer" in pre, loc == "office gym", d)
        by_type = {}
        for d in ROWS:
            by_type.setdefault(office.day_type(d), set()).add(ROWS[d]["session_type"])
        self.assertEqual(by_type["wi"], {"recovery_flow", "walk"})
        self.assertEqual(by_type["travel"], {"walk"})

    def test_steps_match_the_table(self):
        """YOGA-4 order: the lunges run R, R, L, L so the switch lands at the
        top of the crescent; extended puppy follows all four and leads into
        bridge; easy pose closes round 1 only."""
        got = [(s["step"], s["name"], s["side"], s["duration_sec"], s["transition_sec"])
               for s in ROWS[SAT]["blocks"]["flow"]]
        self.assertEqual(got, [
            ("1", "Child's pose", None, 40, 5),
            ("2", "Cobra", None, 40, 3),
            ("3", "Downward dog", None, 40, 3),
            ("4", "Standing forward bend", None, 40, 3),
            ("5", "High lunge", "R", 40, 5),
            ("6", "Crescent lunge", "R", 40, 3),
            ("7", "Crescent lunge", "L", 40, 3),
            ("8", "High lunge", "L", 40, 3),
            ("9", "Extended puppy", None, 40, 5),
            ("10", "Bridge", None, 40, 4),
            ("11", "Supine twist", "R", 40, 3),
            ("12", "Supine twist", "L", 40, 3),
            ("13", "Wind release", "R", 40, 4),
            ("14", "Wind release", "L", 40, 3),
            ("15", "Seated side bend", "L", 40, 5),
            ("16", "Seated twist", "L", 40, 3),
            ("17", "Seated twist", "R", 40, 3),
            ("18", "Seated side bend", "R", 40, 3),
            ("19", "Seated mountain", None, 40, 3),
            ("20", "Easy pose", None, 40, 3),
        ])
        by = {s["step"]: s for s in ROWS[SAT]["blocks"]["flow"]}
        self.assertEqual(by["3"]["easier"], "Dolphin — forearms down")
        for st in ("5", "6", "7", "8"):
            self.assertEqual(by[st]["easier"], "Knee down")
        self.assertEqual(by["5"]["side_label"], "Right leg forward")
        self.assertEqual(by["15"]["side_label"], "Lean left")
        self.assertTrue(all(s["cue"] for s in ROWS[SAT]["blocks"]["flow"]))
        self.assertNotIn("link", by["1"])

    def test_the_lunge_block_runs_R_R_L_L(self):
        names = [(s["name"], s["side"]) for s in ROWS[SAT]["blocks"]["flow"]
                 if s["mirror_group"] == "lunge-unit"]
        self.assertEqual(names, [("High lunge", "R"), ("Crescent lunge", "R"),
                                 ("Crescent lunge", "L"), ("High lunge", "L")])

    def test_extended_puppy_follows_every_lunge_and_precedes_bridge(self):
        flow = ROWS[SAT]["blocks"]["flow"]
        order = [s["name"] for s in flow]
        puppy = order.index("Extended puppy")
        lunges = [i for i, s in enumerate(flow) if s["mirror_group"] == "lunge-unit"]
        self.assertTrue(all(i < puppy for i in lunges), "puppy must come after all four lunges")
        self.assertEqual(order[puppy + 1], "Bridge")

    def test_every_hold_is_40_seconds_in_both_rounds(self):
        """The round-2 doubling is gone: 40 s everywhere, both rounds."""
        for b in (OFFICE_FLOW, ROWS[SAT]["blocks"], ROWS[THU]["blocks"]):
            r1 = office.flow_step_holds(b, 1)
            r2 = office.flow_step_holds(b, 2)
            self.assertEqual(set(r1) | set(r2), {40})
            self.assertEqual((len(r1), len(r2)), (20, 19))
        self.assertNotIn("double_steps", ROWS[SAT]["blocks"])
        self.assertNotIn("double_round", ROWS[SAT]["blocks"])

    def test_easy_pose_is_round_one_only(self):
        b = ROWS[SAT]["blocks"]
        self.assertEqual([s["name"] for s in office.flow_steps_for_round(b, 1)][-1], "Easy pose")
        r2 = [s["name"] for s in office.flow_steps_for_round(b, 2)]
        self.assertNotIn("Easy pose", r2)
        self.assertEqual(r2[-1], "Seated mountain")
        self.assertEqual(next(s for s in b["flow"] if s["name"] == "Easy pose")["rounds"], [1])

    def test_totals(self):
        """40 s holds both rounds, 1 min meditation, 3 min savasana, plus the
        transition table. Both variants must stay under 45 min."""
        thu, sat = OFFICE_FLOW, ROWS[SAT]["blocks"]
        # holds = 60 meditation + 480 Stretch Trainer + 20×40 + 19×40 + 180 savasana
        self.assertEqual((office.flow_hold_sec(thu), office.flow_transition_total_sec(thu)), (2280, 152))
        self.assertEqual((office.flow_hold_sec(sat), office.flow_transition_total_sec(sat)), (1800, 147))
        self.assertEqual(office.flow_total_sec(thu), 2432)   # 40:32
        self.assertEqual(office.flow_total_sec(sat), 1947)   # 32:27
        self.assertEqual((thu["total_sec"], sat["total_sec"]), (2432, 1947))
        for total in (office.flow_total_sec(thu), office.flow_total_sec(sat)):
            self.assertLess(total, 45 * 60)
        self.assertEqual((ROWS[THU]["est_duration_min"], ROWS[SAT]["est_duration_min"]), (33, 33))


class TestTransitions(unittest.TestCase):
    """YOGA-4: the table in FLOW_STEPS is the only source of transition
    lengths. The posture-derived 3 s / 5 s rule is gone."""

    #  from → to, exactly as Ryan wrote it.
    TABLE = [
        ("meditation", "Child's pose", 5),
        ("Child's pose", "Cobra", 3),
        ("Cobra", "Downward dog", 3),
        ("Downward dog", "Standing forward bend", 3),
        ("Standing forward bend", "High lunge R", 5),
        ("High lunge R", "Crescent lunge R", 3),
        ("Crescent lunge R", "Crescent lunge L", 3),
        ("Crescent lunge L", "High lunge L", 3),
        ("High lunge L", "Extended puppy", 5),
        ("Extended puppy", "Bridge", 4),
        ("Bridge", "Supine twist R", 3),
        ("Supine twist R", "Supine twist L", 3),
        ("Supine twist L", "Wind release R", 4),
        ("Wind release R", "Wind release L", 3),
        ("Wind release L", "Seated side bend L", 5),
        ("Seated side bend L", "Seated twist L", 3),
        ("Seated twist L", "Seated twist R", 3),
        ("Seated twist R", "Seated side bend R", 3),
        ("Seated side bend R", "Seated mountain", 3),
        ("Seated mountain", "Easy pose", 3),
    ]

    def labelled(self, blocks):
        """The played sequence as (label, transition_sec) pairs."""
        out = []
        for i in office.flow_sequence(blocks):
            label = i["name"] + (f" {i['side']}" if i.get("side") else "")
            out.append((label, i["transition_sec"]))
        return out

    def test_every_transition_matches_the_table(self):
        for day in (THU, SAT):
            seq = self.labelled(ROWS[day]["blocks"])
            poses = seq[1:]                      # drop the meditation itself
            for rnd in (0, 1):
                offset = rnd * 20
                expected = [(to, sec) for _, to, sec in self.TABLE]
                if rnd == 1:
                    expected = expected[:-1]     # no easy pose in round 2
                self.assertEqual(poses[offset:offset + len(expected)], expected,
                                 f"{day} round {rnd + 1}")

    def test_round_one_ends_easy_pose_then_child_s_pose(self):
        seq = self.labelled(ROWS[SAT]["blocks"])
        i = [n for n, _ in seq].index("Easy pose")
        self.assertEqual(seq[i], ("Easy pose", 3))
        self.assertEqual(seq[i + 1], ("Child's pose", 5))   # starts round 2

    def test_round_two_ends_seated_mountain_then_savasana(self):
        seq = self.labelled(ROWS[SAT]["blocks"])
        self.assertEqual(seq[-2:], [("Seated mountain", 3), ("Savasana", 5)])

    def test_the_pre_items_and_the_office_variant(self):
        thu = self.labelled(OFFICE_FLOW)
        self.assertEqual(thu[:3], [("Seated meditation", 5), ("Stretch Trainer", 5),
                                   ("Child's pose", 5)])
        sat = self.labelled(ROWS[SAT]["blocks"])
        self.assertEqual(sat[:2], [("Seated meditation", 5), ("Child's pose", 5)])

    def test_the_posture_derivation_is_gone(self):
        self.assertFalse(hasattr(office, "transition_sec"))
        for const in ("FLOW_TRANSITION_SHORT_SEC", "FLOW_TRANSITION_LONG_SEC",
                      "FLOW_DOUBLE_ROUND", "FLOW_DOUBLE_STEPS"):
            self.assertFalse(hasattr(office, const), const)
        for key in ("transition_short_sec", "transition_long_sec"):
            self.assertNotIn(key, ROWS[SAT]["blocks"])

    def test_a_transition_is_required_on_every_item(self):
        b = copy.deepcopy(ROWS[SAT]["blocks"])
        b["flow"][4].pop("transition_sec")
        with self.assertRaises(office.FlowError) as cm:
            office.validate_flow(b)
        self.assertIn("transition_sec", str(cm.exception))

    def test_the_lead_in_moved_to_seven_seconds(self):
        for day in (THU, SAT):
            self.assertEqual(ROWS[day]["blocks"]["leadin_sec"], 7)
        self.assertEqual(office.FLOW_LEADIN_SEC, 7)

    def test_meditation_first_savasana_last(self):
        for day in (THU, SAT):
            b = ROWS[day]["blocks"]
            self.assertEqual(b["pre"][0]["name"], "Seated meditation")
            self.assertEqual(b["pre"][0]["duration_sec"], 60)
            self.assertEqual((b["close"]["name"], b["close"]["duration_sec"]), ("Savasana", 180))
        self.assertEqual(next(s for s in ROWS[SAT]["blocks"]["flow"] if s["step"] == "3")["spoken"],
                         "Downward facing dog")

    def test_every_pose_resolves_to_a_sanskrit_entry(self):
        """YOGA-5: a pose with no Sanskrit is a seeding bug, not a fallback."""
        for day in (THU, SAT):
            for st in ROWS[day]["blocks"]["flow"]:
                self.assertTrue(st.get("sanskrit"), f"{st['name']} has no sanskrit")
                self.assertTrue(st.get("sanskrit_spoken"), f"{st['name']} has no sanskrit_spoken")
                # The spoken form is a phonetic respelling, never the display one.
                self.assertNotEqual(st["sanskrit_spoken"], st["sanskrit"], st["name"])
        by = {s["name"]: s for s in ROWS[SAT]["blocks"]["flow"]}
        self.assertEqual((by["Crescent lunge"]["sanskrit"], by["Crescent lunge"]["sanskrit_spoken"]),
                         ("Ashta Chandrasana", "AHSH-tah chahn-DRAH-sah-nah"))
        self.assertEqual(by["Downward dog"]["sanskrit"], "Adho Mukha Svanasana")
        close = ROWS[SAT]["blocks"]["close"]
        self.assertEqual((close["sanskrit"], close["sanskrit_spoken"]),
                         ("Shavasana", "shah-VAH-sah-nah"))

    def test_the_pre_blocks_have_no_sanskrit_and_fall_back(self):
        """Meditation and the Stretch Trainer have no meaningful Sanskrit."""
        for pre in OFFICE_FLOW["pre"]:
            self.assertNotIn("sanskrit", pre, pre["name"])
        office.validate_flow(OFFICE_FLOW)      # and that is not an error

    def test_a_pose_missing_its_sanskrit_fails_validation(self):
        b = copy.deepcopy(ROWS[SAT]["blocks"])
        b["flow"][5].pop("sanskrit_spoken")
        with self.assertRaises(office.FlowError) as cm:
            office.validate_flow(b)
        self.assertIn("no Sanskrit entry", str(cm.exception))

    def test_every_pose_name_in_the_table_is_used(self):
        """A stale FLOW_SANSKRIT entry is dead weight; catch it here."""
        used = {s["name"] for s in office.FLOW_STEPS} | {office.FLOW_CLOSE["name"]}
        self.assertEqual(set(office.FLOW_SANSKRIT) - used, set())

    def test_a_pose_without_a_posture_fails_validation(self):
        b = copy.deepcopy(ROWS[SAT]["blocks"])
        b["flow"][0]["posture"] = None
        with self.assertRaises(office.FlowError) as cm:
            office.validate_flow(b)
        self.assertIn("posture", str(cm.exception))


class TestProgramState(unittest.TestCase):
    def test_program_state_for_the_status_page(self):
        self.assertEqual(office.program_state(), {
            "name": "Foundation", "phase": 1, "anchor": "2026-09-16", "weeks_total": 7,
            "deload_week": 7, "end": "2026-10-31"})

    def test_written_with_the_reseed(self):
        cur = MagicMock()
        office.write_program_state(cur)
        sql, params = cur.execute.call_args.args
        self.assertIn("acos.system_state", sql)
        self.assertEqual(params[0], "health_program")
        self.assertEqual(json.loads(params[1])["anchor"], "2026-09-16")


class TestSideValidator(unittest.TestCase):
    def test_real_flow_passes(self):
        office.validate_flow(OFFICE_FLOW)
        office.validate_rows(list(ROWS.values()))

    def test_rejects_a_missing_side(self):
        b = flow()
        b["flow"] = [s for s in b["flow"] if s["step"] != "14"]      # wind release L
        with self.assertRaisesRegex(office.FlowError, "wind: missing side L"):
            office.validate_flow(b)

    def test_rejects_unequal_time(self):
        b = flow()
        next(s for s in b["flow"] if s["step"] == "11")["duration_sec"] = 45
        with self.assertRaisesRegex(office.FlowError, r"twist-supine: R 45s ≠ L 40s"):
            office.validate_flow(b)

    def test_lunge_unit_compares_as_a_group(self):
        b = flow()
        by = {s["step"]: s for s in b["flow"]}
        # R: 20 + 40, L: 40 + 20 — poses differ, the unit is equal.
        by["5"]["duration_sec"], by["6"]["duration_sec"] = 20, 40    # high R, crescent R
        by["7"]["duration_sec"], by["8"]["duration_sec"] = 40, 20    # crescent L, high L
        office.validate_flow(b)
        by["8"]["duration_sec"] = 30
        with self.assertRaisesRegex(office.FlowError, "lunge-unit"):
            office.validate_flow(b)

    def test_rejects_a_pair_whose_partner_is_round_one_only(self):
        """Round 1 balances, round 2 doesn't: easy pose leaves and takes one
        side of the pair with it. The validator checks every round separately,
        which is what makes the round-1-only easy pose safe."""
        b = flow()
        by = {s["step"]: s for s in b["flow"]}
        by["20"].update(side="L", mirror_group="edge")   # easy pose, round 1 only
        by["4"].update(side="R", mirror_group="edge")    # standing forward bend, both
        with self.assertRaisesRegex(office.FlowError, r"edge: missing side L \(round 2\)"):
            office.validate_flow(b)

    def test_side_without_group_is_rejected(self):
        b = flow()
        b["flow"][0]["side"] = "R"
        with self.assertRaises(office.FlowError):
            office.validate_flow(b)


class TestReseedDiff(unittest.TestCase):
    def test_only_flow_days_from_9_19(self):
        rs = _reseed()
        rows = rs.flow_rows(date(2026, 9, 19))
        # SCHEDULE-2 flow days: the office non-lift day + the msp_home Sat/Sun.
        self.assertEqual([r["plan_date"].isoformat() for r in rows], [
            "2026-09-20", "2026-09-25", "2026-09-27", "2026-10-03",
            "2026-10-04", "2026-10-09", "2026-10-11", "2026-10-17",
            "2026-10-18", "2026-10-23", "2026-10-25", "2026-10-31"])
        self.assertTrue(all(office.session_for(r["plan_date"]) == "recovery_flow" for r in rows))
        existing = {r["plan_date"]: {"phase": 1, "week_num": r["week_num"],
                                     "session_type": "rest_mobility",
                                     "display_name": "Rest / Mobility"} for r in rows}
        lines = rs.flow_diff_lines(existing, rows)
        self.assertIn("2026-09-20 Sun", lines[2])
        self.assertIn("recovery_flow  Recovery Flow · home · 33 min · RPE 2", lines[2])
        self.assertIn("Recovery Flow · Richfield · 33 min · RPE 2", lines[3])
        self.assertEqual(lines[-2], "12 Recovery Flow rows rewritten; no other dates touched.")
        self.assertIn('"anchor": "2026-09-16"', lines[-1])

    def test_preflight_needs_migration_033(self):
        rs = _reseed()
        cur = MagicMock()
        cur.fetchone.return_value = ("CHECK ((session_type = ANY (ARRAY['rest_mobility'::text])))",)
        self.assertEqual(rs._flow_preflight(cur)[0], False)
        cur.fetchone.return_value = ("CHECK (... 'recovery_flow'::text ...)",)
        self.assertEqual(rs._flow_preflight(cur)[0], True)

    def test_migration_033_widens_the_check(self):
        sql = (_HERE.parent / "migrations" / "033_recovery_flow_session_type.sql").read_text()
        self.assertIn("plan_session_type_check", sql)
        self.assertIn("'recovery_flow'", sql)
        self.assertIn("'rest_mobility'", sql)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(office_row(THU, plan_id=104))
        self.cur = self.db.cursor()
        self.before = copy.deepcopy(self.db.plan[THU])

    def checkin(self, text):
        return hc.process_checkin(self.cur, text, THU, checkin_id="c", now=NOW, adjust=True)

    def test_pain_day_off_overrides_the_flow(self):
        reply = self.checkin("shoulder pain 4")
        self.assertEqual(reply, "Pain shoulder 4/5 → day off. Reply `original` to undo.")
        row = self.db.plan[THU]
        self.assertEqual(row["session_type"], "rest_mobility")
        self.assertEqual(row["blocks"]["display_name"], "Day off")
        self.assertEqual(row["blocks"]["original"]["session_type"], "recovery_flow")
        self.assertEqual(hc.process_original(self.cur, THU), "Restored — run Recovery Flow as written.")
        self.assertEqual(self.db.plan[THU]["blocks"]["type"], "recovery_flow")

    def test_rising_day_off_overrides_the_flow(self):
        self.db.daily[THU - timedelta(days=2)] = {"soreness": {"pain": {"knee": 1}}}
        self.db.daily[THU - timedelta(days=1)] = {"soreness": {"pain": {"knee": 2}}}
        self.assertIn("(rising) → day off", self.checkin("knee pain 3"))
        self.assertEqual(self.db.plan[THU]["session_type"], "rest_mobility")

    def test_pain_2_and_3_change_nothing(self):
        self.assertEqual(self.checkin("shoulder pain 3"),
                         "Pain shoulder 3/5 — noted.\nCheck-in logged — Recovery Flow as planned.")
        self.assertEqual(self.checkin("legs pain 2"),
                         "Pain legs 2/5 — noted.\nCheck-in logged — Recovery Flow as planned.")
        self.assertEqual(self.db.plan[THU], self.before)

    def test_soreness_and_recovery_rules_do_not_apply(self):
        for text in ("sore shoulder 4 and legs 5", "legs sore 3", "slept 4 energy 1"):
            self.assertEqual(self.checkin(text), "Check-in logged — Recovery Flow as planned.", text)
        self.assertEqual(self.db.plan[THU], self.before)


class TestNudgeAndFollowups(unittest.TestCase):
    def test_nudge_fires_on_a_flow_day(self):
        from artemis.scheduler import ArtemisScheduler
        db = FakeDB(office_row(THU, plan_id=104))

        @contextmanager
        def conn():
            yield db
        sched = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch("knowledge.db.get_connection", conn), \
             patch("artemis.scheduler._local_today", return_value=THU), \
             patch.object(sched, "_post") as post:
            sched.job_checkin_nudge()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1], "No check-in yet — run Recovery Flow as written.")
        self.assertEqual(post.call_args.kwargs["tier"], "health")

    def test_no_nudge_after_a_checkin(self):
        from artemis.scheduler import ArtemisScheduler
        db = FakeDB(office_row(THU, plan_id=104))
        db.daily[THU] = {"energy": 4}

        @contextmanager
        def conn():
            yield db
        sched = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        with patch("knowledge.db.get_connection", conn), \
             patch("artemis.scheduler._local_today", return_value=THU), \
             patch.object(sched, "_post") as post:
            sched.job_checkin_nudge()
        post.assert_not_called()

    def test_no_debrief_nag_and_no_inferred_miss_on_flow_days(self):
        from artemis import health
        plan = {"plan_id": 104, "session_type": "recovery_flow", "target_rpe": 2.0,
                "is_skipped": False}
        with patch("knowledge.db.execute_one", return_value=plan), \
             patch("knowledge.db.execute_query", return_value=[]) as q, \
             patch("knowledge.db.execute_write") as w:
            self.assertIsNone(health.run_nag_check())
            self.assertFalse(health.insert_inferred_summary())
        w.assert_not_called()


class TestWakePost(unittest.TestCase):
    def test_plan_exact_list_with_total(self):
        from artemis import wake
        lines = wake.flow_lines(OFFICE_FLOW)
        self.assertEqual(lines[0], "\U0001f9d8 Today: **Recovery Flow** (office gym) — 40:32 total, hands-free.")
        self.assertEqual(lines[1], "· Seated meditation — 1 min: Sit tall and comfortable, eyes soft, slow breaths.")
        self.assertEqual(lines[2], "· Stretch Trainer — 8 min: Follow the 8 placard stretches")
        self.assertTrue(lines[3].startswith("· Round 1 (13:20): Child's pose 40s · Cobra 40s · Downward dog 40s"))
        self.assertIn("High lunge R 40s · Crescent lunge R 40s · Crescent lunge L 40s · High lunge L 40s", lines[3])
        self.assertIn("Extended puppy 40s · Bridge 40s", lines[3])
        self.assertTrue(lines[3].endswith("Seated mountain 40s · Easy pose 40s"))
        self.assertEqual(lines[4], "· Round 2 (12:40): same order, without easy pose")
        self.assertEqual(lines[5], "· Close: savasana — 3 min")
        self.assertEqual(lines[6], "· Moving between poses: 2:32 in all (included in the total)")
        home = wake.flow_lines(ROWS[SAT]["blocks"])
        self.assertEqual(home[0], "\U0001f9d8 Today: **Recovery Flow** (home) — 32:27 total, hands-free.")
        self.assertEqual(home[-1], "· Moving between poses: 2:27 in all (included in the total)")
        self.assertFalse(any("Stretch Trainer" in l for l in home))

    def test_wake_message_has_no_workout_later_wording(self):
        from artemis import wake
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        for row in (ROWS[THU],
                    # Tonight's 9/17 row: rest_mobility carrying flow blocks.
                    {**rest_row(THU), "blocks": ROWS[THU]["blocks"], "est_duration_min": 42}):
            with patch("artemis.health.get_today_plan", return_value=row), \
                 patch.object(wake, "_weather_line", return_value=None), \
                 patch.object(wake, "_depart_commitments", return_value=[]), \
                 patch.object(wake, "get_timezone_override", return_value=None), \
                 patch.object(wake, "local_now",
                              return_value=dt(2026, 9, 17, 4, 30, tzinfo=ZoneInfo("America/Chicago"))), \
                 patch.object(wake, "local_today", return_value=THU):
                text = wake.build_wake_message(calendar=None, held_health=[])
            self.assertIn("**Recovery Flow** (Richfield) — 32:27 total", text)
            self.assertIn("Round 2 (12:40): same order, without easy pose", text)
            self.assertNotIn("workout is later", text.lower())
            self.assertEqual(wake.prompt_type_for(row), "logging_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
