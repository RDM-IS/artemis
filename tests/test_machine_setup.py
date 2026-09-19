"""MACHINE-SETUP — named seat/pad/range positions (knowledge/machine_setup.py),
the debrief parser's deterministic extraction, and the report export. No DB.

Run:
    python3 tests/test_machine_setup.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import machine_setup as ms  # noqa: E402


class TestExtract(unittest.TestCase):
    def test_spoken_phrases(self):
        cases = {
            "leg press 12 at 180 seat 4, pin 7 RPE 7": {"seat": 4, "range": 7},
            "seat at 4 back pad 3": {"seat": 4, "pad": 3},
            "chest pad 2, seat #5": {"seat": 5, "pad": 2},
            "range pin 7": {"range": 7},
            "ROM 2": {"range": 2},
            "seat height 6": {"seat": 6},
            "setting 5": {"seat": 5},                      # legacy single number
            "setting 5 seat 3": {"seat": 3},               # an explicit seat wins
            "seat 4.5": {"seat": 4.5},
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ms.extract_setup(text), want)

    def test_no_false_positives(self):
        for text in ("leg press 12 at 180 RPE 7", "rep range 10", "reps range 8", "felt strong",
                     "spin bike 20 min", "", None):
            with self.subTest(text=text):
                self.assertEqual(ms.extract_setup(text), {})


class TestTokens(unittest.TestCase):
    def test_parse_matches_gym_display_tokens(self):
        self.assertEqual(ms.parse_setup("finisher; seat=4; pad=3; range=2; pain=shoulder:2; machine taken"),
                         {"seat": 4, "pad": 3, "range": 2})
        self.assertEqual(ms.parse_setup("setting=7; machine taken"), {"seat": 7})
        self.assertEqual(ms.parse_setup("seat=4; setting=7"), {"seat": 4})
        self.assertEqual(ms.parse_setup("felt strong"), {})

    def test_tokens_and_format_are_fixed_order(self):
        s = {"range": 7, "seat": 4}
        self.assertEqual(ms.setup_tokens(s), "seat=4; range=7")
        self.assertEqual(ms.format_setup({"pad": 2.0, "seat": 5.0}), "seat 5 · pad 2")
        self.assertEqual(ms.format_setup({}), "")

    def test_with_setup_tokens(self):
        self.assertEqual(ms.with_setup_tokens(None, "leg press seat 4, pin 7"), "seat=4; range=7")
        # the LLM copied the phrase into notes: moved into tokens, the rest kept
        self.assertEqual(ms.with_setup_tokens("seat 4, felt strong", "leg press seat 4"),
                         "seat=4; felt strong")
        self.assertEqual(ms.with_setup_tokens("felt strong", "lat pulldown 70"), "felt strong")
        self.assertEqual(ms.with_setup_tokens("seat=4", "seat 9"), "seat=4")   # idempotent


class TestDebrief(unittest.TestCase):
    TEXT = ("Leg press 12 at 180 seat 4, pin 7 RPE 7, lat pulldown 70 x 12 seat 5 back pad 2 "
            "felt strong, DB RDLs 10 at 40")

    def _parse(self, exercises):
        from artemis import health
        parsed = {"exercises": exercises, "session_summary": {"rpe_actual": 7}}
        with patch.object(health, "_call_claude_json", return_value=parsed):
            rows = health.parse_workout_debrief(self.TEXT)
        return {r.exercise: r for r in rows}

    @staticmethod
    def row(name, **kw):
        return {"exercise": name, "log_type": "strength_set", "reps_done": 12, "weight_lbs": 100,
                "notes": None, **kw}

    def test_setup_from_verbatim_source_text(self):
        rows = self._parse([
            self.row("Leg press", source_text="Leg press 12 at 180 seat 4, pin 7 RPE 7"),
            self.row("Lat pulldown", notes="felt strong",
                     source_text="lat pulldown 70 x 12 seat 5 back pad 2 felt strong"),
            self.row("DB Romanian deadlift", source_text="DB RDLs 10 at 40"),
        ])
        self.assertEqual(rows["Leg press"].notes, "seat=4; range=7")
        self.assertEqual(rows["Lat pulldown"].notes, "seat=5; pad=2; felt strong")
        self.assertIsNone(rows["DB Romanian deadlift"].notes)
        self.assertEqual(rows["session_summary"].rpe_actual, 7)

    def test_an_invented_source_text_is_not_trusted(self):
        # source_text that is not in the input is ignored; the exercise's own
        # stretch of the text (by name) is used instead.
        rows = self._parse([
            self.row("Leg press", source_text="leg press seat 9"),
            self.row("Lat pulldown"),
        ])
        self.assertEqual(rows["Leg press"].notes, "seat=4; range=7")
        self.assertEqual(rows["Lat pulldown"].notes, "seat=5; pad=2")

    def test_no_span_no_setup(self):
        # renamed by the LLM and no source_text: nothing to read, nothing guessed
        rows = self._parse([self.row("DB Romanian deadlift")])
        self.assertIsNone(rows["DB Romanian deadlift"].notes)

    def test_prompt_asks_for_verbatim_source_text(self):
        from artemis import health
        self.assertIn('"source_text"', health._DEBRIEF_SYSTEM)
        self.assertIn("VERBATIM", health._DEBRIEF_SYSTEM)


class TestExport(unittest.TestCase):
    def test_settings_in(self):
        import export_report as er
        self.assertEqual(er.settings_in("seat=4; pad=3; machine taken"), "seat 4 · pad 3")
        self.assertEqual(er.settings_in("setting=7"), "seat 7")
        self.assertIsNone(er.settings_in("felt strong"))
        self.assertIsNone(er.settings_in(None))


if __name__ == "__main__":
    unittest.main()
