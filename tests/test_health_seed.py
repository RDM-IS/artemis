"""Smoke tests for the health.plan baseline seed.

Two test classes:
  * TestSeedGeneration — pure unit tests; do not require a database.
  * TestSeedAgainstDB  — only run when RDS_SECRET_ARN + RDS_HOST are set.

Run directly:
    python tests/test_health_seed.py            # generation tests only
    RDS_SECRET_ARN=... RDS_HOST=... python tests/test_health_seed.py  # all tests
"""

import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Optional: jsonschema for validation
try:
    import jsonschema  # noqa: F401
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

from scripts.seed_health_baseline import (  # noqa: E402
    END_DATE,
    PHASE_WINDOWS,
    START_DATE,
    generate_rows,
    load_fixture,
    phase_for_date,
)


class TestSeedGeneration(unittest.TestCase):
    """Pure unit tests against the generation logic."""

    def setUp(self):
        self.fixture = load_fixture()
        self.rows = generate_rows(self.fixture)

    def test_row_count_137(self):
        """Seed produces exactly 137 rows."""
        self.assertEqual(len(self.rows), 137)

    def test_phase_distribution(self):
        """Phase 1: 28, Phase 2: 42, Phase 3: 42, Phase 4: 25."""
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for row in self.rows:
            phase = row[1]
            counts[phase] += 1
        self.assertEqual(counts[1], 28, "Phase 1 should have 28 days")
        self.assertEqual(counts[2], 42, "Phase 2 should have 42 days")
        self.assertEqual(counts[3], 42, "Phase 3 should have 42 days")
        self.assertEqual(counts[4], 25, "Phase 4 should have 25 days")

    def test_no_nulls_in_required(self):
        """Required columns must never be null."""
        for row in self.rows:
            plan_date, phase, week_num, session_type, blocks_json, *_, generated_by, _notes = row
            self.assertIsNotNone(plan_date)
            self.assertIsNotNone(phase)
            self.assertIsNotNone(week_num)
            self.assertIsNotNone(session_type)
            self.assertIsNotNone(blocks_json)
            self.assertEqual(generated_by, "baseline")

    def test_session_types_match_dow(self):
        """Each plan_date's session_type follows the day-of-week mapping."""
        mapping = self.fixture["day_to_session"]
        for row in self.rows:
            plan_date, _, _, session_type, *_ = row
            expected = mapping[plan_date.weekday()]
            self.assertEqual(
                session_type, expected,
                f"{plan_date} ({plan_date.strftime('%A')}) → expected {expected}, got {session_type}",
            )

    def test_week_num_continuous(self):
        """Week numbers must be 1..19 contiguously."""
        weeks_seen = sorted({row[2] for row in self.rows})
        self.assertEqual(weeks_seen, list(range(1, 20)))

    def test_phase_window_boundaries(self):
        """The phase windows in PHASE_WINDOWS must align with the spec."""
        expected = [
            (1, date(2026, 6, 6),  date(2026, 7, 3)),
            (2, date(2026, 7, 4),  date(2026, 8, 14)),
            (3, date(2026, 8, 15), date(2026, 9, 25)),
            (4, date(2026, 9, 26), date(2026, 10, 20)),
        ]
        for window, (p_exp, s_exp, e_exp) in zip(PHASE_WINDOWS, expected):
            phase, start, end = window[0], window[1], window[2]
            self.assertEqual(phase, p_exp)
            self.assertEqual(start, s_exp)
            self.assertEqual(end, e_exp)

    def test_total_days(self):
        """Total date range matches the seed scope."""
        total = (END_DATE - START_DATE).days + 1
        self.assertEqual(total, 137)

    def test_phase_for_date_lookup(self):
        """phase_for_date returns the correct phase + week."""
        # Day 1 of Phase 1 → week 1
        self.assertEqual(phase_for_date(date(2026, 6, 6)), (1, 1))
        # Day 1 of Phase 2 → week 5
        self.assertEqual(phase_for_date(date(2026, 7, 4)), (2, 5))
        # Day 1 of Phase 3 → week 11
        self.assertEqual(phase_for_date(date(2026, 8, 15)), (3, 11))
        # Day 1 of Phase 4 → week 17
        self.assertEqual(phase_for_date(date(2026, 9, 26)), (4, 17))
        # Last day → week 19
        last_phase, last_week = phase_for_date(date(2026, 10, 20))
        self.assertEqual(last_phase, 4)
        self.assertEqual(last_week, 19)

    def test_blocks_jsonb_well_formed(self):
        """Each blocks JSON is parseable and has a known type."""
        valid_types = {"circuit", "intervals", "walk", "mobility", "rest"}
        for row in self.rows:
            blocks = json.loads(row[4])
            self.assertIn(blocks.get("type"), valid_types)

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
    def test_blocks_validate_against_schema(self):
        """Every blocks row validates against scripts/data/blocks.schema.json."""
        import jsonschema

        schema_path = _REPO_ROOT / "scripts" / "data" / "blocks.schema.json"
        with open(schema_path) as f:
            schema = json.load(f)

        for row in self.rows:
            blocks = json.loads(row[4])
            try:
                jsonschema.validate(blocks, schema)
            except jsonschema.ValidationError as e:
                self.fail(
                    f"blocks failed schema validation for date={row[0]} "
                    f"session_type={row[3]}: {e.message}"
                )

    def test_phase_4_week_19_deload(self):
        """Week 19 strength sessions show deload notation."""
        for row in self.rows:
            plan_date, phase, week_num, session_type, blocks_json, *_ = row
            if week_num == 19 and session_type.startswith("strength"):
                blocks = json.loads(blocks_json)
                notes = " ".join(blocks.get("setup_notes", []))
                self.assertIn(
                    "DELOAD", notes.upper(),
                    f"{plan_date}: week 19 strength should be marked as deload",
                )


class TestSeedAgainstDB(unittest.TestCase):
    """Integration tests — only run if RDS env is configured."""

    @classmethod
    def setUpClass(cls):
        if not (os.environ.get("RDS_SECRET_ARN") and os.environ.get("RDS_HOST")):
            raise unittest.SkipTest("RDS_SECRET_ARN/RDS_HOST not set")

    def test_seed_idempotent(self):
        """Running seed twice produces no new rows on the second pass."""
        from scripts.seed_health_baseline import seed
        # First run inserts (or no-ops if already there)
        seed(force=False)
        # Second run is a no-op
        inserted = seed(force=False)
        self.assertEqual(inserted, 0)

    def test_row_counts_in_db(self):
        """After seed, health.plan has exactly 137 rows w/ correct phase distribution."""
        from scripts.seed_health_baseline import get_connection, seed
        seed(force=False)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM health.plan")
                self.assertEqual(cur.fetchone()[0], 137)

                cur.execute(
                    "SELECT phase, COUNT(*) FROM health.plan GROUP BY phase ORDER BY phase"
                )
                rows = cur.fetchall()
                self.assertEqual(rows, [(1, 28), (2, 42), (3, 42), (4, 25)])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
