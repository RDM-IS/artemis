"""PUBLIC-FIXTURES: no test value may sit inside Ryan's real physiological range.

artemis and gym-display are PUBLIC repositories. CLAUDE.md requires synthetic
values OUTSIDE the real range -- not merely values that happen not to match,
because a reader cannot tell an in-range invented number from a recorded one.

This was not being met. On 2026-09-29 a scan found 51 in-range values across 12
files in the two repos, and `tests/fixtures/overview.json` in gym-display
carried `2026-09-16 -> 284.5 lb`, which is an EXACT date-and-value match to
health.daily_state. The 2026-09-25 history scan concluded the fixtures were all
synthetic; that conclusion was wrong.

The bands below are deliberately WIDER than the recorded range. A value one
pound outside it is still indistinguishable from a real one.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Recorded ranges as of 2026-09-29 (health.daily_state): weight 280.5-286.0,
#: resting HR 59-71. Banded out to catch near-misses.
WEIGHT_BAND = (265.0, 300.0)
RHR_BAND = (52, 78)

_WEIGHT = re.compile(r"\b(\d{3})\.(\d)\b")
_WEIGHT_CTX = re.compile(r"weight|bodyweight|\blb\b|lbs", re.I)
_RHR = re.compile(r"(resting_hr|resting_heart_rate|\brhr\b)\D{0,14}?\b(\d{2})\b", re.I)


def _files():
    for sub in ("tests", "scripts"):
        for f in (ROOT / sub).rglob("*"):
            if f.is_file() and f.suffix in (".py", ".json"):
                if f.name == "test_public_fixtures.py":
                    continue
                yield f


class TestNoRealPhysiologyInFixtures(unittest.TestCase):
    def test_no_weight_inside_the_real_range(self):
        bad = []
        for f in _files():
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if not _WEIGHT_CTX.search(line):
                    continue
                for m in _WEIGHT.finditer(line):
                    v = float(m.group(0))
                    if WEIGHT_BAND[0] <= v <= WEIGHT_BAND[1]:
                        bad.append(f"{f.relative_to(ROOT)}:{i} {v}")
        self.assertEqual(bad, [], "weights inside the real range: " + "; ".join(bad))

    def test_no_resting_hr_inside_the_real_range(self):
        bad = []
        for f in _files():
            for i, line in enumerate(f.read_text().splitlines(), 1):
                for m in _RHR.finditer(line):
                    v = int(m.group(2))
                    if RHR_BAND[0] <= v <= RHR_BAND[1]:
                        bad.append(f"{f.relative_to(ROOT)}:{i} {v}")
        self.assertEqual(bad, [], "resting HR inside the real range: " + "; ".join(bad))

    def test_the_guard_actually_bites(self):
        """A scan that cannot fail is not a guard. Proved on a synthetic line
        rather than by trusting the regex."""
        line = '        "weight_lbs": 283.4,'
        self.assertTrue(_WEIGHT_CTX.search(line))
        v = float(_WEIGHT.search(line).group(0))
        self.assertTrue(WEIGHT_BAND[0] <= v <= WEIGHT_BAND[1])
        rhr = 'self.assertEqual(state["resting_hr"], 65)'
        self.assertEqual(int(_RHR.search(rhr).group(2)), 65)


if __name__ == "__main__":
    unittest.main()
