"""CHECKIN-GATE (2026-09-26) — the gate admits every word the parser honours.

Found by the CHECKIN-DEAD diagnostic: `classify()` gated on a hand-kept token
list containing only `sore`/`soreness`, so a pain or tightness report parsed
cleanly and was then dropped with no reply. PAIN-1 had never run on real input.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import unittest

from artemis import health_regions as hr
from artemis.health_checkin import PAIN_WORDS, SORE_WORDS, classify, parse_checkin


class TestGateMatchesParserVocabulary(unittest.TestCase):
    def test_every_body_word_with_a_region_is_a_checkin(self):
        # Derived from the parser's own lists, so a word added there is
        # covered here with no edit — the drift this guards against.
        for w in SORE_WORDS:
            with self.subTest(word=w):
                self.assertEqual(classify(f"left knee {w} 3"), "checkin")

    def test_pain_words_reach_the_pain_field(self):
        for w in PAIN_WORDS:
            with self.subTest(word=w):
                self.assertEqual(parse_checkin(f"knee {w} 3").pain, {"knee": 3})

    def test_pain_alone_is_claimed(self):
        self.assertEqual(classify("knee pain 3"), "checkin")
        self.assertEqual(classify("sharp pain lower back 4"), "checkin")

    def test_region_aliases_exist(self):
        self.assertTrue(hr.ALIASES)


class TestGateStaysClosedOnOrdinaryEnglish(unittest.TestCase):
    """The channel is always-listen: a body word without a body region is not a
    check-in, even with a number in the sentence."""

    def test_non_checkins(self):
        for t in (
            "I pulled the Q3 report, 3 pages",
            "that was a sharp meeting at 3",
            "the tight deadline is 10/3",
            "strain on the budget is 5 percent",
            "pulled 2 threads from the inbox",
            "injury report for the team",
            "show me a chart of my weight",
        ):
            with self.subTest(text=t):
                self.assertIsNone(classify(t))


class TestExistingShapesUnchanged(unittest.TestCase):
    def test_prior_formats_still_claim(self):
        for t in (
            "Sleep 6, energy 4, soreness 0, 281.5",
            "slept 7 energy 4 sore 0 weight 283",
            "energy 4 sore 0",
            "rhr 58",
        ):
            with self.subTest(text=t):
                self.assertEqual(classify(t), "checkin")


if __name__ == "__main__":
    unittest.main()


class TestClausesWithoutCommas(unittest.TestCase):
    """Dictated replies drop commas. A score followed by a new body clause ends
    the clause, so a pain word can't leak onto the next region."""

    def test_pain_does_not_leak_to_the_next_region(self):
        ci = parse_checkin("knee pain 3 back sore 1")
        self.assertEqual(ci.pain, {"knee": 3})
        self.assertEqual(ci.soreness, {"back": 1})

    def test_each_region_keeps_its_own_score(self):
        ci = parse_checkin("back tight 2 shoulder ache 1")
        self.assertEqual(ci.soreness, {"back": 2, "shoulder": 1})

    def test_sides_stay_with_their_clause(self):
        ci = parse_checkin("left knee pain 3 out of 5 right hip stiff 2")
        self.assertEqual(ci.pain, {"knee": 3})
        self.assertEqual(ci.pain_sides, {"knee": "left"})
        self.assertEqual(ci.soreness, {"hip": 2})
        self.assertEqual(ci.sides, {"hip": "right"})

    def test_shared_score_still_shared(self):
        # "and"-joined regions before one score still share it.
        ci = parse_checkin("quads and hamstrings sore 3")
        self.assertEqual(ci.soreness, {"quads": 3, "hamstrings": 3})

    def test_score_first_form_is_one_clause(self):
        # The number before its region ("sore 1 right knee") must not split.
        ci = parse_checkin("Sleep 9, energy 4, sore 1 right knee, weight 286")
        self.assertEqual(ci.soreness, {"knee": 1})
        self.assertEqual(parse_checkin("pain 5 knee").pain, {"knee": 5})
