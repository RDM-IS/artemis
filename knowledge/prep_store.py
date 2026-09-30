"""PREP-1 — reading the prep tables and assembling a list. Shared by both sides.

WHY THIS MODULE EXISTS AT ALL: the box talks to RDS through psycopg2 cursors
(`knowledge.db`) and the Lambda through SQLAlchemy sessions (`api/app/database`).
The queries are the same queries. Rather than write them twice — the failure the
2026-09-28 audit found in the health-plan upsert, where the same tuple existed at
seven call sites and one of them had been broken since migration 042 — every
function here takes a `fetch(sql, params) -> list[dict]` callable and the caller
supplies one for its own driver.

It lives in `knowledge/` because `knowledge/` is in the Lambda package and
`artemis/` is not. It must therefore import NOTHING from `artemis`: no cycle, no
Notion. Stays and menus are resolved on the box and read back from RDS here.
"""

from __future__ import annotations

from datetime import date, timedelta

from knowledge import prep_math

# ── configuration ───────────────────────────────────────────────────────────

def load_config(fetch, key: str, default=None):
    rows = fetch("SELECT value FROM nutrition.prep_config WHERE key = %s", (key,))
    return rows[0]["value"] if rows else default


def store_zones(fetch) -> dict:
    """Aisle zone maps, keyed by lowercase chain name.

    Held in `nutrition.prep_config` rather than read from store_maps.json,
    because store_maps.json sits at the repository root and the Lambda package
    ships only api/, knowledge/ and migrations/ (PACKAGE-IDENTITY). The box seeds
    this key FROM store_maps.json, so the zones Ryan walked are reused
    (alignment decision 6) and both sides read one copy of them.
    """
    return load_config(fetch, "store_zones", {}) or {}


# ── sync health ─────────────────────────────────────────────────────────────

def sync_status(fetch) -> dict:
    """Per-database sync state, and which databases are BLOCKING the list.

    Why the list carries this rather than leaving it to a dashboard: on
    2026-09-29 three of the five Notion databases had never been shared with the
    integration, and the resulting list was EMPTY while sixteen meals were
    planned. An empty list is indistinguishable from "nothing to buy" — the one
    failure that is completely silent in the shop, because he simply buys nothing.

    `blocked` names the databases whose absence removes quantities or stores from
    the calculation. `recipe_lines` blocks every quantity; `store_items` and
    `stores` block every package count and every aisle. `ingredients` and
    `recipes` are listed too, because without them there is nothing at all.
    """
    rows = fetch("SELECT db_key, last_status, last_run, last_edited, "
                 "       last_full_sweep, rows_seen, last_detail "
                 "  FROM nutrition.prep_sync ORDER BY db_key", ())
    counts = fetch("""
        SELECT 'ingredients' AS db_key, count(*) AS n FROM nutrition.prep_ingredient
                WHERE deleted_at IS NULL
        UNION ALL SELECT 'recipes', count(*) FROM nutrition.prep_recipe
                WHERE deleted_at IS NULL
        UNION ALL SELECT 'recipe_lines', count(*) FROM nutrition.prep_recipe_line
                WHERE deleted_at IS NULL
        UNION ALL SELECT 'stores', count(*) FROM nutrition.prep_store
                WHERE deleted_at IS NULL
        UNION ALL SELECT 'store_items', count(*) FROM nutrition.prep_store_item
                WHERE deleted_at IS NULL""", ())
    by_key = {r["db_key"]: int(r["n"]) for r in counts}
    out, blocked = [], []
    for r in rows:
        rows_in_rds = by_key.get(r["db_key"], 0)
        ok = r["last_status"] == "ok" and rows_in_rds > 0
        out.append({
            "db": r["db_key"], "status": r["last_status"],
            "rows_in_rds": rows_in_rds,
            "last_run": r["last_run"].isoformat() if r["last_run"] else None,
            "last_full_sweep": (r["last_full_sweep"].isoformat()
                                if r["last_full_sweep"] else None),
            "detail": (r["last_detail"] or "")[:300] or None,
        })
        if not ok:
            blocked.append(r["db_key"])
    # A database with no prep_sync row at all has never been attempted, which is
    # also a blocker and would otherwise be invisible.
    for key in ("ingredients", "recipes", "recipe_lines", "stores", "store_items"):
        if key not in {r["db"] for r in out}:
            out.append({"db": key, "status": "never_run", "rows_in_rds":
                        by_key.get(key, 0), "last_run": None,
                        "last_full_sweep": None, "detail": None})
            blocked.append(key)
    return {"databases": out, "blocked": sorted(set(blocked))}


# ── stays ───────────────────────────────────────────────────────────────────

_STAY_COLS = ("id, start_date, end_date, source, shop_date, confirmed_at, notes")


def get_stay(fetch, stay_id: int) -> dict | None:
    rows = fetch(f"SELECT {_STAY_COLS} FROM nutrition.prep_stay WHERE id = %s",
                 (stay_id,))
    return rows[0] if rows else None


def current_stay(fetch, today: date) -> dict | None:
    """The stay covering `today`, else the next one starting after it.

    Reads only what the box has already proposed. A Lambda that derived stays
    itself would need the cycle and the override table, and two derivations of the
    same fact drift — the SAME-FACT-MANY-PLACES problem. Here there is one
    proposer and one reader.
    """
    rows = fetch(
        f"SELECT {_STAY_COLS} FROM nutrition.prep_stay "
        "WHERE end_date >= %s ORDER BY start_date LIMIT 1", (today,))
    if rows:
        return rows[0]
    rows = fetch(f"SELECT {_STAY_COLS} FROM nutrition.prep_stay "
                 "ORDER BY start_date DESC LIMIT 1", ())
    return rows[0] if rows else None


# ── state for the calculation ───────────────────────────────────────────────

def load_state(fetch, stay_id: int) -> dict:
    """Everything `prep_math.build_lines` needs. Deleted rows excluded here, so
    the arithmetic never has to know what a Notion deletion is."""
    ingredients = {r["notion_id"]: r for r in fetch("""
        SELECT notion_id, name, slug, category, unit, yield_factor,
               shelf_life_days, par_level_pkgs, bulk, pantry, frequency,
               on_hand_base, on_hand_at, on_hand_source, food_id
          FROM nutrition.prep_ingredient WHERE deleted_at IS NULL""", ())}
    recipes = {r["notion_id"]: r for r in fetch("""
        SELECT notion_id, name, slug, course, status, archived, servings,
               prepped_on_hand, plan_eligible, food_id
          FROM nutrition.prep_recipe WHERE deleted_at IS NULL""", ())}
    stores = {r["notion_id"]: r for r in fetch("""
        SELECT notion_id, name, chain, city, location, active
          FROM nutrition.prep_store WHERE deleted_at IS NULL""", ())}
    store_items = fetch("""
        SELECT notion_id, name, ingredient_notion_id, store_notion_id, rank,
               rank_override, aisle, package_size, package_label, price
          FROM nutrition.prep_store_item WHERE deleted_at IS NULL""", ())
    lines = fetch("""
        SELECT notion_id, label, recipe_notion_id, ingredient_notion_id,
               qty_per_serving, note
          FROM nutrition.prep_recipe_line WHERE deleted_at IS NULL""", ())
    stay_days = fetch("""
        SELECT day_date, day_type, slot, recipe_notion_id, recipe_name,
               servings, source
          FROM nutrition.prep_stay_day WHERE stay_id = %s
         ORDER BY day_date, slot""", (stay_id,))
    return {"ingredients": ingredients, "recipes": recipes, "stores": stores,
            "store_items": store_items, "lines": lines, "stay_days": stay_days}


def shopping_list(fetch, stay_id: int) -> dict:
    """The list for one stay, grouped store then aisle, with a flag summary.

    Recomputed on every call. Deliberately: the alternative is a cached list, and
    a cached list is wrong the moment Ryan counts something — wrong in the shop,
    where he cannot tell.
    """
    stay = get_stay(fetch, stay_id)
    if stay is None:
        raise LookupError(f"no stay {stay_id}")
    state = load_state(fetch, stay_id)
    lines = prep_math.build_lines(
        stay=stay, stay_days=state["stay_days"], recipes=state["recipes"],
        lines=state["lines"], ingredients=state["ingredients"],
        store_items=state["store_items"], stores=state["stores"],
        seed=load_config(fetch, "store_rank_seed"),
        store_zones_by_chain=store_zones(fetch),
        # A Minneapolis stay cannot be fed from the Wisconsin store. Configurable
        # so a stay elsewhere does not need a code change; the code default is
        # MPLS and a `store_locations` config row overrides it.
        locations=load_config(fetch, "store_locations",
                              list(prep_math.DEFAULT_STORE_LOCATIONS)))
    groups = prep_math.group_by_store(lines)
    sync = sync_status(fetch)
    flags: dict = {}
    for ln in lines:
        for f in ln["flags"]:
            flags[f] = flags.get(f, 0) + 1
    return {
        "stay": {
            "id": stay["id"],
            "start_date": stay["start_date"].isoformat(),
            "end_date": stay["end_date"].isoformat(),
            "days": (stay["end_date"] - stay["start_date"]).days + 1,
            "shop_date": stay["shop_date"].isoformat() if stay["shop_date"] else None,
            "source": stay["source"],
            "confirmed": stay["confirmed_at"] is not None,
        },
        "stores": groups,
        "item_count": len(lines),
        "flags": flags,
        # THE LIST SAYS WHEN IT CANNOT BE TRUSTED. An unsynced database removes
        # quantities or stores from the calculation, and the result is a SHORT
        # list that looks finished. See sync_status().
        "sync": sync,
        "incomplete": bool(sync["blocked"]),
        "day_count": len({r["day_date"] for r in state["stay_days"]}),
        # Every day of the stay, menu or not — see macro_check for why a missing
        # day is worse than a day that says it has none.
        "menu_days": _menu_days(stay, state["stay_days"]),
    }


# ── PREP-2: steps, the kitchen profile, and sessions ────────────────────────

DEFAULT_KITCHEN = {
    "oven_slots": 2, "burners": 2, "air_fryer_slots": 1,
    "preheat_min": 10, "temp_change_min": 5,
    "filler_min": 6, "filler_name": "Clean as you go",
}


def kitchen_profile(fetch) -> dict:
    """The kitchen, from config, with the code default underneath.

    Ryan's answer to the spec's open question is the default: 1 oven with 2 usable
    racks, 2 burners, 1 air fryer. Merged over the default rather than replacing it,
    so a config row that sets only `burners` does not silently drop the preheat
    time to zero and make every oven step start instantly.
    """
    stored = load_config(fetch, "kitchen_profile") or {}
    out = dict(DEFAULT_KITCHEN)
    if isinstance(stored, dict):
        out.update({k: v for k, v in stored.items() if v is not None})
    return out


def steps_for_stay(fetch, stay_id: int) -> list:
    """Every step of every recipe the stay still has to cook, with its servings.

    Recipes with nothing left to make are excluded here rather than in the browser:
    the scheduler should never see a zero-serving recipe, and `servings_to_make` is
    a fact about the stay, which is this side's business.
    """
    return fetch("""
        WITH make AS (
            SELECT sd.recipe_notion_id AS rid,
                   GREATEST(0, SUM(sd.servings)
                               - COALESCE(MAX(r.prepped_on_hand), 0)) AS servings
              FROM nutrition.prep_stay_day sd
              JOIN nutrition.prep_recipe r ON r.notion_id = sd.recipe_notion_id
             WHERE sd.stay_id = %s AND r.deleted_at IS NULL AND r.plan_eligible
             GROUP BY sd.recipe_notion_id
        )
        SELECT r.notion_id, r.name, r.slug, m.servings,
               s.step_no, s.name AS step_name, s.resource, s.mode,
               s.base_min, s.per_serving_min, s.temp_f, s.batch_key, s.chain_key,
               s.keep_separate, s.keep_separate_note, s.shortcut_key, s.notes
          FROM make m
          JOIN nutrition.prep_recipe r ON r.notion_id = m.rid
          LEFT JOIN nutrition.recipe_step s ON s.recipe_notion_id = r.notion_id
         WHERE m.servings > 0
         ORDER BY r.name, s.step_no""", (stay_id,))


def all_steps(fetch) -> list:
    """Every recipe and its steps, for the editor. Recipes with no steps included,
    because "this one has none yet" is what the editor exists to fix."""
    return fetch("""
        SELECT r.notion_id, r.name, r.slug, r.servings, r.plan_eligible,
               s.id AS step_id, s.step_no, s.name AS step_name, s.resource, s.mode,
               s.base_min, s.per_serving_min, s.temp_f, s.batch_key, s.chain_key,
               s.keep_separate, s.keep_separate_note, s.shortcut_key, s.notes
          FROM nutrition.prep_recipe r
          LEFT JOIN nutrition.recipe_step s ON s.recipe_notion_id = r.notion_id
         WHERE r.deleted_at IS NULL AND NOT r.archived
         ORDER BY r.name, s.step_no""", ())


#: Replace one recipe's steps wholesale. The editor sends the whole list, so a
#: delete-then-insert in one transaction is both simpler and safer than diffing —
#: there is no state in which half a recipe's steps are the new ones.
DELETE_STEPS_SQL = "DELETE FROM nutrition.recipe_step WHERE recipe_notion_id = %s"
INSERT_STEP_SQL = """
INSERT INTO nutrition.recipe_step
  (recipe_notion_id, step_no, name, resource, mode, base_min, per_serving_min,
   temp_f, batch_key, chain_key, keep_separate, keep_separate_note, shortcut_key,
   notes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def sessions_for(fetch, stay_id: int | None, limit: int = 20) -> list:
    if stay_id is None:
        return fetch("""
            SELECT id, stay_id, session_date, status, started_at, finished_at,
                   planned_min, hands_on_min, actual_min, shortcuts
              FROM nutrition.prep_session ORDER BY session_date DESC, id DESC
             LIMIT %s""", (limit,))
    return fetch("""
        SELECT id, stay_id, session_date, status, started_at, finished_at,
               planned_min, hands_on_min, actual_min, shortcuts
          FROM nutrition.prep_session WHERE stay_id = %s
         ORDER BY session_date DESC, id DESC LIMIT %s""", (stay_id, limit))


def _menu_days(stay: dict, stay_days: list) -> list:
    """One entry per day of the stay, in order, with its meal count.

    A day with no menu is reported with `meals: 0`, not omitted. Sunday 10/04 —
    a travel day whose default row does not exist in Notion — was vanishing from
    the list entirely, which reads as a day that is fine rather than a gap.
    """
    counts: dict = {}
    for r in stay_days:
        counts[r["day_date"]] = counts.get(r["day_date"], 0) + 1
    out = []
    d = stay["start_date"]
    while d <= stay["end_date"]:
        out.append({"day": d.isoformat(), "meals": counts.get(d, 0)})
        d += timedelta(days=1)
    return out


# ── pantry ──────────────────────────────────────────────────────────────────

#: The preferred live store item per ingredient — the one whose package the
#: pantry screen counts in and the shopping list buys. Written once, used by
#: every query that needs a package size, so the pantry stepper and the list can
#: never disagree about what "one package" means.
_PREFERRED_ITEM = """
    SELECT DISTINCT ON (si.ingredient_notion_id)
           si.ingredient_notion_id AS ing, si.package_size, si.package_label,
           s.name AS store_name, si.notion_id AS item_id
      FROM nutrition.prep_store_item si
      JOIN nutrition.prep_store s ON s.notion_id = si.store_notion_id
     WHERE si.deleted_at IS NULL AND s.deleted_at IS NULL AND s.active
     ORDER BY si.ingredient_notion_id,
              COALESCE(si.rank_override, si.rank, 999), si.price NULLS LAST
"""


def pantry_rows(fetch) -> list:
    """Every countable ingredient with its preferred package.

    `on_hand_pkgs` is DERIVED for display only; the stored column is base units
    (alignment decision 7). NULL means either never counted or no package size —
    two different unknowns, which `on_hand_base` and `package_size` distinguish.
    """
    return fetch(f"""
        WITH pref AS ({_PREFERRED_ITEM})
        SELECT i.notion_id AS ingredient_id, i.name, i.category, i.unit,
               i.on_hand_base, i.on_hand_at, i.on_hand_source,
               i.par_level_pkgs, i.shelf_life_days, i.pantry,
               pref.package_size, pref.package_label, pref.store_name,
               CASE WHEN pref.package_size IS NULL OR pref.package_size <= 0
                      OR i.on_hand_base IS NULL THEN NULL
                    ELSE ROUND(i.on_hand_base / pref.package_size, 2) END
                 AS on_hand_pkgs
          FROM nutrition.prep_ingredient i
          LEFT JOIN pref ON pref.ing = i.notion_id
         WHERE i.deleted_at IS NULL
         ORDER BY i.category NULLS LAST, i.name""", ())


def preferred_package_size(fetch, ingredient_id: str):
    rows = fetch(f"""
        WITH pref AS ({_PREFERRED_ITEM})
        SELECT package_size FROM pref WHERE ing = %s
    """, (ingredient_id,))
    if not rows:
        return None
    size = rows[0]["package_size"]
    return float(size) if size and float(size) > 0 else None


#: The one statement that records a count. Base units, always.
SET_ON_HAND_SQL = (
    "UPDATE nutrition.prep_ingredient "
    "SET on_hand_base = %s, on_hand_at = now(), on_hand_source = %s "
    "WHERE notion_id = %s AND deleted_at IS NULL")


def base_from_packages(fetch, ingredient_id: str, packages: float) -> float:
    """Convert a package count to base units, or refuse.

    FAIL-CLOSED: an ingredient with no package size cannot be counted in packages,
    and storing the raw number would silently record "3" grams of olive oil
    because the stepper said 3 bottles. The caller surfaces the refusal.
    """
    size = preferred_package_size(fetch, ingredient_id)
    if size is None:
        raise ValueError(
            "no package size for this ingredient — count it in base units")
    return float(packages) * size


# ── per-day macro check ─────────────────────────────────────────────────────

#: Targets that are CEILINGS, and targets that are FLOORS. The direction belongs
#: to the target, not to the reading: over 2,100 kcal and under 185 g protein are
#: both worth a chip, and a single comparison could only catch one of them.
CEILINGS = ("kcal", "carb_g", "fat_g")
FLOORS = ("protein_g", "fiber_g")


def macro_check(fetch, stay_id: int) -> dict:
    """Per-day planned macros against the open target. WARNS, never blocks.

    A macro the plan cannot supply produces NO chip — never a chip reading zero.
    Notion's recipes carry no sugar figure, so the sugar target has no data source
    and the day reports it under `no_data`. Summing absent values to zero would
    report every day as comfortably inside a limit nothing measured, which is the
    one wrong answer that looks like good news.
    """
    stay = get_stay(fetch, stay_id)
    trows = fetch(
        "SELECT kcal, protein_g, carb_g, fat_g, fiber_g, sugar_g, "
        "       plant_meals_min, set_by, provisional, effective_from "
        "  FROM nutrition.target WHERE effective_to IS NULL "
        " ORDER BY effective_from DESC LIMIT 1", ())
    if not trows:
        return {"target": None, "days": [],
                "detail": "no open target; nothing to check against"}
    t = dict(trows[0])
    t["effective_from"] = t["effective_from"].isoformat()

    rows = fetch("""
        SELECT sd.day_date, sd.slot, sd.servings, sd.recipe_name,
               f.kcal, f.protein_g, f.carb_g, f.fat_g, f.fiber_g, f.is_placeholder
          FROM nutrition.prep_stay_day sd
          LEFT JOIN nutrition.food f
                 ON f.source = 'notion' AND f.kind = 'recipe'
                AND f.source_id = sd.recipe_notion_id
         WHERE sd.stay_id = %s
         ORDER BY sd.day_date""", (stay_id,))

    keys = ("kcal", "protein_g", "carb_g", "fat_g", "fiber_g")
    by_day: dict = {}
    for r in rows:
        d = by_day.setdefault(r["day_date"], {
            "totals": {k: 0.0 for k in keys}, "missing": set(),
            "placeholder": False, "meals": 0})
        d["meals"] += 1
        if r["kcal"] is None:
            # No food row at all: the recipe is not mirrored, so this meal
            # contributes nothing and the day says which meal it was.
            d["missing"].add(r["recipe_name"] or "(unnamed)")
            continue
        if r["is_placeholder"]:
            d["placeholder"] = True
        n = float(r["servings"] or 1)
        for key in keys:
            v = r[key]
            if v is None:
                d["missing"].add(f"{r['recipe_name']}: {key}")
            else:
                d["totals"][key] += float(v) * n

    no_data = [k for k in ("sugar_g", "plant_meals_min") if t.get(k) is not None]

    # EVERY DAY OF THE STAY APPEARS, including the ones with no menu.
    #
    # Sunday 10/04 was simply missing from the list, because it is a travel day and
    # the `default day — travel day` row does not exist in Notion, so it has no
    # stay_day rows to group. A day that silently vanishes reads as a day that is
    # fine; a day that says "no menu" reads as a gap to fill. They are different
    # answers and only one of them is true.
    if stay is not None:
        d = stay["start_date"]
        while d <= stay["end_date"]:
            by_day.setdefault(d, {
                "totals": {k: 0.0 for k in keys}, "missing": set(),
                "placeholder": False, "meals": 0})
            d += timedelta(days=1)

    days = []
    for d in sorted(by_day):
        info = by_day[d]
        if info["meals"] == 0:
            # No menu at all: report the gap and NOTHING else. Chips computed from
            # zero totals would say "under every target", which is true of an empty
            # day in the way that is useless.
            days.append({"day": d.isoformat(), "meals": 0, "totals": None,
                         "chips": [], "no_data": no_data, "placeholder": False,
                         "missing": [], "no_menu": True})
            continue
        chips = []
        for key in CEILINGS + FLOORS:
            limit = t.get(key)
            if limit is None:
                continue
            value = round(info["totals"][key], 1)
            over = key in CEILINGS and value > float(limit)
            under = key in FLOORS and value < float(limit)
            if over or under:
                chips.append({"macro": key, "value": value,
                              "target": float(limit),
                              "direction": "over" if over else "under"})
        days.append({"day": d.isoformat(), "meals": info["meals"],
                     "totals": {k: round(v, 1) for k, v in info["totals"].items()},
                     "chips": chips, "no_data": no_data,
                     "placeholder": info["placeholder"],
                     "missing": sorted(info["missing"])[:8], "no_menu": False})
    return {"target": t, "days": days,
            "days_without_menu": sum(1 for x in days if x["no_menu"])}
