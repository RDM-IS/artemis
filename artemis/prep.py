"""PREP-1 — stays, the stay's menu, and the shopping list assembled from RDS.

Division of labour, decided by which side can reach what (alignment decision 1):

  the box     can read Notion. It syncs the procurement databases and it RESOLVES
              THE MENU for a stay — which recipes each day calls for — because
              that answer lives in Notion's `meal planning` rows.
  the Lambda  cannot read Notion and does not need to. It reads the synced tables
              and the resolved menu out of RDS and recomputes the list on every
              request, so a pantry count entered on the phone changes the list at
              once instead of waiting for the next box run.

Both sides do the arithmetic with `knowledge/prep_math.py`, which is why that
module is pure and lives in `knowledge/` (the Lambda package). There is one
calculation, not a box copy and a Lambda copy.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from knowledge import prep_store

logger = logging.getLogger(__name__)

#: Day types that put him in Minneapolis with a kitchen.
MSP_DAY_TYPES = ("msp_work", "msp_home")

#: How far ahead to look for the next stay. Two cycles: enough to find the stay
#: after the current one even if the current one has just ended.
LOOKAHEAD_DAYS = 28


# ── stays ───────────────────────────────────────────────────────────────────

def day_type_map(cur, start: date, end: date) -> dict:
    """{date: day_type} over the window, overrides honoured, away days marked.

    Goes through `nutrition.meal_day_type` — the ONE resolver that may answer
    "what kind of day is this, for food" (AWAY-DAYKIND, 2026-09-29). This used to
    hold its own copy of the away lookup, written because `cycle.day_type()`
    cannot return `away`. Two copies of that condition is how the two answers
    drift: the round that wrote this one did not notice that
    `nutrition.resolve_meal_source` still asked `cycle.day_type()` and so would
    pre-fill a work-day menu for the same fortnight this correctly excluded from
    a stay. The away stays are prefetched ONCE for the window and threaded in, so
    delegating costs one query rather than one per day.

    Propagates `cycle.OverrideLookupError` and `away.AwayLookupError`. A stay
    proposed from an unreadable override table would be a plausible wrong answer
    that then drives a shopping list — the fail-open shape
    FAIL-CLOSED-RESOLVERS exists to forbid.
    """
    from artemis import away, nutrition
    stays = away.load_stays(cur, start, end)
    out = {}
    d = start
    while d <= end:
        out[d] = nutrition.meal_day_type(d, stays=stays)
        d += timedelta(days=1)
    return out


def find_stays(types: dict) -> list:
    """Maximal runs of Minneapolis days, each with its arrival travel day.

    A `travel` day joins the stay that FOLLOWS it when the next day is an MSP day
    — that is what makes it an arrival rather than a departure, and it is derived
    from the day types themselves so an override that moves a drive moves the
    stay with it. A travel day followed by anything else is a departure and
    belongs to no stay.

    Returns [(start, end)] in date order. The first day may be the travel day;
    the last is always an MSP day.
    """
    days = sorted(types)
    runs: list = []
    cur_run: list = []
    for d in days:
        t = types[d]
        if t in MSP_DAY_TYPES:
            cur_run.append(d)
            continue
        if t == "travel":
            nxt = d + timedelta(days=1)
            if types.get(nxt) in MSP_DAY_TYPES:
                # arrival: it opens a stay, so close any run before it
                if cur_run:
                    runs.append((cur_run[0], cur_run[-1]))
                cur_run = [d]
                continue
        if cur_run:
            runs.append((cur_run[0], cur_run[-1]))
            cur_run = []
    if cur_run:
        runs.append((cur_run[0], cur_run[-1]))
    # A run that is only a travel day is not a stay — he arrives and leaves the
    # same day, which the window's edge can produce.
    return [(a, b) for a, b in runs if not (a == b and types.get(a) == "travel")]


def next_stay(cur, today: date, *, lookahead: int = LOOKAHEAD_DAYS) -> tuple | None:
    """The stay covering `today`, else the next one starting after it.

    The window starts a week BEFORE today so a stay already in progress is
    returned whole, with its real start date, rather than truncated to today —
    the shop date and the top-up split are both measured from the start.
    """
    types = day_type_map(cur, today - timedelta(days=7), today + timedelta(days=lookahead))
    for a, b in find_stays(types):
        if a <= today <= b:
            return (a, b)
    for a, b in find_stays(types):
        if a > today:
            return (a, b)
    return None


def upsert_stay(cur, start: date, end: date, *, source: str = "cycle",
                shop_date: date | None = None) -> int:
    """Record the stay. Idempotent on start_date — re-proposing the same stay
    does not create a second row, and a confirmation is not lost by a re-run.

    A stay Ryan has CONFIRMED is not re-dated by the cycle behind his back: the
    dates only move on a row he has not confirmed. Propose-then-confirm means the
    proposal stops proposing once he has answered.
    """
    cur.execute(
        """INSERT INTO nutrition.prep_stay (start_date, end_date, source, shop_date)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (start_date) DO UPDATE SET
             end_date  = CASE WHEN nutrition.prep_stay.confirmed_at IS NULL
                              THEN EXCLUDED.end_date ELSE nutrition.prep_stay.end_date END,
             shop_date = COALESCE(nutrition.prep_stay.shop_date, EXCLUDED.shop_date),
             updated_at = now()
           RETURNING id""",
        (start, end, source, shop_date))
    return cur.fetchone()[0]


def get_stay(cur, stay_id: int) -> dict | None:
    cur.execute(
        "SELECT id, start_date, end_date, source, shop_date, confirmed_at, notes "
        "FROM nutrition.prep_stay WHERE id = %s", (stay_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "start_date": row[1], "end_date": row[2],
            "source": row[3], "shop_date": row[4], "confirmed_at": row[5],
            "notes": row[6]}


# ── the stay's menu ─────────────────────────────────────────────────────────

def resolve_menu(cur, stay_id: int, start: date, end: date) -> dict:
    """Resolve each day of the stay to its recipes and store them.

    Uses `nutrition.resolve_meal_source`, which IS NUTRITION-2's source order —
    dated pick, then the day kind's default row. Re-implementing that order here
    would let the list and the 00:15 pre-fill disagree about what he is eating,
    and the list would be the one that is wrong.

    A day with no source contributes nothing and is REPORTED, never filled with a
    default menu. An off day genuinely has no default; saying so is the answer.
    """
    from artemis import nutrition

    cur.execute("DELETE FROM nutrition.prep_stay_day WHERE stay_id = %s", (stay_id,))
    out = {"days": [], "no_source": [], "unavailable": []}
    d = start
    while d <= end:
        src = nutrition.resolve_meal_source(d)
        if src.outcome == "unavailable":
            # Notion is unreachable. RAISE: the DELETE above has already cleared
            # the old menu, so returning here would leave a partial menu that
            # silently shortens the shopping list — fewer meals, less food, and
            # nothing on the list saying a day was skipped. The caller runs this
            # in one transaction, so the raise restores the previous menu intact.
            from artemis.notion_meal_plan import NotionUnavailable
            raise NotionUnavailable(
                f"menu for {d.isoformat()} could not be read: {src.detail}")
        if src.outcome != "planned":
            out["no_source"].append({"day": d.isoformat(), "kind": src.kind,
                                     "detail": src.detail})
            d += timedelta(days=1)
            continue
        counts: dict = {}
        for slot, food in src.foods:
            key = (slot, food.page_id, food.name)
            counts[key] = counts.get(key, 0) + 1
        for (slot, page_id, name), n in counts.items():
            cur.execute(
                """INSERT INTO nutrition.prep_stay_day
                     (stay_id, day_date, day_type, slot, recipe_notion_id,
                      recipe_name, servings, source)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (stay_id, d, src.day_type, slot, page_id, name, n, src.chosen))
        out["days"].append({"day": d.isoformat(), "day_type": src.day_type,
                            "source": src.chosen, "recipes": len(counts)})
        d += timedelta(days=1)
    return out


# ── the box's view of RDS: one adapter, then knowledge/prep_store ───────────
#
# Every query lives in `knowledge/prep_store.py` so the box and the Lambda read
# the same SQL (see that module's header). All the box adds is a psycopg2 cursor
# adapter; nothing below restates a query.

def fetcher(cur):
    """A `fetch(sql, params) -> list[dict]` over a psycopg2 cursor."""
    def fetch(sql, params=()):
        cur.execute(sql, params)
        if cur.description is None:
            return []
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return fetch


def sync_store_zones(cur) -> int:
    """Copy store_maps.json into nutrition.prep_config as `store_zones`.

    The aisle zones Ryan walked already exist in store_maps.json at the repository
    root (alignment decision 6: reuse it). The Lambda cannot read that file — the
    package ships only api/, knowledge/ and migrations/ (PACKAGE-IDENTITY) — so
    the box copies it into RDS, where both sides read ONE copy. The file stays the
    place it is edited; this is a mirror, exactly as Notion is for recipes.

    Returns the number of chains mirrored. Zero is not an error: every store item
    that carries its own aisle groups without the fallback.
    """
    path = Path("store_maps.json")
    if not path.exists():
        logger.info("store_maps.json not found; aisle fallback stays unseeded")
        return 0
    try:
        with open(path) as fh:
            maps = json.load(fh)
    except (OSError, ValueError):
        logger.exception("store_maps.json could not be read")
        return 0
    cur.execute(
        "INSERT INTO nutrition.prep_config (key, value) VALUES ('store_zones', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (json.dumps(maps),))
    return len(maps)


def get_stay(cur, stay_id: int):
    return prep_store.get_stay(fetcher(cur), stay_id)


def shopping_list(cur, stay_id: int) -> dict:
    return prep_store.shopping_list(fetcher(cur), stay_id)


def pantry_rows(cur) -> list:
    return prep_store.pantry_rows(fetcher(cur))


def macro_check(cur, stay_id: int) -> dict:
    return prep_store.macro_check(fetcher(cur), stay_id)


def set_on_hand(cur, ingredient_id: str, *, packages=None, base=None,
                source: str = "app") -> dict:
    """Record a count. Either `packages` (converted) or `base` (stored as given).

    Converting on the way IN is what keeps the stored value stable when the
    preferred store changes later: the packages he counted were measured against
    a package size true at that moment, and it is the base quantity that survives.
    """
    if packages is None and base is None:
        raise ValueError("one of packages or base is required")
    fetch = fetcher(cur)
    if base is None:
        base = prep_store.base_from_packages(fetch, ingredient_id, packages)
    cur.execute(prep_store.SET_ON_HAND_SQL, (base, source, ingredient_id))
    if not cur.rowcount:
        raise LookupError(f"no ingredient {ingredient_id}")
    return {"ingredient_id": ingredient_id, "on_hand_base": float(base)}


# ── the whole box-side round ────────────────────────────────────────────────

def refresh(cur, today: date, *, force_sweep: bool = False) -> dict:
    """Sync Notion, propose the stay, resolve its menu. The box's daily job.

    ORDER MATTERS and it is not arbitrary: the sync must land before the menu is
    resolved, because a recipe added in Notion this morning has to exist in RDS
    before a stay day can reference it. And the stay must exist before the menu,
    because stay_day rows hang off it.

    TWO DIFFERENT FAILURE POLICIES, because the two failures differ in kind:

      the sync  is isolated per database (`prep_notion.sync_all`). A database
                that cannot be read keeps its old rows, its old watermark and its
                old sweep time, and is named under `synced["failed"]`. Nothing is
                silently dropped — an unsynced `store items` leaves every list
                line flagged no_store, which is loud.
      the menu  RAISES (`resolve_menu`). Its DELETE has already cleared the old
                menu, so a partial result would be a SHORTER menu — fewer meals,
                less food, and nothing saying a day was skipped.
    """
    from artemis import prep_notion

    out: dict = {"synced": prep_notion.sync_all(cur, force_sweep=force_sweep)}
    out["store_zones"] = sync_store_zones(cur)
    stay = next_stay(cur, today)
    if stay is None:
        out["stay"] = None
        out["detail"] = "no Minneapolis stay found in the lookahead window"
        return out
    start, end = stay
    # The shop happens on the first day of the stay, or TODAY if the stay is
    # already running — a default shop date in the past would build a list for
    # days already eaten. upsert_stay keeps a shop_date Ryan has already set.
    stay_id = upsert_stay(cur, start, end, shop_date=max(start, today))
    out["stay"] = {"id": stay_id, "start": start.isoformat(), "end": end.isoformat()}
    out["menu"] = resolve_menu(cur, stay_id, start, end)
    return out
