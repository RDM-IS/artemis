"""NUTRITION-2 — free-text meal logging, remaining-vs-target, suggestions.

    @artemis for breakfast I had an asiago bagel, 2 eggs, 1 slice of cheese,
    and turkey sandwich with a fruit bowl that had 3 oz of strawberry and
    6 ounces of green grapes

The rules that govern everything here:

* **Routing is deterministic and narrow.** A message is claimed only when it
  names a meal slot with an eating verb ("for lunch I had …", "I ate … for
  dinner", "making … for dinner") or lists a slot ("lunch: …"), or says
  "I had / I ate / snacked on …". Status talk ("lunch was great", "lunch:
  skipped", "I had the flu", "I had coffee with Jennifer") is not a meal log.
* **Macros are never estimated by a model.** Items resolve saved foods first,
  then USDA, then Open Food Facts, and are scaled by
  `nutrition.portion_factor`. What can't be resolved or scaled is asked about;
  the rest is stored. A bare "I had X" (no meal named) stores SAVED foods only.
* **An unreachable source is a question, never a fall-through** — a USDA
  outage does not become an Open Food Facts guess (FAIL-CLOSED-RESOLVERS).
* **A logged slot replaces the plan for that slot.** On a pre-filled day,
  "for breakfast I had X" deletes the slot's still-`assumed` planned rows, so
  the totals never count the plan and the meal together.
* **No DB connection is held during network lookups.** Saved foods resolve
  on one connection, external lookups run with none, writes use another.
* **Artemis never sets a target** (040). **Suggestions come only from the
  recipe DB**, recent repeats excluded, fit to what's left.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from artemis import nutrition
from artemis.nutrition import Item, parse_item

logger = logging.getLogger(__name__)

SLOTS = ("breakfast", "lunch", "dinner", "snacks")
#: How far back a recipe counts as "just had" for suggestions (Ryan: no
#: robotic repeats on off days).
REPEAT_WINDOW_DAYS = 3
MAX_SUGGESTIONS = 2

_SLOT = r"(breakfast|lunch|dinner|snacks?|a\s+snack)"
_DAYS = {"today": 0, "tonight": 0, "this morning": 0, "earlier": 0, "earlier today": 0,
         "this afternoon": 0, "this evening": 0, "yesterday": -1, "last night": -1,
         "yesterday morning": -1, "yesterday evening": -1}
_DAY_ALT = "|".join(sorted((re.escape(k).replace(r"\ ", r"\s+") for k in _DAYS),
                           key=len, reverse=True))
_DAY = rf"(?:\s+({_DAY_ALT}))?"

# "for breakfast [today] I had X" — an explicit eating verb.
_SLOT_VERB_RE = re.compile(
    rf"^\s*(?:for\s+)?{_SLOT}{_DAY}\s*,?\s*(?:i\s+)?(?:had|ate|made|cooked|grabbed|got)\s+"
    r"(?P<body>.+)$", re.I | re.S)
# "breakfast: X" / "lunch - X" — a listing.
_SLOT_LIST_RE = re.compile(rf"^\s*{_SLOT}{_DAY}\s*[:\-–]\s*(?P<body>.+)$", re.I | re.S)
# "I had X for lunch [yesterday]" / "ate X for dinner last night"
_SLOT_LAST_RE = re.compile(
    rf"^\s*(?:i\s+)?(?:had|ate)\s+(?P<body>.+?)\s+for\s+{_SLOT}{_DAY}\s*[.!]*\s*$", re.I | re.S)
# "making X for dinner" / "I'm cooking X for lunch" — counted as eaten.
_MAKING_RE = re.compile(
    rf"^\s*(?:i'?m\s+|i\s+am\s+)?(?:making|cooking)\s+(?P<body>.+?)\s+for\s+{_SLOT}{_DAY}"
    r"\s*[.!]*\s*$", re.I | re.S)
# "I had X [today]" / "snacked on X" — no meal named: saved foods only.
_BARE_RE = re.compile(
    rf"^\s*(?:i\s+(?:had|ate)|snacked\s+on)\s+(?P<body>.+?){_DAY}\s*[.!]*\s*$", re.I | re.S)

# A body that is status talk, not food.
_NON_FOOD_LEAD_RE = re.compile(
    r"^\s*(?:done|skipped|skip|no|none|nothing|n/?a|not|cancel+ed|late|pushed|moved|"
    r"great|good|fine|amazing|awesome|terrible|ok(?:ay)?|fun|plans?|reservations?|"
    r"running|stuck|trouble|a\s+(?:great|good|bad|rough|long|busy|nice)|the\s+(?:flu|day)|"
    r"(?:the\s+)?same\b|(?:the\s+|my\s+)?usual\b|enough\b|too\s+much|a\s+chance|"
    r"to\b|at\b|with\b|over\b|out\b)", re.I)
_NOT_A_LOG_RE = re.compile(
    r"\b(?:meeting|call|appointment|interview|dream|idea|question|thought|chat|talk|"
    r"conversation|workout|session|run|lift|pain|sore|soreness|flu|cold|fever|sick|"
    r"headache|weekend|traffic|plans?|reservations?|stuck|cancel+ed|pushed|trip|"
    r"week|time|fun|night|day|over)\b", re.I)

_CONTAINER_RE = re.compile(
    r"^.*?\b(?:that\s+(?:had|has)|which\s+had|containing|consisting\s+of|made\s+(?:with|of))\s+",
    re.I)
_TOP_SPLIT_RE = re.compile(r"\s*[,;]\s*")
_AND_SPLIT_RE = re.compile(r"\s+(?:and|&|plus)\s+", re.I)
_WITH_SPLIT_RE = re.compile(r"\s+(?:with|w/)\s+", re.I)
_TRAILING_DAY_RE = re.compile(rf"\s+(?:{_DAY_ALT})\s*[.!]*\s*$", re.I)


@dataclass
class MealLog:
    slot: str
    items: list[Item]
    day_offset: int = 0            # 0 today, -1 yesterday
    slot_named: bool = True        # False: bare "I had X" -> snacks, saved foods only
    making: bool = False
    ignored: list[str] = field(default_factory=list)   # "with Jennifer"
    not_read: list[str] = field(default_factory=list)  # later lines that aren't food


def _norm_slot(s: str) -> str:
    s = s.lower().strip()
    return "snacks" if "snack" in s else s


def _day_offset(tok: str | None) -> int:
    return _DAYS.get(re.sub(r"\s+", " ", (tok or "").lower().strip()), 0)


def split_items(body: str, is_known=lambda _t: False) -> tuple[list[str], list[str]]:
    """(item phrases, ignored phrases).

    Commas and semicolons always split. `and` / `with` split only when the
    phrase isn't itself a saved food ("mac and cheese" stays whole). A
    container ("a fruit bowl that had …") is replaced by what it held. A
    capitalised word after `with` is company, not food ("with Jennifer").
    """
    out: list[str] = []
    ignored: list[str] = []
    for top in _TOP_SPLIT_RE.split(body or ""):
        top = re.sub(r"^\s*(?:and|&|plus)\s+", "", top.strip(), flags=re.I)
        if not top:
            continue
        for part in ([top] if is_known(top) else _AND_SPLIT_RE.split(top)):
            part = part.strip()
            if not part:
                continue
            pieces = [part] if is_known(part) else _WITH_SPLIT_RE.split(part)
            for i, piece in enumerate(pieces):
                piece = _CONTAINER_RE.sub("", piece).strip()
                piece = re.sub(r"^(?:and|&|plus)\s+", "", piece, flags=re.I).strip(" .!")
                if not piece:
                    continue
                if i > 0 and re.match(r"^(?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)*$", piece):
                    ignored.append(f"with {piece}")
                    continue
                out.append(piece)
    return out, ignored


def _parse_line(line: str, is_known) -> MealLog | None:
    t = line.strip()
    if not t:
        return None
    making, named = False, True
    m = _SLOT_VERB_RE.match(t) or _SLOT_LIST_RE.match(t)
    if m:
        slot, day, body = m.group(1), m.group(2), m.group("body")
    else:
        m = _SLOT_LAST_RE.match(t) or _MAKING_RE.match(t)
        if m:
            making = m.re is _MAKING_RE
            slot, day, body = m.group(2), m.group(3), m.group("body")
        else:
            m = _BARE_RE.match(t)
            if not m:
                return None
            slot, day, body, named = "snacks", m.group(2), m.group("body"), False
    tail = _TRAILING_DAY_RE.search(body)
    if tail and not day:
        day = tail.group(0).strip(" .!")
    stripped = _TRAILING_DAY_RE.sub("", body)
    if stripped.strip() and not re.search(r"\b(?:as|from|than)\s*$", stripped, re.I):
        body = stripped
    elif tail:
        day = None                     # "same as yesterday": the day word is content
    if _NON_FOOD_LEAD_RE.match(body):
        return None
    if not named and _NOT_A_LOG_RE.search(body):
        return None
    phrases, ignored = split_items(body, is_known)
    items = [it for it in (parse_item(p) for p in phrases) if it]
    if not items:
        return None
    # "I had breakfast", "I had lunch with Jennifer": the slot is the object.
    if any(re.fullmatch(_SLOT, it.name.strip(), re.I) for it in items):
        return None
    return MealLog(slot=_norm_slot(slot), items=items, day_offset=_day_offset(day),
                   slot_named=named, making=making, ignored=ignored)


_SLOT_HEADER_RE = re.compile(rf"^\s*{_SLOT}{_DAY}\s*[:\-–]\s*$", re.I)


def parse_meal_logs(text: str, is_known=lambda _t: False) -> list[MealLog]:
    """Every meal log in a message. Each line that is itself a log starts a
    new one ("breakfast: eggs\\nlunch: salad" is two); a line that isn't
    continues the previous log's items. [] when the first line isn't a log."""
    t = (text or "").strip()
    if not t or len(t) > 2000:
        return []
    logs: list[MealLog] = []
    header: MealLog | None = None      # "breakfast:" alone, items on the next lines
    for line in t.splitlines():
        if not line.strip():
            continue
        hm = _SLOT_HEADER_RE.match(line)
        if hm:
            header = MealLog(slot=_norm_slot(hm.group(1)), items=[],
                             day_offset=_day_offset(hm.group(2)))
            logs.append(header)
            continue
        ml = _parse_line(line, is_known)
        if ml:
            logs.append(ml)
            header = None
            continue
        if not logs:
            return []
        extra = _continuation_items(line, is_known)
        if extra is None:
            # Not food-shaped ("can you check my inbox", "thanks!"): the meal
            # above is still logged; the line is named in the reply, not eaten.
            logs[-1].not_read.append(line.strip())
            continue
        logs[-1].items += extra
    logs = [ml for ml in logs if ml.items]
    return logs


_CHATTY_RE = re.compile(
    r"\?\s*$|\b(?:can\s+you|could\s+you|please|thanks|thank\s+you|check|show|what|how|"
    r"why|when|remind|schedule|email|inbox|calendar|meeting)\b", re.I)


def _continuation_items(line: str, is_known) -> list[Item] | None:
    """Items for a line that continues a meal list — each phrase must carry an
    amount or be a saved food. None when the line isn't a food list."""
    if _CHATTY_RE.search(line) or _NON_FOOD_LEAD_RE.match(line):
        return None
    phrases, _ignored = split_items(line, is_known)
    items = []
    for ph in phrases:
        it = parse_item(ph)
        if it is None:
            return None
        has_amount = it.unit is not None or it.numeric_lead or it.qty != 1.0 or \
            re.match(r"^\s*(?:an?|one|two|three|four|five|six|half|a\s+couple|a\s+dozen)\b",
                     ph, re.I) is not None
        if not (has_amount or is_known(ph)):
            return None
        items.append(it)
    return items or None


def parse_meal_log(text: str, is_known=lambda _t: False) -> MealLog | None:
    """The first log in a message (the gate). None = not a meal log."""
    logs = parse_meal_logs(text, is_known)
    return logs[0] if logs else None


# ============================================================================
# Resolution — saved (DB), external (network, no DB), then write (DB)
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
    replaced_planned: int = 0
    already_logged: int = 0


def _singular(name: str) -> str | None:
    n = name.strip()
    for suf, rep in (("ies", "y"), ("oes", "o"), ("es", ""), ("s", "")):
        if n.lower().endswith(suf) and len(n) > len(suf) + 2:
            if suf == "es" and not re.search(r"(?:ch|sh|s|x|z)es$", n, re.I):
                continue
            return n[: -len(suf)] + rep
    return None


def _saved_candidates(item: Item) -> list[tuple[str, bool]]:
    """(text to look up, use-it-whole?) in order. The whole phrase first when
    a leading number may be part of the name ("7 layer burrito")."""
    out: list[tuple[str, bool]] = []
    whole = re.sub(r"^\s*(?:an?|the|some|my)\s+", "", item.raw, flags=re.I).strip()
    if whole and whole.lower() != item.name.lower():
        out.append((whole, True))
    out.append((item.name, False))
    s = _singular(item.name)
    if s:
        out.append((s, False))
    return out


@dataclass
class _Pending:
    item: Item
    food: dict | None = None
    whole: bool = False
    external: bool = False
    question: str | None = None


def _resolve_saved(cur, ml: MealLog) -> list[_Pending]:
    out = []
    for item in ml.items:
        p = _Pending(item)
        if item.qty is None:
            p.question = nutrition.range_question(item)
        else:
            for text, whole in _saved_candidates(item):
                food = nutrition.saved_food(cur, text)
                if food:
                    p.food, p.whole = food, whole
                    break
            if p.food is None:
                if ml.slot_named:
                    p.external = True
                else:
                    p.question = (f"“{item.raw}” isn't in your saved foods. Name the meal "
                                  f"(`for lunch I had …`) to look it up, or add it in Notion.")
        out.append(p)
    return out


def _resolve_external(pending: list[_Pending]) -> None:
    """Network lookups — call with NO DB connection held."""
    for p in pending:
        if not p.external:
            continue
        try:
            p.food = nutrition.external_food(p.item.name)
            if p.food is None:
                s = _singular(p.item.name)
                p.food = nutrition.external_food(s) if s else None
        except nutrition.SourceUnavailable as exc:
            p.question = f"{exc} — nothing stored for “{p.item.raw}”. Try again shortly."
            continue
        if p.food is None:
            p.question = nutrition._ask_about(p.item.raw)


def _scale(pending: list[_Pending], out: Outcome) -> None:
    for p in pending:
        if p.question:
            out.questions.append(p.question)
            continue
        item = Item(1.0, None, p.item.raw, p.item.raw) if p.whole else p.item
        factor = nutrition.portion_factor(item.qty, item.unit, p.food)
        if factor is None:
            out.questions.append(nutrition.unit_question(p.item, p.food))
            continue
        f = p.food
        out.logged.append(Logged(
            item=item, food=f, factor=factor,
            kcal=int(round(float(f["kcal"]) * factor)),
            protein_g=float(f["protein_g"]) * factor,
            fiber_g=(float(f["fiber_g"]) * factor) if f.get("fiber_g") is not None else None))


def _ensure_day(cur, d: date) -> None:
    """A logged day exists before its entries (FK). Never overwrites a day."""
    from artemis import cycle
    cur.execute(
        "INSERT INTO nutrition.day (day_date, status, day_type, prefilled) "
        "VALUES (%s, %s, %s, FALSE) ON CONFLICT (day_date) DO NOTHING",
        (d, nutrition.DAY_ASSUMED, cycle.day_type(d)))


def _write(cur, ml: MealLog, out: Outcome) -> None:
    if not out.logged:
        return
    d = out.day
    _ensure_day(cur, d)
    if ml.slot_named:
        cur.execute("SELECT count(*) FROM nutrition.entry WHERE day_date = %s AND slot = %s "
                    "AND status = 'corrected'", (d, ml.slot))
        row = cur.fetchone()
        try:
            out.already_logged = int((row[0] if not isinstance(row, dict)
                                      else list(row.values())[0]) or 0)
        except (TypeError, IndexError):
            out.already_logged = 0
        # The meal replaces the PLAN for this slot; earlier logs Ryan made for
        # the same slot (status `corrected`) stay — and the reply says so.
        cur.execute("DELETE FROM nutrition.entry WHERE day_date = %s AND slot = %s "
                    "AND status = 'assumed'", (d, ml.slot))
        out.replaced_planned = cur.rowcount if isinstance(cur.rowcount, int) else 0
    for lg in out.logged:
        nutrition._insert_entry(cur, d, ml.slot, lg.food, lg.factor)
    nutrition._mark_corrected(cur, d)
    nutrition._audit(cur, "nutrition_meal_log", "logged", {
        "day": d.isoformat(), "slot": ml.slot, "making": ml.making,
        "items": [lg.item.raw for lg in out.logged],
        "replaced_planned": out.replaced_planned, "unresolved": len(out.questions)})


def log_meal(cur, ml: MealLog, today: date) -> Outcome:
    """All three phases on one cursor — for tests and callers with no
    network concern. The chat handler uses `handle`, which splits them."""
    out = _begin(ml, today)
    if out.refused:
        return out
    pending = _resolve_saved(cur, ml)
    _resolve_external(pending)
    _scale(pending, out)
    _write(cur, ml, out)
    return out


def _begin(ml: MealLog, today: date) -> Outcome:
    d = today + timedelta(days=ml.day_offset)
    out = Outcome(day=d, slot=ml.slot)
    if not nutrition.is_open(d):
        out.refused = (f"{d:%a %-m/%d} is past the {nutrition.CORRECTION_WINDOW_HOURS}h "
                       "window and is locked — nothing logged.")
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


def _assumed_count(cur, d: date) -> int:
    cur.execute("SELECT count(*) FROM nutrition.entry WHERE day_date = %s "
                "AND status = 'assumed'", (d,))
    row = cur.fetchone()
    if row is None:
        return 0
    v = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(v or 0)


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


def _left_line(left: dict) -> str:
    return "Left: " + " · ".join(
        f"{_fmt_n(v)}{' kcal' if k == 'kcal' else ' g ' + k.replace('_g', '')}"
        for k, v in left.items())


def _suggest_line(picks: list[dict]) -> str:
    def one(r):
        fib = (f", {float(r['fiber_g']):.0f} g fiber" if r.get("fiber_g") is not None else "")
        est = " — estimate" if r.get("is_placeholder") else ""
        return f"{r['name']} ({int(r['kcal'])} kcal, {float(r['protein_g']):.0f} g protein{fib}{est})"
    return "Recipes that fit what's left: " + " · ".join(one(r) for r in picks)


def build_reply(cur, ml: MealLog, out: Outcome, today: date) -> str:
    if out.refused:
        return out.refused
    when = "" if out.day == today else f" ({out.day:%a %-m/%d})"
    lines: list[str] = []
    if out.logged:
        if not ml.slot_named:
            head = f"Logged as a snack{when} — name the meal next time to file it there:"
        else:
            head = f"Logged {out.slot}{when}" + (" (counted as eaten)" if ml.making else "") + ":"
        lines.append(head)
        lines += [_line_for(lg) for lg in out.logged]
        if out.replaced_planned:
            n = out.replaced_planned
            lines.append(f"_Replaces the planned {out.slot} ({n} item{'s' if n != 1 else ''})._")
        if out.already_logged:
            n = out.already_logged
            lines.append(f"_Added to {n} item{'s' if n != 1 else ''} already logged for "
                         f"{out.slot} — to redo it, send `fix` tomorrow or log only what's new._")
    if ml.ignored:
        lines.append("_Not food, ignored: " + ", ".join(ml.ignored) + "._")
    if ml.not_read:
        lines.append("_Not read as food (send separately): " + " / ".join(ml.not_read) + "_")
    if out.questions:
        lines.append("Not logged yet:" if out.logged else "Nothing logged —")
        lines += [f"· {q}" for q in out.questions]
    if not out.logged:
        return "\n".join(lines)

    totals = nutrition.day_totals(cur, out.day)
    assumed = _assumed_count(cur, out.day)
    lines.append("")
    lines.append(f"So far{when or ' today'}: {_fmt_n(float(totals['kcal']))} kcal · "
                 f"{_fmt_n(float(totals['protein_g']))} g protein · "
                 f"{_fmt_n(float(totals['fiber_g']))} g fiber"
                 + (f" _(includes {assumed} planned item{'s' if assumed != 1 else ''} "
                    f"not yet logged)_" if assumed else ""))
    target = open_target(cur, out.day)
    left = remaining(totals, target)
    if left is None:
        lines.append("No target set, so no remaining budget — set one with "
                     "`new target <kcal> cal, <g> protein, <g> fiber`.")
    else:
        lines.append(_left_line(left))
    if out.day == today:
        picks = suggest(cur, out.day, left)
        if picks:
            lines.append(_suggest_line(picks))
    return "\n".join(lines)


def is_known_food_fn(cur):
    """A predicate for split_items: is this whole phrase a saved food?"""
    def _known(text: str) -> bool:
        it = parse_item(text)
        name = it.name if it else text
        return nutrition.find_saved_food(cur, name) is not None
    return _known


def handle(get_connection, text: str, today: date) -> str | None:
    """Parse, resolve and log, holding NO DB connection during network
    lookups. None when the text isn't a meal log.

    `get_connection` is `knowledge.db.get_connection` (a context manager that
    commits on exit).
    """
    with get_connection() as conn:
        cur = conn.cursor()
        logs = parse_meal_logs(text, is_known_food_fn(cur))
        if not logs:
            return None
        staged = []
        for ml in logs:
            out = _begin(ml, today)
            pending = [] if out.refused else _resolve_saved(cur, ml)
            staged.append((ml, out, pending))

    for _ml, out, pending in staged:          # network, no connection held
        if not out.refused:
            _resolve_external(pending)
            _scale(pending, out)

    replies = []
    with get_connection() as conn:
        cur = conn.cursor()
        for ml, out, _pending in staged:
            if not out.refused:
                _write(cur, ml, out)
            replies.append(build_reply(cur, ml, out, today))
    return "\n\n".join(replies)


# ============================================================================
# "what's left" — nutrition questions only
# ============================================================================

_STATUS_RE = re.compile(
    r"\b(?:macros?|calories?|kcal|protein|carbs?|fiber|fibre|fat)\s+(?:left|remaining)\b"
    r"|\bhow\s+(?:much|many)\s+(?:macros?|calories?|kcal|protein|carbs?|fiber|fat)\b"
    r".*\b(?:left|remaining)\b"
    r"|^\s*(?:what'?s|what\s+is)\s+left(?:\s+(?:today|for\s+today|to\s+eat))?\s*\??\s*$"
    r"|^\s*where\s+am\s+i\s+(?:today|at)(?:\s+today)?\s*\??\s*$"
    r"|\b(?:nutrition|food|calorie|macro)\s+budget\b",
    re.I)


def is_status_request(text: str) -> bool:
    return bool(_STATUS_RE.search(text or ""))


def status_reply(cur, today: date) -> str:
    totals = nutrition.day_totals(cur, today)
    assumed = _assumed_count(cur, today)
    lines = [f"Today: {_fmt_n(float(totals['kcal']))} kcal · "
             f"{_fmt_n(float(totals['protein_g']))} g protein · "
             f"{_fmt_n(float(totals['fiber_g']))} g fiber "
             f"({int(totals['n_entries'])} entries"
             + (f", {assumed} planned and not yet logged" if assumed else "") + ")"]
    left = remaining(totals, open_target(cur, today))
    if left is None:
        lines.append("No target set — `new target <kcal> cal, <g> protein, <g> fiber`.")
    else:
        lines.append(_left_line(left))
        picks = suggest(cur, today, left)
        if picks:
            lines.append(_suggest_line(picks))
    return "\n".join(lines)
