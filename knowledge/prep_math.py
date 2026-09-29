"""PREP-1 — the shopping-list arithmetic. ONE definition, no database, no Notion.

This module is pure: every function takes plain dicts and returns plain dicts.
That is deliberate and it is why it lives in `knowledge/` rather than `artemis/`
— `knowledge/` is in the Lambda package, so the API can recompute a list from
RDS on every read. A list computed once on the box and cached would go stale the
moment Ryan counted a pantry item on his phone, and a stale shopping list is
worse than no shopping list: it is wrong in a way that looks right in the shop.

Alignment decision 11 (2026-09-29): the arithmetic is Python, not SQL. There is
no engine-specific CEIL here and no reliance on Postgres numeric behaviour, so
the tests exercise the same code the Lambda runs.

THE FIVE QUANTITIES, kept apart on purpose — conflating any two of them is the
whole difficulty of this calculation:

  as-used      what the recipe consumes (`qty per serving`), in the base unit.
  purchased    what you must buy to have that much: as-used / yield_factor.
               Dry lentils yield 2.5x cooked, so 250 g cooked is 100 g bought.
  on-hand      what is already in the flat, in the base unit.
  short        purchased minus on-hand, floored at zero.
  packages     short divided by the PREFERRED STORE ITEM's package size,
               rounded UP, because half a carton is not purchasable.

Nothing here invents a number. A missing unit, a missing package size, a recipe
with no serving count and an ingredient that has never been counted each produce
a FLAG and a null, never a zero standing in for a fact. A zero here would read
as "you have enough", which is the one wrong answer that is silent in the shop.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

#: Flags a list line can carry. Every one of them is a reason a human should
#: look at the line, not a failure of the calculation.
FLAG_NO_STORE = "no_store"                # nothing sells it, or every store is inactive
FLAG_NO_RANK1 = "no_rank1"                # only a second- or third-choice store has it
FLAG_TOP_UP = "top_up"                    # perishable: part of it is bought mid-stay
FLAG_COUNT_UNKNOWN = "count_unknown"      # never counted; computed as if none on hand
FLAG_NO_PACKAGE_SIZE = "no_package_size"  # cannot convert base units to packages
FLAG_NO_UNIT = "no_unit"                  # the ingredient has no base unit in Notion
FLAG_PAR_ONLY = "par_only"                # on the list only to restock a staple
FLAG_NO_SERVINGS = "no_servings"          # recipe has no serving count; batches unknown

#: Where an effective rank came from, so a list can explain itself.
BASIS_OVERRIDE = "override"
BASIS_NOTION = "notion"
BASIS_SEED = "seed"
BASIS_NONE = "none"

_DEFAULT_SEED = {
    "produce_or_bulk": ["Eastside Co-op", "Aldi", "Cub Foods"],
    "default": ["Aldi", "Eastside Co-op", "Cub Foods"],
    "produce_categories": ["produce"],
}


def _f(v) -> float | None:
    """A number, or None. Decimal from psycopg2 included; '' and None are None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def yield_factor(ingredient: dict) -> float:
    """As-used per 1 purchased unit. Blank means 1 (Notion's own note).

    A zero or negative factor is data corruption, not a divisor: treated as 1 so
    the line still appears with a sane quantity rather than raising or producing
    an infinity that renders as a shopping instruction.
    """
    yf = _f(ingredient.get("yield_factor"))
    if yf is None or yf <= 0:
        return 1.0
    return yf


def purchased_from_as_used(as_used: float, ingredient: dict) -> float:
    return as_used / yield_factor(ingredient)


def packages_for(short_base: float | None, package_size) -> int | None:
    """Packages to buy: short / package size, rounded UP. None when unknowable.

    None and 0 are different answers and must stay different: 0 means "you have
    enough", None means "nobody has told me how big a package is".
    """
    if short_base is None:
        return None
    size = _f(package_size)
    if size is None or size <= 0:
        return None
    if short_base <= 0:
        return 0
    # A hair of floating-point slop must not buy a whole extra package: 2.0000001
    # packages is two packages, not three.
    return int(math.ceil(round(short_base / size, 6)))


# ── store ranking ───────────────────────────────────────────────────────────

def _is_produce_or_bulk(ingredient: dict, seed: dict) -> bool:
    if ingredient.get("bulk"):
        return True
    cats = [c.lower() for c in (seed.get("produce_categories") or [])]
    return (ingredient.get("category") or "").lower() in cats


def seed_rank(ingredient: dict, store: dict, seed: dict | None = None) -> int | None:
    """The fallback rank from the seed rule: produce & bulk favour the co-op.

    None when the store's chain is not in the rule at all — an unranked store is
    not implicitly last, because "I have no opinion" and "worst choice" would
    order differently against a store that IS ranked third.
    """
    seed = seed or _DEFAULT_SEED
    order = (seed.get("produce_or_bulk") if _is_produce_or_bulk(ingredient, seed)
             else seed.get("default")) or []
    chain = store.get("chain") or store.get("name")
    for i, name in enumerate(order):
        if name and chain and name.lower() == str(chain).lower():
            return i + 1
    return None


def effective_rank(item: dict, ingredient: dict, store: dict,
                   seed: dict | None = None) -> tuple[int | None, str]:
    """(rank, basis). An explicit override wins, then Notion, then the seed."""
    ov = item.get("rank_override")
    if ov is not None:
        return int(ov), BASIS_OVERRIDE
    rank = item.get("rank")
    if rank is not None:
        return int(rank), BASIS_NOTION
    s = seed_rank(ingredient, store, seed)
    if s is not None:
        return s, BASIS_SEED
    return None, BASIS_NONE


#: Store locations a Minneapolis stay may shop at. A Minneapolis stay cannot be
#: fed from the Wisconsin store, and nothing in the rank ordering says so — rank
#: is "first choice" among the stores that are actually reachable.
#:
#: Added 2026-09-29 after reading Ryan's source spec, which filters on
#: `location = 'MPLS'` in both the prose and the SQL. The shipped PREP-1 code did
#: not, so once the `stores` database syncs, Piggly Wiggly (location WI) would
#: have been eligible for an MSP stay and could have won on rank. The list was
#: empty at the time only because those databases are unshared, so the bug had
#: not yet had the chance to produce a wrong answer.
DEFAULT_STORE_LOCATIONS = ("MPLS",)


def choose_item(items: list[dict], ingredient: dict, stores: dict,
                seed: dict | None = None, *, locations=DEFAULT_STORE_LOCATIONS,
                need: float | None = None) -> dict | None:
    """The store item to buy this ingredient from, or None.

    Eligible means: the item is live, its store is live and active, AND the store
    is in `locations`. Lowest effective rank wins; an item with NO rank at all
    sorts after every ranked one rather than ahead of them.

    TIE-BREAK, in order (Ryan's spec, adopted 2026-09-29):
      1. an item whose package COVERS the need, before one that does not;
      2. among those that cover it, the SMALLEST such package;
      3. among those that do not, the LARGEST, since it takes fewest of them;
      4. then price, then name.

    (1)-(3) serve the spec's "<= 1 package over per item" criterion: buying the
    smallest carton that still does the job is what keeps the overshoot inside one
    package. Price and name remain as the final tie-break so the answer is stable
    run to run — a shopping list that reorders itself between refreshes reads as a
    bug. `need` is the PURCHASED quantity; with `need=None` the package rules are
    skipped and price decides, which is right for a line that exists only to
    restock a staple.

    `locations=None` disables the location filter. A store whose location is not
    recorded at all is EXCLUDED when filtering: an unknown location is not
    assumed to be the right one, for the same reason an uncounted ingredient is
    not assumed to be in stock.
    """
    allowed = None if locations is None else {str(x).upper() for x in locations}
    eligible = []
    for it in items:
        if it.get("deleted_at"):
            continue
        store = stores.get(it.get("store_notion_id")) or {}
        if not store or store.get("deleted_at") or not store.get("active", True):
            continue
        if allowed is not None:
            loc = store.get("location")
            if loc is None or str(loc).upper() not in allowed:
                continue
        rank, basis = effective_rank(it, ingredient, store, seed)
        size = _f(it.get("package_size"))
        # covers: 0 sorts before 1. size_key: ascending for a package that covers
        # the need, DESCENDING (negated) for one that does not, so the first pick
        # among non-covering packages is the largest and therefore the fewest.
        if need is None or size is None or size <= 0:
            covers, size_key = 1, 0.0
        elif size >= need:
            covers, size_key = 0, size
        else:
            covers, size_key = 1, -size
        price = _f(it.get("price"))
        eligible.append((rank if rank is not None else 10 ** 6,
                         covers, size_key,
                         price if price is not None else 10 ** 6,
                         it.get("name") or "", it, rank, basis, store))
    if not eligible:
        return None
    eligible.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
    it, rank, basis, store = eligible[0][5:]
    out = dict(it)
    out["_rank"] = rank
    out["_rank_basis"] = basis
    out["_store"] = store
    return out


# ── aisles ──────────────────────────────────────────────────────────────────

def aisle_for(item: dict | None, ingredient: dict, store_zones: dict | None) -> str:
    """The aisle label to group under.

    The store item's own `aisle` is authoritative — Ryan walked the shop. Only
    when it is blank does the store_maps.json keyword map get a turn, and only
    then does "Other" appear. store_maps is reused here rather than replaced
    (alignment decision 6): it already holds Aldi's zone order.
    """
    if item and (item.get("aisle") or "").strip():
        return (item["aisle"] or "").strip()
    name = (ingredient.get("name") or "").lower()
    zones = (store_zones or {}).get("zones") or []
    for zone in sorted(zones, key=lambda z: z.get("order", 999)):
        for kw in zone.get("keywords") or []:
            if kw and kw.lower() in name:
                return zone.get("name") or "Other"
    return "Other"


# ── the stay's menu → per-day servings to MAKE ──────────────────────────────

def servings_to_make(stay_days: list[dict], recipes: dict) -> dict:
    """Per-recipe, per-day servings that still have to be COOKED.

    `prepped on hand` is ready-made food already in the fridge, so it is consumed
    by the EARLIEST days of the stay, in order — not spread evenly and not
    subtracted from the total. The distinction matters because the top-up split
    below asks which DAY a need falls on: a recipe with three prepped servings
    eaten on Sunday, Monday and Tuesday needs nothing bought for those days, and
    the perishable ingredients for Wednesday onward are what drive the list.

    Returns {recipe_notion_id: {day: servings_to_make}} and never a negative.
    """
    by_recipe: dict[str, list[tuple[date, float]]] = {}
    for row in stay_days:
        rid = row.get("recipe_notion_id")
        if not rid:
            continue
        rec = recipes.get(rid) or {}
        if rec.get("plan_eligible") is False:
            # Alignment decision 12: eatable, never planned.
            continue
        qty = _f(row.get("servings"))
        by_recipe.setdefault(rid, []).append((row["day_date"], qty if qty is not None else 1.0))

    out: dict[str, dict] = {}
    for rid, entries in by_recipe.items():
        stock = _f((recipes.get(rid) or {}).get("prepped_on_hand")) or 0.0
        per_day: dict[date, float] = {}
        for day, qty in sorted(entries, key=lambda e: e[0]):
            take = min(stock, qty)
            stock -= take
            remaining = qty - take
            if remaining > 0:
                per_day[day] = per_day.get(day, 0.0) + remaining
        out[rid] = per_day
    return out


def ingredient_need_by_day(make: dict, lines: list[dict]) -> dict:
    """{ingredient_notion_id: {day: as-used quantity}} in the base unit."""
    by_recipe: dict[str, list[dict]] = {}
    for ln in lines:
        if ln.get("deleted_at"):
            continue
        rid = ln.get("recipe_notion_id")
        if rid:
            by_recipe.setdefault(rid, []).append(ln)

    out: dict[str, dict] = {}
    for rid, per_day in make.items():
        for ln in by_recipe.get(rid, []):
            ing = ln.get("ingredient_notion_id")
            qps = _f(ln.get("qty_per_serving"))
            if not ing or qps is None:
                continue
            bucket = out.setdefault(ing, {})
            for day, servings in per_day.items():
                bucket[day] = bucket.get(day, 0.0) + qps * servings
    return out


# ── the top-up split ────────────────────────────────────────────────────────

def topup_split(need_by_day: dict, shop_date, shelf_life_days,
                end_date) -> tuple[float, float, date | None]:
    """(buy_now, buy_later, later_day) for one ingredient.

    The rule, stated precisely (alignment decision 11): N is the first day the
    item bought on `shop_date` is PAST its shelf life. Everything needed before N
    is bought now; everything needed on or after N is bought on N. When N falls
    outside the stay, or there is no shelf life, it is one purchase.

    `later_day` is None in exactly that case, and a caller must not render a
    second trip without one.
    """
    total = sum(need_by_day.values())
    life = None if shelf_life_days is None else int(shelf_life_days)
    if not life or life <= 0 or shop_date is None or end_date is None:
        return total, 0.0, None
    n = shop_date + timedelta(days=life)
    if n > end_date:
        return total, 0.0, None
    now = sum(q for d, q in need_by_day.items() if d < n)
    later = sum(q for d, q in need_by_day.items() if d >= n)
    if later <= 0:
        return total, 0.0, None
    return now, later, n


# ── the list ────────────────────────────────────────────────────────────────

def build_lines(*, stay: dict, stay_days: list[dict], recipes: dict,
                lines: list[dict], ingredients: dict, store_items: list[dict],
                stores: dict, seed: dict | None = None,
                store_zones_by_chain: dict | None = None,
                locations=DEFAULT_STORE_LOCATIONS) -> list[dict]:
    """One line per ingredient that has to be bought, or flagged.

    Recipe demand and par level are combined as a FLOOR, not a sum: Notion's own
    note reads "keep this many packages on hand regardless of recipes", so the
    requirement is max(what the stay consumes, what the staple floor is). Adding
    them would buy a fortnight of olive oil every fortnight.
    """
    seed = seed or _DEFAULT_SEED
    make = servings_to_make(stay_days, recipes)
    need = ingredient_need_by_day(make, lines)

    # Ingredients belonging to a planned recipe that has no serving count.
    no_serving_ings: set[str] = set()
    for ln in lines:
        rid = ln.get("recipe_notion_id")
        if rid in make and _f((recipes.get(rid) or {}).get("servings")) is None:
            if ln.get("ingredient_notion_id"):
                no_serving_ings.add(ln["ingredient_notion_id"])

    items_by_ing: dict[str, list[dict]] = {}
    for it in store_items:
        ing = it.get("ingredient_notion_id")
        if ing:
            items_by_ing.setdefault(ing, []).append(it)

    shop_date = stay.get("shop_date") or stay.get("start_date")
    end_date = stay.get("end_date")

    # Every ingredient the stay needs, plus every staple with a par level, even
    # one no recipe touches — a staple is on the list because it ran low, not
    # because a recipe asked for it.
    candidates = set(need)
    for nid, ing in ingredients.items():
        if ing.get("deleted_at"):
            continue
        if _f(ing.get("par_level_pkgs")):
            candidates.add(nid)

    out: list[dict] = []
    for nid in candidates:
        ing = ingredients.get(nid)
        if not ing or ing.get("deleted_at"):
            continue
        flags: list[str] = []
        # ONLY DAYS FROM THE SHOP DATE ONWARD. A stay already in progress has days
        # behind it that have been eaten, and food cannot be bought for
        # yesterday. Counting them would inflate every quantity by however long
        # the stay had been running — on 2026-09-29 the stay covering the 10/04
        # shop had started on 09/28, so a whole week of meals would have been
        # bought twice.
        per_day = {d: q for d, q in (need.get(nid) or {}).items()
                   if shop_date is None or d >= shop_date}

        as_used_total = sum(per_day.values())
        purchased_by_day = {d: purchased_from_as_used(q, ing) for d, q in per_day.items()}
        purchased_total = sum(purchased_by_day.values())

        # `need` is the purchased quantity, which is what the tie-break measures
        # a package against. It is known before the item is chosen, and `short`
        # is not — short depends on the package size, so it cannot also decide it.
        item = choose_item(items_by_ing.get(nid, []), ing, stores, seed,
                           locations=locations,
                           need=purchased_total if purchased_total > 0 else None)
        if item is None:
            flags.append(FLAG_NO_STORE)
        elif item.get("_rank") is None or item["_rank"] > 1:
            flags.append(FLAG_NO_RANK1)

        pkg_size = _f(item.get("package_size")) if item else None
        if not ing.get("unit"):
            flags.append(FLAG_NO_UNIT)

        on_hand = _f(ing.get("on_hand_base"))
        if on_hand is None:
            # Never counted. Computed as if none on hand — the safe direction, a
            # spare package rather than a missing dinner — and flagged so the
            # line says the count is a gap, not a measurement.
            flags.append(FLAG_COUNT_UNKNOWN)
            on_hand = 0.0

        par_pkgs = _f(ing.get("par_level_pkgs"))
        par_base = par_pkgs * pkg_size if (par_pkgs and pkg_size) else None
        required = purchased_total
        if par_base is not None and par_base > required:
            required = par_base
            if purchased_total <= 0:
                flags.append(FLAG_PAR_ONLY)

        short = max(0.0, required - on_hand)
        pkgs = packages_for(short, pkg_size)
        if pkg_size is None and required > 0:
            flags.append(FLAG_NO_PACKAGE_SIZE)

        # A recipe on this line with no serving count: the per-serving quantities
        # are sound but the batch arithmetic above them is unverifiable.
        if nid in no_serving_ings:
            flags.append(FLAG_NO_SERVINGS)

        buy_now_base, buy_later_base, later_day = topup_split(
            purchased_by_day, shop_date, ing.get("shelf_life_days"), end_date)
        if later_day is not None and short > 0:
            flags.append(FLAG_TOP_UP)
            # The split is of the NEED; what is short comes off the first trip
            # first, because that is the trip that happens.
            now_short = max(0.0, min(short, max(0.0, buy_now_base - on_hand)))
            later_short = max(0.0, short - now_short)
        else:
            now_short, later_short = short, 0.0

        store = item.get("_store") if item else None
        chain = (store or {}).get("chain") or (store or {}).get("name")
        zones = (store_zones_by_chain or {}).get(str(chain).lower()) if chain else None

        # What earns a line a place on the list. Short of something is the
        # obvious case. The other case is an ingredient the stay genuinely needs
        # that cannot be TURNED INTO a shopping instruction — nobody sells it, or
        # nobody has said how big a package is. Dropping those would make the
        # list look complete while quietly missing a dinner, so they appear with
        # their flag and no package count.
        blocked = (FLAG_NO_STORE in flags or FLAG_NO_PACKAGE_SIZE in flags
                   or FLAG_NO_UNIT in flags)
        if short <= 0 and not (required > 0 and blocked):
            continue

        out.append({
            "ingredient_id": nid,
            "name": ing.get("name"),
            "category": ing.get("category"),
            "unit": ing.get("unit"),
            "as_used_base": round(as_used_total, 3),
            "purchased_base": round(purchased_total, 3),
            "yield_factor": yield_factor(ing),
            "on_hand_base": None if FLAG_COUNT_UNKNOWN in flags else round(on_hand, 3),
            "par_level_pkgs": par_pkgs,
            "required_base": round(required, 3),
            "short_base": round(short, 3),
            "packages": pkgs,
            "package_size": pkg_size,
            "package_label": (item or {}).get("package_label"),
            "price": _f((item or {}).get("price")),
            "store": (store or {}).get("name"),
            "store_chain": chain,
            "store_id": (store or {}).get("notion_id"),
            "rank": (item or {}).get("_rank"),
            "rank_basis": (item or {}).get("_rank_basis", BASIS_NONE),
            "aisle": aisle_for(item, ing, zones),
            "packages_now": packages_for(now_short, pkg_size),
            "packages_later": packages_for(later_short, pkg_size) if later_day else None,
            "later_day": later_day.isoformat() if later_day else None,
            "flags": flags,
        })
    return out


def group_by_store(lines: list[dict]) -> list[dict]:
    """Lines grouped store → aisle, in shopping order.

    Stores with no rank-1 item sort after ranked ones; the no-store group is LAST
    and is named, because an item nobody sells is a decision for Ryan, not a
    silent omission.
    """
    groups: dict[str, dict] = {}
    for ln in lines:
        key = ln.get("store_id") or "_none"
        g = groups.setdefault(key, {
            "store": ln.get("store"), "chain": ln.get("store_chain"),
            "store_id": ln.get("store_id"), "aisles": {},
            "rank": ln.get("rank") if ln.get("rank") is not None else 10 ** 6,
        })
        if ln.get("rank") is not None:
            g["rank"] = min(g["rank"], ln["rank"])
        g["aisles"].setdefault(ln.get("aisle") or "Other", []).append(ln)

    out = []
    for key, g in groups.items():
        aisles = [{"aisle": a, "items": sorted(v, key=lambda x: (x.get("name") or ""))}
                  for a, v in sorted(g["aisles"].items(), key=lambda kv: _aisle_key(kv[0]))]
        out.append({"store": g["store"], "chain": g["chain"], "store_id": g["store_id"],
                    "rank": None if g["rank"] >= 10 ** 6 else g["rank"],
                    "item_count": sum(len(a["items"]) for a in aisles),
                    "aisles": aisles})
    out.sort(key=lambda s: (s["store_id"] is None, s["rank"] if s["rank"] is not None else 10 ** 6,
                            s["store"] or ""))
    return out


_NUM_RE = re.compile(r"\d+")


def _aisle_key(name: str):
    """Numeric aisles sort numerically ("Aisle 2" before "Aisle 10"); the rest
    alphabetically; "Other" last."""
    if (name or "").strip().lower() == "other":
        return (2, 0, "")
    m = _NUM_RE.search(name or "")
    if m:
        return (0, int(m.group()), name)
    return (1, 0, name)
