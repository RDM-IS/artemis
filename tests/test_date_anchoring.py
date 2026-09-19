"""WAKE-1 — "today" is anchored to the ACTIVE timezone, not a literal.

Class (a) — anything that means "today for Ryan" — must go through
quiet_hours.local_tz / local_now / local_today, or through parameterized SQL
`(now() AT TIME ZONE %s)::date`. Only class (b) (config defaults, the city map,
historical migrations, display of home time) may name America/Chicago.

This is the guard that keeps a new module from quietly re-pinning the schedule
to Central while Ryan is away.

Run:
    python3.11 tests/test_date_anchoring.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

PKG = _REPO_ROOT / "artemis"

# Class (b): files allowed to name the home zone, and why.
_ALLOWED_FILES = {
    "config.py": "defines HOME_TIMEZONE — the default itself",
    "quiet_hours.py": "the city/zone map and the home fallback live here",
}


def _python_sources():
    for path in sorted(PKG.glob("*.py")):
        yield path, path.read_text()


def _code_lines(text: str):
    """(lineno, line) for lines that are not comments and not inside a docstring."""
    in_doc = False
    quote = ""
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if in_doc:
            if quote in stripped:
                in_doc = False
            continue
        if stripped.startswith(("'''", '"""')):
            quote = stripped[:3]
            # A one-line docstring opens and closes on the same line.
            if not (len(stripped) > 3 and stripped.endswith(quote)):
                in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        yield i, line


class TestNoHardCodedHomeZone(unittest.TestCase):
    def test_no_class_a_america_chicago_literal(self):
        offenders = []
        for path, text in _python_sources():
            if path.name in _ALLOWED_FILES:
                continue
            for lineno, line in _code_lines(text):
                if "America/Chicago" in line:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "hard-coded America/Chicago outside config.py — use "
            "quiet_hours.local_tz()/local_today() so the schedule follows the "
            "active timezone:\n" + "\n".join(offenders))

    def test_no_literal_time_zone_in_sql_date_anchors(self):
        """`at time zone 'America/Chicago'` must become `AT TIME ZONE %s`."""
        rx = re.compile(r"at\s+time\s+zone\s+'", re.IGNORECASE)
        offenders = []
        for path, text in _python_sources():
            for lineno, line in _code_lines(text):
                if rx.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "SQL date anchor with a literal zone — pass the active "
                         "timezone as a parameter:\n" + "\n".join(offenders))

    def test_bare_current_date_is_not_used_for_today(self):
        """RDS runs UTC — bare current_date is a day ahead of Ryan after ~19:00."""
        rx = re.compile(r"\bcurrent_date\b", re.IGNORECASE)
        offenders = []
        for path, text in _python_sources():
            for lineno, line in _code_lines(text):
                if rx.search(line) and "AT TIME ZONE" not in line.upper():
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "bare current_date (UTC) used as 'today':\n" + "\n".join(offenders))

    def test_allowed_files_still_exist(self):
        """A rename must not silently widen the allow-list."""
        for name in _ALLOWED_FILES:
            with self.subTest(file=name):
                self.assertTrue((PKG / name).exists())


class TestLocalHelpersAreTheAnchor(unittest.TestCase):
    def test_modules_that_anchor_today_import_a_local_helper(self):
        """Every module doing local-date math routes through quiet_hours."""
        anchoring = ["health.py", "inbox.py", "vault.py", "commitments.py",
                     "dossier.py", "morning_brief.py", "ops_api.py", "scheduler.py",
                     "wake.py"]
        for name in anchoring:
            with self.subTest(module=name):
                text = (PKG / name).read_text()
                self.assertRegex(
                    text, r"local_tz|local_today|local_now|get_active_timezone",
                    f"{name} does local-date work but never asks for the active timezone")

    def test_the_helpers_exist_and_agree(self):
        from artemis import quiet_hours
        self.assertEqual(quiet_hours.local_today(), quiet_hours.local_now().date())
        self.assertEqual(str(quiet_hours.local_tz()), quiet_hours.get_active_timezone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
