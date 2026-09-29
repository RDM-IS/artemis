"""PREP-1 — the sync's own rules: rate limit, watermark, sweep cadence, TEST rows.

These are the parts of the sync that have no database and no network, so they can
be asserted directly rather than inferred from a run. PUBLIC-FIXTURES: no real
Notion ids and no real ingredient names.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD

import unittest
from datetime import datetime, timedelta, timezone

from artemis import prep_notion as pn


class TestTokenBucket(unittest.TestCase):
    """~3 requests/second on average, with a small burst."""

    def setUp(self):
        self.slept = []
        self.now = [0.0]

    def bucket(self, rate=3.0, burst=3.0):
        def sleep(s):
            self.slept.append(s)
            self.now[0] += s
        return pn.TokenBucket(rate=rate, burst=burst, sleep=sleep,
                              clock=lambda: self.now[0])

    def test_the_burst_is_free(self):
        b = self.bucket()
        for _ in range(3):
            b.take()
        self.assertEqual(self.slept, [])

    def test_past_the_burst_it_waits_one_over_the_rate(self):
        b = self.bucket()
        for _ in range(3):
            b.take()
        b.take()
        self.assertEqual(len(self.slept), 1)
        self.assertAlmostEqual(self.slept[0], 1 / 3.0, places=6)

    def test_tokens_refill_with_elapsed_time(self):
        b = self.bucket()
        for _ in range(3):
            b.take()
        self.now[0] += 1.0          # a second passes: 3 tokens back
        for _ in range(3):
            b.take()
        self.assertEqual(self.slept, [])

    def test_a_long_run_averages_the_rate(self):
        b = self.bucket()
        for _ in range(33):
            b.take()
        # 33 requests, 3 free from the burst: 30 must be paced at 1/3 s each.
        self.assertAlmostEqual(sum(self.slept), 10.0, places=5)


class TestTestPrefix(unittest.TestCase):
    def test_a_scratch_row_is_recognised_case_insensitively(self):
        self.assertTrue(pn._is_test_row("TEST beans"))
        self.assertTrue(pn._is_test_row("test row"))
        self.assertTrue(pn._is_test_row("  Test something"))

    def test_a_real_row_that_merely_contains_test_is_kept(self):
        # The check is a PREFIX on the title. An ingredient legitimately called
        # "contest winner sauce" or "protein test kit" must survive, and matching
        # anywhere in the string would eat both.
        self.assertFalse(pn._is_test_row("contest winner sauce"))
        self.assertFalse(pn._is_test_row("protein test kit"))
        self.assertFalse(pn._is_test_row(""))
        self.assertFalse(pn._is_test_row(None))


class TestSweepCadence(unittest.TestCase):
    def test_never_swept_is_due_not_postponed(self):
        # A database whose deletions have NEVER been checked is due. "Never" must
        # not read as "not yet", or the first sweep would never happen.
        self.assertTrue(pn.due_for_sweep({"last_full_sweep": None}))

    def test_a_sweep_is_due_after_a_week(self):
        now = datetime(2031, 3, 20, tzinfo=timezone.utc)
        self.assertFalse(pn.due_for_sweep(
            {"last_full_sweep": now - timedelta(days=6)}, now))
        self.assertTrue(pn.due_for_sweep(
            {"last_full_sweep": now - timedelta(days=7)}, now))

    def test_a_naive_stored_timestamp_is_read_as_utc_not_crashed_on(self):
        now = datetime(2031, 3, 20, tzinfo=timezone.utc)
        naive = datetime(2031, 3, 1)
        self.assertTrue(pn.due_for_sweep({"last_full_sweep": naive}, now))


class TestSlugAgreement(unittest.TestCase):
    def test_the_prep_slug_is_the_same_key_nutrition_food_uses(self):
        """prep_notion.slugify duplicates nutrition.slugify once, and its docstring
        says a test asserts they agree. This is that test — without it the two
        could drift and the food_id join would start missing rows silently."""
        from artemis import nutrition
        for name in ("Protein bar — peanut", "protein bar (peanut)",
                     "  Mixed  Greens  ", "olive oil, extra virgin", ""):
            self.assertEqual(pn.slugify(name), nutrition.slugify(name), name)


class TestMappersIgnoreComputedColumns(unittest.TestCase):
    """Notion's formulas and rollups must never reach RDS.

    Artemis recomputes every one of them in knowledge/prep_math.py. Copying them
    would give two answers to each question with no way to tell which was stale —
    and Notion's would always be the stale one, because it cannot see a pantry
    count entered on the phone.
    """

    COMPUTED = ("buy", "needed to buy", "buy pkgs", "eff rank", "preferred",
                "short", "pref pkg size", "usable", "flags", "servings to make",
                "qty needed", "planned servings", "best rank", "in stock",
                "this week?", "needed as used", "recipe servings to make")

    def test_no_mapper_reads_a_formula_or_rollup_property(self):
        import inspect
        for mapper in (pn.map_ingredient, pn.map_recipe, pn.map_recipe_line,
                       pn.map_store, pn.map_store_item):
            src = inspect.getsource(mapper)
            for name in self.COMPUTED:
                self.assertNotIn(f'"{name}"', src,
                                 f"{mapper.__name__} reads computed column {name!r}")


class TestUpsertsPreserveAppOwnedColumns(unittest.TestCase):
    """The columns RDS owns must not appear in any ON CONFLICT UPDATE list.

    on_hand_base is the one that matters most: a sync that reset it would undo
    every pantry count the moment the next sync ran, and the count is the whole
    point of the pantry screen.
    """

    APP_OWNED = ("on_hand_base", "on_hand_at", "on_hand_source", "pushed_pkgs",
                 "pushed_at", "food_id", "plan_eligible", "rank_override")

    def test_no_upsert_overwrites_an_app_owned_column(self):
        for key, sql in pn._UPSERTS.items():
            update = sql.split("DO UPDATE SET", 1)[1]
            for col in self.APP_OWNED:
                self.assertNotIn(f"{col} =", update,
                                 f"{key} upsert overwrites app-owned {col}")

    def test_every_upsert_clears_deleted_at(self):
        # A row that comes back from Notion is not deleted any more. Leaving the
        # flag set would keep it off the list for ever.
        for key, sql in pn._UPSERTS.items():
            self.assertIn("deleted_at = NULL", sql, key)

    def test_every_synced_table_has_an_upsert_and_a_place_in_the_order(self):
        self.assertEqual(set(pn._UPSERTS), set(pn._SPEC))
        self.assertEqual(set(pn.ORDER), set(pn._SPEC))

    def test_parents_are_synced_before_their_children(self):
        # A recipe line references a recipe and an ingredient; a store item
        # references an ingredient and a store. The foreign keys only resolve if
        # the parent table is filled first.
        pos = {k: i for i, k in enumerate(pn.ORDER)}
        table_to_key = {t: k for k, (_d, _m, t) in pn._SPEC.items()}
        for child, parents in pn._PARENTS.items():
            for _col, table in parents:
                self.assertLess(pos[table_to_key[table]], pos[child],
                                f"{table} must be synced before {child}")


if __name__ == "__main__":
    unittest.main()
