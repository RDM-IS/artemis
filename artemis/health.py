"""Personal training intents — morning check-in + workout debrief.

Two Mattermost intents:
  log_morning_state    → UPSERT health.daily_state for today (CT)
  log_workout_debrief  → INSERT N exercise rows + 1 summary row in health.session_log

Both pass user-facing output through the trainer voice template:
short, direct, no fluff, no shame, no fake hype.

The autoregulator is downstream — these handlers ONLY write data.
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")


# ============================================================================
# Trainer voice — every user-facing string passes through this filter
# ============================================================================

TRAINER_VOICE_PROMPT = """
You are a personal trainer. Short. Direct. No fluff. No fake hype. No shame.

YES: "Slept 4.5. Today is mobility and a walk. Don't argue."
YES: "Logged. Tomorrow's plan held. Get to work at 5."
NO:  "Wow, only 4 hours? You should really try to sleep more!"
NO:  "Awesome job today, champ! 💪"
"""


# ============================================================================
# Pydantic models
# ============================================================================

class MorningState(BaseModel):
    sleep_hrs: Optional[float] = None
    energy: Optional[int] = None
    soreness: Optional[dict] = None
    weight_lbs: Optional[float] = None
    resting_hr: Optional[int] = None
    free_text: Optional[str] = None


class ExerciseReport(BaseModel):
    exercise: str
    log_type: str = Field(..., pattern=r"^(strength_set|cardio_block|session_summary)$")
    reps_done: Optional[int] = None
    weight_lbs: Optional[float] = None
    duration_sec: Optional[int] = None
    rpe_actual: Optional[float] = None
    hr_avg: Optional[int] = None
    hr_peak: Optional[int] = None
    notes: Optional[str] = None
    user_suggestion: Optional[str] = None
    is_skipped: bool = False


# ============================================================================
# Soreness region normalization
# ============================================================================

_SORENESS_REGIONS = {
    "legs":      {"legs", "quads", "thighs", "hamstrings", "calves", "lower body"},
    "back":      {"back", "lower back", "upper back", "lumbar", "spine"},
    "shoulders": {"shoulders", "delts", "traps"},
    "core":      {"core", "abs", "obliques", "midsection"},
    "knees":     {"knees", "knee"},
    "arms":      {"arms", "biceps", "triceps", "forearms"},
}


def normalize_soreness_region(raw: str) -> str:
    """Map a raw soreness label to a canonical region. Returns the raw label if no match."""
    lower = raw.lower().strip()
    for canonical, aliases in _SORENESS_REGIONS.items():
        if lower in aliases:
            return canonical
    return lower


# ============================================================================
# Claude client (cached)
# ============================================================================

_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        from knowledge.secrets import get_anthropic_key
        _anthropic_client = anthropic.Anthropic(api_key=get_anthropic_key())
    return _anthropic_client


def _call_claude_json(system: str, user_msg: str, max_tokens: int = 600) -> dict | list:
    """Call Claude with a system prompt that emits JSON. Returns parsed dict/list."""
    client = _get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


# ============================================================================
# Morning check-in parser
# ============================================================================

_MORNING_SYSTEM = """You parse a morning check-in message into structured state.

Return ONLY valid JSON matching this schema, no other text:
{
  "sleep_hrs": float or null,
  "energy": int 1-5 or null,
  "soreness": object {region: int 1-5} or null,
  "weight_lbs": float or null,
  "resting_hr": int or null,
  "free_text": string or null
}

RULES:
- "feel great"/"feeling solid" → energy 5; "good"/"fine" → 4; "ok"/"meh" → 3;
  "tired"/"sluggish" → 2; "trash"/"wrecked"/"awful" → 1
- Soreness regions: legs, back, shoulders, core, knees, arms (lowercase singular).
  Normalize "quads"/"thighs" → "legs", "lumbar" → "back", "delts" → "shoulders".
- Soreness scale: "a little"/"mild" → 2, "moderate" → 3, "very"/"really" → 4,
  "destroyed"/"can't move" → 5. Plain "sore" without intensity → 3.
- "RHR" or "resting HR" → resting_hr (int).
- Anything that doesn't fit a structured field → free_text verbatim.

Examples:

Input: "slept 6.5, energy 3, legs sore 3 out of 5, RHR 58, feel slow this morning"
Output: {"sleep_hrs":6.5,"energy":3,"soreness":{"legs":3},"weight_lbs":null,"resting_hr":58,"free_text":"feel slow this morning"}

Input: "morning. 5 hrs. quads wrecked. feel like trash"
Output: {"sleep_hrs":5.0,"energy":1,"soreness":{"legs":5},"weight_lbs":null,"resting_hr":null,"free_text":null}

Input: "checkin slept 7 woke up 271 energy good"
Output: {"sleep_hrs":7.0,"energy":4,"soreness":null,"weight_lbs":271.0,"resting_hr":null,"free_text":null}

Input: "morning all good"
Output: {"sleep_hrs":null,"energy":4,"soreness":null,"weight_lbs":null,"resting_hr":null,"free_text":"all good"}

Input: "slept maybe 4 hours, knees a little tight, back fine"
Output: {"sleep_hrs":4.0,"energy":null,"soreness":{"knees":2},"weight_lbs":null,"resting_hr":null,"free_text":null}
"""


def parse_morning_checkin(text: str) -> MorningState:
    """Parse free-form morning check-in text into a MorningState.

    Raises ValidationError if Claude returns malformed JSON or invalid types.
    """
    data = _call_claude_json(_MORNING_SYSTEM, f"Input: {text}")

    # Normalize soreness regions
    if isinstance(data.get("soreness"), dict):
        normalized = {}
        for region, score in data["soreness"].items():
            try:
                score_int = int(score)
                if 1 <= score_int <= 5:
                    normalized[normalize_soreness_region(region)] = score_int
            except (TypeError, ValueError):
                continue
        data["soreness"] = normalized or None

    return MorningState(**data)


# ============================================================================
# Workout debrief parser
# ============================================================================

_DEBRIEF_SYSTEM = """You parse a workout debrief message into structured exercise reports.

Return ONLY valid JSON matching this schema, no other text:
{
  "exercises": [
    {
      "exercise": "string",
      "log_type": "strength_set"|"cardio_block",
      "reps_done": int or null,
      "weight_lbs": float or null,
      "duration_sec": int or null,
      "rpe_actual": float 1-10 or null,
      "hr_avg": int or null,
      "hr_peak": int or null,
      "notes": "string or null",
      "user_suggestion": "string or null",
      "is_skipped": false
    }
  ],
  "session_summary": {
    "rpe_actual": float or null,
    "notes": "string or null",
    "user_suggestion": "string or null"
  }
}

RULES:
- Each exercise mentioned by name → one row.
- Skipped exercises: is_skipped=true, notes="skipped: <reason>".
- "RPE 8" or "8 out of 10" → rpe_actual.
- "HR peak 159"/"peak HR 159" → hr_peak. "HR avg X" → hr_avg.
- Weights: "@ 50lb"/"50 lbs"/"at 50" → weight_lbs.
- Reps: "10 reps"/"x 10"/"10x" → reps_done.
- Cardio: log_type="cardio_block". Strength: log_type="strength_set".
- User suggestions for plan changes (e.g. "next time take it down", "should be harder",
  "60 seconds is too short") → user_suggestion VERBATIM (paraphrasing forbidden).
- session_summary RPE: "overall RPE X" or "felt RPE X" applied to the whole session.

Today's plan context:
{plan_context}

Examples:

Input: "Burpees 15 reps RPE 10 HR peak 159, RDLs 10 at 50 RPE 6, rows were good felt strong, skipped planks knee was off, overall RPE 8 felt gassed."
Output:
{
  "exercises": [
    {"exercise":"Burpees","log_type":"cardio_block","reps_done":15,"weight_lbs":null,"duration_sec":null,"rpe_actual":10.0,"hr_avg":null,"hr_peak":159,"notes":null,"user_suggestion":null,"is_skipped":false},
    {"exercise":"RDL","log_type":"strength_set","reps_done":10,"weight_lbs":50.0,"duration_sec":null,"rpe_actual":6.0,"hr_avg":null,"hr_peak":null,"notes":null,"user_suggestion":null,"is_skipped":false},
    {"exercise":"Rows","log_type":"strength_set","reps_done":null,"weight_lbs":null,"duration_sec":null,"rpe_actual":null,"hr_avg":null,"hr_peak":null,"notes":"felt strong","user_suggestion":null,"is_skipped":false},
    {"exercise":"Plank","log_type":"strength_set","reps_done":null,"weight_lbs":null,"duration_sec":null,"rpe_actual":null,"hr_avg":null,"hr_peak":null,"notes":"skipped: knee was off","user_suggestion":null,"is_skipped":true}
  ],
  "session_summary": {"rpe_actual":8.0,"notes":"felt gassed","user_suggestion":null}
}

Input: "done. squats 3x10 @ 35 RPE 7. plank 30s. all good."
Output:
{
  "exercises": [
    {"exercise":"Goblet squat","log_type":"strength_set","reps_done":10,"weight_lbs":35.0,"duration_sec":null,"rpe_actual":7.0,"hr_avg":null,"hr_peak":null,"notes":"3 sets","user_suggestion":null,"is_skipped":false},
    {"exercise":"Plank","log_type":"strength_set","reps_done":null,"weight_lbs":null,"duration_sec":30,"rpe_actual":null,"hr_avg":null,"hr_peak":null,"notes":null,"user_suggestion":null,"is_skipped":false}
  ],
  "session_summary": {"rpe_actual":null,"notes":null,"user_suggestion":null}
}

Input: "intervals done. 8 rounds, peak HR 152. rest was too easy at 60s, try 45 next time. overall RPE 7."
Output:
{
  "exercises": [
    {"exercise":"Treadmill intervals","log_type":"cardio_block","reps_done":8,"weight_lbs":null,"duration_sec":null,"rpe_actual":7.0,"hr_avg":null,"hr_peak":152,"notes":null,"user_suggestion":"rest was too easy at 60s, try 45 next time","is_skipped":false}
  ],
  "session_summary": {"rpe_actual":7.0,"notes":null,"user_suggestion":"rest was too easy at 60s, try 45 next time"}
}
"""


def parse_workout_debrief(text: str, plan: dict | None = None) -> list[ExerciseReport]:
    """Parse free-form workout debrief into structured exercise reports.

    Returns N exercise rows + 1 session_summary row.
    Raises ValidationError if Claude output is malformed.
    """
    plan_context = "(no plan available)"
    if plan:
        plan_context = (
            f"session_type={plan.get('session_type')}, "
            f"phase={plan.get('phase')}, "
            f"target_rpe={plan.get('target_rpe')}, "
            f"blocks={json.dumps(plan.get('blocks', {}))[:600]}"
        )

    system = _DEBRIEF_SYSTEM.replace("{plan_context}", plan_context)
    data = _call_claude_json(system, f"Input: {text}", max_tokens=1500)

    reports: list[ExerciseReport] = []

    # Per-exercise rows
    for ex in data.get("exercises", []):
        ex.setdefault("is_skipped", False)
        reports.append(ExerciseReport(**ex))

    # Session summary row
    summary = data.get("session_summary") or {}
    reports.append(ExerciseReport(
        exercise="session_summary",
        log_type="session_summary",
        rpe_actual=summary.get("rpe_actual"),
        notes=summary.get("notes"),
        user_suggestion=summary.get("user_suggestion"),
    ))

    return reports


# ============================================================================
# DB writers
# ============================================================================

def get_today_plan() -> dict | None:
    """Fetch today's plan from health.plan (CT date). Returns None if absent."""
    from knowledge.db import execute_one
    today = datetime.now(CT).date()
    return execute_one(
        """SELECT plan_id, plan_date, phase, week_num, session_type,
                  target_rpe, target_hr_zone, est_duration_min, blocks, is_skipped
           FROM health.plan WHERE plan_date = %s""",
        (today,),
    )


def upsert_daily_state(state: MorningState) -> None:
    """UPSERT health.daily_state for today (CT)."""
    from knowledge.db import execute_write

    today = datetime.now(CT).date()
    soreness_json = json.dumps(state.soreness) if state.soreness else None

    execute_write(
        """INSERT INTO health.daily_state
           (state_date, weight_lbs, sleep_hrs, energy, soreness, resting_hr, free_text)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
           ON CONFLICT (state_date) DO UPDATE SET
               weight_lbs = COALESCE(EXCLUDED.weight_lbs, health.daily_state.weight_lbs),
               sleep_hrs  = COALESCE(EXCLUDED.sleep_hrs,  health.daily_state.sleep_hrs),
               energy     = COALESCE(EXCLUDED.energy,     health.daily_state.energy),
               soreness   = COALESCE(EXCLUDED.soreness,   health.daily_state.soreness),
               resting_hr = COALESCE(EXCLUDED.resting_hr, health.daily_state.resting_hr),
               free_text  = COALESCE(EXCLUDED.free_text,  health.daily_state.free_text),
               logged_at  = NOW()""",
        (
            today,
            state.weight_lbs,
            state.sleep_hrs,
            state.energy,
            soreness_json,
            state.resting_hr,
            state.free_text,
        ),
    )


def insert_session_logs(reports: list[ExerciseReport], plan_id: int | None) -> int:
    """Insert N session_log rows. Returns number of rows inserted."""
    from knowledge.db import execute_write

    inserted = 0
    for r in reports:
        execute_write(
            """INSERT INTO health.session_log (
                   plan_id, log_type, exercise, reps_done, weight_lbs,
                   duration_sec, rpe_actual, hr_avg, hr_peak,
                   notes, user_suggestion, is_skipped, logged_via
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'mattermost')""",
            (
                plan_id,
                r.log_type,
                r.exercise,
                r.reps_done,
                r.weight_lbs,
                r.duration_sec,
                r.rpe_actual,
                r.hr_avg,
                r.hr_peak,
                r.notes,
                r.user_suggestion,
                r.is_skipped,
            ),
        )
        inserted += 1
    return inserted


# ============================================================================
# Confirm-back formatters (trainer voice)
# ============================================================================

def format_morning_confirm(state: MorningState) -> str:
    """Confirm-back for morning check-in. Trainer voice."""
    parts = []
    if state.sleep_hrs is not None:
        parts.append(f"{state.sleep_hrs}h sleep")
    if state.energy is not None:
        parts.append(f"energy {state.energy}/5")
    if state.soreness:
        bits = [f"{r} sore ({s})" for r, s in state.soreness.items()]
        parts.append(", ".join(bits))
    if state.resting_hr is not None:
        parts.append(f"RHR {state.resting_hr}")
    if state.weight_lbs is not None:
        parts.append(f"{state.weight_lbs}lb")

    if not parts:
        return "Logged. Add sleep/energy next time so I have something to work with."

    summary = ", ".join(parts)
    return f"Logged: {summary}. Anything to fix?"


def format_debrief_confirm(reports: list[ExerciseReport]) -> str:
    """Confirm-back for workout debrief. Trainer voice."""
    exercises = [r for r in reports if r.log_type != "session_summary"]
    summary_rows = [r for r in reports if r.log_type == "session_summary"]

    lines = [f"Logged {len(exercises)} exercise{'s' if len(exercises) != 1 else ''}:"]

    for r in exercises:
        bits = [r.exercise]
        if r.is_skipped:
            note = r.notes or "skipped"
            lines.append(f"• {r.exercise}: SKIPPED ({note})")
            continue

        details = []
        if r.reps_done is not None:
            d = f"{r.reps_done} reps"
            if r.weight_lbs is not None:
                d += f" @ {r.weight_lbs}lb"
            details.append(d)
        if r.duration_sec is not None:
            details.append(f"{r.duration_sec}s")
        if r.rpe_actual is not None:
            details.append(f"RPE {r.rpe_actual}")
        if r.hr_peak is not None:
            details.append(f"peak HR {r.hr_peak}")
        if r.hr_avg is not None:
            details.append(f"avg HR {r.hr_avg}")

        line = f"• {r.exercise}: {', '.join(details) if details else 'completed'}"
        if r.notes:
            line += f", \"{r.notes}\""
        lines.append(line)

    # Overall summary
    if summary_rows:
        s = summary_rows[0]
        if s.rpe_actual is not None:
            lines.append(f"Overall RPE {s.rpe_actual}.")
        if s.user_suggestion:
            lines.append(f"\nNoted: \"{s.user_suggestion}\"")

    # Suggestions captured on individual exercises
    suggestions = [r.user_suggestion for r in exercises if r.user_suggestion]
    for sug in suggestions:
        lines.append(f"Noted: \"{sug}\"")

    lines.append(
        "\nReply 'fix <exercise> rpe <N>' or 'good' / nothing."
    )
    return "\n".join(lines)


def format_confirmation(items) -> str:
    """Generic confirmation formatter — dispatches by type."""
    if isinstance(items, MorningState):
        return format_morning_confirm(items)
    if isinstance(items, list) and items and isinstance(items[0], ExerciseReport):
        return format_debrief_confirm(items)
    return "Logged."


# ============================================================================
# Intent handlers — wired into main.py dispatch
# ============================================================================

def _idempotency_key(message_id: str | None) -> str | None:
    """Mattermost message ID is the natural idempotency key. Used to prevent
    double-writes on retries. Returns None if no message_id provided."""
    if not message_id:
        return None
    return hashlib.sha256(message_id.encode()).hexdigest()[:16]


def handle_morning_intent(message: str, message_id: str | None = None, user_id: str | None = None) -> str:
    """Parse a morning check-in and UPSERT into health.daily_state.

    Returns the trainer-voice confirmation string. On parse failure returns
    a useful error message.
    """
    try:
        state = parse_morning_checkin(message)
    except (ValidationError, json.JSONDecodeError, anthropic.APIError) as e:
        logger.warning("Morning check-in parse failed: %s", e)
        return "I couldn't parse that. Try: 'slept 6.5, energy 3, legs sore 3'."
    except Exception:
        logger.exception("Morning check-in parse failed (unknown)")
        return "I couldn't parse that. Try: 'slept 6.5, energy 3, legs sore 3'."

    try:
        upsert_daily_state(state)
    except Exception:
        logger.exception("Failed to write daily_state")
        return "⚠️ Couldn't save morning check-in — check DB."

    return format_morning_confirm(state)


def handle_debrief_intent(message: str, message_id: str | None = None, user_id: str | None = None) -> str:
    """Parse a workout debrief and INSERT into health.session_log.

    Returns the trainer-voice confirmation string.
    """
    try:
        plan = get_today_plan()
    except Exception:
        logger.exception("Failed to fetch today's plan")
        plan = None

    try:
        reports = parse_workout_debrief(message, plan)
    except (ValidationError, json.JSONDecodeError, anthropic.APIError) as e:
        logger.warning("Debrief parse failed: %s", e)
        return "I couldn't parse that debrief. Try: 'goblet sq 10 @ 30 RPE 7, plank 30s, overall RPE 7'."
    except Exception:
        logger.exception("Debrief parse failed (unknown)")
        return "I couldn't parse that debrief. Try: 'goblet sq 10 @ 30 RPE 7, plank 30s, overall RPE 7'."

    plan_id = plan.get("plan_id") if plan else None

    try:
        insert_session_logs(reports, plan_id)
    except Exception:
        logger.exception("Failed to write session_log")
        return "⚠️ Couldn't save debrief — check DB."

    return format_debrief_confirm(reports)


# ============================================================================
# Lightweight intent detection (regex pre-router)
# ============================================================================

_MORNING_TRIGGER = re.compile(
    r"^\s*(@?artemis\s+)?(morning|checkin|check[- ]in)\b|"
    r"\b(slept|sleep)\b",
    re.IGNORECASE,
)
_DEBRIEF_TRIGGER = re.compile(
    r"^\s*(@?artemis\s+)?(done|debrief|workout\s+done|finished|that.s\s+it)\b|"
    r"\bRPE\s+\d",
    re.IGNORECASE,
)


def detect_health_intent(message: str) -> str | None:
    """Lightweight regex pre-check for health intents.

    Returns 'log_morning_state', 'log_workout_debrief', 'trainer_override',
    or None. Cheaper than calling Claude — used as a first pass before
    the main router.
    """
    # Trainer override is most specific — match first.
    if _OVERRIDE_RE.match(message):
        return INTENT_TRAINER_OVERRIDE
    # Debrief next because "done" + "RPE X" is more specific than
    # the morning trigger which catches "sleep"/"slept".
    if _DEBRIEF_TRIGGER.search(message):
        return "log_workout_debrief"
    if _MORNING_TRIGGER.search(message):
        return "log_morning_state"
    return None


# ============================================================================
# Edit / fix flow — "fix burpees rpe 9"
# ============================================================================

_FIX_RE = re.compile(
    r"^\s*fix\s+(?P<exercise>[a-zA-Z0-9 _\-]+?)\s+rpe\s+(?P<rpe>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def handle_fix_intent(message: str) -> str | None:
    """Apply a 'fix <exercise> rpe <N>' edit to the most recent matching log row.

    Returns confirmation string on match, or None if message doesn't match the
    fix grammar (so the caller can fall through to other handlers).
    """
    m = _FIX_RE.match(message)
    if not m:
        return None

    exercise = m.group("exercise").strip()
    rpe = float(m.group("rpe"))

    from knowledge.db import execute_write
    result = execute_write(
        """UPDATE health.session_log
           SET rpe_actual = %s
           WHERE log_id = (
               SELECT log_id FROM health.session_log
               WHERE LOWER(exercise) LIKE LOWER(%s)
               ORDER BY logged_at DESC
               LIMIT 1
           )
           RETURNING log_id, exercise, rpe_actual""",
        (rpe, f"%{exercise}%"),
    )

    if not result:
        return f"No recent log row for '{exercise}'. Spelling?"

    return f"Fixed {result['exercise']}: RPE → {result['rpe_actual']}."


# ============================================================================
# Nag job — runs at 23:00 CT
# ============================================================================

def run_nag_check() -> Optional[str]:
    """Check whether today's session has a debrief logged.

    If the day was a rest/skipped/walk/mobility, no nag.
    If a debrief is already logged, no nag.
    Otherwise: returns the nag message (caller posts to Mattermost).
    """
    from knowledge.db import execute_one, execute_query

    today = datetime.now(CT).date()

    plan = execute_one(
        "SELECT plan_id, session_type, target_rpe, is_skipped FROM health.plan WHERE plan_date = %s",
        (today,),
    )

    if not plan:
        # No plan for today — nothing to nag about.
        return None

    if plan["is_skipped"] or plan["session_type"] in ("rest_mobility", "walk"):
        return None

    # Check for any session_log rows tied to this plan_id
    logs = execute_query(
        "SELECT log_id FROM health.session_log WHERE plan_id = %s LIMIT 1",
        (plan["plan_id"],),
    )

    if logs:
        return None

    # No debrief logged — nag in trainer voice.
    pretty = plan["session_type"].replace("_", " ").title()
    return f"No debrief logged for today's {pretty}. How did it go?"


def insert_inferred_summary() -> bool:
    """Insert a placeholder session_summary row for TODAY if still missing.

    Called at 21:50 CT (50 min after the 21:00 nag, just before quiet hours
    start at 22:00) and operates on the current calendar day. The
    autoregulator/trends will know it's inferred via logged_via='inferred'.

    Returns True if a row was inserted, False if a real debrief already exists.
    """
    from knowledge.db import execute_one, execute_write

    today = datetime.now(CT).date()

    plan = execute_one(
        "SELECT plan_id, session_type, target_rpe, is_skipped FROM health.plan WHERE plan_date = %s",
        (today,),
    )

    if not plan or plan["is_skipped"] or plan["session_type"] in ("rest_mobility", "walk"):
        return False

    existing = execute_one(
        "SELECT log_id FROM health.session_log WHERE plan_id = %s LIMIT 1",
        (plan["plan_id"],),
    )
    if existing:
        return False

    execute_write(
        """INSERT INTO health.session_log
           (plan_id, log_type, exercise, rpe_actual, notes, logged_via)
           VALUES (%s, 'session_summary', 'inferred',
                   %s, 'no debrief — assumed at baseline', 'inferred')""",
        (plan["plan_id"], plan["target_rpe"]),
    )
    return True


# ============================================================================
# T4: Equipment & location resolver (PB-009)
# ============================================================================

# Static map of session_type → base equipment + location.
# Bike-based cardio (cardio_intervals, cardio_z2) is computed dynamically
# from weather + override; this table is for everything else.
_EQUIPMENT_MAP: dict[str, dict] = {
    "strength_a": {
        "location": "downstairs gym",
        "equipment": [
            "PowerBlock dumbbells", "flat bench", "TRX",
            "resistance bands", "exercise mat",
        ],
        "first_lift": "Goblet squat",
    },
    "strength_b": {
        "location": "downstairs gym",
        "equipment": [
            "PowerBlock dumbbells", "TRX", "resistance bands", "rower",
        ],
        "first_lift": "DB deadlift",
    },
    "strength_c": {
        "location": "downstairs gym",
        "equipment": [
            "PowerBlock dumbbells", "TRX", "exercise mat", "rower or bike",
        ],
        "first_lift": "Goblet squat",
    },
    "cardio_z2": {
        # Default; bike branch overrides location below.
        "location": "downstairs gym",
        "equipment": ["rower", "bike"],
        "first_lift": None,
    },
    "cardio_intervals": {
        # Default; bike branch overrides location below.
        "location": "downstairs gym (treadmill)",
        "equipment": ["treadmill"],
        "first_lift": None,
    },
    "walk": {
        "location": "outside",
        "equipment": ["walking shoes"],
        "first_lift": None,
    },
    "rest_mobility": {
        "location": "anywhere — living room is fine",
        "equipment": ["mat", "resistance bands"],
        "first_lift": None,
    },
}

# Bike-based sessions — these consult weather/override for location.
_BIKE_SESSIONS = {"cardio_z2", "cardio_intervals"}


def resolve_equipment_and_location(
    session_type: str,
    weather: dict | None = None,
    user_override: str | None = None,
) -> dict:
    """Return {'location': str, 'equipment': list[str], 'notes': str | None,
                'first_lift': str | None}.

    For bike-based sessions (cardio_z2, cardio_intervals) the location is
    chosen by:
        1. user_override='indoor' or 'outdoor' wins outright (with note)
        2. otherwise: temp_f < 40 OR precip_next_90min → indoor
        3. otherwise → outdoor

    Pure function. No I/O. Caller passes weather + override in.
    """
    base = _EQUIPMENT_MAP.get(session_type)
    if not base:
        # Unknown session type — return safe defaults rather than raise
        return {
            "location": "downstairs gym",
            "equipment": [],
            "notes": f"Unknown session_type '{session_type}'.",
            "first_lift": None,
        }

    result = {
        "location": base["location"],
        "equipment": list(base["equipment"]),
        "first_lift": base.get("first_lift"),
        "notes": None,
    }

    # Walk/strength/rest don't need indoor/outdoor reasoning
    if session_type not in _BIKE_SESSIONS:
        return result

    # Bike branch: override > weather
    if user_override == "indoor":
        result["location"] = "downstairs gym (bike on trainer)"
        result["equipment"] = ["bike on trainer", "fan", "towel"]
        result["notes"] = "Per your override: indoor."
        return result

    if user_override == "outdoor":
        result["location"] = "outside (road bike)"
        result["equipment"] = ["road bike", "helmet", "water bottle"]
        result["notes"] = "Per your override: outdoor."
        return result

    # No override — consult weather (or safe default if unavailable)
    w = weather or {}
    temp_f = w.get("temp_f", 50.0)
    precip = bool(w.get("precip_next_90min", False))

    if precip:
        result["location"] = "downstairs gym (bike on trainer)"
        result["equipment"] = ["bike on trainer", "fan", "towel"]
        result["notes"] = "Rain expected in next 90 min — indoor."
    elif temp_f < 40:
        result["location"] = "downstairs gym (bike on trainer)"
        result["equipment"] = ["bike on trainer", "fan", "towel"]
        result["notes"] = f"Cold ({temp_f:.0f}°F) — indoor."
    else:
        result["location"] = "outside (road bike)"
        result["equipment"] = ["road bike", "helmet", "water bottle"]
        result["notes"] = f"Clear and {temp_f:.0f}°F — outside."

    return result


# ============================================================================
# T4: Trainer override capture
# ============================================================================

INTENT_TRAINER_OVERRIDE = "trainer_override"

_OVERRIDE_RE = re.compile(
    r"^\s*(?:@?artemis\s+)?trainer\s+set\s+(?P<mode>indoor|outdoor)\s*$",
    re.IGNORECASE,
)


def _next_cardio_date(today: date | None = None) -> date:
    """Return the date of the next plan row whose session_type is cardio.

    Looks forward up to 14 days from `today` (default = now in CT). If no
    cardio session is found in that window, returns tomorrow as a fallback.
    """
    from knowledge.db import execute_one

    base = today or datetime.now(CT).date()
    row = execute_one(
        """SELECT plan_date FROM health.plan
           WHERE plan_date >= %s
             AND session_type IN ('cardio_intervals', 'cardio_z2')
           ORDER BY plan_date
           LIMIT 1""",
        (base,),
    )
    if row and row.get("plan_date"):
        return row["plan_date"]
    # Fallback: tomorrow
    return base + timedelta(days=1)


def detect_trainer_override(text: str) -> str | None:
    """Returns 'indoor' or 'outdoor' if the text matches; else None."""
    m = _OVERRIDE_RE.match(text)
    if not m:
        return None
    return m.group("mode").lower()


def write_bike_override(target_date: date, mode: str) -> None:
    """Stash bike_setup_override inside health.plan.blocks JSONB for target_date.

    Uses jsonb_set so we don't clobber the rest of the blocks shape. If no
    plan row exists for target_date (shouldn't happen for seeded dates), this
    is a no-op silently.
    """
    from knowledge.db import execute_write

    execute_write(
        """UPDATE health.plan
           SET blocks = jsonb_set(
               COALESCE(blocks, '{}'::jsonb),
               '{bike_setup_override}',
               to_jsonb(%s::text)
           )
           WHERE plan_date = %s""",
        (mode, target_date),
    )


def read_bike_override(target_date: date) -> str | None:
    """Read bike_setup_override from health.plan.blocks for target_date.

    Returns 'indoor', 'outdoor', or None.
    """
    from knowledge.db import execute_one

    row = execute_one(
        "SELECT blocks FROM health.plan WHERE plan_date = %s",
        (target_date,),
    )
    if not row:
        return None
    blocks = row.get("blocks") or {}
    val = blocks.get("bike_setup_override")
    if val in ("indoor", "outdoor"):
        return val
    return None


def handle_trainer_override(message: str, message_id: str | None = None,
                             user_id: str | None = None) -> str:
    """Parse 'trainer set indoor/outdoor' and write to next cardio plan row.

    Always targets the *next* cardio workout from now forward (today included
    if today is cardio). Per PB-009: morning workout days (Tue/Thu/Fri 04:01)
    pick up overrides written the prior evening; evening workout days
    (Wed/Sat 16:30) pick up same-day overrides written before the prompt fires.

    Returns trainer-voice confirm string.
    """
    mode = detect_trainer_override(message)
    if mode is None:
        # Caller should have pre-checked, but be defensive.
        return "I couldn't parse that. Try: 'trainer set indoor' or 'trainer set outdoor'."

    target_date = _next_cardio_date()
    try:
        write_bike_override(target_date, mode)
    except Exception:
        logger.exception("Failed to write bike override")
        return "⚠️ Couldn't save override — check DB."

    nice_date = target_date.strftime("%a %b %-d") if hasattr(target_date, "strftime") else str(target_date)
    return f"Got it — bike on trainer set {mode} for {nice_date}."


# ============================================================================
# T4: Prompt builders (trainer voice)
# ============================================================================

def _session_pretty_name(session_type: str) -> str:
    return {
        "strength_a":       "Strength A — Push/Legs",
        "strength_b":       "Strength B — Pull/Hinge",
        "strength_c":       "Strength C — Full Body",
        "cardio_intervals": "Cardio Intervals",
        "cardio_z2":        "Cardio Zone 2",
        "walk":             "Walk + mobility",
        "rest_mobility":    "Rest / Mobility",
    }.get(session_type, session_type)


_SURVEY_QUESTIONS = (
    "Reply with: sleep hrs, energy 1-5, soreness (region 1-5), weight if you weighed, RHR if you took it.\n"
    "Example: `slept 6.5 energy 3 legs sore 3 weight 271 RHR 58`"
)


def build_morning_survey_prompt(plan: dict, prompt_type: str) -> str:
    """Builds the morning prompt text. prompt_type ∈ {'workout_am', 'logging_only'}.

    workout_am: full survey + heads-up that the calibrated plan arrives in 15 min.
    logging_only: just the survey; the workout is later in the day.
    """
    session = _session_pretty_name(plan.get("session_type", "?"))
    duration = plan.get("est_duration_min")
    duration_str = f" — {duration} min" if duration else ""

    if prompt_type == "logging_only":
        return (
            f"Morning. Today's workout is later: **{session}**{duration_str}.\n"
            f"For now: morning check-in.\n\n"
            f"{_SURVEY_QUESTIONS}"
        )

    # workout_am
    return (
        f"Morning. Today: **{session}**{duration_str}.\n\n"
        f"{_SURVEY_QUESTIONS}\n\n"
        f"_Calibrated plan in ~15 min once you reply._"
    )


def build_evening_prompt(plan: dict, resolved: dict) -> str:
    """Build the evening pre-workout prompt for Wed/Sat 16:30."""
    session = _session_pretty_name(plan.get("session_type", "?"))
    duration = plan.get("est_duration_min")
    duration_str = f" — {duration} min" if duration else ""

    lines = [
        f"Evening. Tonight: **{session}**{duration_str}.",
        f"Where: {resolved['location']}",
    ]
    if resolved["equipment"]:
        lines.append(f"Bring: {', '.join(resolved['equipment'])}")
    if resolved.get("first_lift"):
        lines.append(f"First lift: {resolved['first_lift']}")
    if resolved.get("notes"):
        lines.append(f"_{resolved['notes']}_")
    lines.append("gym.rdm.is is up — full plan there.")
    return "\n".join(lines)


def build_calibrated_plan_post(plan: dict, resolved: dict, state: dict | None) -> str:
    """Build the trainer-voice calibrated plan post that follows morning survey
    by ~15 minutes."""
    session = _session_pretty_name(plan.get("session_type", "?"))
    duration = plan.get("est_duration_min")
    duration_str = f" — {duration} min" if duration else ""

    lines = [f"Today: **{session}**{duration_str}.", f"Where: {resolved['location']}"]
    if resolved["equipment"]:
        lines.append(f"Bring: {', '.join(resolved['equipment'])}")
    if resolved.get("first_lift"):
        lines.append(f"First lift: {resolved['first_lift']}")
    blocks = plan.get("blocks") or {}
    if blocks.get("warmup"):
        lines.append(f"Warmup: {blocks['warmup']}")
    if resolved.get("notes"):
        lines.append(f"_{resolved['notes']}_")

    # Recovery-day override notice if morning state suggests it
    if state:
        sleep = state.get("sleep_hrs")
        energy = state.get("energy")
        if (sleep is not None and sleep < 5) or (energy is not None and energy <= 2):
            lines.insert(0, "**Recovery override.** Sleep low or energy low. Today drops to mobility + walk.")

    lines.append("gym.rdm.is is up — full plan there.")
    return "\n".join(lines)


# ============================================================================
# T4: Idempotency for proactive prompts
# ============================================================================

def already_prompted_today(slot: str, today: date | None = None) -> bool:
    """Check whether a given prompt slot already fired today.

    slot: 'morning', 'morning_calibration', 'evening' — namespaced per day.
    Uses the existing system_state KV (artemis.quiet_hours.set_system_value)
    which is already used for catch-up prevention elsewhere.

    Returns True if the slot has fired today (so caller should skip).
    """
    from artemis.quiet_hours import get_system_value

    d = today or datetime.now(CT).date()
    key = f"health_prompt:{slot}:{d.isoformat()}"
    return bool(get_system_value(key))


def mark_prompted(slot: str, today: date | None = None) -> None:
    """Record that a given prompt slot has fired today."""
    from artemis.quiet_hours import set_system_value

    d = today or datetime.now(CT).date()
    key = f"health_prompt:{slot}:{d.isoformat()}"
    set_system_value(key, datetime.now(CT).isoformat())


def get_today_state() -> dict | None:
    """Fetch today's morning daily_state row (CT). Returns None if absent."""
    from knowledge.db import execute_one
    today = datetime.now(CT).date()
    return execute_one(
        """SELECT state_date, weight_lbs, sleep_hrs, energy, soreness,
                  resting_hr, free_text
           FROM health.daily_state WHERE state_date = %s""",
        (today,),
    )
