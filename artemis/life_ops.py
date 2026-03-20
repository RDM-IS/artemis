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

def add_grocery_item(item: str, store: str = "", quantity: str = "", db: sqlite3.Connection | None = None) -> dict:
    conn = db or get_db()
    category = _categorize_item(item)
    conn.execute(
        "INSERT INTO grocery_list (item, category, quantity, store, added_at) VALUES (?, ?, ?, ?, ?)",
        (item, category, quantity, store, _now_iso()),
    )
    conn.commit()
    return {"item": item, "category": category}


def get_grocery_list(db: sqlite3.Connection | None = None) -> list[dict]:
    conn = db or get_db()
    rows = conn.execute("SELECT * FROM grocery_list WHERE is_purchased = 0 ORDER BY category, item").fetchall()
    return [dict(r) for r in rows]


def mark_purchased(item_text: str, db: sqlite3.Connection | None = None) -> bool:
    conn = db or get_db()
    result = conn.execute(
        "UPDATE grocery_list SET is_purchased = 1, purchased_at = ? WHERE item LIKE ? AND is_purchased = 0",
        (_now_iso(), f"%{item_text}%"),
    )
    conn.commit()
    return result.rowcount > 0


def clear_grocery_list(db: sqlite3.Connection | None = None) -> int:
    conn = db or get_db()
    result = conn.execute("UPDATE grocery_list SET is_purchased = 1, purchased_at = ? WHERE is_purchased = 0", (_now_iso(),))
    conn.commit()
    return result.rowcount


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


def build_store_list(store_name: str, db: sqlite3.Connection | None = None) -> str:
    store_map = load_store_map(store_name)
    if not store_map:
        return f"\U0001f6d2 No store map found for '{store_name}'. Configure it in store_maps.json."
    conn = db or get_db()
    grocery_items = get_grocery_list(db=conn)
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
