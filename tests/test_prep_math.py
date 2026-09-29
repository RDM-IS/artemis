"""PREP-1 — the shopping-list arithmetic.

PUBLIC-FIXTURES: every id here is a readable stub ("ing-oats"), never a Notion
UUID, and every quantity is synthetic. This repository is public and a fixture
that looked like Ryan's real pantry would BE Ryan's real pantry — the 2026-09-25
history scan concluded gym-display's fixtures were synthetic and was wrong,
because it checked the wrong field names. Ids that cannot be Notion ids make that
mistake impossible to repeat here: there is nothing to cross-check.
"""

import unittest
from datetime import date

from knowledge import prep_math as pm

STAY = {"start_date": date(2031, 3, 2), "end_date": date(2031, 3, 8),
        "shop_date": date(2031, 3, 2)}

CO_OP = {"notion_id": "st-coop", "name": "Co-op North", "chain": "Eastside Co-op",
         "active": True}
ALDI = {"notion_id": "st-aldi", "name": "Aldi West", "chain": "Aldi", "active": True}
CUB = {"notion_id": "st-cub", "name": "Cub South", "chain": "Cub Foods", "active": True}
SHUT = {"notion_id": "st-shut", "name": "Closed Market", "chain": "Aldi",
        "active": False}
STORES = {s["notion_id"]: s for s in (CO_OP, ALDI, CUB, SHUT)}


def ing(nid, **kw):
    base = {"notion_id": nid, "name": nid, "unit": "g", "category": "pantry",
            "yield_factor": None, "shelf_life_days": None, "par_level_pkgs": None,
            "bulk": False, "on_hand_base": 0.0}
    base.update(kw)
    return base


def item(nid, ing_id, store_id, **kw):
    base = {"notion_id": nid, "name": nid, "ingredient_notion_id": ing_id,
            "store_notion_id": store_id, "rank": None, "rank_override": None,
            "aisle": None, "package_size": 1000.0, "price": None}
    base.update(kw)
    return base


def line(nid, recipe, ing_id, qty):
    return {"notion_id": nid, "recipe_notion_id": recipe,
            "ingredient_notion_id": ing_id, "qty_per_serving": qty}


def recipe(nid, servings=4, prepped=0, eligible=True):
    return {"notion_id": nid, "name": nid, "servings": servings,
            "prepped_on_hand": prepped, "plan_eligible": eligible}


def day(d, recipe_id, slot="dinner", servings=1):
    return {"day_date": d, "slot": slot, "recipe_notion_id": recipe_id,
            "servings": servings}


def build(**kw):
    args = {"stay": STAY, "stay_days": [], "recipes": {}, "lines": [],
            "ingredients": {}, "store_items": [], "stores": STORES}
    args.update(kw)
    return pm.build_lines(**args)


class TestPackages(unittest.TestCase):
    def test_rounds_up_because_half_a_carton_is_not_purchasable(self):
        self.assertEqual(pm.packages_for(1200.0, 1000.0), 2)

    def test_floating_point_slop_does_not_buy_an_extra_package(self):
        # 2 packages of 333.333… must be 2, not 3.
        self.assertEqual(pm.packages_for(1000.0, 500.0000000001), 2)

    def test_zero_short_is_zero_packages(self):
        self.assertEqual(pm.packages_for(0.0, 800.0), 0)

    def test_no_package_size_is_None_not_zero(self):
        # THE distinction: 0 means "you have enough", None means "nobody has said
        # how big a package is". A None rendered as 0 reads as "enough".
        self.assertIsNone(pm.packages_for(900.0, None))
        self.assertIsNone(pm.packages_for(900.0, 0))


class TestYield(unittest.TestCase):
    def test_dry_goods_buy_less_than_they_yield(self):
        lentils = ing("ing-lentils", yield_factor=2.5)
        self.assertAlmostEqual(pm.purchased_from_as_used(500.0, lentils), 200.0)

    def test_blank_yield_factor_is_one(self):
        self.assertAlmostEqual(pm.purchased_from_as_used(500.0, ing("ing-x")), 500.0)

    def test_nonsense_yield_factor_does_not_produce_an_infinity(self):
        self.assertAlmostEqual(
            pm.purchased_from_as_used(500.0, ing("ing-x", yield_factor=0)), 500.0)


class TestPreppedOnHand(unittest.TestCase):
    def test_prepped_servings_are_eaten_earliest_first(self):
        # 3 prepped servings, one eaten per day for 5 days: the first three days
        # cook nothing, and only days 4 and 5 drive the list.
        days = [day(date(2031, 3, 2 + i), "rec-chili") for i in range(5)]
        make = pm.servings_to_make(days, {"rec-chili": recipe("rec-chili", prepped=3)})
        per_day = make["rec-chili"]
        self.assertNotIn(date(2031, 3, 2), per_day)
        self.assertNotIn(date(2031, 3, 4), per_day)
        self.assertEqual(per_day[date(2031, 3, 5)], 1.0)
        self.assertEqual(per_day[date(2031, 3, 6)], 1.0)
        self.assertEqual(sum(per_day.values()), 2.0)

    def test_more_prepped_than_planned_needs_nothing(self):
        days = [day(date(2031, 3, 2), "rec-chili")]
        make = pm.servings_to_make(days, {"rec-chili": recipe("rec-chili", prepped=9)})
        self.assertEqual(make["rec-chili"], {})

    def test_plan_ineligible_recipe_is_never_planned(self):
        days = [day(date(2031, 3, 2), "rec-pbcup")]
        make = pm.servings_to_make(days, {"rec-pbcup": recipe("rec-pbcup", eligible=False)})
        self.assertNotIn("rec-pbcup", make)


class TestParLevel(unittest.TestCase):
    def test_par_is_a_floor_not_an_addition(self):
        # Notion's own note: "keep this many packages on hand regardless of
        # recipes". Adding par to the recipe need would buy a fortnight of oil
        # every fortnight.
        oil = ing("ing-oil", par_level_pkgs=2, on_hand_base=0.0)
        lines = build(
            stay_days=[day(date(2031, 3, 3), "rec-fry")],
            recipes={"rec-fry": recipe("rec-fry")},
            lines=[line("ln-1", "rec-fry", "ing-oil", 300.0)],
            ingredients={"ing-oil": oil},
            store_items=[item("si-oil", "ing-oil", "st-aldi", rank=1,
                              package_size=1000.0)])
        row = next(r for r in lines if r["ingredient_id"] == "ing-oil")
        # recipe need 300, par floor 2000 → required 2000, not 2300.
        self.assertEqual(row["required_base"], 2000.0)
        self.assertEqual(row["packages"], 2)

    def test_a_staple_no_recipe_touches_still_reaches_the_list(self):
        soap = ing("ing-soap", par_level_pkgs=1, on_hand_base=0.0, category="cleaning")
        lines = build(ingredients={"ing-soap": soap},
                      store_items=[item("si-soap", "ing-soap", "st-aldi", rank=1,
                                        package_size=500.0)])
        row = next(r for r in lines if r["ingredient_id"] == "ing-soap")
        self.assertIn(pm.FLAG_PAR_ONLY, row["flags"])
        self.assertEqual(row["packages"], 1)

    def test_a_staple_already_at_par_is_not_on_the_list(self):
        soap = ing("ing-soap", par_level_pkgs=1, on_hand_base=500.0)
        lines = build(ingredients={"ing-soap": soap},
                      store_items=[item("si-soap", "ing-soap", "st-aldi", rank=1,
                                        package_size=500.0)])
        self.assertEqual([r for r in lines if r["ingredient_id"] == "ing-soap"], [])


class TestOnHandUnknown(unittest.TestCase):
    def test_never_counted_is_flagged_and_reported_as_unknown_not_zero(self):
        rice = ing("ing-rice", on_hand_base=None)
        lines = build(
            stay_days=[day(date(2031, 3, 3), "rec-bowl")],
            recipes={"rec-bowl": recipe("rec-bowl")},
            lines=[line("ln-r", "rec-bowl", "ing-rice", 400.0)],
            ingredients={"ing-rice": rice},
            store_items=[item("si-rice", "ing-rice", "st-aldi", rank=1)])
        row = next(r for r in lines if r["ingredient_id"] == "ing-rice")
        self.assertIn(pm.FLAG_COUNT_UNKNOWN, row["flags"])
        # Buying a spare is the safe direction; claiming a measurement is not.
        self.assertIsNone(row["on_hand_base"])
        self.assertEqual(row["packages"], 1)


class TestTopUp(unittest.TestCase):
    def test_a_perishable_splits_into_two_trips_on_the_right_day(self):
        need = {date(2031, 3, 2): 100.0, date(2031, 3, 3): 100.0,
                date(2031, 3, 6): 100.0, date(2031, 3, 7): 100.0}
        now, later, n = pm.topup_split(need, date(2031, 3, 2), 4, date(2031, 3, 8))
        self.assertEqual(n, date(2031, 3, 6))
        self.assertEqual(now, 200.0)
        self.assertEqual(later, 200.0)

    def test_shelf_life_outlasting_the_stay_is_one_trip(self):
        need = {date(2031, 3, 3): 100.0}
        now, later, n = pm.topup_split(need, date(2031, 3, 2), 30, date(2031, 3, 8))
        self.assertIsNone(n)
        self.assertEqual((now, later), (100.0, 0.0))

    def test_no_shelf_life_is_one_trip(self):
        need = {date(2031, 3, 3): 100.0}
        self.assertIsNone(pm.topup_split(need, date(2031, 3, 2), None,
                                         date(2031, 3, 8))[2])

    def test_a_second_trip_is_never_rendered_without_a_day(self):
        spinach = ing("ing-spinach", shelf_life_days=3, on_hand_base=0.0)
        lines = build(
            stay_days=[day(date(2031, 3, 2), "rec-salad"),
                       day(date(2031, 3, 7), "rec-salad")],
            recipes={"rec-salad": recipe("rec-salad")},
            lines=[line("ln-s", "rec-salad", "ing-spinach", 150.0)],
            ingredients={"ing-spinach": spinach},
            store_items=[item("si-sp", "ing-spinach", "st-coop", rank=1,
                              package_size=150.0)])
        row = next(r for r in lines if r["ingredient_id"] == "ing-spinach")
        self.assertIn(pm.FLAG_TOP_UP, row["flags"])
        self.assertEqual(row["later_day"], "2031-03-05")
        self.assertIsNotNone(row["packages_later"])
        for other in lines:
            if other["later_day"] is None:
                self.assertIsNone(other["packages_later"])


class TestRanking(unittest.TestCase):
    def test_produce_defaults_to_the_co_op_and_pantry_to_aldi(self):
        carrot = ing("ing-carrot", category="produce")
        beans = ing("ing-beans", category="pantry")
        self.assertEqual(pm.seed_rank(carrot, CO_OP), 1)
        self.assertEqual(pm.seed_rank(carrot, ALDI), 2)
        self.assertEqual(pm.seed_rank(beans, ALDI), 1)
        self.assertEqual(pm.seed_rank(beans, CO_OP), 2)
        self.assertEqual(pm.seed_rank(beans, CUB), 3)

    def test_bulk_follows_the_produce_rule_whatever_its_category(self):
        oats = ing("ing-oats", category="pantry", bulk=True)
        self.assertEqual(pm.seed_rank(oats, CO_OP), 1)

    def test_override_beats_notion_beats_seed(self):
        beans = ing("ing-beans", category="pantry")
        self.assertEqual(pm.effective_rank(item("a", "ing-beans", "st-cub"), beans, CUB),
                         (3, pm.BASIS_SEED))
        self.assertEqual(pm.effective_rank(item("a", "ing-beans", "st-cub", rank=2),
                                          beans, CUB), (2, pm.BASIS_NOTION))
        self.assertEqual(pm.effective_rank(
            item("a", "ing-beans", "st-cub", rank=2, rank_override=1), beans, CUB),
            (1, pm.BASIS_OVERRIDE))

    def test_an_inactive_store_is_never_chosen(self):
        beans = ing("ing-beans")
        chosen = pm.choose_item([item("si-x", "ing-beans", "st-shut", rank=1)],
                                beans, STORES)
        self.assertIsNone(chosen)

    def test_only_a_third_choice_store_raises_no_rank1(self):
        beans = ing("ing-beans", on_hand_base=0.0)
        lines = build(
            stay_days=[day(date(2031, 3, 3), "rec-b")],
            recipes={"rec-b": recipe("rec-b")},
            lines=[line("ln-b", "rec-b", "ing-beans", 500.0)],
            ingredients={"ing-beans": beans},
            store_items=[item("si-b", "ing-beans", "st-cub", rank=3)])
        row = next(r for r in lines if r["ingredient_id"] == "ing-beans")
        self.assertIn(pm.FLAG_NO_RANK1, row["flags"])
        self.assertEqual(row["store"], "Cub South")

    def test_nobody_sells_it_is_on_the_list_with_a_flag_not_omitted(self):
        # The failure mode this guards: a complete-looking list that quietly
        # drops a dinner's main ingredient.
        saffron = ing("ing-saffron", on_hand_base=0.0)
        lines = build(
            stay_days=[day(date(2031, 3, 3), "rec-rice")],
            recipes={"rec-rice": recipe("rec-rice")},
            lines=[line("ln-sa", "rec-rice", "ing-saffron", 2.0)],
            ingredients={"ing-saffron": saffron}, store_items=[])
        row = next(r for r in lines if r["ingredient_id"] == "ing-saffron")
        self.assertIn(pm.FLAG_NO_STORE, row["flags"])
        self.assertIsNone(row["packages"])


class TestAisles(unittest.TestCase):
    ZONES = {"zones": [{"name": "Produce", "order": 1, "keywords": ["carrot"]},
                       {"name": "Dairy", "order": 2, "keywords": ["milk"]}]}

    def test_the_store_items_own_aisle_wins(self):
        self.assertEqual(
            pm.aisle_for(item("si", "ing-carrot", "st-aldi", aisle="Aisle 4"),
                         ing("ing-carrot"), self.ZONES), "Aisle 4")

    def test_store_maps_keywords_are_the_fallback(self):
        self.assertEqual(pm.aisle_for(item("si", "i", "st-aldi"),
                                      ing("carrot bunch"), self.ZONES), "Produce")

    def test_an_unmatched_ingredient_is_Other(self):
        self.assertEqual(pm.aisle_for(None, ing("tinned sardines"), self.ZONES), "Other")


class TestGrouping(unittest.TestCase):
    def test_numeric_aisles_sort_numerically_and_Other_is_last(self):
        rows = [{"store": "Aldi West", "store_chain": "Aldi", "store_id": "st-aldi",
                 "rank": 1, "aisle": a, "name": f"x{i}"}
                for i, a in enumerate(["Aisle 10", "Other", "Aisle 2", "Bakery"])]
        groups = pm.group_by_store(rows)
        self.assertEqual([a["aisle"] for a in groups[0]["aisles"]],
                         ["Aisle 2", "Aisle 10", "Bakery", "Other"])

    def test_the_nobody_sells_it_group_is_last_and_named(self):
        rows = [{"store": None, "store_chain": None, "store_id": None, "rank": None,
                 "aisle": "Other", "name": "saffron"},
                {"store": "Aldi West", "store_chain": "Aldi", "store_id": "st-aldi",
                 "rank": 1, "aisle": "Aisle 1", "name": "beans"}]
        groups = pm.group_by_store(rows)
        self.assertEqual(groups[0]["store"], "Aldi West")
        self.assertIsNone(groups[-1]["store_id"])


class TestEnoughOnHand(unittest.TestCase):
    def test_a_fully_stocked_ingredient_is_not_on_the_list(self):
        beans = ing("ing-beans", on_hand_base=5000.0)
        lines = build(
            stay_days=[day(date(2031, 3, 3), "rec-b")],
            recipes={"rec-b": recipe("rec-b")},
            lines=[line("ln-b", "rec-b", "ing-beans", 500.0)],
            ingredients={"ing-beans": beans},
            store_items=[item("si-b", "ing-beans", "st-aldi", rank=1)])
        self.assertEqual(lines, [])


class TestPublicFixtures(unittest.TestCase):
    def test_no_fixture_id_here_could_be_a_notion_page_id(self):
        """A Notion id is a 32-hex UUID. Nothing in this file may look like one,
        so no fixture can ever be cross-checked against Ryan's real workspace."""
        import re
        with open(__file__) as fh:
            text = fh.read()
        uuids = re.findall(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                           r"[0-9a-f]{4}-[0-9a-f]{12}\b", text)
        self.assertEqual(uuids, [])
        self.assertEqual(re.findall(r"\b[0-9a-f]{32}\b", text), [])


if __name__ == "__main__":
    unittest.main()
