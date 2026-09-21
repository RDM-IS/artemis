"""DIET-1 — plan-first nutrition logging (work-day scope).

Recording is default-to-plan; logging is by exception.

  00:15 local   pre-fill today from the Notion default day, on msp_work days.
                Silent — no post, no nudge. Entries are `assumed`.
  morning       the check-in reply appends one line about YESTERDAY.
  `fix`         opens a correction for the PREVIOUS day, while it is open.
  +48h          a still-`assumed` day locks as `locked_unconfirmed`.

Two rules run through all of it:

  * **Macros are never invented.** An unmatched food gets exactly one question
    and is stored only once it resolves, or explicitly as `estimated`. There
    is no silent fallback number anywhere in this module.
  * **"Today" is the ACTIVE timezone's today.** Every date here comes from
    quiet_hours.local_today() / local_tz(); no bare current_date, no literal
    'America/Chicago'. The 48h window is computed against local midnight, so
    it follows a `set timezone to <place>` override.

OUT OF SCOPE tonight (stays in the backlog): non-work days, the barcode page,
photo estimates, the Apple Shortcut, the dietitian report.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

logger = logging.getLogger(__name__)

SLOTS = ("breakfast", "lunch", "dinner", "snacks")

# Corrections are accepted within 48 hours OF THE DAY'S END (local midnight).
CORRECTION_WINDOW_HOURS = 48

DAY_ASSUMED = "assumed"
DAY_CORRECTED = "corrected"
DAY_LOCKED = "locked_unconfirmed"


def _as_dict(cur, row) -> dict:
    """A row as a dict, whatever the cursor. knowledge.db.get_connection()
    hands out a PLAIN cursor (tuple rows, as health_checkin._rows assumes);
    execute_query uses a dict cursor. Handle both."""
    if isinstance(row, dict):
        return row
    return dict(zip([c[0] for c in cur.description], row))


# ============================================================================
# Time — the 48h window, anchored to local midnight in the ACTIVE timezone
# ============================================================================

def _local_midnight(d: date) -> datetime:
    """00:00 local on `d`, tz-aware in the active timezone."""
    from artemis.quiet_hours import local_tz
    return datetime.combine(d, time(0, 0), tzinfo=local_tz())


def day_end(d: date) -> datetime:
    """The instant day `d` ends: local midnight starting d+1."""
    return _local_midnight(d + timedelta(days=1))


def correction_deadline(d: date) -> datetime:
    """When day `d` locks — 48h after its end."""
    return day_end(d) + timedelta(hours=CORRECTION_WINDOW_HOURS)


def is_open(d: date, now: datetime | None = None) -> bool:
    """Is day `d` still correctable?"""
    from artemis.quiet_hours import local_now
    now = now or local_now()
    return now < correction_deadline(d)


# ============================================================================
# Day rows
# ============================================================================

def get_day(cur, d: date) -> dict | None:
    cur.execute(
        "SELECT day_date, status, day_type, prefilled, prefill_outcome, "
        "prefill_note, plan_source_id, locked_at "
        "FROM nutrition.day WHERE day_date = %s", (d,))
    row = cur.fetchone()
    if row is None:
        return None
    return _as_dict(cur, row)


def _upsert_day(cur, d: date, *, status: str, day_type: str | None,
                prefilled: bool, outcome: str | None,
                note: str | None, plan_source_id: str | None) -> None:
    cur.execute(
        """INSERT INTO nutrition.day
             (day_date, status, day_type, prefilled, prefill_outcome,
              prefill_note, plan_source_id, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (day_date) DO UPDATE SET
             status          = EXCLUDED.status,
             day_type        = EXCLUDED.day_type,
             prefilled       = EXCLUDED.prefilled,
             prefill_outcome = EXCLUDED.prefill_outcome,
             prefill_note    = EXCLUDED.prefill_note,
             plan_source_id  = EXCLUDED.plan_source_id,
             updated_at      = now()""",
        (d, status, day_type, prefilled, outcome, note, plan_source_id))


def day_totals(cur, d: date) -> dict:
    cur.execute(
        "SELECT kcal, protein_g, carb_g, fat_g, fiber_g, n_entries "
        "FROM nutrition.day_totals(%s)", (d,))
    row = cur.fetchone()
    if row is None:
        return {"kcal": 0, "protein_g": 0, "carb_g": 0,
                "fat_g": 0, "fiber_g": 0, "n_entries": 0}
    return _as_dict(cur, row)


# ============================================================================
# 00:15 pre-fill
# ============================================================================

@dataclass
class PrefillResult:
    outcome: str            # planned | not_work_day | unavailable | no_plan | already
    entries_written: int = 0
    note: str | None = None

    @property
    def wrote(self) -> bool:
        return self.entries_written > 0


def prefill_day(cur, d: date, *, now: datetime | None = None) -> PrefillResult:
    """Pre-fill `d` from the Notion default day. Silent — the caller posts
    nothing. Idempotent: a day that already has entries is left alone.

    Work-day scope: only `msp_work` days are pre-filled. Every other day type
    records `not_work_day` and stays empty, because no non-work meal set
    exists yet — inventing one is exactly what this module must not do.
    """
    from artemis import cycle

    existing = get_day(cur, d)
    if existing and existing.get("prefilled"):
        return PrefillResult("already", 0, "day already pre-filled")

    day_type = cycle.day_type(d)
    if day_type != "msp_work":
        _upsert_day(cur, d, status=DAY_ASSUMED, day_type=day_type,
                    prefilled=False, outcome="not_work_day",
                    note=f"{day_type} — no non-work meal set exists yet",
                    plan_source_id=None)
        return PrefillResult("not_work_day", 0, f"{d} is {day_type}")

    from artemis import notion_meal_plan as nmp

    try:
        plan = nmp.fetch_default_day()
    except nmp.NotionUnavailable as exc:
        # The specced degradation: nothing pre-filled, the day says so.
        _upsert_day(cur, d, status=DAY_ASSUMED, day_type=day_type,
                    prefilled=False, outcome="unavailable",
                    note=str(exc)[:500], plan_source_id=None)
        logger.warning("nutrition pre-fill %s: Notion unavailable — %s", d, exc)
        return PrefillResult("unavailable", 0, str(exc))
    except LookupError as exc:
        _upsert_day(cur, d, status=DAY_ASSUMED, day_type=day_type,
                    prefilled=False, outcome="no_plan",
                    note=str(exc)[:500], plan_source_id=None)
        logger.warning("nutrition pre-fill %s: no default day — %s", d, exc)
        return PrefillResult("no_plan", 0, str(exc))

    foods = plan.all_foods()
    if not foods:
        _upsert_day(cur, d, status=DAY_ASSUMED, day_type=day_type,
                    prefilled=False, outcome="no_plan",
                    note="default day links no usable recipes",
                    plan_source_id=plan.page_id)
        return PrefillResult("no_plan", 0, "default day links no usable recipes")

    _upsert_day(cur, d, status=DAY_ASSUMED, day_type=day_type,
                prefilled=True, outcome="planned",
                note=("skipped (no macros): " + ", ".join(plan.skipped))
                     if plan.skipped else None,
                plan_source_id=plan.page_id)

    # Replace rather than append: pre-fill owns an untouched day outright.
    cur.execute("DELETE FROM nutrition.entry WHERE day_date = %s", (d,))

    written = 0
    for slot, food in foods:
        upsert_food(cur, food)
        cur.execute(
            """INSERT INTO nutrition.entry
                 (day_date, slot, food_id, description, quantity,
                  kcal, protein_g, carb_g, fat_g, fiber_g,
                  source, source_id, confidence, status)
               VALUES (%s, %s,
                       (SELECT id FROM nutrition.food
                         WHERE kind = 'recipe' AND source_id = %s),
                       %s, 1, %s, %s, %s, %s, %s,
                       'notion', %s, 'exact', 'assumed')""",
            (d, slot, food.page_id, food.name, food.kcal, food.protein_g,
             food.carb_g, food.fat_g, food.fiber_g, food.page_id))
        written += 1

    _audit(cur, "nutrition_prefill", "planned",
           {"day": d.isoformat(), "entries": written,
            "plan_page": plan.page_id, "skipped": plan.skipped})
    return PrefillResult("planned", written)


# ============================================================================
# Saved foods
# ============================================================================

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def upsert_food(cur, food, kind: str = "recipe") -> None:
    """Mirror a Notion recipe or ingredient into nutrition.food.

    This is a CACHE of the first lookup tier, not a second source of truth:
    every row keeps source='notion' and the Notion page id, and the 00:15 job
    refreshes it. `kind` keeps "Protein bar — peanut" (recipe) and
    "protein bar (peanut)" (ingredient) apart even though they share a slug.
    """
    cur.execute(
        """INSERT INTO nutrition.food
             (kind, name, slug, kcal, protein_g, carb_g, fat_g, fiber_g, portion,
              source, source_id, source_detail, is_placeholder, active, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                   'notion', %s, %s, %s, TRUE, now())
           ON CONFLICT (kind, slug) DO UPDATE SET
             name           = EXCLUDED.name,
             portion        = EXCLUDED.portion,
             active         = TRUE,
             kcal           = EXCLUDED.kcal,
             protein_g      = EXCLUDED.protein_g,
             carb_g         = EXCLUDED.carb_g,
             fat_g          = EXCLUDED.fat_g,
             fiber_g        = EXCLUDED.fiber_g,
             source         = EXCLUDED.source,
             source_id      = EXCLUDED.source_id,
             source_detail  = EXCLUDED.source_detail,
             is_placeholder = EXCLUDED.is_placeholder,
             updated_at     = now()""",
        (kind, food.name, slugify(food.name), food.kcal, food.protein_g,
         food.carb_g, food.fat_g, food.fiber_g, getattr(food, "portion", None),
         food.page_id, food.source_detail, food.is_placeholder))


def sync_ingredients(cur) -> dict:
    """Mirror every `ingredients` row that has macros into nutrition.food as
    kind='ingredient' — the SECOND saved-food source, looked up after recipes.

    Macros are per the row's `serving`, stored as the portion. A row that has
    vanished from Notion (or lost its macros) is marked inactive rather than
    left matching. Raises NotionUnavailable; the caller decides what that
    means (the 00:15 job logs it and carries on with the pre-fill).
    """
    from artemis import notion_meal_plan as nmp

    foods, skipped = nmp.fetch_ingredients()
    seen: set[str] = set()
    for food in foods:
        slug = slugify(food.name)
        if not slug or slug in seen:
            # two ingredient rows with the same name: keep the first, name it
            skipped.append(f"{food.name} (duplicate name)")
            continue
        seen.add(slug)
        upsert_food(cur, food, kind="ingredient")

    cur.execute(
        "UPDATE nutrition.food SET active = FALSE, updated_at = now() "
        "WHERE kind = 'ingredient' AND active AND NOT (slug = ANY(%s))",
        (list(seen),))
    deactivated = cur.rowcount if isinstance(cur.rowcount, int) else 0

    _audit(cur, "nutrition_ingredient_sync", "ok",
           {"synced": len(seen), "deactivated": deactivated, "skipped": skipped})
    return {"synced": len(seen), "deactivated": deactivated, "skipped": skipped}


def sync_recipes(cur) -> dict:
    """Mirror every active `recipes` row with macros into nutrition.food as
    kind='recipe' — the FIRST saved-food source. Before this, a recipe only
    reached the mirror when the default day linked it, so a backup meal the
    plan doesn't name ("dinner: patty bowl") was never found.

    A recipe that is no longer active in Notion is marked inactive, not
    deleted. Raises NotionUnavailable; the caller decides what that means.
    """
    from artemis import notion_meal_plan as nmp

    foods, skipped = nmp.fetch_recipes()
    seen: set[str] = set()
    for food in foods:
        slug = slugify(food.name)
        if not slug or slug in seen:
            skipped.append(f"{food.name} (duplicate name)")
            continue
        seen.add(slug)
        upsert_food(cur, food, kind="recipe")

    cur.execute(
        "UPDATE nutrition.food SET active = FALSE, updated_at = now() "
        "WHERE kind = 'recipe' AND active AND NOT (slug = ANY(%s))",
        (list(seen),))
    deactivated = cur.rowcount if isinstance(cur.rowcount, int) else 0

    _audit(cur, "nutrition_recipe_sync", "ok",
           {"synced": len(seen), "deactivated": deactivated, "skipped": skipped})
    return {"synced": len(seen), "deactivated": deactivated, "skipped": skipped}


SAVED_FOOD_KINDS = ("recipe", "ingredient")   # lookup order

_FOOD_COLS = ("id, kind, name, kcal, protein_g, carb_g, fat_g, fiber_g, portion, "
              "source, source_id, is_placeholder")


def find_saved_food(cur, text: str) -> dict | None:
    """Tier 1 of the source order: recipes, then ingredients.

    Within each kind: exact slug first, then a unique prefix. An ambiguous
    prefix in a kind stops the search for that kind rather than picking one.
    """
    slug = slugify(text)
    if not slug:
        return None
    for kind in SAVED_FOOD_KINDS:
        cur.execute(
            f"SELECT {_FOOD_COLS} FROM nutrition.food "
            "WHERE kind = %s AND slug = %s AND active", (kind, slug))
        row = cur.fetchone()
        if row:
            return _as_dict(cur, row)
        cur.execute(
            f"SELECT {_FOOD_COLS} FROM nutrition.food "
            "WHERE kind = %s AND slug LIKE %s AND active LIMIT 2",
            (kind, slug + "%"))
        rows = cur.fetchall()
        if len(rows) == 1:
            return _as_dict(cur, rows[0])
    return None


# ============================================================================
# The morning line
# ============================================================================

def morning_line(cur, today: date, *, now: datetime | None = None) -> str | None:
    """The one line appended to the morning check-in reply, about YESTERDAY.

    Omitted when yesterday had corrections, or had no plan — per spec. It is
    part of the check-in reply, never a separate post, and it must not affect
    check-in intent routing.
    """
    yesterday = today - timedelta(days=1)
    day = get_day(cur, yesterday)
    if day is None:
        return None
    if day.get("prefill_outcome") != "planned" or not day.get("prefilled"):
        return None                      # no plan yesterday
    if day.get("status") != DAY_ASSUMED:
        return None                      # already corrected, or locked
    if not is_open(yesterday, now):
        return None                      # window shut; nothing to offer
    return "Yesterday logged as planned — reply `fix` to correct."


# ============================================================================
# `fix` — open a correction for the previous day
# ============================================================================

_PENDING_KEY = "nutrition_fix_pending:{channel}"


def _pending_key(channel_id: str) -> str:
    return _PENDING_KEY.format(channel=channel_id)


def open_correction(cur, channel_id: str, today: date,
                    *, now: datetime | None = None) -> str:
    """Handle a bare `fix`. Targets the PREVIOUS day, never today."""
    from artemis.quiet_hours import local_now
    now = now or local_now()
    target = today - timedelta(days=1)

    day = get_day(cur, target)
    if day is None or not day.get("prefilled"):
        return (f"Nothing logged for {target:%a %b %-d} — there's no plan to correct.")

    if not is_open(target, now):
        # Lock it now if the sweep hasn't run yet, so the reply and the stored
        # state agree.
        lock_day(cur, target, now=now)
        return (f"{target:%a %b %-d} locked {CORRECTION_WINDOW_HOURS}h after it "
                f"ended — corrections are closed. It stays recorded as "
                f"`locked_unconfirmed`.")

    _set_system_value(cur, _pending_key(channel_id),
                      json.dumps({"day": target.isoformat(),
                                  "opened_at": now.isoformat()}))
    totals = day_totals(cur, target)
    hours_left = int((correction_deadline(target) - now).total_seconds() // 3600)
    return (
        f"Correcting {target:%a %b %-d} — logged as planned: "
        f"{totals['kcal']} kcal, {float(totals['protein_g']):g} g protein.\n"
        f"Send the deviations, one per line — e.g. `lunch: chipotle chicken bowl`, "
        f"`skipped breakfast`, `+2 beers`. Reply `done` when finished.\n"
        f"About {hours_left}h left on this day.")


def pending_correction(cur, channel_id: str) -> date | None:
    raw = _get_system_value(cur, _pending_key(channel_id))
    if not raw:
        return None
    try:
        return date.fromisoformat(json.loads(raw)["day"])
    except (ValueError, KeyError, TypeError):
        return None


def clear_correction(cur, channel_id: str) -> None:
    _set_system_value(cur, _pending_key(channel_id), "")


# ============================================================================
# Deviations
# ============================================================================

_SKIP_RE = re.compile(
    r"^\s*(?:skip(?:ped)?|no|missed)\s+(breakfast|lunch|dinner|snacks?)\s*$", re.I)
_SLOT_RE = re.compile(
    r"^\s*(breakfast|lunch|dinner|snacks?)\s*[:\-]\s*(.+?)\s*$", re.I)
_ADD_RE = re.compile(r"^\s*\+\s*(.+?)\s*$")
_QTY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:x\s*)?(.+?)\s*$", re.I)


def _norm_slot(s: str) -> str:
    s = s.lower()
    return "snacks" if s.startswith("snack") else s


@dataclass
class Deviation:
    kind: str                 # skip | replace | add
    slot: str | None
    text: str = ""
    quantity: float = 1.0


def parse_deviation(line: str) -> Deviation | None:
    """One deviation line -> intent. Returns None when the line isn't one."""
    if not (line or "").strip():
        return None
    m = _SKIP_RE.match(line)
    if m:
        return Deviation("skip", _norm_slot(m.group(1)))
    m = _SLOT_RE.match(line)
    if m:
        return Deviation("replace", _norm_slot(m.group(1)), m.group(2).strip())
    m = _ADD_RE.match(line)
    if m:
        body = m.group(1)
        qty, text = 1.0, body
        q = _QTY_RE.match(body)
        if q:
            qty, text = float(q.group(1)), q.group(2).strip()
        return Deviation("add", None, text, qty)
    return None


def apply_deviation(cur, d: date, dev: Deviation) -> str:
    """Apply one deviation to day `d`. Never writes invented macros: an
    unmatched food returns a question and changes nothing."""
    if dev.kind == "skip":
        cur.execute("DELETE FROM nutrition.entry WHERE day_date = %s AND slot = %s",
                    (d, dev.slot))
        _mark_corrected(cur, d)
        return f"{dev.slot}: skipped."

    resolved = resolve_food(cur, dev.text)
    if resolved is None:
        return _ask_about(dev.text)

    slot = dev.slot or "snacks"
    if dev.kind == "replace":
        cur.execute("DELETE FROM nutrition.entry WHERE day_date = %s AND slot = %s",
                    (d, slot))

    _insert_entry(cur, d, slot, resolved, dev.quantity)
    _mark_corrected(cur, d)
    tier = resolved["confidence"]
    mark = " *(estimated)*" if tier == "estimated" else ""
    per = ""
    if resolved.get("kind") == "ingredient" and resolved.get("portion"):
        per = (f" ({dev.quantity:g} × {resolved['portion']})" if dev.quantity != 1
               else f" ({resolved['portion']})")
    kcal = int(round(float(resolved["kcal"]) * dev.quantity))
    protein = float(resolved["protein_g"]) * dev.quantity
    return (f"{slot}: {resolved['name']}{per} — {kcal} kcal, "
            f"{protein:g} g protein{mark}.")


def _ask_about(text: str) -> str:
    """The ONE question an unmatched food gets before anything is stored."""
    return (f"I don't have macros for “{text}”. How big was the portion, or "
            f"which brand? (I won't guess the numbers.)")


def resolve_food(cur, text: str) -> dict | None:
    """Source order: saved foods -> USDA -> Open Food Facts. Returns None when
    nothing matches — the caller asks rather than inventing macros."""
    saved = find_saved_food(cur, text)
    if saved:
        return {**saved, "confidence": "exact", "source": "saved",
                "source_id": saved.get("source_id")}

    hit = lookup_usda(text)
    if hit:
        return {**hit, "confidence": "matched"}

    hit = lookup_off(text)
    if hit:
        return {**hit, "confidence": "matched"}

    return None


def lookup_usda(text: str) -> dict | None:
    """Tier 2. Returns None (not a guess) when no key is configured."""
    from knowledge.secrets import get_usda_api_key
    key = get_usda_api_key()
    if not key:
        logger.info("USDA lookup skipped — no api key configured")
        return None
    import requests
    try:
        r = requests.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"query": text, "pageSize": 1, "api_key": key}, timeout=10)
        if r.status_code != 200:
            return None
        foods = (r.json() or {}).get("foods") or []
    except Exception:
        logger.debug("USDA lookup failed", exc_info=True)
        return None
    if not foods:
        return None
    f = foods[0]
    by_id = {n.get("nutrientNumber"): n.get("value") for n in f.get("foodNutrients") or []}
    kcal = by_id.get("208")
    protein = by_id.get("203")
    if kcal is None or protein is None:
        return None
    return {"name": f.get("description") or text,
            "kcal": int(round(kcal)), "protein_g": protein,
            "carb_g": by_id.get("205"), "fat_g": by_id.get("204"),
            "fiber_g": by_id.get("291"),
            "source": "usda", "source_id": str(f.get("fdcId") or "")}


def lookup_off(text: str) -> dict | None:
    """Tier 3. Open Food Facts needs no key."""
    import requests
    try:
        r = requests.get(
            "https://world.openfoodfacts.org/cgi/search.pl",
            params={"search_terms": text, "search_simple": 1, "action": "process",
                    "json": 1, "page_size": 1},
            headers={"User-Agent": "artemis-acos/1.0 (personal nutrition log)"},
            timeout=10)
        if r.status_code != 200:
            return None
        products = (r.json() or {}).get("products") or []
    except Exception:
        logger.debug("Open Food Facts lookup failed", exc_info=True)
        return None
    if not products:
        return None
    p = products[0]
    n = p.get("nutriments") or {}
    kcal = n.get("energy-kcal_serving") or n.get("energy-kcal_100g")
    protein = n.get("proteins_serving") or n.get("proteins_100g")
    if kcal is None or protein is None:
        return None
    return {"name": p.get("product_name") or text,
            "kcal": int(round(float(kcal))), "protein_g": float(protein),
            "carb_g": n.get("carbohydrates_serving") or n.get("carbohydrates_100g"),
            "fat_g": n.get("fat_serving") or n.get("fat_100g"),
            "fiber_g": n.get("fiber_serving") or n.get("fiber_100g"),
            "source": "off", "source_id": str(p.get("code") or "")}


def _insert_entry(cur, d: date, slot: str, food: dict, qty: float) -> None:
    def scaled(v):
        return None if v is None else float(v) * qty
    cur.execute(
        """INSERT INTO nutrition.entry
             (day_date, slot, food_id, description, quantity,
              kcal, protein_g, carb_g, fat_g, fiber_g,
              source, source_id, confidence, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'corrected')""",
        (d, slot, food.get("id"), food["name"], qty,
         int(round(scaled(food["kcal"]))), scaled(food["protein_g"]),
         scaled(food.get("carb_g")), scaled(food.get("fat_g")),
         scaled(food.get("fiber_g")),
         food["source"], food.get("source_id"), food["confidence"]))


def _mark_corrected(cur, d: date) -> None:
    cur.execute(
        "UPDATE nutrition.day SET status = %s, updated_at = now() "
        "WHERE day_date = %s AND status <> %s",
        (DAY_CORRECTED, d, DAY_LOCKED))


# ============================================================================
# The 48h lock
# ============================================================================

def lock_day(cur, d: date, *, now: datetime | None = None) -> bool:
    """A still-`assumed` day becomes `locked_unconfirmed`. A `corrected` day
    stays `corrected`."""
    from artemis.quiet_hours import local_now
    now = now or local_now()
    cur.execute(
        "UPDATE nutrition.day SET status = %s, locked_at = %s, updated_at = now() "
        "WHERE day_date = %s AND status = %s",
        (DAY_LOCKED, now, d, DAY_ASSUMED))
    return cur.rowcount > 0


def lock_expired_days(cur, *, now: datetime | None = None) -> list[date]:
    """Sweep: lock every still-assumed day whose window has closed. Silent."""
    from artemis.quiet_hours import local_now
    now = now or local_now()
    cur.execute(
        "SELECT day_date FROM nutrition.day WHERE status = %s ORDER BY day_date",
        (DAY_ASSUMED,))
    locked = []
    for row in cur.fetchall():
        d = row[0] if not isinstance(row, dict) else row["day_date"]
        if not is_open(d, now):
            if lock_day(cur, d, now=now):
                locked.append(d)
    if locked:
        _audit(cur, "nutrition_lock", "locked_unconfirmed",
               {"days": [d.isoformat() for d in locked]})
    return locked


# ============================================================================
# small helpers
# ============================================================================

def _get_system_value(cur, key: str) -> str | None:
    cur.execute("SELECT value FROM acos.system_state WHERE key = %s", (key,))
    row = cur.fetchone()
    if not row:
        return None
    return row[0] if not isinstance(row, dict) else row.get("value")


def _set_system_value(cur, key: str, value: str) -> None:
    cur.execute(
        "INSERT INTO acos.system_state (key, value, updated_at) "
        "VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value))


def _audit(cur, action: str, outcome: str, metadata: dict) -> None:
    cur.execute(
        "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
        "outcome, token_count, api_cost_usd, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        ("nutrition", None, action, "health", None, outcome, 0, 0.0,
         json.dumps(metadata, default=str)))
