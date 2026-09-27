"""Notion meal-plan reader (DIET-1).

Notion is the source of truth for the meal plan. This module READS it and
never writes. It resolves one `meal planning` row (the undated default day)
into its four slots, and each slot into `recipes` rows carrying per-portion
macros.

The one rule that governs every path here: **a macro that is not in Notion is
not produced.** A missing token, an unreachable API, a missing default-day row
and a recipe with no calories all resolve to "no plan", never to a guess.

Property names are the live ones, read from the databases on 2026-09-21:
  meal planning : name (title), date, breakfast / lunch / dinner / snacks
                  (relations -> recipes)
  recipes       : recipe (title), course, status, calories, protein, carbs,
                  fats, fiber, servings, source
  ingredients   : ingredient (title), serving, calories, protein, carbs, fats,
                  fiber, source — macros are per `serving`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
_TIMEOUT = 15

# The two databases, by id. These are stable Notion ids, not secrets.
MEAL_PLANNING_DB = "634b4123-5502-41d4-8ae8-b627d5f8b175"
RECIPES_DB = "89624308-605a-4eb2-af4c-58fb78ea3657"
INGREDIENTS_DB = "4bdd6a3a-c4e7-4cb6-9e5c-2d101e93ed11"

# The undated row the 00:15 pre-fill reads on msp_work days.
DEFAULT_DAY_NAME = "default day — work day"
# NUTRITION-2: the undated row for travel days — prepped, drive-friendly food
# (bowls, wraps, smoothies). Created by Ryan; until it exists a travel day
# records `no_plan` and pre-fills nothing.
TRAVEL_DAY_NAME = "default day — travel day"

SLOTS = ("breakfast", "lunch", "dinner", "snacks")


class NotionUnavailable(Exception):
    """Notion could not be reached, or is not configured.

    Callers treat this as `prefill_outcome = 'unavailable'` — nothing
    pre-filled, the day says so.
    """


@dataclass
class PlannedFood:
    """One recipe row, one portion as eaten."""
    name: str
    page_id: str
    course: str | None = None
    kcal: int | None = None
    protein_g: float | None = None
    carb_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    source_detail: str | None = None
    # what the macros are per: None for a recipe (one portion as eaten),
    # the Notion `serving` text for an ingredient
    portion: str | None = None

    @property
    def has_macros(self) -> bool:
        """Calories and protein are the floor. A row missing either is not
        usable as a planned entry — it would put an invented zero in the day."""
        return self.kcal is not None and self.protein_g is not None

    @property
    def is_placeholder(self) -> bool:
        """True when the row's own `source` says its macros are not from a
        label: "placeholder" or "estimate" (Ryan, 2026-09-21 — the chicken
        thigh bowl is computed partly from USDA generics)."""
        src = (self.source_detail or "").lower()
        return "placeholder" in src or "estimate" in src


@dataclass
class DefaultDay:
    page_id: str
    name: str
    slots: dict[str, list[PlannedFood]] = field(default_factory=dict)
    # recipe rows that were linked but unusable (no calories / no protein)
    skipped: list[str] = field(default_factory=list)

    def all_foods(self) -> list[tuple[str, PlannedFood]]:
        return [(slot, f) for slot in SLOTS for f in self.slots.get(slot, [])]


# ── low-level ───────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _token() -> str:
    from knowledge.secrets import get_notion_token
    token = get_notion_token()
    if not token:
        raise NotionUnavailable(
            "no Notion token configured (Secrets Manager rdmis/dev/notion-token)")
    return token


def _post(path: str, token: str, payload: dict) -> dict:
    import requests
    try:
        r = requests.post(f"{NOTION_API}{path}", headers=_headers(token),
                          json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise NotionUnavailable(f"Notion request failed: {exc}") from exc
    if r.status_code != 200:
        raise NotionUnavailable(
            f"Notion {path} returned {r.status_code}: {r.text[:200]}")
    return r.json()


def _get(path: str, token: str) -> dict:
    import requests
    try:
        r = requests.get(f"{NOTION_API}{path}", headers=_headers(token),
                         timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise NotionUnavailable(f"Notion request failed: {exc}") from exc
    if r.status_code != 200:
        raise NotionUnavailable(
            f"Notion {path} returned {r.status_code}: {r.text[:200]}")
    return r.json()


# ── property extraction ─────────────────────────────────────────────────────

def _plain_title(prop: dict | None) -> str:
    if not prop:
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("title") or []).strip()


def _plain_text(prop: dict | None) -> str | None:
    if not prop:
        return None
    parts = "".join(t.get("plain_text", "") for t in prop.get("rich_text") or [])
    return parts.strip() or None


def _number(prop: dict | None) -> float | None:
    if not prop:
        return None
    return prop.get("number")


def _select_name(prop: dict | None) -> str | None:
    if not prop:
        return None
    sel = prop.get("select") or prop.get("status")
    return (sel or {}).get("name")


def _relation_ids(prop: dict | None) -> list[str]:
    if not prop:
        return []
    return [r["id"] for r in prop.get("relation") or [] if r.get("id")]


def _as_int(v: float | None) -> int | None:
    return None if v is None else int(round(v))


def _recipe_from_page(page: dict) -> PlannedFood:
    props = page.get("properties") or {}
    return PlannedFood(
        name=_plain_title(props.get("recipe")) or "(unnamed recipe)",
        page_id=page.get("id", ""),
        course=_select_name(props.get("course")),
        kcal=_as_int(_number(props.get("calories"))),
        protein_g=_number(props.get("protein")),
        carb_g=_number(props.get("carbs")),
        fat_g=_number(props.get("fats")),
        fiber_g=_number(props.get("fiber")),
        source_detail=_plain_text(props.get("source")),
    )


def _ingredient_from_page(page: dict) -> PlannedFood:
    props = page.get("properties") or {}
    return PlannedFood(
        name=_plain_title(props.get("ingredient")) or "(unnamed ingredient)",
        page_id=page.get("id", ""),
        kcal=_as_int(_number(props.get("calories"))),
        protein_g=_number(props.get("protein")),
        carb_g=_number(props.get("carbs")),
        fat_g=_number(props.get("fats")),
        fiber_g=_number(props.get("fiber")),
        source_detail=_plain_text(props.get("source")),
        portion=_plain_text(props.get("serving")),
    )


# ── public API ──────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True when a Notion token exists. Does NOT prove the databases are
    shared with the integration — only a real read proves that."""
    from knowledge.secrets import get_notion_token
    return bool(get_notion_token())


def fetch_default_day(name: str = DEFAULT_DAY_NAME) -> DefaultDay:
    """The undated default-day row, with every slot resolved to recipe rows.

    Raises NotionUnavailable when Notion is not configured or not reachable.
    Returns a DefaultDay whose slots may be empty when the row exists but
    links nothing usable — the caller distinguishes those cases.
    """
    token = _token()

    result = _post(f"/databases/{MEAL_PLANNING_DB}/query", token, {
        "filter": {"property": "name", "title": {"equals": name}},
        "page_size": 2,
    })
    rows = result.get("results") or []
    if not rows:
        raise LookupError(f"no meal-planning row named {name!r}")
    if len(rows) > 1:
        # Ambiguity is a data problem, not something to resolve by guessing.
        raise LookupError(f"{len(rows)} meal-planning rows named {name!r}")
    return _resolve_day(rows[0], name, token)


def fetch_dated_day(d) -> DefaultDay | None:
    """NUTRITION-2: the `meal planning` row whose `date` is `d` — the menu Ryan
    picked for that day in Notion. None when there is no such row.

    Raises NotionUnavailable like every read here, and LookupError when two
    rows share the date (ambiguity is a data problem, never resolved by
    picking one).
    """
    token = _token()
    result = _post(f"/databases/{MEAL_PLANNING_DB}/query", token, {
        "filter": {"property": "date", "date": {"equals": d.isoformat()}},
        "page_size": 2,
    })
    rows = result.get("results") or []
    if not rows:
        return None
    if len(rows) > 1:
        raise LookupError(f"{len(rows)} meal-planning rows dated {d.isoformat()}")
    row = rows[0]
    name = _plain_title((row.get("properties") or {}).get("name")) or d.isoformat()
    return _resolve_day(row, name, token)


def _resolve_day(row: dict, name: str, token: str) -> DefaultDay:
    """One `meal planning` row -> its slots resolved to recipe rows."""
    props = row.get("properties") or {}
    day = DefaultDay(page_id=row.get("id", ""), name=name)

    # Resolve each slot's relations. Recipe pages are fetched individually:
    # the relation gives ids only, and a slot holds at most a handful.
    cache: dict[str, PlannedFood] = {}
    for slot in SLOTS:
        foods: list[PlannedFood] = []
        for page_id in _relation_ids(props.get(slot)):
            food = cache.get(page_id)
            if food is None:
                food = _recipe_from_page(_get(f"/pages/{page_id}", token))
                cache[page_id] = food
            if food.has_macros:
                foods.append(food)
            else:
                day.skipped.append(food.name)
                logger.warning(
                    "notion recipe %r has no calories/protein — skipped, not guessed",
                    food.name)
        day.slots[slot] = foods
    return day


def fetch_recipes() -> tuple[list[PlannedFood], list[str]]:
    """Every ACTIVE, un-archived `recipes` row that carries calories AND
    protein, plus the names of active rows skipped for lacking them.

    Why: the saved-food lookup only knew recipes the default day links, so a
    backup meal ("dinner: patty bowl") fell through to USDA. Paginated.
    Raises NotionUnavailable when Notion is not configured or not reachable.
    """
    token = _token()
    foods: list[PlannedFood] = []
    skipped: list[str] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100, "filter": {"and": [
            {"property": "status", "status": {"equals": "active"}},
            {"property": "archive", "checkbox": {"equals": False}},
        ]}}
        if cursor:
            payload["start_cursor"] = cursor
        result = _post(f"/databases/{RECIPES_DB}/query", token, payload)
        for page in result.get("results") or []:
            food = _recipe_from_page(page)
            if food.has_macros:
                foods.append(food)
            else:
                skipped.append(food.name)
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return foods, skipped


def fetch_ingredients() -> tuple[list[PlannedFood], list[str]]:
    """Every `ingredients` row that carries calories AND protein, plus the
    names of rows that were skipped for lacking them.

    The database also holds non-food rows (cleaning, hygiene) and foods with
    no macros yet; those are skipped, never zero-filled. Paginated — the
    database is a shopping list as much as a food table and will grow.

    Raises NotionUnavailable when Notion is not configured or not reachable.
    """
    token = _token()
    foods: list[PlannedFood] = []
    skipped: list[str] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        result = _post(f"/databases/{INGREDIENTS_DB}/query", token, payload)
        for page in result.get("results") or []:
            food = _ingredient_from_page(page)
            if food.has_macros:
                foods.append(food)
            elif food.kcal is not None or food.protein_g is not None:
                # half-filled macros are worth naming; empty rows are just
                # shopping-list items and are not noise-logged
                skipped.append(food.name)
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return foods, skipped
