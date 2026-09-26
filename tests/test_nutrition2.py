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
    "asiago bagel": {"id": 1, "name": "Test bagel", "kcal": 300, "protein_g": 11, "fiber_g": 2,
                     "basis": "portion", "portion": None, "source": "saved", "confidence": "exact"},
    "egg": {"id": 2, "name": "Test egg", "kcal": 70, "protein_g": 6, "fiber_g": 0,
            "basis": "portion", "portion": "1 large egg", "source": "saved", "confidence": "exact"},
    "strawberry": {"id": None, "name": "Strawberries, raw", "kcal": 32, "protein_g": 0.7, "fiber_g": 2,
                   "basis": "100g", "portion": "100 g", "source": "usda", "source_id": "x",
                   "confidence": "matched"},
    "green grapes": {"id": None, "name": "Grapes", "kcal": 69, "protein_g": 0.7, "fiber_g": 0.9,
                     "basis": "100g", "portion": "100 g", "source": "usda", "source_id": "y",
                     "confidence": "matched"},
    "cheese": {"id": None, "name": "Cheddar", "kcal": 400, "protein_g": 25, "fiber_g": 0,
               "basis": "100g", "portion": "100 g", "source": "usda", "source_id": "z",
               "confidence": "matched"},
}


def fake_resolve(_cur, name):
    return FOODS.get(name.lower())


class TestLogMeal(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(nutrition, "resolve_food", side_effect=fake_resolve),
            mock.patch.object(nutrition, "is_open", return_value=True),
            mock.patch.object(nutrition, "_insert_entry"),
            mock.patch.object(nutrition, "_mark_corrected"),
            mock.patch.object(nutrition, "_audit"),
            mock.patch.object(meal_log, "_ensure_day"),
        ]
        self.m = [p.start() for p in self.patches]
        self.insert = self.m[2]

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_resolves_what_it_can_and_asks_about_the_rest(self):
        ml = meal_log.parse_meal_log(EXAMPLE)
        out = meal_log.log_meal(mock.Mock(), ml, D)
        logged = {lg.item.name: lg for lg in out.logged}
        self.assertEqual(set(logged), {"asiago bagel", "eggs", "strawberry", "green grapes"})
        self.assertEqual(logged["eggs"].factor, 2)                     # plural -> saved "egg"
        self.assertAlmostEqual(logged["strawberry"].factor, 0.850485, places=5)   # 3 oz / 100 g
        self.assertEqual(logged["strawberry"].kcal, 27)
        # a slice against per-100 g is a question; an unknown food is a question
        self.assertEqual(len(out.questions), 2)
        self.assertTrue(any("turkey sandwich" in q for q in out.questions))
        self.assertTrue(any("Cheddar" in q for q in out.questions))
        self.assertEqual(self.insert.call_count, 4)

    def test_nothing_resolvable_writes_nothing(self):
        ml = meal_log.parse_meal_log("I had a test mystery dish for lunch")
        out = meal_log.log_meal(mock.Mock(), ml, D)
        self.assertEqual(out.logged, [])
        self.insert.assert_not_called()

    def test_a_locked_day_is_refused(self):
        with mock.patch.object(nutrition, "is_open", return_value=False):
            ml = meal_log.parse_meal_log("for lunch yesterday I had 2 eggs")
            out = meal_log.log_meal(mock.Mock(), ml, D)
        self.assertIsNotNone(out.refused)
        self.insert.assert_not_called()


class TestReply(unittest.TestCase):
    def _reply(self, target):
        ml = meal_log.parse_meal_log("for breakfast I had 2 eggs")
        out = meal_log.Outcome(day=D, slot="breakfast", logged=[meal_log.Logged(
            item=ml.items[0], food=FOODS["egg"], factor=2, kcal=140, protein_g=12, fiber_g=0)])
        totals = {"kcal": 140, "protein_g": 12, "carb_g": 0, "fat_g": 0, "fiber_g": 0, "n_entries": 1}
        with mock.patch.object(nutrition, "day_totals", return_value=totals), \
             mock.patch.object(meal_log, "open_target", return_value=target), \
             mock.patch.object(meal_log, "suggest", return_value=[
                 {"name": "Test bowl", "kcal": 500, "protein_g": 40, "fiber_g": 9}]):
            return meal_log.build_reply(mock.Mock(), ml, out, D)

    def test_with_a_target(self):
        r = self._reply({"kcal": 2000, "protein_g": 190, "fiber_g": 40})
        self.assertIn("Logged breakfast", r)
        self.assertIn("Left: 1,860 kcal · 178 g protein · 40 g fiber", r)
        self.assertIn("For lunch, fits: Test bowl", r)

    def test_without_a_target_says_so_and_invents_none(self):
        r = self._reply(None)
        self.assertIn("No target set", r)
        self.assertNotIn("Left:", r)


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
