"""PREP-1 — Notion -> RDS sync for the procurement databases.

Notion is where Ryan EDITS recipes, ingredients, stores and store items. RDS is
the system of record everything else reads. This module moves rows one way,
Notion -> RDS, plus one narrow write back: the pantry count.

It reuses `notion_meal_plan`'s request primitives (`_post`, `_get`, `_token`, the
property extractors) rather than restating them. The 2026-09-28 finding that the
same three-regex fence strip existed in eleven modules is the reason: a second
copy of a Notion client is a second place for the token handling, the timeout and
the NotionUnavailable contract to drift.

HOW A ROW IS IDENTIFIED: by Notion page id, always. Names change and slugs
collide ("Protein bar — peanut" and "protein bar (peanut)" slugify the same); the
page id does neither. Every upsert here is ON CONFLICT (notion_id).

WHAT IS NOT SYNCED: every formula and rollup column. Notion computes `buy`,
`needed to buy`, `buy pkgs`, `eff rank`, `preferred`, `short`, `pref pkg size`,
`usable`, `flags` and `servings to make` for Ryan's own views. Artemis recomputes
all of them in `knowledge/prep_math.py`. Copying them would give two answers to
every question with no way to tell which was stale — and Notion's would always be
the stale one, because it cannot see a pantry count entered on the phone.

WATERMARK vs SWEEP. An incremental run asks for rows edited since the watermark:
cheap, and enough to see every edit and addition. It CANNOT see a deletion, since
a deleted row is not returned by any query. Only a full sweep — every row, no
filter — can prove absence, so a sweep runs weekly and marks what it did not find
as deleted rather than removing it. A shopping list built before the sweep still
renders.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from artemis.notion_meal_plan import (  # noqa: F401  (NotionUnavailable re-exported)
    NotionUnavailable,
    _get,
    _headers,
    _number,
    _plain_text,
    _plain_title,
    _post,
    _relation_ids,
    _select_name,
    _token,
    INGREDIENTS_DB,
    RECIPES_DB,
)

logger = logging.getLogger(__name__)

# Database ids. Stable Notion ids, not secrets — same as the DIET-1 pair.
# `recipe lines`, `stores` and `store items` were discovered 2026-09-29 under
# "— ryan's brain / in the kitchen".
RECIPE_LINES_DB = "8bccb676-329a-4b32-8a50-fdd90b270b8b"
STORES_DB = "f88ff590-4769-47a7-9e6e-818a96941cd2"
STORE_ITEMS_DB = "0fe5c4ee-d733-42cd-85fc-09ec63299130"

#: Keys in nutrition.prep_sync.
DB_RECIPES = "recipes"
DB_RECIPE_LINES = "recipe_lines"
DB_INGREDIENTS = "ingredients"
DB_STORES = "stores"
DB_STORE_ITEMS = "store_items"

#: A full sweep at least this often — the only run that can see a deletion.
SWEEP_EVERY = timedelta(days=7)

#: Rows whose title starts with this are scratch rows Ryan uses while editing and
#: are never synced. Case-insensitive, checked on the TITLE only: an ingredient
#: legitimately called "contest" must not be swallowed by a prefix match on a
#: different column.
TEST_PREFIX = "test"

# ── rate limiting ───────────────────────────────────────────────────────────
#
# Notion's published limit is an average of 3 requests/second. This is a token
# bucket rather than a sleep-per-call because a sync is bursty: five databases,
# each paginated, plus a page fetch per relation. A flat sleep would make the
# common small run needlessly slow while still bursting past the limit on a big
# one.

class TokenBucket:
    """Average `rate` requests/second with a small burst allowance."""

    def __init__(self, rate: float = 3.0, burst: float = 3.0,
                 sleep=time.sleep, clock=time.monotonic):
        self.rate = rate
        self.burst = burst
        self._tokens = burst
        self._last = clock()
        self._sleep = sleep
        self._clock = clock

    def take(self, n: float = 1.0) -> float:
        """Block until `n` tokens are available. Returns seconds actually slept."""
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= n:
            self._tokens -= n
            return 0.0
        deficit = n - self._tokens
        wait = deficit / self.rate
        self._sleep(wait)
        self._last = self._clock()
        self._tokens = 0.0
        return wait


@dataclass
class SyncReport:
    """What one database's sync did. Counts, not prose — the caller renders."""
    db_key: str
    mode: str = "incremental"          # incremental | sweep
    seen: int = 0
    upserted: int = 0
    skipped_test: int = 0
    marked_deleted: int = 0
    requests: int = 0
    slept_sec: float = 0.0
    watermark: str | None = None
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"db": self.db_key, "mode": self.mode, "seen": self.seen,
                "upserted": self.upserted, "skipped_test": self.skipped_test,
                "marked_deleted": self.marked_deleted, "requests": self.requests,
                "slept_sec": round(self.slept_sec, 2), "watermark": self.watermark,
                "errors": self.errors[:10]}


# ── extraction helpers the DIET-1 reader did not need ───────────────────────

def _checkbox(prop: dict | None) -> bool:
    return bool((prop or {}).get("checkbox"))


def _first_relation(prop: dict | None) -> str | None:
    ids = _relation_ids(prop)
    return ids[0] if ids else None


def _as_int(v) -> int | None:
    return None if v is None else int(round(v))


def slugify(name: str) -> str:
    """The SAME key nutrition.food uses. Imported rather than restated would be
    better still, but nutrition imports notion_meal_plan and this imports
    nutrition's sibling, so it is duplicated once here and asserted equal in
    tests/test_prep_sync.py."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _is_test_row(title: str) -> bool:
    return (title or "").strip().lower().startswith(TEST_PREFIX)


def _edited(page: dict) -> str | None:
    return page.get("last_edited_time")


# ── paging ──────────────────────────────────────────────────────────────────

def query_pages(db_id: str, token: str, *, since: str | None,
                bucket: TokenBucket, report: SyncReport):
    """Yield every page of `db_id`, oldest edit first.

    `since` filters on Notion's own last_edited_time timestamp. Sorting by it
    ascending matters: the watermark is only advanced after a page is stored, so
    a run that dies half way leaves a watermark that re-reads the tail rather
    than skipping it. Descending order would skip everything it had not reached.
    """
    cursor = None
    while True:
        payload: dict = {"page_size": 100,
                         "sorts": [{"timestamp": "last_edited_time",
                                    "direction": "ascending"}]}
        if since:
            payload["filter"] = {"timestamp": "last_edited_time",
                                 "last_edited_time": {"on_or_after": since}}
        if cursor:
            payload["start_cursor"] = cursor
        report.slept_sec += bucket.take()
        report.requests += 1
        result = _post(f"/databases/{db_id}/query", token, payload)
        for page in result.get("results") or []:
            yield page
        if not result.get("has_more"):
            return
        cursor = result.get("next_cursor")


# ── row mappers: Notion page -> a dict of RDS columns ───────────────────────

def map_ingredient(page: dict) -> dict:
    p = page.get("properties") or {}
    name = _plain_title(p.get("ingredient"))
    return {
        "notion_id": page.get("id"),
        "name": name,
        "slug": slugify(name),
        "category": _select_name(p.get("category")),
        "unit": _select_name(p.get("unit")),
        "yield_factor": _number(p.get("yield factor")),
        "shelf_life_days": _as_int(_number(p.get("shelf life (days)"))),
        "par_level_pkgs": _number(p.get("par level (pkgs)")),
        "bulk": _checkbox(p.get("bulk")),
        "pantry": _checkbox(p.get("pantry")),
        "frequency": _select_name(p.get("frequency")),
        # seed-only: RDS owns on-hand after the first count. See migration 051.
        "notion_on_hand_pkgs": _number(p.get("on hand (pkgs)")),
        "notion_edited": _edited(page),
    }


def map_recipe(page: dict) -> dict:
    p = page.get("properties") or {}
    name = _plain_title(p.get("recipe"))
    return {
        "notion_id": page.get("id"),
        "name": name,
        "slug": slugify(name),
        "course": _select_name(p.get("course")),
        "status": _select_name(p.get("status")),
        "archived": _checkbox(p.get("archive")),
        "servings": _number(p.get("servings")),
        "prepped_on_hand": _number(p.get("prepped on hand")),
        "notion_edited": _edited(page),
    }


def map_recipe_line(page: dict) -> dict:
    p = page.get("properties") or {}
    return {
        "notion_id": page.get("id"),
        "label": _plain_title(p.get("line")),
        "recipe_notion_id": _first_relation(p.get("recipe")),
        "ingredient_notion_id": _first_relation(p.get("ingredient")),
        "qty_per_serving": _number(p.get("qty per serving")),
        "note": _plain_text(p.get("note")),
        "notion_edited": _edited(page),
    }


def map_store(page: dict) -> dict:
    p = page.get("properties") or {}
    return {
        "notion_id": page.get("id"),
        "name": _plain_title(p.get("store")),
        "chain": _select_name(p.get("chain")),
        "city": _plain_text(p.get("city")),
        "location": _select_name(p.get("location")),
        "active": _checkbox(p.get("active")),
        "notes": _plain_text(p.get("notes")),
        "notion_edited": _edited(page),
    }


def map_store_item(page: dict) -> dict:
    p = page.get("properties") or {}
    return {
        "notion_id": page.get("id"),
        "name": _plain_title(p.get("item")),
        "ingredient_notion_id": _first_relation(p.get("ingredient")),
        "store_notion_id": _first_relation(p.get("store")),
        "rank": _as_int(_number(p.get("rank"))),
        "aisle": _plain_text(p.get("aisle")),
        "package_size": _number(p.get("package size")),
        "package_label": _plain_text(p.get("package label")),
        "price": _number(p.get("price")),
        "notion_edited": _edited(page),
    }


#: title property per database, for the TEST-prefix check
_TITLE_PROP = {DB_INGREDIENTS: "ingredient", DB_RECIPES: "recipe",
               DB_RECIPE_LINES: "line", DB_STORES: "store",
               DB_STORE_ITEMS: "item"}

_SPEC = {
    DB_INGREDIENTS: (INGREDIENTS_DB, map_ingredient, "nutrition.prep_ingredient"),
    DB_RECIPES: (RECIPES_DB, map_recipe, "nutrition.prep_recipe"),
    DB_RECIPE_LINES: (RECIPE_LINES_DB, map_recipe_line, "nutrition.prep_recipe_line"),
    DB_STORES: (STORES_DB, map_store, "nutrition.prep_store"),
    DB_STORE_ITEMS: (STORE_ITEMS_DB, map_store_item, "nutrition.prep_store_item"),
}

#: Sync order. Parents before children: a recipe line references a recipe and an
#: ingredient, and a store item references an ingredient and a store, so the
#: foreign keys only resolve in this order. A child whose parent has not arrived
#: yet is held back and reported, never written with a dangling reference.
ORDER = (DB_INGREDIENTS, DB_STORES, DB_RECIPES, DB_STORE_ITEMS, DB_RECIPE_LINES)


# ── upserts ─────────────────────────────────────────────────────────────────
#
# One statement per table, written out once. The 2026-09-28 finding that the
# health-plan upsert tuple was hand-written at seven call sites — one of them
# passing 10 values into 11 placeholders and broken since migration 042 — is why
# these are module constants with the column list and the value list adjacent.
#
# EVERY statement clears deleted_at. A row that comes back from Notion is not
# deleted any more, and leaving the flag set would keep it off the list forever.

_UPSERT_INGREDIENT = """
INSERT INTO nutrition.prep_ingredient
  (notion_id, name, slug, category, unit, yield_factor, shelf_life_days,
   par_level_pkgs, bulk, pantry, frequency, notion_on_hand_pkgs,
   notion_edited, seen_at, deleted_at)
VALUES (%(notion_id)s, %(name)s, %(slug)s, %(category)s, %(unit)s,
        %(yield_factor)s, %(shelf_life_days)s, %(par_level_pkgs)s, %(bulk)s,
        %(pantry)s, %(frequency)s, %(notion_on_hand_pkgs)s,
        %(notion_edited)s, now(), NULL)
ON CONFLICT (notion_id) DO UPDATE SET
  name = EXCLUDED.name, slug = EXCLUDED.slug, category = EXCLUDED.category,
  unit = EXCLUDED.unit, yield_factor = EXCLUDED.yield_factor,
  shelf_life_days = EXCLUDED.shelf_life_days,
  par_level_pkgs = EXCLUDED.par_level_pkgs, bulk = EXCLUDED.bulk,
  pantry = EXCLUDED.pantry, frequency = EXCLUDED.frequency,
  notion_on_hand_pkgs = EXCLUDED.notion_on_hand_pkgs,
  notion_edited = EXCLUDED.notion_edited, seen_at = now(), deleted_at = NULL
"""
# NOT in the UPDATE list, on purpose: on_hand_base, on_hand_at, on_hand_source,
# pushed_pkgs, pushed_at, food_id. Those are app-owned (migration 051's header).

_UPSERT_RECIPE = """
INSERT INTO nutrition.prep_recipe
  (notion_id, name, slug, course, status, archived, servings, prepped_on_hand,
   notion_edited, seen_at, deleted_at)
VALUES (%(notion_id)s, %(name)s, %(slug)s, %(course)s, %(status)s,
        %(archived)s, %(servings)s, %(prepped_on_hand)s,
        %(notion_edited)s, now(), NULL)
ON CONFLICT (notion_id) DO UPDATE SET
  name = EXCLUDED.name, slug = EXCLUDED.slug, course = EXCLUDED.course,
  status = EXCLUDED.status, archived = EXCLUDED.archived,
  servings = EXCLUDED.servings, prepped_on_hand = EXCLUDED.prepped_on_hand,
  notion_edited = EXCLUDED.notion_edited, seen_at = now(), deleted_at = NULL
"""
# plan_eligible and food_id are app-owned and absent above.

_UPSERT_RECIPE_LINE = """
INSERT INTO nutrition.prep_recipe_line
  (notion_id, label, recipe_notion_id, ingredient_notion_id, qty_per_serving,
   note, notion_edited, seen_at, deleted_at)
VALUES (%(notion_id)s, %(label)s, %(recipe_notion_id)s,
        %(ingredient_notion_id)s, %(qty_per_serving)s, %(note)s,
        %(notion_edited)s, now(), NULL)
ON CONFLICT (notion_id) DO UPDATE SET
  label = EXCLUDED.label, recipe_notion_id = EXCLUDED.recipe_notion_id,
  ingredient_notion_id = EXCLUDED.ingredient_notion_id,
  qty_per_serving = EXCLUDED.qty_per_serving, note = EXCLUDED.note,
  notion_edited = EXCLUDED.notion_edited, seen_at = now(), deleted_at = NULL
"""

_UPSERT_STORE = """
INSERT INTO nutrition.prep_store
  (notion_id, name, chain, city, location, active, notes,
   notion_edited, seen_at, deleted_at)
VALUES (%(notion_id)s, %(name)s, %(chain)s, %(city)s, %(location)s,
        %(active)s, %(notes)s, %(notion_edited)s, now(), NULL)
ON CONFLICT (notion_id) DO UPDATE SET
  name = EXCLUDED.name, chain = EXCLUDED.chain, city = EXCLUDED.city,
  location = EXCLUDED.location, active = EXCLUDED.active, notes = EXCLUDED.notes,
  notion_edited = EXCLUDED.notion_edited, seen_at = now(), deleted_at = NULL
"""

_UPSERT_STORE_ITEM = """
INSERT INTO nutrition.prep_store_item
  (notion_id, name, ingredient_notion_id, store_notion_id, rank, aisle,
   package_size, package_label, price, notion_edited, seen_at, deleted_at)
VALUES (%(notion_id)s, %(name)s, %(ingredient_notion_id)s, %(store_notion_id)s,
        %(rank)s, %(aisle)s, %(package_size)s, %(package_label)s, %(price)s,
        %(notion_edited)s, now(), NULL)
ON CONFLICT (notion_id) DO UPDATE SET
  name = EXCLUDED.name, ingredient_notion_id = EXCLUDED.ingredient_notion_id,
  store_notion_id = EXCLUDED.store_notion_id, rank = EXCLUDED.rank,
  aisle = EXCLUDED.aisle, package_size = EXCLUDED.package_size,
  package_label = EXCLUDED.package_label, price = EXCLUDED.price,
  notion_edited = EXCLUDED.notion_edited, seen_at = now(), deleted_at = NULL
"""
# rank_override is app-owned and absent above.

_UPSERTS = {
    DB_INGREDIENTS: _UPSERT_INGREDIENT,
    DB_RECIPES: _UPSERT_RECIPE,
    DB_RECIPE_LINES: _UPSERT_RECIPE_LINE,
    DB_STORES: _UPSERT_STORE,
    DB_STORE_ITEMS: _UPSERT_STORE_ITEM,
}

#: A child row's parent columns and the table each points at. A row whose parent
#: is not in RDS yet is held back rather than written, because the foreign key
#: would reject it and abort the whole transaction.
_PARENTS = {
    DB_RECIPE_LINES: (("recipe_notion_id", "nutrition.prep_recipe"),
                      ("ingredient_notion_id", "nutrition.prep_ingredient")),
    DB_STORE_ITEMS: (("ingredient_notion_id", "nutrition.prep_ingredient"),
                     ("store_notion_id", "nutrition.prep_store")),
}


def _known_ids(cur, table: str) -> set:
    cur.execute(f"SELECT notion_id FROM {table}")   # noqa: S608 - fixed literals
    return {r[0] for r in cur.fetchall()}


# ── watermarks ──────────────────────────────────────────────────────────────

def read_state(cur, db_key: str) -> dict:
    cur.execute(
        "SELECT db_key, last_edited, last_full_sweep, last_run, last_status "
        "FROM nutrition.prep_sync WHERE db_key = %s", (db_key,))
    row = cur.fetchone()
    if not row:
        return {"db_key": db_key, "last_edited": None, "last_full_sweep": None}
    return {"db_key": row[0], "last_edited": row[1], "last_full_sweep": row[2],
            "last_run": row[3], "last_status": row[4]}


def write_state(cur, db_key: str, *, last_edited=None, swept: bool,
                status: str, detail: str | None, rows_seen: int) -> None:
    """Advance the watermark. `last_edited` only ever moves FORWARD.

    A run that saw nothing new must not push the watermark to `now()`: the
    watermark is the newest edit STORED, not the time of the last attempt. Moving
    it to now() would skip any row edited between the query and the write.
    """
    cur.execute(
        """INSERT INTO nutrition.prep_sync
             (db_key, last_edited, last_full_sweep, last_run, last_status,
              last_detail, rows_seen)
           VALUES (%s, %s, CASE WHEN %s THEN now() ELSE NULL END, now(), %s, %s, %s)
           ON CONFLICT (db_key) DO UPDATE SET
             last_edited = GREATEST(
                 nutrition.prep_sync.last_edited,
                 COALESCE(EXCLUDED.last_edited, nutrition.prep_sync.last_edited)),
             last_full_sweep = CASE WHEN %s THEN now()
                                    ELSE nutrition.prep_sync.last_full_sweep END,
             last_run = now(), last_status = EXCLUDED.last_status,
             last_detail = EXCLUDED.last_detail, rows_seen = EXCLUDED.rows_seen""",
        (db_key, last_edited, swept, status, detail, rows_seen, swept))


def due_for_sweep(state: dict, now: datetime | None = None) -> bool:
    """True when a full sweep is due — including the FIRST ever run.

    A database that has never been swept has never had its deletions checked, so
    "never" is due, not "not yet".
    """
    last = state.get("last_full_sweep")
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= SWEEP_EVERY


# ── one database ────────────────────────────────────────────────────────────

def sync_one(cur, db_key: str, *, token: str, bucket: TokenBucket,
             force_sweep: bool = False, now: datetime | None = None) -> SyncReport:
    """Sync one Notion database into its RDS table. Returns a SyncReport.

    Raises NotionUnavailable on a failed read — it does NOT return a zero-row
    report. A sync that reported "0 rows, all good" after a failed read would
    then let the sweep mark every row deleted, which is the fail-open shape that
    cost eleven plan rows on 2026-09-26. Here the caller sees the exception and
    the sweep never runs.
    """
    db_id, mapper, table = _SPEC[db_key]
    state = read_state(cur, db_key)
    sweep = force_sweep or due_for_sweep(state, now)
    since = None if sweep else (
        state["last_edited"].isoformat() if state.get("last_edited") else None)
    report = SyncReport(db_key=db_key, mode="sweep" if sweep else "incremental")

    parents = _PARENTS.get(db_key, ())
    parent_ids = {col: _known_ids(cur, tbl) for col, tbl in parents}

    seen_ids: set = set()
    newest: str | None = None
    held: list = []
    for page in query_pages(db_id, token, since=since, bucket=bucket, report=report):
        report.seen += 1
        row = mapper(page)
        title_ok = not _is_test_row(row.get("name") or row.get("label") or "")
        # A sweep must count a TEST row as SEEN even though it is not stored, or
        # the deletion pass would mark its (nonexistent) RDS row and, worse, a
        # previously-synced row later renamed to TEST would be deleted twice over.
        seen_ids.add(row["notion_id"])
        if not title_ok:
            report.skipped_test += 1
            continue
        missing = [col for col, ids in parent_ids.items()
                   if row.get(col) and row[col] not in ids]
        if missing:
            held.append((row, missing))
            continue
        cur.execute(_UPSERTS[db_key], row)
        report.upserted += 1
        if row.get("notion_edited"):
            newest = row["notion_edited"] if newest is None else max(newest, row["notion_edited"])

    # Second pass: a parent may have arrived during THIS run (the ingredients
    # sync runs before store items, but Notion can hand back a child edited
    # earlier than its parent). Re-read the parent sets once and retry.
    if held:
        parent_ids = {col: _known_ids(cur, tbl) for col, tbl in parents}
        for row, _ in held:
            missing = [col for col, ids in parent_ids.items()
                       if row.get(col) and row[col] not in ids]
            if missing:
                report.errors.append(
                    f"{row.get('name') or row['notion_id']}: parent not synced ({', '.join(missing)})")
                continue
            cur.execute(_UPSERTS[db_key], row)
            report.upserted += 1
            if row.get("notion_edited"):
                newest = row["notion_edited"] if newest is None else max(newest, row["notion_edited"])

    if sweep:
        # The ONLY place a row is marked gone, and only from a run that
        # enumerated every row without a filter.
        cur.execute(
            f"UPDATE {table} SET deleted_at = now() "                 # noqa: S608
            "WHERE deleted_at IS NULL AND NOT (notion_id = ANY(%s))",
            (list(seen_ids),))
        report.marked_deleted = cur.rowcount if isinstance(cur.rowcount, int) else 0

    report.watermark = newest
    write_state(cur, db_key, last_edited=newest, swept=sweep, status="ok",
                detail=None, rows_seen=report.seen)
    return report


def link_food(cur) -> dict:
    """Join the procurement rows to their macro identity in nutrition.food.

    The join is the Notion page id (alignment decision 2: one recipe identity).
    A row with no matching food row keeps food_id NULL — see migration 051: the
    ingredients database holds cleaning and hygiene rows, and inventing food rows
    for them would put dish soap in the meal-deviation lookup.
    """
    out = {}
    for table, kind in (("nutrition.prep_ingredient", "ingredient"),
                        ("nutrition.prep_recipe", "recipe")):
        cur.execute(
            f"UPDATE {table} t SET food_id = f.id "                   # noqa: S608
            "FROM nutrition.food f "
            "WHERE f.source = 'notion' AND f.kind = %s AND f.source_id = t.notion_id "
            "  AND (t.food_id IS DISTINCT FROM f.id)",
            (kind,))
        out[kind] = cur.rowcount if isinstance(cur.rowcount, int) else 0
    return out


def seed_on_hand(cur) -> int:
    """Seed on_hand_base from Notion's `on hand (pkgs)` — ONCE, per ingredient.

    Runs after the store items sync because packages cannot be converted to base
    units until a package size exists. Only touches rows where on_hand_base IS
    NULL: after the first count RDS owns the number, and re-seeding it would undo
    every pantry count on the next sync.

    The package size used is the one from the LOWEST-ranked live store item, which
    is the item the pantry screen shows packages of.
    """
    cur.execute("""
        WITH pref AS (
            SELECT DISTINCT ON (si.ingredient_notion_id)
                   si.ingredient_notion_id AS ing, si.package_size
            FROM nutrition.prep_store_item si
            JOIN nutrition.prep_store s ON s.notion_id = si.store_notion_id
            WHERE si.deleted_at IS NULL AND s.deleted_at IS NULL AND s.active
              AND si.package_size IS NOT NULL AND si.package_size > 0
            ORDER BY si.ingredient_notion_id,
                     COALESCE(si.rank_override, si.rank, 999), si.price NULLS LAST
        )
        UPDATE nutrition.prep_ingredient i
           SET on_hand_base = i.notion_on_hand_pkgs * pref.package_size,
               on_hand_at = now(), on_hand_source = 'notion_seed'
          FROM pref
         WHERE pref.ing = i.notion_id
           AND i.on_hand_base IS NULL
           AND i.notion_on_hand_pkgs IS NOT NULL
    """)
    return cur.rowcount if isinstance(cur.rowcount, int) else 0


def sync_all(cur, *, force_sweep: bool = False, rate: float = 3.0,
             now: datetime | None = None) -> dict:
    """Every database, parents first. One database's failure does not stop the rest.

    WHY PER-DATABASE AND NOT ALL-OR-NOTHING. Each sync writes one table and each
    sweep reads back that same table, so a failed read of `store items` cannot
    make the `ingredients` sweep wrong — the isolation is real, not assumed. And
    the alternative was verified useless: on 2026-09-29 three of the five
    databases had never been shared with the integration, and an all-or-nothing
    sync_all meant NOTHING synced, including the two that were readable.

    What a failure must never do is (a) advance that database's watermark or
    (b) run its sweep. Either would be the fail-open shape: a sweep off the back
    of an outage marks every row deleted, and an advanced watermark skips every
    edit made during it. `sync_one` raises before reaching both, and the handler
    below records status='unavailable' WITHOUT touching last_edited or
    last_full_sweep.

    The caller decides what a partial sync means. It is visible rather than
    silent: an unsynced `store items` leaves every list line flagged no_store,
    which is loud.
    """
    token = _token()
    bucket = TokenBucket(rate=rate)
    reports, failures = [], {}
    for db_key in ORDER:
        try:
            reports.append(sync_one(cur, db_key, token=token, bucket=bucket,
                                    force_sweep=force_sweep, now=now))
        except NotionUnavailable as exc:
            detail = str(exc)[:400]
            failures[db_key] = detail
            logger.warning("prep sync: %s unavailable — %s", db_key, detail)
            # Status only. last_edited and last_full_sweep are deliberately NOT
            # passed, so neither moves: GREATEST() keeps the stored watermark and
            # the CASE keeps the stored sweep time.
            write_state(cur, db_key, last_edited=None, swept=False,
                        status="unavailable", detail=detail, rows_seen=0)
    linked = link_food(cur)
    seeded = seed_on_hand(cur)
    return {"databases": [r.as_dict() for r in reports],
            "failed": failures,
            "linked_food": linked, "seeded_on_hand": seeded,
            "requests": sum(r.requests for r in reports),
            "slept_sec": round(sum(r.slept_sec for r in reports), 2)}


# ── the one write back to Notion ────────────────────────────────────────────

def push_on_hand(cur, *, limit: int = 200, bucket: TokenBucket | None = None) -> dict:
    """Write RDS on-hand back to Notion's `on hand (pkgs)`, in PACKAGES.

    Only rows whose count has changed since the last push are sent, and the
    package size used is the preferred item's — the same one the pantry screen
    counted in, which is what makes the round trip lossless. An ingredient with
    no package size cannot be expressed in packages and is reported, not
    approximated into one.

    Notion is not the system of record here (alignment decision 2), so a failed
    push is not a failed count: RDS already holds the number. The report says
    what did not land.
    """
    bucket = bucket or TokenBucket()
    token = _token()
    cur.execute("""
        WITH pref AS (
            SELECT DISTINCT ON (si.ingredient_notion_id)
                   si.ingredient_notion_id AS ing, si.package_size
            FROM nutrition.prep_store_item si
            JOIN nutrition.prep_store s ON s.notion_id = si.store_notion_id
            WHERE si.deleted_at IS NULL AND s.deleted_at IS NULL AND s.active
              AND si.package_size IS NOT NULL AND si.package_size > 0
            ORDER BY si.ingredient_notion_id,
                     COALESCE(si.rank_override, si.rank, 999), si.price NULLS LAST
        )
        SELECT i.notion_id, i.name,
               ROUND(i.on_hand_base / pref.package_size, 2) AS pkgs
          FROM nutrition.prep_ingredient i
          JOIN pref ON pref.ing = i.notion_id
         WHERE i.deleted_at IS NULL
           AND i.on_hand_base IS NOT NULL
           AND i.on_hand_source <> 'notion_seed'
           AND (i.pushed_pkgs IS NULL OR i.pushed_pkgs
                IS DISTINCT FROM ROUND(i.on_hand_base / pref.package_size, 2))
         ORDER BY i.on_hand_at DESC NULLS LAST
         LIMIT %s
    """, (limit,))
    rows = cur.fetchall()

    import requests
    pushed, failed = [], []
    for notion_id, name, pkgs in rows:
        bucket.take()
        try:
            r = requests.patch(
                f"https://api.notion.com/v1/pages/{notion_id}",
                headers=_headers(token),
                json={"properties": {"on hand (pkgs)": {"number": float(pkgs)}}},
                timeout=15)
        except requests.RequestException as exc:
            failed.append(f"{name}: {exc}")
            continue
        if r.status_code != 200:
            failed.append(f"{name}: HTTP {r.status_code} {r.text[:120]}")
            continue
        cur.execute(
            "UPDATE nutrition.prep_ingredient "
            "SET pushed_pkgs = %s, pushed_at = now() WHERE notion_id = %s",
            (pkgs, notion_id))
        pushed.append(f"{name} = {pkgs}")
    return {"pushed": len(pushed), "failed": len(failed),
            "detail": pushed[:20], "errors": failed[:20]}
