"""NUTRITION-2 — day-kind meal sources, free-text meal logging, remaining and
suggestions. Synthetic foods and numbers only (PUBLIC-FIXTURES).
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from artemis import meal_log, nutrition
from artemis.nutrition import Item, parse_item, portion_factor

_REPO_ROOT = Path(__file__).resolve().parent.parent
D = date(2027, 2, 6)     # synthetic

EXAMPLE = ("for breakfast I had an asiago bagel, 2 eggs, 1 slice of cheese, and turkey "
           "sandwich with a fruit bowl that had 3 oz of strawberry and 6 ounces of green grapes.")


class TestDayKind(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(nutrition.day_kind("msp_work"), "work")
        self.assertEqual(nutrition.day_kind("travel"), "travel")
        for t in ("msp_home", "wi", None):          # leave days are off days
            self.assertEqual(nutrition.day_kind(t), "off")


class TestPrefillSources(unittest.TestCase):
    def _cur(self):
        cur = mock.Mock()
        cur.fetchone.return_value = (0,)
        return cur

    def _run(self, day_type, dated, default=None):
        from artemis import notion_meal_plan as nmp
        from artemis.notion_meal_plan import DefaultDay, PlannedFood
        plan = DefaultDay("page-x", "x", slots={"breakfast": [PlannedFood("Test oats", "p1", kcal=300, protein_g=20)]})
        cur = self._cur()
        with mock.patch("artemis.cycle.day_type", return_value=day_type), \
             mock.patch.object(nutrition, "get_day", return_value=None), \
             mock.patch.object(nutrition, "upsert_food"), \
             mock.patch.object(nutrition, "_audit"), \
             mock.patch.object(nmp, "fetch_dated_day", return_value=plan if dated else None), \
             mock.patch.object(nmp, "fetch_default_day", return_value=plan) as fdd, \
             mock.patch.object(nutrition, "_upsert_day") as upsert:
            result = nutrition.prefill_day(cur, D)
        return result, fdd, upsert

    def test_a_dated_pick_wins_on_an_off_day(self):
        result, fdd, upsert = self._run("wi", dated=True)
        self.assertEqual(result.outcome, "planned")
        fdd.assert_not_called()
        self.assertIn("picked:", upsert.call_args.kwargs["note"])

    def test_travel_day_reads_the_travel_default(self):
        from artemis import notion_meal_plan as nmp
        result, fdd, _ = self._run("travel", dated=False)
        self.assertEqual(result.outcome, "planned")
        fdd.assert_called_once_with(nmp.TRAVEL_DAY_NAME)

    def test_work_day_reads_the_work_default(self):
        from artemis import notion_meal_plan as nmp
        _, fdd, _ = self._run("msp_work", dated=False)
        fdd.assert_called_once_with(nmp.DEFAULT_DAY_NAME)

    def test_off_day_without_pick_is_empty(self):
        result, fdd, upsert = self._run("msp_home", dated=False)
        self.assertEqual(result.outcome, "no_plan")
        fdd.assert_not_called()
        self.assertFalse(upsert.call_args.kwargs["prefilled"])

    def test_a_logged_day_is_never_replaced(self):
        cur = mock.Mock()
        cur.fetchone.return_value = (3,)
        with mock.patch.object(nutrition, "get_day", return_value=None):
            self.assertEqual(nutrition.prefill_day(cur, D).outcome, "already")


class TestParseItem(unittest.TestCase):
    def test_forms(self):
        cases = {
            "an asiago bagel": (1, None, "asiago bagel"),
            "2 eggs": (2, None, "eggs"),
            "1 slice of cheese": (1, "slice", "cheese"),
            "3 oz of strawberry": (3, "oz", "strawberry"),
            "6 ounces of green grapes": (6, "ounces", "green grapes"),
            "half a cup of oats": (0.5, "cup", "oats"),
            "3/4 cup yogurt": (0.75, "cup", "yogurt"),
            "gala apple": (1, None, "gala apple"),
        }
        for text, (q, u, n) in cases.items():
            with self.subTest(text=text):
                it = parse_item(text)
                self.assertEqual((it.qty, it.unit, it.name), (q, u, n))


class TestPortionFactor(unittest.TestCase):
    def test_weight_against_per_100g(self):
        self.assertAlmostEqual(portion_factor(3, "oz", {"basis": "100g"}), 0.850485, places=5)

    def test_count_against_per_100g_is_a_question(self):
        self.assertIsNone(portion_factor(2, None, {"basis": "100g"}))

    def test_count_against_a_portion(self):
        self.assertEqual(portion_factor(2, None, {"basis": "portion", "portion": "1 large egg"}), 2)

    def test_weight_against_a_portion_needs_its_grams(self):
        self.assertAlmostEqual(portion_factor(34, "g", {"basis": "portion", "portion": "1 bar (68 g)"}), 0.5)
        self.assertIsNone(portion_factor(34, "g", {"basis": "portion", "portion": "1 bar"}))

    def test_volume_same_unit_only(self):
        f = {"basis": "portion", "portion": "1/2 cup (120 g)"}
        self.assertEqual(portion_factor(1, "cup", f), 2)
        self.assertIsNone(portion_factor(1, "tbsp", f))


class TestParseMealLog(unittest.TestCase):
    def test_ryans_example(self):
        ml = meal_log.parse_meal_log(EXAMPLE)
        self.assertEqual(ml.slot, "breakfast")
        self.assertEqual([i.raw for i in ml.items], [
            "an asiago bagel", "2 eggs", "1 slice of cheese", "turkey sandwich",
            "3 oz of strawberry", "6 ounces of green grapes"])

    def test_shapes(self):
        cases = {
            "I had a test wrap for lunch": ("lunch", 0, False),
            "for lunch yesterday I had a test wrap": ("lunch", -1, False),
            "making test bowl for dinner": ("dinner", 0, True),
            "breakfast: 2 eggs and toast": ("breakfast", 0, False),
            "snacked on edamame": ("snacks", 0, False),
        }
        for text, (slot, off, making) in cases.items():
            with self.subTest(text=text):
                ml = meal_log.parse_meal_log(text)
                self.assertEqual((ml.slot, ml.day_offset, ml.making), (slot, off, making))

    def test_known_food_stays_whole(self):
        known = lambda t: "salad with goat cheese" in t.lower()
        ml = meal_log.parse_meal_log("I had test salad with goat cheese for lunch", known)
        self.assertEqual([i.name for i in ml.items], ["test salad with goat cheese"])

    def test_not_meal_logs(self):
        for t in ("I had a meeting with the team", "show me a chart of my weight",
                  "Sleep 6, energy 4", "lunch was great, the meeting ran long", ""):
            with self.subTest(text=t):
                self.assertIsNone(meal_log.parse_meal_log(t))


FOODS = {
    "asiago bagel": {"id": 1, "kind": "recipe", "name": "Test bagel", "kcal": 300, "protein_g": 11,
                     "fiber_g": 2, "basis": "portion", "portion": None, "source": "saved",
                     "confidence": "exact"},
    "egg": {"id": 2, "kind": "ingredient", "name": "Test egg", "kcal": 70, "protein_g": 6,
            "fiber_g": 0, "basis": "portion", "portion": "1 large egg", "source": "saved",
            "confidence": "exact"},
}
EXTERNAL = {
    "strawberry": {"id": None, "name": "Strawberries, raw", "kcal": 32, "protein_g": 0.7,
                   "fiber_g": 2, "basis": "100g", "portion": "100 g", "source": "usda",
                   "source_id": "x", "confidence": "matched"},
    "green grapes": {"id": None, "name": "Grapes", "kcal": 69, "protein_g": 0.7, "fiber_g": 0.9,
                     "basis": "100g", "portion": "100 g", "source": "usda", "source_id": "y",
                     "confidence": "matched"},
    "cheese": {"id": None, "name": "Cheddar", "kcal": 400, "protein_g": 25, "fiber_g": 0,
               "basis": "100g", "portion": "100 g", "source": "usda", "source_id": "z",
               "confidence": "matched"},
}


def fake_saved(_cur, name):
    return FOODS.get(name.lower())


def fake_external(name):
    return EXTERNAL.get((name or "").lower())


class _Cur:
    rowcount = 0

    def execute(self, *_a, **_k):
        pass

    def fetchone(self):
        return (0,)


class TestLogMeal(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(nutrition, "saved_food", side_effect=fake_saved),
            mock.patch.object(nutrition, "external_food", side_effect=fake_external),
            mock.patch.object(nutrition, "is_open", return_value=True),
            mock.patch.object(nutrition, "_insert_entry"),
            mock.patch.object(nutrition, "_mark_corrected"),
            mock.patch.object(nutrition, "_audit"),
            mock.patch.object(meal_log, "_ensure_day"),
        ]
        self.m = [p.start() for p in self.patches]
        self.insert = self.m[3]

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_resolves_what_it_can_and_asks_about_the_rest(self):
        ml = meal_log.parse_meal_log(EXAMPLE)
        out = meal_log.log_meal(_Cur(), ml, D)
        logged = {lg.item.name: lg for lg in out.logged}
        self.assertEqual(set(logged), {"asiago bagel", "eggs", "strawberry", "green grapes"})
        self.assertEqual(logged["eggs"].factor, 2)                     # plural -> SAVED "egg"
        self.assertEqual(logged["eggs"].food["source"], "saved")
        self.assertAlmostEqual(logged["strawberry"].factor, 0.850485, places=5)
        self.assertEqual(logged["strawberry"].kcal, 27)
        self.assertEqual(len(out.questions), 2)                        # sandwich; a slice of per-100 g
        self.assertTrue(any("turkey sandwich" in q for q in out.questions))
        self.assertTrue(any("Cheddar" in q for q in out.questions))
        self.assertEqual(self.insert.call_count, 4)

    def test_nothing_resolvable_writes_nothing(self):
        ml = meal_log.parse_meal_log("I had a test mystery dish for lunch")
        out = meal_log.log_meal(_Cur(), ml, D)
        self.assertEqual(out.logged, [])
        self.insert.assert_not_called()

    def test_a_locked_day_is_refused(self):
        with mock.patch.object(nutrition, "is_open", return_value=False):
            ml = meal_log.parse_meal_log("for lunch yesterday I had 2 eggs")
            out = meal_log.log_meal(_Cur(), ml, D)
        self.assertIsNotNone(out.refused)
        self.insert.assert_not_called()

    def test_bare_form_stores_saved_foods_only(self):
        ml = meal_log.parse_meal_log("I had 2 eggs and strawberry")
        out = meal_log.log_meal(_Cur(), ml, D)
        self.assertEqual([lg.item.name for lg in out.logged], ["eggs"])
        self.assertTrue(any("saved foods" in q for q in out.questions))

    def test_a_usda_outage_is_a_question_not_a_fallthrough(self):
        with mock.patch.object(nutrition, "external_food",
                               side_effect=nutrition.SourceUnavailable("USDA could not be reached")):
            ml = meal_log.parse_meal_log("for lunch I had 3 oz strawberry")
            out = meal_log.log_meal(_Cur(), ml, D)
        self.assertEqual(out.logged, [])
        self.assertIn("USDA could not be reached", out.questions[0])

    def test_a_range_is_a_question(self):
        ml = meal_log.parse_meal_log("for lunch I had 3-4 eggs")
        out = meal_log.log_meal(_Cur(), ml, D)
        self.assertEqual(out.logged, [])
        self.assertIn("range", out.questions[0])

    def test_a_named_slot_replaces_only_the_planned_rows(self):
        cur = mock.Mock()
        cur.rowcount = 3
        ml = meal_log.parse_meal_log("for breakfast I had 2 eggs")
        out = meal_log.log_meal(cur, ml, D)
        deletes = [c for c in cur.execute.call_args_list if "DELETE FROM nutrition.entry" in str(c)]
        self.assertEqual(len(deletes), 1)
        self.assertIn("status = 'assumed'", deletes[0].args[0])
        self.assertEqual(deletes[0].args[1], (D, "breakfast"))
        self.assertEqual(out.replaced_planned, 3)

    def test_a_bare_snack_replaces_nothing(self):
        cur = mock.Mock()
        ml = meal_log.parse_meal_log("snacked on eggs")
        meal_log.log_meal(cur, ml, D)
        self.assertFalse([c for c in cur.execute.call_args_list if "DELETE" in str(c)])


class TestHandleHoldsNoConnectionDuringLookups(unittest.TestCase):
    def test_external_lookups_run_with_no_connection_open(self):
        state = {"open": 0, "during_external": []}

        class Conn:
            def cursor(self):
                return _Cur()

        from contextlib import contextmanager

        @contextmanager
        def get_connection():
            state["open"] += 1
            try:
                yield Conn()
            finally:
                state["open"] -= 1

        def ext(name):
            state["during_external"].append(state["open"])
            return fake_external(name)

        with mock.patch.object(nutrition, "saved_food", side_effect=fake_saved), \
             mock.patch.object(nutrition, "external_food", side_effect=ext), \
             mock.patch.object(nutrition, "find_saved_food", return_value=None), \
             mock.patch.object(nutrition, "is_open", return_value=True), \
             mock.patch.object(nutrition, "_insert_entry"), \
             mock.patch.object(nutrition, "_mark_corrected"), \
             mock.patch.object(nutrition, "_audit"), \
             mock.patch.object(nutrition, "day_totals", return_value={
                 "kcal": 1, "protein_g": 1, "carb_g": 0, "fat_g": 0, "fiber_g": 0, "n_entries": 1}), \
             mock.patch.object(meal_log, "open_target", return_value=None), \
             mock.patch.object(meal_log, "suggest", return_value=[]), \
             mock.patch.object(meal_log, "_ensure_day"):
            reply = meal_log.handle(get_connection, "for lunch I had 3 oz strawberry", D)
        self.assertTrue(state["during_external"])
        self.assertEqual(set(state["during_external"]), {0})
        self.assertIn("Logged lunch", reply)

    def test_multi_line_logs_each_meal(self):
        logs = meal_log.parse_meal_logs("breakfast: eggs\nlunch: salad")
        self.assertEqual([(m.slot, [i.name for i in m.items]) for m in logs],
                         [("breakfast", ["eggs"]), ("lunch", ["salad"])])


class TestReply(unittest.TestCase):
    def _reply(self, target, assumed=0, making=False, replaced=0):
        text = "making 2 eggs for breakfast" if making else "for breakfast I had 2 eggs"
        ml = meal_log.parse_meal_log(text)
        out = meal_log.Outcome(day=D, slot="breakfast", replaced_planned=replaced,
                               logged=[meal_log.Logged(item=ml.items[0], food=FOODS["egg"],
                                                       factor=2, kcal=140, protein_g=12, fiber_g=0)])
        totals = {"kcal": 140, "protein_g": 12, "carb_g": 0, "fat_g": 0, "fiber_g": 0, "n_entries": 1}
        with mock.patch.object(nutrition, "day_totals", return_value=totals), \
             mock.patch.object(meal_log, "_assumed_count", return_value=assumed), \
             mock.patch.object(meal_log, "open_target", return_value=target), \
             mock.patch.object(meal_log, "suggest", return_value=[
                 {"name": "Test bowl", "kcal": 500, "protein_g": 40, "fiber_g": 9,
                  "is_placeholder": True}]):
            return meal_log.build_reply(mock.Mock(), ml, out, D)

    def test_with_a_target(self):
        r = self._reply({"kcal": 2000, "protein_g": 190, "fiber_g": 40})
        self.assertIn("Logged breakfast", r)
        self.assertIn("Left: 1,860 kcal · 178 g protein · 40 g fiber", r)
        self.assertIn("Recipes that fit what's left: Test bowl", r)
        self.assertIn("estimate", r)                    # placeholder recipes say so

    def test_without_a_target_says_so_and_invents_none(self):
        r = self._reply(None)
        self.assertIn("No target set", r)
        self.assertNotIn("Left:", r)

    def test_planned_rows_in_totals_are_named(self):
        self.assertIn("includes 4 planned items not yet logged", self._reply(None, assumed=4))

    def test_making_is_counted_as_eaten_and_replacement_is_stated(self):
        r = self._reply(None, making=True, replaced=2)
        self.assertIn("counted as eaten", r)
        self.assertIn("Replaces the planned breakfast (2 items)", r)


class FakeCur:
    def __init__(self, rows):
        self.rows, self.sql = rows, []
        self.description = [(c,) for c in ("id", "name", "kcal", "protein_g", "fiber_g", "is_placeholder")]

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows


class TestSuggest(unittest.TestCase):
    ROWS = [
        (1, "Test dense", 400, 45, 10, False),
        (2, "Test big", 900, 50, 5, False),
        (3, "Test light", 200, 5, 1, False),
    ]

    def test_fits_and_ranks(self):
        cur = FakeCur(self.ROWS)
        picks = meal_log.suggest(cur, D, {"kcal": 600})
        self.assertEqual([p["name"] for p in picks], ["Test dense", "Test light"])
        self.assertIn("NOT IN", cur.sql[0][0])      # recent repeats excluded in SQL

    def test_nothing_left_means_no_suggestion(self):
        self.assertEqual(meal_log.suggest(FakeCur(self.ROWS), D, {"kcal": -50}), [])


class TestRouting(unittest.TestCase):
    def test_chain_order(self):
        src = (_REPO_ROOT / "artemis" / "main.py").read_text()
        i = {name: src.index(f'("{name}", ') for name in
             ("morning_flow", "nutrition_fix", "meal_log", "nutrition")}
        self.assertLess(i["morning_flow"], i["meal_log"])
        self.assertLess(i["nutrition_fix"], i["meal_log"])
        self.assertLess(i["meal_log"], i["nutrition"])

    def test_a_checkin_is_not_a_meal_log(self):
        from artemis.health_checkin import classify
        t = "energy 4 sore 0"
        self.assertEqual(classify(t), "checkin")
        self.assertIsNone(meal_log.parse_meal_log(t))

    def test_status_requests(self):
        self.assertTrue(meal_log.is_status_request("what's left"))
        self.assertTrue(meal_log.is_status_request("protein remaining"))
        self.assertFalse(meal_log.is_status_request("for lunch I had eggs"))


class TestOffBasis(unittest.TestCase):
    def test_off_never_mixes_serving_and_100g(self):
        prod = {"products": [{"product_name": "Test bar", "code": "1", "serving_size": "1 bar (50 g)",
                              "nutriments": {"energy-kcal_100g": 400, "proteins_100g": 20,
                                             "proteins_serving": 10}}]}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = prod
        with mock.patch("requests.get", return_value=resp):
            f = nutrition.lookup_off("test bar")
        # no serving kcal -> the whole product is per 100 g, protein included
        self.assertEqual(f["basis"], "100g")
        self.assertEqual(f["protein_g"], 20.0)


if __name__ == "__main__":
    unittest.main()


class TestReviewFindings(unittest.TestCase):
    """The 2026-09-26 independent review of #208, one test per finding."""

    NOT_LOGS = (
        "Lunch was great, thanks", "lunch was cancelled", "breakfast was late today",
        "dinner was amazing last night", "lunch: pushed to 1pm", "breakfast - done",
        "dinner: done", "lunch: skipped", "dinner: no", "for lunch I got stuck in traffic",
        "for breakfast I was running late so skipped it",
        "dinner tonight: reservations at 7 with partner", "having Dennis over for dinner",
        "I'm making plans for dinner", "I had a great weekend", "i had the flu", "I had fun",
        "I had nothing", "I had no breakfast", "I had knee pain 2 this morning",
        "I had lunch with Jennifer", "I ate at chipotle", "I had breakfast",
    )

    def test_status_talk_is_not_a_meal_log(self):
        for t in self.NOT_LOGS:
            with self.subTest(text=t):
                self.assertIsNone(meal_log.parse_meal_log(t))

    def test_company_is_not_food(self):
        ml = meal_log.parse_meal_log("for lunch I had tacos with Jennifer")
        self.assertEqual([i.name for i in ml.items], ["tacos"])
        self.assertEqual(ml.ignored, ["with Jennifer"])

    def test_day_phrases(self):
        for t, off, slot in (("I had pizza for dinner last night", -1, "dinner"),
                             ("for dinner last night I had pizza", -1, "dinner"),
                             ("I had 2 eggs this morning", 0, "snacks")):
            with self.subTest(text=t):
                ml = meal_log.parse_meal_log(t)
                self.assertEqual((ml.day_offset, ml.slot), (off, slot))
                self.assertNotIn("night", ml.items[0].name)
                self.assertNotIn("morning", ml.items[0].name)

    def test_quantities(self):
        cases = {"½ bagel": (0.5, None, "bagel"), "1½ cups rice": (1.5, "cups", "rice"),
                 "2x eggs": (2, None, "eggs"), "eggs x2": (2, None, "eggs"),
                 "1/2 an avocado": (0.5, None, "avocado"), "a 12 oz steak": (12, "oz", "steak"),
                 "a dozen eggs": (12, None, "eggs")}
        for text, want in cases.items():
            with self.subTest(text=text):
                it = parse_item(text)
                self.assertEqual((it.qty, it.unit, it.name), want)
        self.assertIsNone(parse_item("3-4 strawberries").qty)
        self.assertTrue(parse_item("7 layer burrito").numeric_lead)

    def test_count_against_weight_or_external_portions_asks(self):
        ing = {"basis": "portion", "kind": "ingredient"}
        self.assertIsNone(portion_factor(3, None, {**ing, "portion": "100 g"}))
        self.assertEqual(portion_factor(1, "slice", {**ing, "portion": "2 slices (56 g)"}), 0.5)
        self.assertIsNone(portion_factor(1, "slice", {**ing, "portion": "1 oz (28 g)"}))
        self.assertIsNone(portion_factor(1, None, {"basis": "serving", "portion": "1 bar",
                                                    "source": "off"}))
        self.assertEqual(portion_factor(2, None, {"basis": "portion", "kind": "recipe",
                                                  "portion": None}), 2)

    def test_status_gate_answers_nutrition_only(self):
        for t in ("what's left on my calendar today?", "what's left in my inbox",
                  "how many emails are left", "how much budget is remaining for the Paris trip",
                  "where am i at on the backlog"):
            with self.subTest(text=t):
                self.assertFalse(meal_log.is_status_request(t))


class TestSavedFoodMatch(unittest.TestCase):
    """An exact slug in ANY kind beats a prefix, and a prefix ends at a word."""

    class Cur:
        def __init__(self, rows):
            self.rows, self.last = rows, None
            self.description = [(c,) for c in ("id", "kind", "name", "slug")]

        def execute(self, sql, params):
            kind, pat = params
            if "slug = %s" in sql:
                self.last = [r for r in self.rows if r[1] == kind and r[3] == pat]
            else:
                pre = pat[:-1]
                self.last = [r for r in self.rows if r[1] == kind and r[3].startswith(pre)][:2]

        def fetchone(self):
            return self.last[0] if self.last else None

        def fetchall(self):
            return self.last

    ROWS = [(1, "recipe", "Cheesecake bites", "cheesecake bites"),
            (2, "ingredient", "Cheese", "cheese"),
            (3, "recipe", "Egg white bites", "egg white bites"),
            (4, "recipe", "Protein bar peanut", "protein bar peanut")]

    def test_exact_ingredient_beats_recipe_prefix(self):
        with mock.patch.object(nutrition, "_as_dict", side_effect=lambda _c, r: {"id": r[0]}):
            self.assertEqual(nutrition.find_saved_food(self.Cur(self.ROWS), "cheese")["id"], 2)
            self.assertIsNone(nutrition.find_saved_food(self.Cur(self.ROWS), "egg"))
            self.assertEqual(nutrition.find_saved_food(self.Cur(self.ROWS), "protein bar")["id"], 4)


class TestReReviewFindings(unittest.TestCase):
    """The re-verification of #208 (2026-09-26)."""

    def test_units_only_follow_an_amount(self):
        self.assertEqual(parse_item("pound cake").name, "pound cake")
        self.assertIsNone(parse_item("pound cake").unit)
        self.assertEqual(parse_item("slice of pizza").name, "slice of pizza")
        it = parse_item("2 lbs chicken")
        self.assertEqual((it.qty, it.unit, it.name), (2, "lbs", "chicken"))

    def test_real_meals_with_ordinary_words_still_log(self):
        for t in ("for lunch I had leftover chili from the other day",
                  "for dinner I had a day-old bagel", "for lunch I had a sandwich over rice",
                  "for lunch I grabbed a burrito", "for dinner I made tacos"):
            with self.subTest(text=t):
                self.assertIsNotNone(meal_log.parse_meal_log(t))

    def test_bare_chat_stays_unclaimed(self):
        for t in ("I ate too much", "I had enough", "I had a chance to review the PR",
                  "breakfast: same as yesterday"):
            with self.subTest(text=t):
                self.assertIsNone(meal_log.parse_meal_log(t))

    def test_a_chatty_next_line_is_not_food(self):
        ml = meal_log.parse_meal_log("for lunch I had a salad\ncan you check my inbox")
        self.assertEqual([i.raw for i in ml.items], ["a salad"])
        self.assertEqual(ml.not_read, ["can you check my inbox"])
        ml = meal_log.parse_meal_log("for breakfast I had eggs\nthanks!")
        self.assertEqual(ml.not_read, ["thanks!"])

    def test_a_slot_header_takes_the_lines_below(self):
        logs = meal_log.parse_meal_logs("breakfast:\n2 eggs\n1 slice toast")
        self.assertEqual([(m.slot, [i.raw for i in m.items]) for m in logs],
                         [("breakfast", ["2 eggs", "1 slice toast"])])
        self.assertEqual(meal_log.parse_meal_logs("lunch:"), [])

    def test_whole_phrase_match_displays_as_matched(self):
        with mock.patch.object(nutrition, "saved_food",
                               side_effect=lambda _c, t: ({"id": 9, "kind": "recipe", "name": "7 layer burrito",
                                                           "kcal": 500, "protein_g": 20, "fiber_g": 9,
                                                           "basis": "portion", "portion": None,
                                                           "source": "saved", "confidence": "exact"}
                                                          if t.lower() == "7 layer burrito" else None)), \
             mock.patch.object(nutrition, "is_open", return_value=True), \
             mock.patch.object(nutrition, "_insert_entry"), \
             mock.patch.object(nutrition, "_mark_corrected"), \
             mock.patch.object(nutrition, "_audit"), \
             mock.patch.object(meal_log, "_ensure_day"):
            out = meal_log.log_meal(_Cur(), meal_log.parse_meal_log("for lunch I had 7 layer burrito"), D)
        self.assertEqual(out.logged[0].factor, 1)
        self.assertEqual(out.logged[0].kcal, 500)
        self.assertNotIn("7 ×", meal_log._line_for(out.logged[0]))

    def test_legacy_log_path_never_estimates(self):
        src = (_REPO_ROOT / "artemis" / "main.py").read_text()
        body = src[src.index("def _handle_nutrition("):src.index("def _handle_mention(")]
        self.assertNotIn("log_nutrition(", body)
        self.assertIn("I didn't log anything", body)

    def test_same_day_target_correction_updates_in_place(self):
        from artemis import health

        class Cur:
            def __init__(self):
                self.sql = []
                self._next = None

            def execute(self, sql, params=None):
                self.sql.append(sql)
                if "RETURNING id" in sql:
                    self._next = (41,)
                elif "FROM nutrition.target" in sql:
                    self._next = (7, True)        # open row starts the same day
                else:
                    self._next = None

            def fetchone(self):
                return self._next

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        cur = Cur()

        class Conn:
            def cursor(self):
                return cur

        from contextlib import contextmanager

        @contextmanager
        def get_connection():
            yield Conn()

        t = health.NutritionTarget(kcal=1999, protein_g=199, effective_from="2027-02-06")
        with mock.patch("knowledge.db.get_connection", get_connection):
            health.insert_nutrition_target_tx(t)
        joined = " | ".join(cur.sql)
        self.assertIn("UPDATE nutrition.target SET kcal", joined)
        self.assertNotIn("INSERT INTO nutrition.target", joined)


class TestPostMealWalk(unittest.TestCase):
    """PROGRAM-2 (Ryan, 2026-09-28): one line after lunch and dinner. No new
    scheduled automation — it rides on a reply he is already reading."""

    D = date(2027, 6, 7)

    def test_lunch_and_dinner_get_the_line(self):
        for slot in ("lunch", "dinner"):
            with self.subTest(slot=slot):
                self.assertEqual(meal_log.post_meal_walk_line(slot, self.D, self.D),
                                 meal_log.WALK_LINE)

    def test_breakfast_and_snacks_do_not(self):
        """A line on every logged item is a nag, and a nag is how a true line
        stops being read."""
        for slot in ("breakfast", "snack", None, ""):
            with self.subTest(slot=slot):
                self.assertIsNone(meal_log.post_meal_walk_line(slot, self.D, self.D))

    def test_back_logging_an_earlier_day_gets_no_line(self):
        """The advice is about the next ten minutes."""
        from datetime import timedelta
        self.assertIsNone(
            meal_log.post_meal_walk_line("dinner", self.D - timedelta(1), self.D))

    def test_the_line_says_what_it_is_for(self):
        self.assertIn("10-minute", meal_log.WALK_LINE)
        self.assertIn("glucose", meal_log.WALK_LINE)

    def test_it_is_not_a_scheduled_job(self):
        """No new standing automation: nothing in the scheduler fires this."""
        import inspect
        from artemis import scheduler
        self.assertNotIn("post_meal_walk", inspect.getsource(scheduler))
