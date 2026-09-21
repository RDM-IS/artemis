"""DIET-1 — plan-first nutrition logging (work-day scope).

Covers the invariants that matter most and are cheapest to get wrong:

  * the 48h correction window, measured from the day's END in the ACTIVE
    timezone — 47h works, 49h does not
  * `fix` targets the PREVIOUS day, never today
  * the morning line appears only on a planned, still-assumed, still-open day
  * an unmatched food ASKS rather than guessing macros
  * no scheduled nutrition post exists in quiet hours (registry check)
  * the schema's no-invented-macros guard is actually in the migration
  * `fix <exercise> rpe <n>` still belongs to the workout handler

Run:
    python3 tests/test_diet1.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import re
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from artemis import nutrition  # noqa: E402

CT = ZoneInfo("America/Chicago")


def _ct(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CT)


class TestCorrectionWindow(unittest.TestCase):
    """48 hours from the day's END — not from the day's start."""

    def setUp(self):
        # Pin the active timezone so the window math is deterministic.
        patcher = mock.patch("artemis.quiet_hours.local_tz", return_value=CT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_day_ends_at_local_midnight_of_the_next_day(self):
        self.assertEqual(nutrition.day_end(date(2026, 9, 21)), _ct(2026, 9, 22))

    def test_deadline_is_48h_after_the_day_ended(self):
        self.assertEqual(nutrition.correction_deadline(date(2026, 9, 21)),
                         _ct(2026, 9, 24))

    def test_47h_after_the_day_ended_is_still_open(self):
        day = date(2026, 9, 21)
        at_47h = nutrition.day_end(day) + timedelta(hours=47)
        self.assertTrue(nutrition.is_open(day, at_47h))

    def test_49h_after_the_day_ended_is_closed(self):
        day = date(2026, 9, 21)
        at_49h = nutrition.day_end(day) + timedelta(hours=49)
        self.assertFalse(nutrition.is_open(day, at_49h))

    def test_boundary_is_exclusive_at_exactly_48h(self):
        day = date(2026, 9, 21)
        self.assertFalse(nutrition.is_open(day, nutrition.correction_deadline(day)))

    def test_window_follows_a_timezone_override(self):
        """The same day ends at a different INSTANT in a different zone —
        this is the guard against a literal America/Chicago creeping in."""
        day = date(2026, 9, 21)
        with mock.patch("artemis.quiet_hours.local_tz",
                        return_value=ZoneInfo("Pacific/Honolulu")):
            hst_end = nutrition.day_end(day)
        ct_end = nutrition.day_end(day)
        self.assertNotEqual(hst_end, ct_end)


class TestDeviationParsing(unittest.TestCase):

    def test_skipped_meal(self):
        d = nutrition.parse_deviation("skipped breakfast")
        self.assertEqual((d.kind, d.slot), ("skip", "breakfast"))

    def test_skip_snack_normalises_to_snacks(self):
        d = nutrition.parse_deviation("skipped snack")
        self.assertEqual(d.slot, "snacks")

    def test_slot_replacement(self):
        d = nutrition.parse_deviation("lunch: chipotle chicken bowl")
        self.assertEqual((d.kind, d.slot, d.text),
                         ("replace", "lunch", "chipotle chicken bowl"))

    def test_addition_with_quantity(self):
        d = nutrition.parse_deviation("+2 beers")
        self.assertEqual((d.kind, d.quantity, d.text), ("add", 2.0, "beers"))

    def test_addition_without_quantity(self):
        d = nutrition.parse_deviation("+ handful of almonds")
        self.assertEqual((d.kind, d.quantity), ("add", 1.0))

    def test_non_deviation_returns_none(self):
        for text in ("", "   ", "how many calories did I eat", "yes"):
            self.assertIsNone(nutrition.parse_deviation(text), text)


class TestNeverInventsMacros(unittest.TestCase):

    def test_unmatched_food_asks_instead_of_guessing(self):
        cur = mock.Mock()
        with mock.patch.object(nutrition, "find_saved_food", return_value=None), \
             mock.patch.object(nutrition, "lookup_usda", return_value=None), \
             mock.patch.object(nutrition, "lookup_off", return_value=None):
            resolved = nutrition.resolve_food(cur, "mystery casserole")
            self.assertIsNone(resolved)

            dev = nutrition.parse_deviation("lunch: mystery casserole")
            reply = nutrition.apply_deviation(cur, date(2026, 9, 21), dev)

        self.assertIn("I don't have macros", reply)
        # Nothing was inserted.
        inserts = [c for c in cur.execute.call_args_list
                   if "INSERT INTO nutrition.entry" in str(c)]
        self.assertEqual(inserts, [], "an unmatched food must write nothing")

    def test_usda_tier_is_skipped_not_guessed_without_a_key(self):
        with mock.patch("knowledge.secrets.get_usda_api_key", return_value=None):
            self.assertIsNone(nutrition.lookup_usda("chicken breast"))

    def test_source_order_prefers_saved_then_usda_then_off(self):
        cur = mock.Mock()
        saved = {"id": 1, "name": "Chicken wrap", "kcal": 265, "protein_g": 25,
                 "source_id": "notion-page", "source": "notion"}
        with mock.patch.object(nutrition, "find_saved_food", return_value=saved), \
             mock.patch.object(nutrition, "lookup_usda") as usda:
            got = nutrition.resolve_food(cur, "chicken wrap")
        self.assertEqual(got["confidence"], "exact")
        usda.assert_not_called()

        with mock.patch.object(nutrition, "find_saved_food", return_value=None), \
             mock.patch.object(nutrition, "lookup_usda",
                               return_value={"name": "x", "kcal": 1, "protein_g": 1,
                                             "source": "usda", "source_id": "9"}), \
             mock.patch.object(nutrition, "lookup_off") as off:
            got = nutrition.resolve_food(cur, "anything")
        self.assertEqual(got["confidence"], "matched")
        off.assert_not_called()

    def test_ambiguous_saved_prefix_does_not_pick_one(self):
        cur = mock.Mock()
        cur.fetchone.return_value = None
        cur.fetchall.return_value = [{"id": 1}, {"id": 2}]   # two matches
        self.assertIsNone(nutrition.find_saved_food(cur, "protein bar"))


class TestIngredientsSavedFoods(unittest.TestCase):
    """Ingredients are the SECOND saved-food source: recipes first."""

    class _KindCursor:
        """Answers find_saved_food's queries from a {(kind, slug): row} map."""
        def __init__(self, rows):
            self.rows, self._one, self._all = rows, None, []
        def execute(self, sql, params=()):
            kind, key = params
            if "slug = %s" in sql:
                self._one = self.rows.get((kind, key))
            else:
                prefix = key[:-1]
                self._all = [r for (k, sl), r in self.rows.items()
                             if k == kind and sl.startswith(prefix)]
        def fetchone(self):
            return self._one
        def fetchall(self):
            return self._all

    def test_recipe_wins_over_an_ingredient_with_the_same_slug(self):
        cur = self._KindCursor({
            ("recipe", "protein bar peanut"): {"kind": "recipe", "name": "Protein bar — peanut"},
            ("ingredient", "protein bar peanut"): {"kind": "ingredient", "name": "protein bar (peanut)"},
        })
        self.assertEqual(nutrition.find_saved_food(cur, "Protein bar — peanut")["kind"], "recipe")

    def test_falls_through_to_ingredients(self):
        cur = self._KindCursor({
            ("ingredient", "chicken patties"): {"kind": "ingredient", "name": "chicken patties"},
        })
        got = nutrition.find_saved_food(cur, "chicken patties")
        self.assertEqual(got["kind"], "ingredient")

    def test_ingredient_prefix_match(self):
        cur = self._KindCursor({
            ("ingredient", "salmon atlantic fresh portion"): {"kind": "ingredient", "name": "salmon"},
        })
        self.assertEqual(nutrition.find_saved_food(cur, "salmon")["name"], "salmon")

    def test_ingredient_deviation_scales_by_serving_and_names_it(self):
        cur = mock.Mock()
        food = {"id": 7, "kind": "ingredient", "name": "banana", "kcal": 105,
                "protein_g": 1.3, "carb_g": 27, "fat_g": 0.4, "fiber_g": 3.1,
                "portion": "1 medium (118g)", "source": "notion", "source_id": "pg"}
        with mock.patch.object(nutrition, "find_saved_food", return_value=food), \
             mock.patch.object(nutrition, "_mark_corrected"):
            reply = nutrition.apply_deviation(
                cur, date(2026, 9, 21), nutrition.parse_deviation("+2 banana"))
        self.assertIn("2 × 1 medium (118g)", reply)
        self.assertIn("210 kcal", reply)
        insert = [c for c in cur.execute.call_args_list
                  if "INSERT INTO nutrition.entry" in str(c)][0]
        params = insert.args[1]
        self.assertEqual(params[5], 210)            # kcal scaled by quantity
        self.assertEqual(params[10], "saved")       # source
        self.assertEqual(params[11], "pg")          # Notion page id kept
        self.assertEqual(params[12], "exact")       # saved food => exact

    def test_fetch_ingredients_skips_rows_without_macros_and_paginates(self):
        from artemis import notion_meal_plan as nmp

        def page(name, cal=None, prot=None, serving="1 cup"):
            return {"id": name, "properties": {
                "ingredient": {"title": [{"plain_text": name}]},
                "serving": {"rich_text": [{"plain_text": serving}]},
                "calories": {"number": cal}, "protein": {"number": prot},
                "carbs": {"number": None}, "fats": {"number": None},
                "fiber": {"number": None}, "source": {"rich_text": []}}}

        pages = [
            {"results": [page("oats", 200, 10), page("dish soap")],
             "has_more": True, "next_cursor": "c2"},
            {"results": [page("half-filled", 50, None), page("tomato", 22, 1.1)],
             "has_more": False},
        ]
        calls = []
        def fake_post(path, token, payload):
            calls.append(payload.get("start_cursor"))
            return pages[len(calls) - 1]
        with mock.patch.object(nmp, "_token", return_value="t"), \
             mock.patch.object(nmp, "_post", side_effect=fake_post):
            foods, skipped = nmp.fetch_ingredients()
        self.assertEqual([f.name for f in foods], ["oats", "tomato"])
        self.assertEqual(foods[0].portion, "1 cup")
        self.assertEqual(skipped, ["half-filled"])      # empty rows aren't noise
        self.assertEqual(calls, [None, "c2"])

    def test_sync_deactivates_ingredients_gone_from_notion(self):
        from artemis import notion_meal_plan as nmp
        from artemis.notion_meal_plan import PlannedFood
        cur = mock.Mock()
        cur.rowcount = 1
        foods = [PlannedFood("oats", "p1", kcal=200, protein_g=10, portion="1/3 cup")]
        with mock.patch.object(nmp, "fetch_ingredients", return_value=(foods, [])):
            out = nutrition.sync_ingredients(cur)
        self.assertEqual(out["synced"], 1)
        upsert = [c for c in cur.execute.call_args_list
                  if "INSERT INTO nutrition.food" in str(c)][0]
        self.assertEqual(upsert.args[1][0], "ingredient")
        deact = [c for c in cur.execute.call_args_list
                 if "SET active = FALSE" in str(c)][0]
        self.assertIn("kind = 'ingredient'", deact.args[0])
        self.assertEqual(deact.args[1], (["oats"],))

    def test_fetch_recipes_only_active_unarchived_with_macros(self):
        """2026-09-21: a backup meal the default day doesn't link must still be
        a saved food, so every active recipe is synced — not just linked ones."""
        from artemis import notion_meal_plan as nmp

        def page(name, cal=None, prot=None, source=""):
            return {"id": name, "properties": {
                "recipe": {"title": [{"plain_text": name}]},
                "course": {"select": {"name": "dinner"}},
                "calories": {"number": cal}, "protein": {"number": prot},
                "carbs": {"number": None}, "fats": {"number": None},
                "fiber": {"number": None},
                "source": {"rich_text": [{"plain_text": source}] if source else []}}}

        payloads = []
        def fake_post(path, token, payload):
            payloads.append((path, payload))
            return {"results": [page("Patty bowl (patty + veg)", 310, 32),
                                page("Chicken thigh bowl", 366, 41, "ESTIMATE — computed"),
                                page("Untested idea")], "has_more": False}
        with mock.patch.object(nmp, "_token", return_value="t"), \
             mock.patch.object(nmp, "_post", side_effect=fake_post):
            foods, skipped = nmp.fetch_recipes()
        self.assertEqual([f.name for f in foods], ["Patty bowl (patty + veg)", "Chicken thigh bowl"])
        self.assertEqual(skipped, ["Untested idea"])
        path, payload = payloads[0]
        self.assertIn(nmp.RECIPES_DB, path)
        self.assertIn({"property": "status", "status": {"equals": "active"}},
                      payload["filter"]["and"])
        self.assertIn({"property": "archive", "checkbox": {"equals": False}},
                      payload["filter"]["and"])
        self.assertFalse(foods[0].is_placeholder)
        self.assertTrue(foods[1].is_placeholder)        # "estimate" marks it

    def test_sync_recipes_upserts_as_recipe_and_deactivates_the_rest(self):
        from artemis import notion_meal_plan as nmp
        from artemis.notion_meal_plan import PlannedFood
        cur = mock.Mock()
        cur.rowcount = 0
        foods = [PlannedFood("Patty bowl (patty + veg)", "p9", kcal=310, protein_g=32)]
        with mock.patch.object(nmp, "fetch_recipes", return_value=(foods, [])):
            out = nutrition.sync_recipes(cur)
        self.assertEqual(out["synced"], 1)
        upsert = [c for c in cur.execute.call_args_list
                  if "INSERT INTO nutrition.food" in str(c)][0]
        self.assertEqual(upsert.args[1][0], "recipe")
        deact = [c for c in cur.execute.call_args_list
                 if "SET active = FALSE" in str(c)][0]
        self.assertIn("kind = 'recipe'", deact.args[0])
        self.assertEqual(deact.args[1], (["patty bowl patty veg"],))

    def test_patty_bowl_resolves_by_prefix(self):
        """'dinner: patty bowl' → the recipe titled 'Patty bowl (patty + veg)'."""
        self.assertTrue(nutrition.slugify("Patty bowl (patty + veg)")
                        .startswith(nutrition.slugify("patty bowl")))
        dev = nutrition.parse_deviation("dinner: patty bowl")
        self.assertIsNotNone(dev)

    def test_prefill_job_syncs_recipes_in_its_own_savepoint(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        body = src[src.index("def job_nutrition_prefill"):
                   src.index("def job_pain_pattern_recompute")]
        self.assertLess(body.index("SAVEPOINT recipe_sync"), body.index("prefill_day("))
        self.assertIn("ROLLBACK TO SAVEPOINT recipe_sync", body)

    def test_ddl_uniqueness_is_per_kind(self):
        sql = (_REPO_ROOT / "migrations" / "040_nutrition_diet1.sql").read_text()
        self.assertIn("UNIQUE (kind, slug)", sql)
        self.assertNotIn("slug           TEXT NOT NULL UNIQUE", sql)

    def test_prefill_job_isolates_the_sync_in_a_savepoint(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        body = src[src.index("def job_nutrition_prefill"):
                   src.index("def job_pain_pattern_recompute")]
        self.assertLess(body.index("SAVEPOINT ingredient_sync"),
                        body.index("prefill_day("))
        self.assertIn("ROLLBACK TO SAVEPOINT ingredient_sync", body)


class TestPlainTupleCursor(unittest.TestCase):
    """knowledge.db.get_connection() hands out a PLAIN cursor with tuple rows.
    Regression guard: the first deploy assumed dict rows (the mocks returned
    dicts), and find_saved_food raised TypeError on the box."""

    class _TupleCursor:
        def __init__(self, cols, rows):
            self.description = [(c,) for c in cols]
            self._rows = list(rows)
        def execute(self, sql, params=()):
            pass
        def fetchone(self):
            return self._rows[0] if self._rows else None
        def fetchall(self):
            return list(self._rows)

    def test_get_day_with_tuple_rows(self):
        cur = self._TupleCursor(
            ["day_date", "status", "day_type", "prefilled", "prefill_outcome",
             "prefill_note", "plan_source_id", "locked_at"],
            [(date(2026, 9, 21), "assumed", "msp_work", True, "planned", None, "pg", None)])
        day = nutrition.get_day(cur, date(2026, 9, 21))
        self.assertEqual(day["status"], "assumed")
        self.assertTrue(day["prefilled"])

    def test_day_totals_with_tuple_rows(self):
        cur = self._TupleCursor(
            ["kcal", "protein_g", "carb_g", "fat_g", "fiber_g", "n_entries"],
            [(2015, 188, 206, 55, 45, 7)])
        self.assertEqual(nutrition.day_totals(cur, date(2026, 9, 21))["kcal"], 2015)

    def test_find_saved_food_with_tuple_rows(self):
        cols = [c.strip() for c in nutrition._FOOD_COLS.split(",")]
        row = (1, "ingredient", "chicken patties", 180, 30, 4, 4.5, 1,
               "1 patty (151g)", "notion", "pg", False)
        cur = self._TupleCursor(cols, [row])
        got = nutrition.find_saved_food(cur, "chicken patties")
        self.assertEqual((got["kind"], got["portion"]), ("ingredient", "1 patty (151g)"))


class TestMorningLine(unittest.TestCase):
    """Appears only on a planned, still-assumed, still-open previous day."""

    TODAY = date(2026, 9, 22)
    PLANNED = {"prefilled": True, "prefill_outcome": "planned",
               "status": "assumed"}

    def _line(self, day_row, *, now=None):
        cur = mock.Mock()
        with mock.patch.object(nutrition, "get_day", return_value=day_row), \
             mock.patch("artemis.quiet_hours.local_tz", return_value=CT):
            return nutrition.morning_line(cur, self.TODAY, now=now or _ct(2026, 9, 22, 6))

    def test_present_on_a_planned_assumed_open_day(self):
        self.assertEqual(self._line(dict(self.PLANNED)),
                         "Yesterday logged as planned — reply `fix` to correct.")

    def test_absent_after_a_corrected_day(self):
        self.assertIsNone(self._line({**self.PLANNED, "status": "corrected"}))

    def test_absent_when_yesterday_had_no_plan(self):
        self.assertIsNone(self._line(
            {"prefilled": False, "prefill_outcome": "unavailable",
             "status": "assumed"}))
        self.assertIsNone(self._line(
            {"prefilled": False, "prefill_outcome": "not_work_day",
             "status": "assumed"}))

    def test_absent_when_there_is_no_row_at_all(self):
        self.assertIsNone(self._line(None))

    def test_absent_once_the_window_has_closed(self):
        self.assertIsNone(self._line(dict(self.PLANNED), now=_ct(2026, 9, 25, 6)))

    def test_it_is_one_line_and_names_fix(self):
        line = self._line(dict(self.PLANNED))
        self.assertNotIn("\n", line)
        self.assertIn("`fix`", line)


class TestFixTargetsYesterday(unittest.TestCase):

    def test_open_correction_uses_the_previous_day(self):
        cur = mock.Mock()
        captured = {}

        def fake_get_day(_cur, d):
            captured["day"] = d
            return {"prefilled": True, "prefill_outcome": "planned",
                    "status": "assumed"}

        with mock.patch.object(nutrition, "get_day", side_effect=fake_get_day), \
             mock.patch.object(nutrition, "day_totals",
                               return_value={"kcal": 2015, "protein_g": 188}), \
             mock.patch.object(nutrition, "_set_system_value"), \
             mock.patch("artemis.quiet_hours.local_tz", return_value=CT):
            nutrition.open_correction(cur, "chan", date(2026, 9, 22),
                                      now=_ct(2026, 9, 22, 6))
        self.assertEqual(captured["day"], date(2026, 9, 21))

    def test_yesterday_is_always_inside_the_window(self):
        """Structural fact worth pinning: `fix` targets yesterday, and
        yesterday is at most ~24h past its end, so the normal path can never
        hit the 48h refusal. The refusal exists for a correction that goes
        STALE (see test_stale_pending_correction_is_refused)."""
        with mock.patch("artemis.quiet_hours.local_tz", return_value=CT):
            for hour in (0, 6, 23):
                today = date(2026, 9, 22)
                self.assertTrue(
                    nutrition.is_open(today - timedelta(days=1),
                                      _ct(2026, 9, 22, hour)),
                    f"yesterday should still be open at {hour}:00")

    def test_a_closed_day_is_refused_and_locked(self):
        """The defensive branch, driven directly: a day already past its
        window is refused and locked rather than reopened."""
        cur = mock.Mock()
        with mock.patch.object(nutrition, "get_day",
                               return_value={"prefilled": True,
                                             "prefill_outcome": "planned",
                                             "status": "assumed"}), \
             mock.patch.object(nutrition, "lock_day") as lock, \
             mock.patch("artemis.quiet_hours.local_tz", return_value=CT):
            # today = 9/28 => target 9/27, whose window shut at 9/30 00:00;
            # `now` is well past that.
            reply = nutrition.open_correction(cur, "chan", date(2026, 9, 28),
                                              now=_ct(2026, 10, 5, 6))
        self.assertIn("locked", reply.lower())
        lock.assert_called_once()

    def test_stale_pending_correction_is_refused(self):
        """A correction opened inside the window but continued after it
        closed must not back-date an edit into a locked day."""
        with mock.patch("artemis.quiet_hours.local_tz", return_value=CT):
            opened_for = date(2026, 9, 21)          # window shuts 9/24 00:00
            self.assertTrue(nutrition.is_open(opened_for, _ct(2026, 9, 22, 8)))
            self.assertFalse(nutrition.is_open(opened_for, _ct(2026, 9, 25, 8)))

    def test_a_day_with_no_plan_says_so(self):
        cur = mock.Mock()
        with mock.patch.object(nutrition, "get_day", return_value=None), \
             mock.patch("artemis.quiet_hours.local_tz", return_value=CT):
            reply = nutrition.open_correction(cur, "chan", date(2026, 9, 22),
                                              now=_ct(2026, 9, 22, 6))
        self.assertIn("no plan", reply.lower())


class TestPrefillScope(unittest.TestCase):
    """Work-day scope: nothing is invented for a day with no meal set."""

    def test_non_work_day_is_recorded_and_left_empty(self):
        cur = mock.Mock()
        with mock.patch("artemis.cycle.day_type", return_value="wi"), \
             mock.patch.object(nutrition, "get_day", return_value=None), \
             mock.patch.object(nutrition, "_upsert_day") as upsert:
            result = nutrition.prefill_day(cur, date(2026, 9, 25))
        self.assertEqual(result.outcome, "not_work_day")
        self.assertEqual(result.entries_written, 0)
        self.assertEqual(upsert.call_args.kwargs["outcome"], "not_work_day")

    def test_notion_unavailable_writes_no_entries_and_says_why(self):
        cur = mock.Mock()
        from artemis import notion_meal_plan as nmp
        with mock.patch("artemis.cycle.day_type", return_value="msp_work"), \
             mock.patch.object(nutrition, "get_day", return_value=None), \
             mock.patch.object(nmp, "fetch_default_day",
                               side_effect=nmp.NotionUnavailable("no token")), \
             mock.patch.object(nutrition, "_upsert_day") as upsert:
            result = nutrition.prefill_day(cur, date(2026, 9, 22))
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual(result.entries_written, 0)
        self.assertEqual(upsert.call_args.kwargs["outcome"], "unavailable")
        self.assertFalse(upsert.call_args.kwargs["prefilled"])
        inserts = [c for c in cur.execute.call_args_list
                   if "INSERT INTO nutrition.entry" in str(c)]
        self.assertEqual(inserts, [], "an unreachable Notion must invent nothing")

    def test_prefill_is_idempotent(self):
        cur = mock.Mock()
        with mock.patch.object(nutrition, "get_day",
                               return_value={"prefilled": True}):
            result = nutrition.prefill_day(cur, date(2026, 9, 22))
        self.assertEqual(result.outcome, "already")


class TestNotionRecipeGuard(unittest.TestCase):
    """A recipe with no calories/protein is skipped, never zero-filled."""

    def test_recipe_without_macros_is_not_usable(self):
        from artemis.notion_meal_plan import PlannedFood
        self.assertFalse(PlannedFood("x", "id").has_macros)
        self.assertFalse(PlannedFood("x", "id", kcal=100).has_macros)
        self.assertTrue(PlannedFood("x", "id", kcal=100, protein_g=5).has_macros)

    def test_placeholder_is_detected_from_the_source_column(self):
        from artemis.notion_meal_plan import PlannedFood
        f = PlannedFood("Cottage cheese bowl", "id", kcal=220, protein_g=25,
                        source_detail="USDA generic low-fat cottage cheese — "
                                      "placeholder, pending label")
        self.assertTrue(f.is_placeholder)
        g = PlannedFood("Chicken wrap", "id", kcal=265, protein_g=25,
                        source_detail="recipe: computed from ingredient labels")
        self.assertFalse(g.is_placeholder)


class TestQuietHoursRegistry(unittest.TestCase):
    """No scheduled nutrition POST exists. The 00:15 job must be silent."""

    def test_prefill_job_posts_nothing(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        start = src.index("def job_nutrition_prefill")
        end = src.index("def job_pain_pattern_recompute")
        body = src[start:end]
        for forbidden in ("post_to_channel", "_mm.post", "send_message"):
            self.assertNotIn(forbidden, body,
                             f"the 00:15 nutrition job must not {forbidden}")

    def test_prefill_is_registered_at_0015(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        self.assertRegex(
            src,
            r'CronSpec\(\s*"nutrition_prefill",\s*"job_nutrition_prefill",\s*0,\s*15')

    def test_no_other_nutrition_cron_posts(self):
        src = (_REPO_ROOT / "artemis" / "scheduler.py").read_text()
        specs = re.findall(r'CronSpec\(\s*"([a-z_]*nutrition[a-z_]*)"', src)
        self.assertEqual(specs, ["nutrition_prefill"],
                         "only one nutrition cron job may exist")


class TestSchemaGuards(unittest.TestCase):
    """The no-invented-macros rule is enforced in the DDL, not just in code."""

    MIGRATION = _REPO_ROOT / "migrations" / "040_nutrition_diet1.sql"

    def setUp(self):
        self.sql = self.MIGRATION.read_text()

    def test_entry_requires_a_source_id_unless_estimated(self):
        self.assertIn("entry_source_id_required", self.sql)
        self.assertIn("confidence = 'estimated'", self.sql)

    def test_day_status_values(self):
        self.assertRegex(
            self.sql,
            r"status IN \('assumed', 'corrected', 'locked_unconfirmed'\)")

    def test_entry_status_values(self):
        self.assertRegex(self.sql, r"status IN \('assumed', 'corrected'\)")

    def test_confidence_tiers(self):
        self.assertRegex(
            self.sql, r"confidence IN \('exact', 'matched', 'estimated'\)")

    def test_no_hard_coded_timezone_in_the_executable_ddl(self):
        """Comments may NAME the zone when explaining what 016 got wrong; the
        statements that actually run must not contain it."""
        statements = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--"))
        self.assertNotIn("America/Chicago", statements)

    def test_day_totals_takes_a_required_date(self):
        """No tz-defaulted date parameter — the caller resolves local today."""
        self.assertIn("nutrition.day_totals(p_date DATE)", self.sql)
        self.assertNotIn("p_date DATE DEFAULT", self.sql)

    def test_migration_number_is_unique(self):
        names = sorted(p.name for p in (_REPO_ROOT / "migrations").glob("*.sql"))
        numbers = [n.split("_")[0] for n in names]
        self.assertEqual(len(numbers), len(set(numbers)), "duplicate migration number")


class TestFixRoutingDoesNotStealWorkoutFix(unittest.TestCase):
    """`fix <exercise> rpe <n>` is the workout-set correction and must survive."""

    def test_bare_fix_only(self):
        # main.py imports flask at module level, which isn't installed in the
        # test environment — read the pattern from source rather than import it.
        src = (_REPO_ROOT / "artemis" / "main.py").read_text()
        m = re.search(r'_NUTRITION_FIX_RE = re\.compile\(r"([^"]+)"', src)
        self.assertIsNotNone(m, "_NUTRITION_FIX_RE not found in main.py")
        fix_re = re.compile(m.group(1), re.I)
        self.assertTrue(fix_re.match("fix"))
        self.assertTrue(fix_re.match("  Fix  "))
        self.assertIsNone(fix_re.match("fix squat rpe 8"))
        self.assertIsNone(fix_re.match("fix leg press rpe 7"))

    def test_handler_text_gates_before_touching_the_database(self):
        """Every handler in deterministic_chain runs on every message, and the
        dispatcher ABORTS THE WHOLE CHAIN when one raises. So the nutrition
        handler must text-gate before it opens a connection, and must fail
        open if the DB is unreachable. Regression guard: an earlier version
        opened a connection unconditionally and broke routing for every
        handler after it."""
        src = (_REPO_ROOT / "artemis" / "main.py").read_text()
        start = src.index("def _handle_nutrition_fix")
        end = src.index("def _handle_pattern_thread")
        body = src[start:end]

        gate = body.index("if not (is_fix or is_done or maybe_deviation):")
        conn = body.index("get_connection")
        self.assertLess(gate, conn,
                        "the text gate must come before any DB import/use")
        self.assertIn("return False", body[gate:gate + 120])
        # and the DB acquisition is guarded
        self.assertRegex(body, r"except Exception:[\s\S]{0,300}?return False")

    def test_nutrition_fix_is_registered_after_morning_flow(self):
        src = (_REPO_ROOT / "artemis" / "main.py").read_text()
        chain = src[src.index("deterministic_chain = ["):]
        chain = chain[:chain.index("]")]
        self.assertLess(chain.index('"morning_flow"'), chain.index('"nutrition_fix"'),
                        "a check-in must outrank the nutrition fix handler")

    def test_workout_fix_still_matches_its_own_regex(self):
        from artemis.health import _FIX_RE
        self.assertTrue(_FIX_RE.match("fix squat rpe 8"))
        self.assertIsNone(_FIX_RE.match("fix"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
