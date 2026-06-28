"""Life ops — grocery list, store maps, staple generator, health plan context.

The legacy SQLite interactive workout-logging flow was removed — superseded by
the RDS path in artemis/health.py (sessions, set-logging, debrief/capture,
history Q&A, ad-hoc rest day) plus the gym-display frontend. Grocery and staples
live on Postgres (acos.grocery_list / health.meal) via knowledge.db; no SQLite
remains in this module.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from artemis import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Grocery auto-categorization
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    "Produce & Refrigerated": [
        "salad", "onion", "banana", "lemon", "fruit", "vegetable",
        "produce", "pepper", "avocado", "tomato fresh",
    ],
    "Protein & Refrigerated": [
        "chicken", "yogurt", "marinated", "dairy", "egg", "meat",
        "thigh", "turkey",
    ],
    "Frozen": [
        "frozen", "broccoli", "green bean",
    ],
    "Pantry": [
        "oats", "chia", "beans", "lentils", "broth", "tomatoes",
        "spices", "rice", "pasta", "pantry", "almond milk", "coffee",
        "protein powder", "supplement", "olive oil", "pineapple",
        "coconut water", "cold brew", "chili powder", "cumin",
        "paprika", "cayenne", "salt", "garlic",
    ],
}


def _categorize_item(item: str) -> str:
    item_lower = item.lower()
    for category, keywords in CATEGORY_MAP.items():
        for kw in keywords:
            if kw in item_lower:
                return category
    return "Other"


# ---------------------------------------------------------------------------
# Grocery list
# ---------------------------------------------------------------------------

# Backend: acos.grocery_list (Postgres) via knowledge.db. The auto-categorization
# (CATEGORY_MAP / _categorize_item) and the store_maps aisle ordering are
# unchanged — only the storage layer moved off SQLite. Connections come from the
# shared pool; these no longer take a sqlite handle.

def add_grocery_item(item: str, store: str = "", quantity: str = "") -> dict:
    from knowledge.db import execute_write
    category = _categorize_item(item)
    execute_write(
        "INSERT INTO acos.grocery_list (item, category, quantity, store) "
        "VALUES (%s, %s, %s, %s)",
        (item, category, quantity, store),
    )
    return {"item": item, "category": category}


def get_grocery_list() -> list[dict]:
    from knowledge.db import execute_query
    return [
        dict(r)
        for r in execute_query(
            "SELECT * FROM acos.grocery_list WHERE is_purchased = false "
            "ORDER BY category, item"
        )
    ]


def mark_purchased(item_text: str) -> bool:
    """Mark matching unpurchased items bought. ILIKE preserves the SQLite LIKE
    (case-insensitive) substring match. Returns True if any row changed."""
    from knowledge.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE acos.grocery_list SET is_purchased = true, purchased_at = now() "
                "WHERE item ILIKE %s AND is_purchased = false",
                (f"%{item_text}%",),
            )
            return cur.rowcount > 0


def clear_grocery_list() -> int:
    """Mark every unpurchased item bought (end-of-trip). Returns the count."""
    from knowledge.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE acos.grocery_list SET is_purchased = true, purchased_at = now() "
                "WHERE is_purchased = false"
            )
            return cur.rowcount


def format_grocery_list(items: list[dict]) -> str:
    if not items:
        return "\U0001f6d2 Grocery list is empty."
    by_category: dict[str, list[dict]] = {}
    for item in items:
        by_category.setdefault(item.get("category", "Other"), []).append(item)
    lines = [f"\U0001f6d2 **Grocery list ({len(items)} items):**\n"]
    for cat in ["Produce & Refrigerated", "Protein & Refrigerated", "Frozen", "Pantry", "Other"]:
        if cat not in by_category:
            continue
        lines.append(f"**{cat}**")
        for item in by_category[cat]:
            qty = f" x {item['quantity']}" if item.get("quantity") else ""
            lines.append(f"\u25a1 {item['item']}{qty}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Store-optimized list
# ---------------------------------------------------------------------------

def load_store_map(store_name: str) -> dict | None:
    map_path = Path("store_maps.json")
    if not map_path.exists():
        logger.warning("store_maps.json not found")
        return None
    try:
        with open(map_path) as f:
            maps = json.load(f)
        return maps.get(store_name.lower())
    except Exception:
        logger.exception("Failed to load store map")
        return None


def get_weekly_staples() -> list[str]:
    raw = config.WEEKLY_STAPLES if hasattr(config, "WEEKLY_STAPLES") else ""
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def build_store_list(store_name: str) -> str:
    store_map = load_store_map(store_name)
    if not store_map:
        return f"\U0001f6d2 No store map found for '{store_name}'. Configure it in store_maps.json."
    grocery_items = get_grocery_list()
    existing_lower = {item["item"].lower() for item in grocery_items}
    staples = get_weekly_staples()
    all_items = [item["item"] for item in grocery_items]
    for staple in staples:
        if staple.lower() not in existing_lower:
            all_items.append(staple)
    if not all_items:
        return f"\U0001f6d2 Nothing on the list for {store_map['display_name']}."

    zones = store_map.get("zones", [])
    zone_items: dict[int, tuple[str, list[str]]] = {}
    unmatched = []
    for item in all_items:
        matched = False
        for zone in zones:
            for kw in zone["keywords"]:
                if kw in item.lower():
                    order = zone["order"]
                    if order not in zone_items:
                        zone_items[order] = (zone["name"], [])
                    zone_items[order][1].append(item)
                    matched = True
                    break
            if matched:
                break
        if not matched:
            unmatched.append(item)

    zone_emojis = {1: "\U0001f96c", 2: "\U0001f96b", 3: "\U0001f9ca", 4: "\U0001f9ca"}
    lines = [f"\U0001f6d2 **{store_map['display_name']} list \u2014 {len(all_items)} items, sorted by aisle:**\n"]
    for order in sorted(zone_items.keys()):
        name, items = zone_items[order]
        emoji = zone_emojis.get(order, "\U0001f4e6")
        lines.append(f"{emoji} **{name}**")
        for item in items:
            lines.append(f"\u25a1 {item}")
        lines.append("")
    if unmatched:
        lines.append("\U0001f4e6 **Other**")
        for item in unmatched:
            lines.append(f"\u25a1 {item}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Grocery command handler
# ---------------------------------------------------------------------------

def handle_grocery_command(question: str) -> str | None:
    q = question.lower().strip()
    for store in ["aldi"]:
        if store in q and any(kw in q for kw in ["going to", "heading to", "shopping at", "list"]):
            return build_store_list(store)
    if any(kw in q for kw in ["grocery list", "shopping list", "what do i need"]):
        return format_grocery_list(get_grocery_list())
    m = re.match(r"(?:add|put|need)\s+(.+?)(?:\s+(?:to|on)\s+(?:(?:the\s+)?grocery\s+list|(?:the\s+)?list|(\w+)\s+list))?$", q, re.IGNORECASE)
    if m:
        item = m.group(1).strip()
        store = m.group(2) or ""
        item = re.sub(r"\s+(to|on)\s+(the\s+)?(grocery\s+)?list$", "", item, flags=re.IGNORECASE)
        if item:
            result = add_grocery_item(item, store=store)
            return f"\U0001f6d2 **{item}** added to grocery list ({result['category']})"
    m = re.match(r"(?:remove|got|crossed off|cross off)\s+(.+)", q, re.IGNORECASE)
    if m:
        item = m.group(1).strip()
        if mark_purchased(item):
            return f"\U0001f6d2 **{item}** marked purchased."
        return f"\U0001f6d2 Couldn't find '{item}' on the list."
    if any(kw in q for kw in ["done shopping", "clear grocery list", "finished shopping"]):
        count = clear_grocery_list()
        return f"\U0001f6d2 Shopping complete \u2014 {count} items marked purchased."
    m = re.match(r"(?:i\s+)?don'?t\s+need\s+(.+?)(?:\s+this\s+week)?$", q, re.IGNORECASE)
    if m:
        return f"\U0001f6d2 **{m.group(1).strip()}** skipped for this trip."
    return None


# ---------------------------------------------------------------------------
# Grocery staple generator (cross-schema orchestrator)
#
# This is the ONE sanctioned cross-schema write in the nutrition subsystem: it
# READS health.meal (under the current open nutrition target) and WRITES
# acos.grocery_list. It lives here in life_ops — NOT in artemis/health.py — so
# the health handlers keep their "health schema only" invariant; this is an
# orchestrator that reaches across, not a health handler reaching out.
#
# generate-and-prune: emit the FULL week's staples (ingredients × times_per_week)
# and let Ryan check off what he already has. No pantry / inventory tracking.
# Confirmed (batch) write — propose via the durable system_state KV, write on
# an explicit `confirm`.
# ---------------------------------------------------------------------------

_STAPLE_CATEGORY_ORDER = [
    "Produce & Refrigerated", "Protein & Refrigerated", "Frozen", "Pantry", "Other",
]


def _coerce_qty(raw) -> float:
    """Best-effort numeric quantity. Handles ints/floats, "2", "1.5", and "1/2".
    Missing/unparseable → 1.0 so the ingredient still counts once per occurrence."""
    if raw is None or raw == "":
        return 1.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", s)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if den:
            return num / den
    return 1.0


def _fmt_qty(qty: float) -> str:
    """Render an aggregated quantity: whole numbers without a decimal tail."""
    if abs(qty - round(qty)) < 1e-9:
        return str(int(round(qty)))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


def _open_nutrition_target_id() -> int | None:
    """id of the current open nutrition target (effective_to IS NULL), or None."""
    from knowledge.db import execute_one
    row = execute_one(
        "SELECT id FROM health.nutrition_target WHERE effective_to IS NULL "
        "ORDER BY effective_from DESC, id DESC LIMIT 1"
    )
    return row["id"] if row else None


def aggregate_staples() -> list[dict]:
    """Aggregate active-meal ingredients × times_per_week under the open target.

    Returns a list of {"item", "unit", "qty"} in stable first-seen order. Empty
    if there is no open target or no active meals carry ingredients.
    """
    from knowledge.db import execute_query
    target_id = _open_nutrition_target_id()
    if target_id is None:
        return []
    meals = execute_query(
        "SELECT name, times_per_week, ingredients FROM health.meal "
        "WHERE active = true AND target_id = %s",
        (target_id,),
    )
    totals: dict[tuple[str, str], float] = {}
    display: dict[tuple[str, str], tuple[str, str]] = {}
    for m in meals:
        tpw = m.get("times_per_week") or 0
        ingredients = m.get("ingredients") or []
        if isinstance(ingredients, str):
            try:
                ingredients = json.loads(ingredients)
            except (ValueError, TypeError):
                ingredients = []
        for ing in ingredients:
            if not isinstance(ing, dict):
                continue
            item = (ing.get("item") or "").strip()
            if not item:
                continue
            unit = (ing.get("unit") or "").strip()
            qty = _coerce_qty(ing.get("qty"))
            key = (item.lower(), unit.lower())
            totals[key] = totals.get(key, 0.0) + qty * tpw
            display.setdefault(key, (item, unit))
    return [
        {"item": display[k][0], "unit": display[k][1], "qty": totals[k]}
        for k in totals
    ]


def format_staples_proposal(staples: list[dict]) -> str:
    """Render the proposed weekly staples grouped by aisle category. No write."""
    if not staples:
        return (
            "\U0001f6d2 No active meals under an open nutrition target — "
            "set a nutrition target with meals first."
        )
    by_cat: dict[str, list[dict]] = {}
    for s in staples:
        by_cat.setdefault(_categorize_item(s["item"]), []).append(s)
    lines = ["\U0001f6d2 **Weekly grocery staples — review and confirm:**\n"]
    for cat in _STAPLE_CATEGORY_ORDER:
        if cat not in by_cat:
            continue
        lines.append(f"**{cat}**")
        for s in by_cat[cat]:
            unit = f" {s['unit']}" if s["unit"] else ""
            lines.append(f"□ {s['item']} — {_fmt_qty(s['qty'])}{unit}")
        lines.append("")
    n = len(staples)
    lines.append(
        f"{n} staple{'s' if n != 1 else ''} for the full week. "
        "Reply `confirm` to add to the grocery list, `cancel` to discard."
    )
    return "\n".join(lines)


# ── Durable pending payload (system_state KV) ───────────────────────────────

def _staples_pending_key(channel_id: str) -> str:
    return f"grocery_staples_pending:{channel_id}"


def store_staples_pending(channel_id: str, staples: list[dict]) -> None:
    from artemis.quiet_hours import set_system_value
    payload = {
        "staples": staples,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    set_system_value(_staples_pending_key(channel_id), json.dumps(payload))


def load_staples_pending(channel_id: str, max_age_sec: int = 1800) -> dict | None:
    from artemis.quiet_hours import get_system_value
    raw = get_system_value(_staples_pending_key(channel_id))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    created = payload.get("created_at")
    if created:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds()
            if age > max_age_sec:
                return None
        except (ValueError, TypeError):
            pass
    return payload


def clear_staples_pending(channel_id: str) -> None:
    from artemis.quiet_hours import set_system_value
    set_system_value(_staples_pending_key(channel_id), "")


# ── Orchestration: propose / commit / cancel ────────────────────────────────

def _upsert_grocery_staple(item: str, quantity: str) -> None:
    """Upsert a staple into acos.grocery_list. If an unpurchased row with the
    same item exists, refresh its quantity; otherwise insert. Uses the existing
    auto-categorization. No pantry tracking — generate-and-prune."""
    from knowledge.db import get_connection
    category = _categorize_item(item)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE acos.grocery_list SET quantity = %s, category = %s "
                "WHERE LOWER(item) = LOWER(%s) AND is_purchased = false "
                "RETURNING id",
                (quantity, category, item),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO acos.grocery_list (item, category, quantity) "
                    "VALUES (%s, %s, %s)",
                    (item, category, quantity),
                )


def build_grocery_staples(channel_id: str) -> str:
    """`@artemis build grocery list` — propose the full week's staples (no write).

    Aggregates active meals under the open target and stores the proposal in the
    durable KV; the actual upsert happens on a later `confirm`.
    """
    staples = aggregate_staples()
    if not staples:
        return format_staples_proposal(staples)
    store_staples_pending(channel_id, staples)
    return format_staples_proposal(staples)


def commit_grocery_staples(channel_id: str) -> str:
    """Write the pending staples into acos.grocery_list (upsert). Confirm leg."""
    payload = load_staples_pending(channel_id)
    if payload is None:
        return "Nothing pending (it may have expired). Re-run `build grocery list`."
    staples = payload.get("staples", [])
    written = 0
    for s in staples:
        unit = (s.get("unit") or "").strip()
        quantity = f"{_fmt_qty(s.get('qty', 1))}{(' ' + unit) if unit else ''}".strip()
        try:
            _upsert_grocery_staple(s["item"], quantity)
            written += 1
        except Exception:
            logger.exception("Failed to upsert staple %r", s.get("item"))
    clear_staples_pending(channel_id)
    return f"\U0001f6d2 Added {written} staple{'s' if written != 1 else ''} to the grocery list."


def cancel_grocery_staples(channel_id: str) -> str:
    clear_staples_pending(channel_id)
    return "Discarded — nothing added to the grocery list."


# ---------------------------------------------------------------------------
# Health plan context
# ---------------------------------------------------------------------------

_health_plan_content: str = ""


def load_health_plan() -> str:
    global _health_plan_content
    if _health_plan_content:
        return _health_plan_content
    plan_path = Path("health_plan.md")
    if plan_path.exists():
        _health_plan_content = plan_path.read_text()
    else:
        logger.warning("health_plan.md not found")
    return _health_plan_content


def handle_health_command(question: str) -> str | None:
    q = question.lower().strip()
    if any(kw in q for kw in ["sunday prep", "meal prep"]):
        return (
            "\U0001f957 **Sunday meal prep (~30 min active):**\n"
            "\u25a1 Make chili (~45 min, mostly passive)\n  - Portion into 7 containers, freeze 2\n"
            "\u25a1 Hard boil 10 eggs (~12 min)\n  - Grab-and-go snacks all week\n"
            "\u25a1 Freeze bananas (2 min)\n  - Peel, bag, freeze for smoothies"
        )
    if any(kw in q for kw in ["what's my goal", "whats my goal", "weight goal"]):
        return (
            "\U0001f3af **Goal:** 275 \u2192 225 lbs by September 3, 2026 "
            "(49th birthday). ~2.3 lbs/week, 1,900 cal/day, 205-225g protein."
        )
    if any(kw in q for kw in ["daily targets", "what should i eat"]):
        return (
            "\U0001f3af **Daily targets:**\n- Calories: 1,900 cal\n- Protein: 205-225g\n- Water: 100+ oz\n\n"
            "**Meals:**\n- Breakfast (~450 cal, ~45g protein): 3 eggs, 1/2 cup oats + chia, coffee\n"
            "- Lunch (~500 cal, ~55g protein): 6oz chicken, big salad, beans/lentils\n"
            "- Dinner (~550 cal, ~60g protein): 6oz protein, veggies, 1/2 cup rice\n"
            "- Snacks (~400 cal, ~50g protein): shake, yogurt, eggs, banana"
        )
    if any(kw in q for kw in ["calories", "protein today", "macros"]):
        return "What have you had today? I'll estimate your macros against your 1,900 cal / 215g protein target."
    return None
