"""BW-CIRCUIT — the swap, the gate-driven intensity, and the no-static-rest rule.

Content is a DRAFT pending Ryan's approval; these tests are about the shape and
the rules, not about approving the movements.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import json
import unittest
from datetime import date

from artemis import bw_circuit as bw

DAY = date(2099, 3, 4)


class Cur:
    def __init__(self, *, logged=False, row=None):
        self.logged, self.row, self._res, self.updates = logged, row, [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._res = []
        if "FROM health.session_log" in s:
            self._res = [(1,)] if self.logged else []
        elif s.startswith("SELECT plan_id FROM health.plan"):
            self._res = [self.row] if self.row else []
        elif s.startswith("SELECT plan_id, session_type, blocks"):
            self._res = [self.row] if self.row else []
        elif s.startswith("SELECT session_type FROM health.plan"):
            self._res = [self.row] if self.row else []
        elif s.startswith("UPDATE health.plan"):
            self.updates.append(params)

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


def a_row(session_type="strength_a"):
    return {"plan_id": 42, "session_type": session_type,
            "blocks": {"display_name": "Strength A", "location": "the office",
                       "location_key": "office", "type": "circuit"},
            "target_rpe": 7.0, "target_hr_zone": 3, "est_duration_min": 55}


class TestTheContentIsMarkedDraft(unittest.TestCase):
    def test_the_source_says_so(self):
        self.assertTrue(bw.CONTENT_IS_A_DRAFT)
        blocks, *_ = bw.build(gate={"ok": True})
        self.assertTrue(blocks["content_is_draft"])


class TestNoStaticRest(unittest.TestCase):
    """Ryan, 11:29: every 'rest' is active recovery. A circuit whose rest is
    standing still is a circuit that stops."""

    def test_no_step_is_called_rest(self):
        blocks, *_ = bw.build(gate={"ok": True})
        for s in blocks["steps"]:
            with self.subTest(s["name"]):
                self.assertNotIn("rest", s["name"].lower())

    def test_every_recovery_segment_is_marching(self):
        blocks, *_ = bw.build(gate={"ok": True})
        recover = [s for s in blocks["steps"] if s.get("kind") == "recover"]
        self.assertTrue(recover)
        self.assertTrue(all(s["name"] == bw.RECOVER_LOW for s in recover))

    def test_impact_on_swaps_marching_for_jogging(self):
        blocks, *_ = bw.build(gate={"ok": True}, impact=True)
        recover = [s for s in blocks["steps"] if s.get("kind") == "recover"]
        self.assertTrue(all(s["name"] == bw.RECOVER_IMPACT for s in recover))

    def test_even_the_warm_up_and_cool_down_move(self):
        blocks, *_ = bw.build(gate={"ok": True})
        for kind in ("warmup", "cooldown"):
            seg = [s for s in blocks["steps"] if s.get("kind") == kind]
            with self.subTest(kind):
                self.assertTrue(seg)
                self.assertTrue(all(s["duration_sec"] > 0 for s in seg))


class TestTheGateDrivesIntensity(unittest.TestCase):
    def test_a_passed_gate_gives_the_full_session(self):
        blocks, rpe, zone, _est = bw.build(gate={"ok": True})
        self.assertEqual((blocks["work_sec"], blocks["recover_sec"]), bw.FULL[:2])
        self.assertEqual(zone, 4)
        self.assertEqual(blocks["ran_as"], "full")
        self.assertNotIn("moderate_reason", blocks)

    def test_a_blocked_gate_gives_the_moderate_session_and_names_why(self):
        blocks, rpe, zone, _est = bw.build(gate={"ok": False, "reason": "you haven't sent x"})
        self.assertEqual((blocks["work_sec"], blocks["recover_sec"]), bw.MODERATE[:2])
        self.assertEqual(zone, 3)
        self.assertEqual(blocks["moderate_reason"], "you haven't sent x")
        self.assertTrue(any("Moderate today" in n for n in blocks["setup_notes"]))

    def test_NO_gate_is_a_fail_not_a_pass(self):
        """Same rule as cardio_intervals: Zone 4 is the sharpest thing in the
        program and 'I could not check' must not look like 'go'."""
        blocks, _rpe, zone, _est = bw.build(gate=None)
        self.assertEqual(zone, 3)
        self.assertEqual(blocks["ran_as"], "moderate")
        self.assertIn("not been evaluated", blocks["moderate_reason"])


class TestEquipmentVariants(unittest.TestCase):
    """The pull station is the weak link without kit, and the draft says so
    rather than pretending otherwise."""

    def _pull(self, **kw):
        blocks, *_ = bw.build(gate={"ok": True}, **kw)
        return [s["name"] for s in blocks["steps"] if s.get("kind") == "work"][1]

    def test_trx_gives_a_trx_row(self):
        self.assertEqual(self._pull(kit={"trx"}), "TRX row")

    def test_bands_give_a_band_row(self):
        self.assertEqual(self._pull(kit={"bands"}), "Band row")

    def test_no_kit_falls_back_honestly(self):
        self.assertEqual(self._pull(kit=set()), "Table row or towel row")

    def test_the_other_stations_ignore_the_kit(self):
        a, *_ = bw.build(gate={"ok": True}, kit={"trx"})
        b, *_ = bw.build(gate={"ok": True}, kit=set())
        work = lambda bl: [s["name"] for s in bl["steps"] if s.get("kind") == "work"]
        self.assertEqual(work(a)[0], work(b)[0])            # push unchanged
        self.assertEqual(work(a)[2], work(b)[2])            # legs unchanged
        self.assertNotEqual(work(a)[1], work(b)[1])         # only the pull differs


class TestTheShape(unittest.TestCase):
    def test_it_is_about_25_minutes(self):
        for gate in ({"ok": True}, None):
            with self.subTest(gate):
                _b, _r, _z, est = bw.build(gate=gate)
                self.assertGreaterEqual(est, 22)
                self.assertLessEqual(est, 28)

    def test_it_renders_on_the_flow_screen(self):
        """Reusing recovery_flow's renderer is what makes it hands-free."""
        blocks, *_ = bw.build(gate={"ok": True})
        self.assertEqual(blocks["type"], "recovery_flow")
        self.assertEqual(blocks["session_type"], "bodyweight_circuit")

    def test_no_step_asks_for_input(self):
        """No reps, no rounds, no per-set logging — nothing waits on him."""
        blocks, *_ = bw.build(gate={"ok": True})
        for s in blocks["steps"]:
            with self.subTest(s["name"]):
                self.assertNotIn("reps", s)
                self.assertGreater(s["duration_sec"], 0)

    def test_the_card_tells_him_to_start_a_watch_workout(self):
        """The web app cannot read the watch live; the zone split comes from
        ZONE-0 afterwards, which needs dense samples."""
        blocks, *_ = bw.build(gate={"ok": True})
        self.assertTrue(any("watch" in n.lower() for n in blocks["setup_notes"]))


class TestTheSwap(unittest.TestCase):
    def test_it_refuses_a_day_that_is_already_logged(self):
        ok, why = bw.can_swap(Cur(logged=True, row=a_row()), DAY)
        self.assertFalse(ok)
        self.assertIn("already has a logged session", why)

    def test_it_refuses_a_day_with_no_planned_session(self):
        ok, why = bw.can_swap(Cur(logged=False, row=None), DAY)
        self.assertFalse(ok)

    def test_it_allows_an_unlogged_planned_day(self):
        ok, _why = bw.can_swap(Cur(logged=False, row=a_row()), DAY)
        self.assertTrue(ok)

    def test_the_swap_keeps_the_original(self):
        cur = Cur(row=a_row())
        out = bw.swap_row(cur, DAY, gate={"ok": True})
        self.assertEqual(out["replaced"], "strength_a")
        blocks = json.loads(cur.updates[0][0])
        self.assertEqual(blocks["original"]["session_type"], "strength_a")
        self.assertEqual(blocks["original"]["blocks"]["display_name"], "Strength A")

    def test_the_swap_persists_the_gate_answer_on_the_row(self):
        """A same-day swap evaluates at swap time, so the row has to carry what
        it decided — the card must not re-derive it later and disagree."""
        cur = Cur(row=a_row())
        bw.swap_row(cur, DAY, gate=None)
        blocks = json.loads(cur.updates[0][0])
        self.assertEqual(blocks["ran_as"], "moderate")
        self.assertIn("moderate_reason", blocks)

    def test_undo_restores_the_original_rather_than_rebuilding(self):
        """A rebuild would silently pick up any template change since."""
        swapped = {"plan_id": 42, "session_type": "bodyweight_circuit",
                   "blocks": {"original": {"session_type": "strength_a",
                                           "blocks": {"display_name": "Strength A"},
                                           "target_rpe": 7.0, "target_hr_zone": 3,
                                           "est_duration_min": 55}}}
        cur = Cur(row=swapped)
        out = bw.undo(cur, DAY)
        self.assertEqual(out["restored"], "strength_a")
        self.assertEqual(cur.updates[0][0], "strength_a")

    def test_undo_on_a_day_that_is_not_a_circuit_does_nothing(self):
        cur = Cur(row=a_row())
        self.assertIsNone(bw.undo(cur, DAY))
        self.assertEqual(cur.updates, [])


class TestTheCommandsDoNotCollide(unittest.TestCase):
    def test_yes_circuit_is_its_own_qualified_form(self):
        from artemis import main as m
        self.assertEqual(m._QUALIFIED_SUFFIX["circuit"], "circuit")
        self.assertIn("circuit", m.consuming_flows())

    def test_yes_swap_is_still_the_modality_swap(self):
        """Two flows answering to one word is the collision CONFIRM-ARB exists
        to prevent."""
        from artemis import main as m
        self.assertEqual(m._QUALIFIED_SUFFIX["swap"], "swap")
        self.assertNotEqual(m._QUALIFIED_SUFFIX["swap"], m._QUALIFIED_SUFFIX["circuit"])

    def test_the_swap_command_matches_the_forms_and_nothing_else(self):
        from artemis import main as m
        for yes in ("swap today for bodyweight circuit",
                    "swap tomorrow for a bodyweight circuit",
                    "Swap 2099-03-04 for the bodyweight circuit."):
            with self.subTest(yes):
                self.assertTrue(m._SWAP_CIRCUIT_RE.match(yes))
        for no in ("swap", "swap today", "yes swap", "bodyweight circuit",
                   "swap today for circuit"):
            with self.subTest(no):
                self.assertIsNone(m._SWAP_CIRCUIT_RE.match(no))


if __name__ == "__main__":
    unittest.main()
