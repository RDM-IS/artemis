"""Life ops — workout tracker, grocery list, store maps, health plan context."""

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from artemis import config
from artemis.commitments import get_db as _get_commitments_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite tables
# ---------------------------------------------------------------------------

CREATE_WORKOUT_SESSIONS = """
CREATE TABLE IF NOT EXISTS workout_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    notes TEXT
)
"""

CREATE_WORKOUT_LOG = """
CREATE TABLE IF NOT EXISTS workout_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES workout_sessions(id),
    exercise TEXT NOT NULL,
    weight_lbs REAL,
    reps INTEGER,
    set_number INTEGER,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_WORKOUT_EXERCISES = """
CREATE TABLE IF NOT EXISTS workout_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT
)
"""

CREATE_GROCERY_LIST = """
CREATE TABLE IF NOT EXISTS grocery_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item TEXT NOT NULL,
    category TEXT,
    quantity TEXT,
    store TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    purchased_at TIMESTAMP,
    is_purchased INTEGER DEFAULT 0,
    notes TEXT
)
"""

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
# Workout schedule & definitions
# ---------------------------------------------------------------------------

WORKOUT_SCHEDULE = {
    0: ("Strength A", "Push + Legs"),
    1: ("Away", "Treadmill + Bodyweight"),
    2: ("Strength B", "Pull + Hinge"),
    3: ("Cardio", "Row or Bike"),
    4: ("Rest", "Rest Day"),
    5: ("Strength C", "Full Body"),
    6: ("Recovery", "Yoga / Walk"),
}

WORKOUT_DEFINITIONS = {
    "Strength A": [
        ("Goblet Squat", "3x12", "dumbbell"),
        ("Floor Press", "3x12", "dumbbells"),
        ("Romanian Deadlift", "3x12", "dumbbells"),
        ("Band Chest Press", "3x15", "resistance bands"),
        ("TRX Fallout", "3x10", "TRX"),
        ("Exercise Ball Plank", "3x30sec", "exercise ball"),
    ],
    "Strength B": [
        ("TRX Row", "3x12", "TRX"),
        ("Bicep Curl", "3x12", "curl bar"),
        ("Romanian Deadlift", "3x12", "curl bar/dumbbells"),
        ("Band Pull-Apart", "3x20", "resistance bands"),
        ("TRX Single-Leg Deadlift", "3x10/side", "TRX"),
        ("Exercise Ball Hamstring Curl", "3x12", "exercise ball"),
    ],
    "Strength C": [
        ("Goblet Squat", "3x12", "dumbbell"),
        ("TRX Row", "3x12", "TRX"),
        ("Dumbbell Floor Press", "3x12", "dumbbells"),
        ("Romanian Deadlift", "3x12", "curl bar"),
        ("Band Pull-Apart", "3x20", "resistance bands"),
        ("Reverse Lunge", "3x10/side", "dumbbells"),
        ("Exercise Ball Plank", "3x30sec", "exercise ball"),
    ],
    "Away": [
        ("Treadmill walk/jog warmup", "10 min", "treadmill"),
        ("Push-ups", "3x15", "bodyweight"),
        ("Bodyweight squats", "3x15", "bodyweight"),
        ("Reverse lunges", "3x10/side", "bodyweight"),
        ("Plank", "3x30sec", "bodyweight"),
        ("Treadmill cooldown walk", "5 min", "treadmill"),
    ],
    "Cardio": [("Row or Bike", "30 min", "")],
    "Recovery": [("Yoga / Walk", "30 min", "")],
}

EXERCISE_ALIASES = {
    "goblet squat": ["goblet", "squat", "goblet squat"],
    "floor press": ["bench", "floor press", "chest press", "dumbbell floor press"],
    "romanian deadlift": ["rdl", "romanian deadlift", "romanian", "deadlift"],
    "band chest press": ["band chest", "band press", "chest press band"],
    "trx fallout": ["trx fallout", "fallout"],
    "exercise ball plank": ["ball plank", "plank ball", "exercise ball plank"],
    "trx row": ["trx row", "row trx"],
    "bicep curl": ["bicep curl", "curl", "curls", "bicep"],
    "band pull-apart": ["band pull", "pull apart", "band pull-apart"],
    "trx single-leg deadlift": ["trx single leg", "single leg deadlift trx"],
    "exercise ball hamstring curl": ["hamstring curl", "ball hamstring", "ball curl"],
    "push-ups": ["push-ups", "pushups", "push up", "pushup"],
    "bodyweight squats": ["bodyweight squat", "bw squat", "air squat"],
    "reverse lunge": ["reverse lunge", "lunge", "lunges", "reverse lunges"],
    "plank": ["plank"],
    "treadmill": ["treadmill", "walk", "jog"],
    "row or bike": ["row", "bike", "rowing", "cycling"],
    "yoga / walk": ["yoga", "recovery walk"],
}


def _match_exercise(text: str) -> str | None:
    text_lower = text.lower().strip()
    for canonical, aliases in EXERCISE_ALIASES.items():
        for alias in aliases:
            if alias == text_lower:
                return canonical
    for canonical, aliases in EXERCISE_ALIASES.items():
        for alias in aliases:
            if alias in text_lower or text_lower in alias:
                return canonical
    return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = _get_commitments_db()
    conn.execute(CREATE_WORKOUT_SESSIONS)
    conn.execute(CREATE_WORKOUT_LOG)
    conn.execute(CREATE_WORKOUT_EXERCISES)
    conn.execute(CREATE_GROCERY_LIST)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Workout tracker
# ---------------------------------------------------------------------------

def start_workout(workout_type: str, db: sqlite3.Connection | None = None) -> int:
    conn = db or get_db()
    today = date.today().isoformat()
    cursor = conn.execute(
        "INSERT INTO workout_sessions (date, type, started_at) VALUES (?, ?, ?)",
        (today, workout_type, _now_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def get_today_session(db: sqlite3.Connection | None = None) -> dict | None:
    conn = db or get_db()
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT * FROM workout_sessions WHERE date = ? ORDER BY id DESC LIMIT 1",
        (today,),
    ).fetchone()
    return dict(row) if row else None


def get_open_session(db: sqlite3.Connection | None = None) -> dict | None:
    conn = db or get_db()
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT * FROM workout_sessions WHERE date = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (today,),
    ).fetchone()
    return dict(row) if row else None


def end_workout(session_id: int, db: sqlite3.Connection | None = None) -> dict:
    conn = db or get_db()
    conn.execute(
        "UPDATE workout_sessions SET ended_at = ? WHERE id = ?",
        (_now_iso(), session_id),
    )
    conn.commit()
    session = conn.execute("SELECT * FROM workout_sessions WHERE id = ?", (session_id,)).fetchone()
    sets = conn.execute(
        "SELECT exercise, COUNT(*) as cnt FROM workout_log WHERE session_id = ? GROUP BY exercise",
        (session_id,),
    ).fetchall()
    total_sets = conn.execute(
        "SELECT COUNT(*) as cnt FROM workout_log WHERE session_id = ?", (session_id,)
    ).fetchone()

    duration_min = 0
    if session and session["started_at"] and session["ended_at"]:
        try:
            start = datetime.strptime(session["started_at"], "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(session["ended_at"], "%Y-%m-%d %H:%M:%S")
            duration_min = int((end - start).total_seconds() / 60)
        except (ValueError, TypeError):
            pass

    return {
        "type": session["type"] if session else "?",
        "duration_min": duration_min,
        "total_sets": total_sets["cnt"] if total_sets else 0,
        "exercises": [{"name": s["exercise"], "sets": s["cnt"]} for s in sets],
    }


def log_set(
    session_id: int, exercise: str,
    weight_lbs: float | None = None, reps: int | None = None,
    db: sqlite3.Connection | None = None,
) -> dict:
    conn = db or get_db()
    existing = conn.execute(
        "SELECT COUNT(*) as cnt FROM workout_log WHERE session_id = ? AND exercise = ?",
        (session_id, exercise),
    ).fetchone()
    set_number = (existing["cnt"] if existing else 0) + 1

    conn.execute(
        "INSERT INTO workout_log (session_id, exercise, weight_lbs, reps, set_number, logged_at) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, exercise, weight_lbs, reps, set_number, _now_iso()),
    )
    conn.commit()

    pr_info = {"is_weight_pr": False, "is_rep_pr": False, "prev_best_weight": None, "prev_best_reps": None}
    prev = conn.execute(
        "SELECT MAX(weight_lbs) as max_weight, MAX(reps) as max_reps FROM workout_log WHERE exercise = ? AND id != last_insert_rowid()",
        (exercise,),
    ).fetchone()
    if prev:
        pr_info["prev_best_weight"] = prev["max_weight"]
        pr_info["prev_best_reps"] = prev["max_reps"]
        if weight_lbs and prev["max_weight"] and weight_lbs > prev["max_weight"]:
            pr_info["is_weight_pr"] = True
        elif weight_lbs and prev["max_weight"] and weight_lbs >= prev["max_weight"] and reps and prev["max_reps"] and reps > prev["max_reps"]:
            pr_info["is_rep_pr"] = True

    pr_info["set_number"] = set_number
    return pr_info


def get_recent_workouts(limit: int = 7, db: sqlite3.Connection | None = None) -> list[dict]:
    conn = db or get_db()
    rows = conn.execute(
        """SELECT ws.*, COUNT(wl.id) as set_count
           FROM workout_sessions ws LEFT JOIN workout_log wl ON ws.id = wl.session_id
           GROUP BY ws.id ORDER BY ws.date DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_last_exercise(exercise: str, db: sqlite3.Connection | None = None) -> dict | None:
    conn = db or get_db()
    row = conn.execute(
        "SELECT * FROM workout_log WHERE exercise = ? ORDER BY logged_at DESC LIMIT 1", (exercise,)
    ).fetchone()
    return dict(row) if row else None


def log_rest_day(db: sqlite3.Connection | None = None) -> int:
    conn = db or get_db()
    today = date.today().isoformat()
    cursor = conn.execute(
        "INSERT INTO workout_sessions (date, type, started_at, ended_at) VALUES (?, 'rest', ?, ?)",
        (today, _now_iso(), _now_iso()),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Workout parsing
# ---------------------------------------------------------------------------

_SET_LOG_PATTERN_ALT = re.compile(
    r"^(.+?)\s+(\d+(?:\.\d+)?)\s*(?:lbs?)?\s+(\d+)\s*(?:reps?)?\s*$", re.IGNORECASE
)


def parse_exercise_log(text: str) -> dict | None:
    text = text.strip()
    # "exercise NxM weightlbs"
    m = re.match(r"^(.+?)\s+(\d+)\s*x\s*(\d+)\s+(\d+(?:\.\d+)?)\s*(?:lbs?)?\s*$", text, re.IGNORECASE)
    if m:
        exercise = _match_exercise(m.group(1))
        if exercise:
            return {"exercise": exercise, "sets": int(m.group(2)), "reps": int(m.group(3)), "weight_lbs": float(m.group(4))}
    # "exercise weight reps"
    m = _SET_LOG_PATTERN_ALT.match(text)
    if m:
        exercise = _match_exercise(m.group(1))
        if exercise:
            return {"exercise": exercise, "weight_lbs": float(m.group(2)), "reps": int(m.group(3)), "sets": 1}
    # "exercise weightlbs reps reps"
    m = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s*lbs?\s+(\d+)\s*(?:reps?)?\s*$", text, re.IGNORECASE)
    if m:
        exercise = _match_exercise(m.group(1))
        if exercise:
            return {"exercise": exercise, "weight_lbs": float(m.group(2)), "reps": int(m.group(3)), "sets": 1}
    return None


# ---------------------------------------------------------------------------
# Workout command handler
# ---------------------------------------------------------------------------

def handle_workout_command(question: str) -> str | None:
    q = question.lower().strip()

    if any(kw in q for kw in ["let's workout", "lets workout", "start workout",
                               "workout time", "let's work out", "lets work out"]):
        return _start_workout_flow()
    if any(kw in q for kw in ["workout done", "finished", "that's it", "thats it"]):
        if q.strip() in ("done", "finished", "that's it", "thats it", "workout done"):
            return _end_workout_flow()
    if any(kw in q for kw in ["skip today", "rest day", "taking today off", "day off"]):
        log_rest_day()
        return "\u2705 Rest day logged. Recovery matters."
    if any(kw in q for kw in ["workout history", "recent workouts"]):
        return _workout_history()
    m = re.match(r"last\s+(.+)", q)
    if m:
        exercise = _match_exercise(m.group(1))
        if exercise:
            last = get_last_exercise(exercise)
            if not last:
                return f"\U0001f4aa No logged sets for {exercise} yet."
            weight = f"{last['weight_lbs']}lbs" if last.get("weight_lbs") else "bodyweight"
            reps = f" x {last['reps']}" if last.get("reps") else ""
            return f"\U0001f4aa Last **{exercise}**: {weight}{reps} on {last['logged_at'][:10]}"
    parsed = parse_exercise_log(question)
    if parsed:
        return _log_exercise(parsed)
    return None


def _start_workout_flow() -> str:
    today_dow = date.today().weekday()
    sched = WORKOUT_SCHEDULE.get(today_dow, ("Rest", "Rest Day"))
    session_type, session_desc = sched
    if session_type == "Rest":
        return "Today is a rest day. Recovery matters. Say `@artemis skip today` to log it."
    existing = get_today_session()
    if existing and existing.get("ended_at"):
        return (f"You already logged a session today ({existing['type']}, started {existing['started_at']}).\n"
                f"Log another? Reply 'yes' to confirm.")
    open_session = get_open_session()
    if open_session:
        return f"You have an open session ({open_session['type']}). Log sets or say `done` to finish."

    start_workout(session_type)
    day_name = date.today().strftime("%A")
    exercises = WORKOUT_DEFINITIONS.get(session_type, [])
    exercise_lines = []
    for name, sets_reps, equipment in exercises:
        equip = f" ({equipment})" if equipment else ""
        exercise_lines.append(f"  {name} \u2014 {sets_reps}{equip}")
    return (
        f"\U0001f4aa **{day_name} \u2014 {session_type}: {session_desc}**\n\n"
        + "\n".join(exercise_lines)
        + "\n\nSay `@artemis [exercise] [weight] [reps]` to log sets.\nSay `@artemis done` when finished."
    )


def _end_workout_flow() -> str:
    session = get_open_session()
    if not session:
        return "No active workout session. Say `@artemis let's workout` to start one."
    summary = end_workout(session["id"])
    exercise_list = ", ".join(e["name"] for e in summary["exercises"])
    return (
        f"\U0001f4aa **Workout complete!**\nType: {summary['type']}\n"
        f"Duration: {summary['duration_min']} min\nSets logged: {summary['total_sets']}\n"
        f"Exercises: {exercise_list}\nGreat work."
    )


def _workout_history() -> str:
    workouts = get_recent_workouts(limit=7)
    if not workouts:
        return "\U0001f4aa No workout history yet."
    lines = ["\U0001f4aa **Recent workouts:**"]
    for w in workouts:
        duration = ""
        if w.get("started_at") and w.get("ended_at"):
            try:
                s = datetime.strptime(w["started_at"], "%Y-%m-%d %H:%M:%S")
                e = datetime.strptime(w["ended_at"], "%Y-%m-%d %H:%M:%S")
                duration = f", {int((e - s).total_seconds() / 60)} min"
            except (ValueError, TypeError):
                pass
        lines.append(f"- {w['date']} \u2014 {w['type']}{duration}, {w.get('set_count', 0)} sets")
    return "\n".join(lines)


def _log_exercise(parsed: dict) -> str:
    session = get_open_session()
    if not session:
        return "No active workout session. Say `@artemis let's workout` to start one."
    exercise = parsed["exercise"]
    weight = parsed.get("weight_lbs")
    reps = parsed.get("reps")
    num_sets = parsed.get("sets", 1)
    results = []
    for _ in range(num_sets):
        pr = log_set(session["id"], exercise, weight_lbs=weight, reps=reps)
        results.append(pr)
    last_pr = results[-1]
    weight_str = f"{weight}lbs" if weight else "bodyweight"
    reps_str = f" x {reps}" if reps else ""
    if num_sets > 1:
        base = f"\U0001f4aa \u2705 {exercise} {weight_str}{reps_str} x {num_sets} sets logged."
    else:
        base = f"\U0001f4aa \u2705 {exercise} {weight_str}{reps_str} logged (set {last_pr['set_number']})."
    if last_pr["is_weight_pr"]:
        base += f" \U0001f3c6 New weight PR! (previous best: {last_pr.get('prev_best_weight')}lbs)"
    elif last_pr["is_rep_pr"]:
        base += f" \U0001f3c6 New rep PR at this weight!"
    elif last_pr.get("prev_best_weight"):
        base += f" Previous best: {last_pr['prev_best_weight']}lbs x {last_pr['prev_best_reps']}"
    return base


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
