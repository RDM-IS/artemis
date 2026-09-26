"""NUTRITION-2 — free-text meal logging, remaining-vs-target, suggestions.

    @artemis for breakfast I had an asiago bagel, 2 eggs, 1 slice of cheese,
    and turkey sandwich with a fruit bowl that had 3 oz of strawberry and
    6 ounces of green grapes

The rules that govern everything here:

* **Routing is deterministic.** A message is claimed only when it names a
  meal slot with an eating verb, or says "I had / I ate". No LLM classifier.
* **Macros are never estimated by a model.** Every item resolves through
  `nutrition.resolve_food` (saved foods -> USDA -> Open Food Facts) and is
  scaled by `nutrition.portion_factor`. An item that can't be resolved or
  scaled is listed back as a question; the rest are stored.
* **Artemis never sets a target** (040). With no open `nutrition.target` the
  reply gives totals only and says so.
* **Suggestions come only from the recipe DB** (`nutrition.food`, kind
  `recipe`): recent repeats excluded, fit to what's left, ranked by protein
  and fiber per calorie. No fit -> no suggestion, never an invented meal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from artemis import nutrition
from artemis.nutrition import Item, parse_item

logger = logging.getLogger(__name__)

SLOT_ORDER = ("breakfast", "lunch", "dinner", "snacks")
NEXT_SLOT = {"breakfast": "lunch", "lunch": "dinner", "dinner": "snacks", "snacks": "snacks"}
#: How far back a recipe counts as "just had" for suggestions (Ryan: no
#: robotic repeats on off days).
REPEAT_WINDOW_DAYS = 3
MAX_SUGGESTIONS = 2

_SLOT = r"(breakfast|lunch|dinner|snacks?|a\s+snack)"
_DAY = r"(?:\s+(today|yesterday|this\s+morning|tonight))?"
_EAT = r"(?:i\s+)?(?:had|ate|was|got|grabbed|made)"

# "for breakfast [today] I had X" / "breakfast: X" / "breakfast was X"
_SLOT_FIRST_RE = re.compile(
    rf"^\s*(?:for\s+)?{_SLOT}{_DAY}\s*(?:,\s*)?(?:{_EAT}\b|[:\-])\s*(?P<body>.+)$", re.I | re.S)
# "I had X for lunch [yesterday]" / "ate X for dinner"
_SLOT_LAST_RE = re.compile(
    rf"^\s*(?:i\s+)?(?:had|ate)\s+(?P<body>.+?)\s+for\s+{_SLOT}{_DAY}\s*[.!]*\s*$", re.I | re.S)
# "making X for dinner" / "I'm having X for lunch"
_MAKING_RE = re.compile(
    rf"^\s*(?:i'?m\s+|i\s+am\s+)?(?:making|having|cooking)\s+(?P<body>.+?)\s+for\s+{_SLOT}"
    rf"{_DAY}\s*[.!]*\s*$", re.I | re.S)
# "I had X" / "snacked on X" — no slot named: logged as snacks, and said so.
_BARE_RE = re.compile(r"^\s*(?:i\s+(?:had|ate)|snacked\s+on)\s+(?P<body>.+?)\s*[.!]*\s*$",
                      re.I | re.S)

# Not a meal log even though it matches a shape above.
_NOT_A_LOG_RE = re.compile(
    r"\b(?:meeting|call|appointment|interview|dream|idea|question|thought|chat|talk|"
    r"conversation|workout|session|run|lift)\b", re.I)

# A container whose contents are the real items: "a fruit bowl that had X".
_CONTAINER_RE = re.compile(
    r"^.*?\b(?:that\s+(?:had|has)|which\s+had|containing|consisting\s+of|made\s+(?:with|of))\s+",
    re.I)
_TOP_SPLIT_RE = re.compile(r"\s*[,;]\s*|\s*\n\s*")
_AND_SPLIT_RE = re.compile(r"\s+(?:and|&|plus)\s+", re.I)
_WITH_SPLIT_RE = re.compile(r"\s+(?:with|w/)\s+", re.I)


@dataclass
class MealLog:
    slot: str
    items: list[Item]
    day_offset: int = 0            # 0 today, -1 yesterday
    slot_named: bool = True
    making: bool = False


def _norm_slot(s: str) -> str:
    s = s.lower().strip()
    return "snacks" if "snack" in s else s


def _day_offset(tok: str | None) -> int:
    return -1 if (tok or "").lower() == "yesterday" else 0


def split_items(body: str, is_known=lambda _t: False) -> list[str]:
    """Split a meal description into item phrases.

    Commas and semicolons always split. `and` / `with` split only when the
    phrase isn't itself a known food ("mac and cheese", "chicken salad with
    goat cheese" saved as a recipe stay whole). A container ("a fruit bowl
    that had …") is replaced by what it held.
    """
    out: list[str] = []
    for top in _TOP_SPLIT_RE.split(body or ""):
        top = re.sub(r"^\s*(?:and|&|plus)\s+", "", top.strip(), flags=re.I)
        if not top:
            continue
        for part in ([top] if is_known(top) else _AND_SPLIT_RE.split(top)):
            part = part.strip()
            if not part:
                continue
            pieces = [part] if is_known(part) else _WITH_SPLIT_RE.split(part)
            for piece in pieces:
                piece = _CONTAINER_RE.sub("", piece).strip()
                piece = re.sub(r"^(?:and|&|plus)\s+", "", piece, flags=re.I).strip(" .!")
                if piece:
                    out.append(piece)
    return out


def parse_meal_log(text: str, is_known=lambda _t: False) -> MealLog | None:
    """The deterministic gate AND the parse. None = not a meal log."""
    t = (text or "").strip()
    if not t or len(t) > 1000:
        return None
    making = False
    m = _SLOT_FIRST_RE.match(t)
    if m:
        slot, day, body, named = m.group(1), m.group(2), m.group("body"), True
    else:
        m = _SLOT_LAST_RE.match(t) or _MAKING_RE.match(t)
        if m:
            making = m.re is _MAKING_RE
            slot, day, body, named = m.group(2), m.group(3), m.group("body"), True
        else:
            m = _BARE_RE.match(t)
            if not m:
                return None
            slot, day, body, named = "snacks", None, m.group("body"), False
    if _NOT_A_LOG_RE.search(body):
        return None
    items = [it for it in (parse_item(p) for p in split_items(body, is_known)) if it]
    if not items:
        return None
    return MealLog(slot=_norm_slot(slot), items=items, day_offset=_day_offset(day),
                   slot_named=named, making=making)


# ============================================================================
# Resolution + storage
# ============================================================================

@dataclass
class Logged:
    item: Item
    food: dict
    factor: float
    kcal: int
    protein_g: float
    fiber_g: float | None


@dataclass
class Outcome:
    day: date
    slot: str
    logged: list[Logged] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    refused: str | None = None


def _singular(name: str) -> str | None:
    n = name.strip()
    for suf, rep in (("ies", "y"), ("oes", "o"), ("es", ""), ("s", "")):
        if n.lower().endswith(suf) and len(n) > len(suf) + 2:
            cand = n[: -len(suf)] + rep
            if suf == "es" and not re.search(r"(?:ch|sh|s|x|z)es$", n, re.I):
                continue
            return cand
    return None


def resolve_item(cur, item: Item) -> dict | None:
    food = nutrition.resolve_food(cur, item.name)
    if food is None:
        s = _singular(item.name)
        if s:
            food = nutrition.resolve_food(cur, s)
    return food


def _ensure_day(cur, d: date) -> None:
    """A logged day exists before its entries (FK). Never overwrites a day."""
    from artemis import cycle
    cur.execute(
        "INSERT INTO nutrition.day (day_date, status, day_type, prefilled) "
        "VALUES (%s, %s, %s, FALSE) ON CONFLICT (day_date) DO NOTHING",
        (d, nutrition.DAY_ASSUMED, cycle.day_type(d)))


def log_meal(cur, ml: MealLog, today: date) -> Outcome:
    d = today + timedelta(days=ml.day_offset)
    out = Outcome(day=d, slot=ml.slot)
    if not nutrition.is_open(d):
        out.refused = (f"{d:%a %-m/%d} is past the {nutrition.CORRECTION_WINDOW_HOURS}h "
                       "window and is locked — nothing logged.")
        return out
    for item in ml.items:
        food = resolve_item(cur, item)
        if food is None:
            out.questions.append(nutrition._ask_about(item.raw))
            continue
        factor = nutrition.portion_factor(item.qty, item.unit, food)
        if factor is None:
            out.questions.append(nutrition.unit_question(item, food))
            continue
        out.logged.append(Logged(
            item=item, food=food, factor=factor,
            kcal=int(round(float(food["kcal"]) * factor)),
            protein_g=float(food["protein_g"]) * factor,
            fiber_g=(float(food["fiber_g"]) * factor) if food.get("fiber_g") is not None else None,
        ))
    if out.logged:
        _ensure_day(cur, d)
        for lg in out.logged:
            nutrition._insert_entry(cur, d, ml.slot, lg.food, lg.factor)
        nutrition._mark_corrected(cur, d)
        nutrition._audit(cur, "nutrition_meal_log", "logged", {
            "day": d.isoformat(), "slot": ml.slot,
            "items": [lg.item.raw for lg in out.logged],
            "unresolved": len(out.questions)})
    return out


# ============================================================================
# Target, remaining, suggestions
# ============================================================================

def open_target(cur, d: date) -> dict | None:
    cur.execute(
        "SELECT kcal, protein_g, carb_g, fat_g, fiber_g, provisional "
        "FROM nutrition.target WHERE effective_from <= %s "
        "AND (effective_to IS NULL OR effective_to >= %s) "
        "ORDER BY effective_from DESC LIMIT 1", (d, d))
    row = cur.fetchone()
    return nutrition._as_dict(cur, row) if row else None


def remaining(totals: dict, target: dict | None) -> dict | None:
    if not target:
        return None
    out = {}
    for k in ("kcal", "protein_g", "fiber_g"):
        if target.get(k) is not None:
            out[k] = float(target[k]) - float(totals.get(k) or 0)
    return out


def suggest(cur, d: date, left: dict | None, *, limit: int = MAX_SUGGESTIONS) -> list[dict]:
    """Recipes from the DB only. Excludes anything eaten in the last
    REPEAT_WINDOW_DAYS days; with a target, only what fits the calories left."""
    cur.execute(
        "SELECT id, name, kcal, protein_g, fiber_g, is_placeholder FROM nutrition.food "
        "WHERE kind = 'recipe' AND active AND kcal > 0 AND id NOT IN ("
        "  SELECT food_id FROM nutrition.entry WHERE food_id IS NOT NULL "
        "  AND day_date BETWEEN %s AND %s)",
        (d - timedelta(days=REPEAT_WINDOW_DAYS), d))
    rows = [nutrition._as_dict(cur, r) for r in cur.fetchall()]
    kcal_left = (left or {}).get("kcal")
    if kcal_left is not None:
        if kcal_left <= 0:
            return []
        rows = [r for r in rows if float(r["kcal"]) <= kcal_left * 1.05]

    def density(r):
        k = float(r["kcal"])
        return (float(r["protein_g"] or 0) + 2 * float(r["fiber_g"] or 0)) / k
    rows.sort(key=lambda r: (-density(r), r["name"]))
    return rows[:limit]


# ============================================================================
# Reply
# ============================================================================

def _fmt_n(v: float | None, unit: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}{unit}"


def _line_for(lg: Logged) -> str:
    it, f = lg.item, lg.food
    amount = f"{it.qty:g} {it.unit} " if it.unit else (f"{it.qty:g} × " if it.qty != 1 else "")
    src = ""
    if f.get("source") in ("usda", "off"):
        src = f" · _{'USDA' if f['source'] == 'usda' else 'Open Food Facts'}: {f['name']}_"
    elif f.get("is_placeholder"):
        src = " · _estimate_"
    fib = f", {lg.fiber_g:.0f} g fiber" if lg.fiber_g is not None else ""
    return f"· {amount}{it.name} — {lg.kcal} kcal, {lg.protein_g:.0f} g protein{fib}{src}"


def build_reply(cur, ml: MealLog, out: Outcome, today: date) -> str:
    if out.refused:
        return out.refused
    when = "" if out.day == today else f" ({out.day:%a %-m/%d})"
    lines: list[str] = []
    if out.logged:
        head = "Planned" if ml.making else "Logged"
        lines.append(f"{head} {out.slot}{when}:" if ml.slot_named
                     else f"{head} as a snack{when} — name the meal next time to file it there:")
        lines += [_line_for(lg) for lg in out.logged]
    if out.questions:
        lines.append("Not logged yet:" if out.logged else "Nothing logged —")
        lines += [f"· {q}" for q in out.questions]
    if not out.logged:
        return "\n".join(lines)

    totals = nutrition.day_totals(cur, out.day)
    lines.append("")
    lines.append(f"So far{when or ' today'}: {_fmt_n(float(totals['kcal']))} kcal · "
                 f"{_fmt_n(float(totals['protein_g']))} g protein · "
                 f"{_fmt_n(float(totals['fiber_g']))} g fiber")
    target = open_target(cur, out.day)
    left = remaining(totals, target)
    if left is None:
        lines.append("No target set, so no remaining budget — set one with "
                     "`new target <kcal> cal, <g> protein, <g> fiber`.")
    else:
        lines.append("Left: " + " · ".join(
            f"{_fmt_n(v)}{' kcal' if k == 'kcal' else ' g ' + k.replace('_g', '')}"
            for k, v in left.items()))
    if out.day == today:
        nxt = NEXT_SLOT[out.slot]
        picks = suggest(cur, out.day, left)
        if picks:
            lines.append(f"For {nxt}, fits: " + " · ".join(
                f"{r['name']} ({int(r['kcal'])} kcal, {float(r['protein_g']):.0f} g protein"
                f"{', ' + format(float(r['fiber_g']), '.0f') + ' g fiber' if r.get('fiber_g') is not None else ''})"
                for r in picks))
    return "\n".join(lines)


def is_known_food_fn(cur):
    """A predicate for split_items: is this whole phrase a saved food?"""
    def _known(text: str) -> bool:
        it = parse_item(text)
        name = it.name if it else text
        return nutrition.find_saved_food(cur, name) is not None
    return _known


def handle(cur, text: str, today: date) -> str | None:
    """Parse, log and reply. None when the text isn't a meal log."""
    ml = parse_meal_log(text, is_known_food_fn(cur))
    if ml is None:
        return None
    out = log_meal(cur, ml, today)
    return build_reply(cur, ml, out, today)


# "what's left" / "macros remaining" — the same regex the legacy handler used,
# answered from nutrition.* instead of the retired health.* budget.
_STATUS_RE = re.compile(
    r"\b(?:macros?|calories?|protein|carbs?|fiber|budget)\s+(?:left|remaining)\b"
    r"|\bwhere\s+am\s+i\s+(?:today|at)\b"
    r"|\bhow\s+(?:much|many)\b.*\b(?:left|remaining)\b"
    r"|\bremaining\s+budget\b"
    r"|\bwhat'?s\s+left\b",
    re.I)


def is_status_request(text: str) -> bool:
    return bool(_STATUS_RE.search(text or ""))


def status_reply(cur, today: date) -> str:
    totals = nutrition.day_totals(cur, today)
    lines = [f"Today: {_fmt_n(float(totals['kcal']))} kcal · "
             f"{_fmt_n(float(totals['protein_g']))} g protein · "
             f"{_fmt_n(float(totals['fiber_g']))} g fiber "
             f"({int(totals['n_entries'])} entries)"]
    left = remaining(totals, open_target(cur, today))
    if left is None:
        lines.append("No target set — `new target <kcal> cal, <g> protein, <g> fiber`.")
    else:
        lines.append("Left: " + " · ".join(
            f"{_fmt_n(v)}{' kcal' if k == 'kcal' else ' g ' + k.replace('_g', '')}"
            for k, v in left.items()))
        picks = suggest(cur, today, left)
        if picks:
            lines.append("Fits: " + " · ".join(
                f"{r['name']} ({int(r['kcal'])} kcal, {float(r['protein_g']):.0f} g protein)"
                for r in picks))
    return "\n".join(lines)
