"""RECOMMEND — the highlight rule. Synthetic dates (2031)."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import recommend as rec  # noqa: E402

NOW = datetime(2031, 3, 11, 16, 30, tzinfo=timezone.utc)   # a Tuesday
LADDER = {"row_kind": "ladder", "cardio": {"modality": "row"}}


def r(**kw):
    base = dict(now=NOW, planned_type="cardio_z2", planned_blocks=LADDER, planned_logged=False,
                room_holds_strength=True, next_strength="strength_c",
                last_strength_at=NOW - timedelta(days=5), last_more={})
    base.update(kw)
    return rec.recommend(**base)


class TestPrimary(unittest.TestCase):
    def test_planned_rest_is_recovery(self):
        self.assertEqual(r(planned_type="rest", planned_blocks={})["primary"], "recovery")

    def test_an_unlogged_plan_is_its_own_button(self):
        self.assertEqual(r()["primary"], "row")
        self.assertEqual(r(planned_blocks={"row_kind": "circuit"})["primary"], "row_circuit")
        self.assertEqual(r(planned_blocks={"cardio": {"modality": "bike"}})["primary"], "ride")
        self.assertEqual(r(planned_blocks={"cardio": {"modality": "treadmill"}})["primary"], "run")
        self.assertEqual(r(planned_type="strength_b", planned_blocks={})["primary"], "strength")

    def test_ryans_example_row_done_then_strength_c(self):
        out = r(planned_logged=True)
        self.assertEqual((out["primary"], out["strength"]), ("strength", "strength_c"))
        self.assertIn("Row done", out["reason"])

    def test_a_lift_inside_48h_is_recovery(self):
        out = r(planned_logged=True, last_strength_at=NOW - timedelta(hours=30))
        self.assertEqual(out["primary"], "recovery")
        self.assertIn("48 h", out["reason"])

    def test_a_room_without_strength_is_recovery(self):
        self.assertEqual(r(planned_logged=True, room_holds_strength=False)["primary"], "recovery")

    def test_an_unreadable_room_highlights_nothing(self):
        self.assertFalse(r(planned_logged=True, room_holds_strength=None)["available"])

    def test_no_plan_falls_to_strength(self):
        self.assertEqual(r(planned_type=None, planned_blocks=None)["primary"], "strength")


class TestMore(unittest.TestCase):
    def test_did_mobility_last_highlights_core(self):
        out = r(last_more={"core": NOW - timedelta(days=3), "mobility": NOW - timedelta(days=1)})
        self.assertEqual(out["more"], ["core"])

    def test_only_one_ever_done_highlights_the_other(self):
        self.assertEqual(r(last_more={"core": NOW - timedelta(days=1)})["more"], ["mobility"])

    def test_none_done_highlights_none(self):
        self.assertEqual(r()["more"], [])


if __name__ == "__main__":
    unittest.main()


class TestBodyweight(unittest.TestCase):
    """SESSION-OPEN 3a (Ryan, 2026-11-10): no lift in 48 h and the room can't
    hold the next strength session -> Bodyweight, where the room lists it."""

    def test_highlighted_when_the_room_cant_hold_the_next_lift(self):
        out = r(planned_logged=True, room_holds_strength=False, room_holds_bodyweight=True)
        self.assertEqual((out["primary"], out["label"], out["strength"]), ("bodyweight", "Bodyweight", None))

    def test_not_after_a_lift_in_the_last_48h(self):
        out = r(planned_logged=True, room_holds_strength=False, room_holds_bodyweight=True,
                last_strength_at=NOW - timedelta(hours=20))
        self.assertEqual(out["primary"], "recovery")

    def test_strength_still_wins_where_the_room_holds_it(self):
        out = r(planned_logged=True, room_holds_strength=True, room_holds_bodyweight=True)
        self.assertEqual(out["primary"], "strength")

    def test_a_room_without_it_falls_to_recovery(self):
        out = r(planned_logged=True, room_holds_strength=False, room_holds_bodyweight=False)
        self.assertEqual(out["primary"], "recovery")

    def test_unreadable_is_unavailable_never_a_guess(self):
        self.assertFalse(r(planned_logged=True, room_holds_strength=False,
                           room_holds_bodyweight=None)["available"])

    def test_it_is_a_button(self):
        self.assertIn("bodyweight", rec.BUTTONS)
