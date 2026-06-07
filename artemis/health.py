"""Personal training intents — morning check-in + workout debrief.

Two Mattermost intents:
  log_morning_state    → UPSERT health.daily_state for today (CT)
  log_workout_debrief  → INSERT N exercise rows + 1 summary row in health.session_log

Both pass user-facing output through the trainer voice template:
short, direct, no fluff, no shame, no fake hype.

The autoregulator is downstream — these handlers ONLY write data.
"""

import difflib
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


def _call_claude_text(system: str, user_msg: str, max_tokens: int = 200) -> str:
    """Call Claude with a system prompt that emits plain text. Returns the text.

    Same trainer-voice LLM path used elsewhere; raises on any API/auth failure so
    callers can degrade gracefully (e.g. return the structured render alone)."""
    client = _get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


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

# PB-009 plan-lookup intent — READ-intent for "what's the plan" style questions.
# Deliberately requires a plan/workout/schedule noun so it never swallows a
# debrief ("done", "RPE 7"), a morning check-in, or unrelated chatter.
INTENT_PLAN_LOOKUP = "plan_lookup"

_PLAN_NOUN = r"(?:workouts?|sessions?|plan|schedule|training|lift(?:s|ing)?)"
_WEEKDAY_WORD = r"(?:mon|tues?|wednes|thurs?|fri|satur|sun)day"
_PLAN_LOOKUP_RE = re.compile(
    # "show my plan", "what's tomorrow's workout", "what's my workout monday"
    rf"\b(?:show|see|what'?s|what\s+is|whats)\b.*\b{_PLAN_NOUN}\b"
    # "<plan-noun> tomorrow/today/this week/next N days/<weekday>"
    rf"|\b{_PLAN_NOUN}\b.*\b(?:tomorrow|today|tonight|this\s+week|next\s+\d+\s+days?|{_WEEKDAY_WORD})\b"
    # "tomorrow's/this week's <plan-noun>"
    rf"|\b(?:tomorrow|today|tonight|this\s+week|next\s+\d+\s+days?|{_WEEKDAY_WORD})('?s)?\b.*\b{_PLAN_NOUN}\b"
    # bare ranges that only make sense as a plan lookup here
    rf"|\bnext\s+\d+\s+days?\b"
    rf"|\bthis\s+week'?s?\s+{_PLAN_NOUN}\b",
    re.IGNORECASE,
)

# PB-009 plan-DETAIL intent — single-day DEPTH ("explain the whole session"),
# as opposed to plan_lookup which is multi-day BREADTH ("list the next 7 days").
INTENT_PLAN_DETAIL = "plan_detail"

# Depth cues → user wants the full session broken down. Includes "tell me more /
# more about / expand / what exactly" so follow-up depth asks are caught.
_DETAIL_CUE = (
    r"(?:detail(?:ed|s)?|full|complete|whole|entire|break[\s-]*down|"
    r"explain(?:ed)?|explanation|walk\s+me\s+through|in[\s-]?depth|"
    r"deep\s+dive|elaborate|everything|expand|more\s+detail|tell\s+me\s+more|"
    r"more\s+about|what\s+exactly|spell\s+out)"
)
# Breadth cues → a multi-day list; these stay plan_lookup (depth never applies).
_MULTIDAY_RE = re.compile(
    r"\b(?:next\s+\d+\s+days?|this\s+week|the\s+week|coming\s+days?|"
    r"rest\s+of\s+the\s+week|\d+\s+days)\b",
    re.IGNORECASE,
)
_SINGLE_DAY_REF = rf"(?:today|tonight|tomorrow|{_WEEKDAY_WORD})"
# Explicit depth request tied to a session / day / plan noun.
_PLAN_DETAIL_RE = re.compile(
    rf"\b{_DETAIL_CUE}\b.*\b(?:{_PLAN_NOUN}|session|{_SINGLE_DAY_REF})\b"
    rf"|\b(?:{_PLAN_NOUN}|session|{_SINGLE_DAY_REF})\b.*\b{_DETAIL_CUE}\b",
    re.IGNORECASE,
)
# A single-day plan request with no breadth cue → defaults to DEPTH (per spec:
# a bare "today's workout" should return the full session, not a one-liner).
_SINGLE_DAY_PLAN_RE = re.compile(
    rf"\b{_SINGLE_DAY_REF}\b.*\b(?:{_PLAN_NOUN}|session)\b"
    rf"|\b(?:{_PLAN_NOUN}|session)\b.*\b{_SINGLE_DAY_REF}\b",
    re.IGNORECASE,
)
# META / DATABASE asks about the training data store. These must read health.plan
# — NEVER fall through to general_reply (which confabulates "I have no workout DB").
_PLAN_META_RE = re.compile(
    r"\bdeep\s+quer(?:y|ies)\b"
    r"|\bquer(?:y|ies)\b.*\b(?:workout|training|plan|session|routine)\b"
    r"|\b(?:workout|training|plan|session|routine)s?\s+(?:database|table|data)\b"
    r"|\b(?:pull|read|look|get)\b.*\b(?:workout|training|plan)\b.*\b(?:database|table|data|db)\b"
    r"|\bpull\s+from\s+the\s+database\b"
    r"|\bwhat\s+data\s+do\s+you\s+have\b.*\b(?:training|workout|plan|exercise|fitness)\b"
    r"|\bwhat'?s\s+in\s+the\s+(?:plan|workout|training)\b",
    re.IGNORECASE,
)
# Plan/exercise terms used by the (tightened) data-retrieval fallback. Strong
# fitness terms only — no bare "plan/session/sets/reps/lift" that would hijack
# calendar / CRM / "set up a meeting".
_PLAN_TERM_BODY = (
    r"work\s?outs?|exercises?|cardio|routines?|training|program|"
    r"warm\s?ups?|cool\s?downs?|circuits?|intervals?|finishers?|"
    r"deadlift|goblet|squat|lunge|rdl|kettlebell|dumbbell|powerblock|rower|treadmill|"
    r"run[\s-]?walk|rest\s+day|zone\s+\d|z2|workout\s+plan|training\s+plan"
)
# Retrieval signals: possessive/demonstrative or a retrieval verb adjacent to a
# plan/exercise term — i.e. the user wants the DATA, not a discussion.
_PLAN_POSSESS = r"my|the|today'?s|tonight'?s|tomorrow'?s|this"
_PLAN_RETRIEVAL_VERB = (
    r"show|pull(?:\s+up)?|give\s+me|tell\s+me(?:\s+about)?|what'?s\s+the|"
    r"what\s+is\s+the|list|bring\s+up|look\s+up|break\s*down|run\s+down|breakdown\s+of"
)
_PLAN_RETRIEVAL_RE = re.compile(
    rf"\b(?:{_PLAN_POSSESS})\s+(?:\w+\s+){{0,2}}(?:{_PLAN_TERM_BODY})\b"        # "my cardio", "the deadlift weight", "today's session"
    rf"|\b(?:{_PLAN_RETRIEVAL_VERB})\b.*\b(?:{_PLAN_TERM_BODY})\b"             # "tell me about my cardio", "what's the deadlift weight"
    rf"|\b(?:{_PLAN_RETRIEVAL_VERB})\s+(?:me\s+|us\s+|up\s+)?(?:today|tonight|tomorrow)(?:'s)?\s*[.!?]*$",  # "show me today" (day at end, not "today's calendar")
    re.IGNORECASE,
)
# Conceptual / progress / state markers. When present, the message is a question
# or opinion (not a data pull), so it must reach the (now plan-aware)
# general_reply trainer-voice path — NOT plan_detail.
_CONCEPTUAL_RE = re.compile(
    r"\b(?:why|how'?s|how\s+is|how\s+are|how\s+am|how\s+many|how\s+much|"
    r"better\s+than|worse\s+than|should\s+i|vs\.?|versus|feels?\s+like|feeling\s+like|"
    r"going|on\s+track|worth\s+it|matters?|important|explain|difference|over\s?train\w*|"
    r"sore|wrecked|hurts?|tired|exhausted|"
    r"is\s+it\s+(?:ok|okay|normal|good|bad|fine))\b",
    re.IGNORECASE,
)


def detect_health_intent(message: str) -> str | None:
    """Lightweight regex pre-check for health intents.

    Returns 'plan_detail', 'plan_lookup', 'log_morning_state',
    'log_workout_debrief', 'trainer_override', or None. Cheaper than calling
    Claude — used as a first pass before the main router.
    """
    # Trainer override is most specific — match first.
    if _OVERRIDE_RE.match(message):
        return INTENT_TRAINER_OVERRIDE

    # Depth vs breadth: a multi-day request is always breadth (plan_lookup). A
    # single-day request — explicit detail OR a bare "today's workout" — is
    # depth (plan_detail). Depth is checked first so single-day wins.
    if not _MULTIDAY_RE.search(message):
        if _PLAN_DETAIL_RE.search(message) or _SINGLE_DAY_PLAN_RE.search(message):
            return INTENT_PLAN_DETAIL

    # Meta / database asks ("deep query the workout database") → read health.plan.
    if _PLAN_META_RE.search(message):
        return INTENT_PLAN_DETAIL

    # Plan-lookup (breadth read-intent): requires a plan noun plus a temporal/
    # show cue, so it can't collide with "done"/"RPE"/"slept".
    if _PLAN_LOOKUP_RE.search(message):
        return INTENT_PLAN_LOOKUP
    # Debrief next because "done" + "RPE X" is more specific than
    # the morning trigger which catches "sleep"/"slept".
    if _DEBRIEF_TRIGGER.search(message):
        return "log_workout_debrief"
    if _MORNING_TRIGGER.search(message):
        return "log_morning_state"

    # DATA-RETRIEVAL fallback (tightened — Option C): only a message that wants
    # the plan DATA (possessive/retrieval signal + a plan term, and NOT a
    # conceptual/progress/state question) routes to plan_detail. Conceptual asks
    # ("why is zone 2 important", "how's my training going", "is the rower better
    # than running") fall through to the now plan-aware general_reply path. The
    # scrub_db_denial output guard remains the anti-denial backstop.
    if not _CONCEPTUAL_RE.search(message) and _PLAN_RETRIEVAL_RE.search(message):
        return INTENT_PLAN_DETAIL
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
# Cardio sessions (cardio_intervals, cardio_z2) and walk consult weather +
# override dynamically below; the static base lists below describe the
# non-bike-decision equipment that's always relevant.
#
# Equipment inventory (canonical, from PB-009):
#   - Road bike with indoor trainer (whatever is set at 04:00 wins)
#   - Water rower
#   - Walking pad (lives in home office, movable to gym)
#   - TRX bands, exercise ball, yoga mat, resistance bands w/ anchors
#   - PowerBlock dumbbells, curl bar (2x 10# + 2x 25# plates), flat bench
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
        # Z2 = sustained low-intensity. Walking pad is appropriate here
        # (low-impact, well-suited for Z2 pace). Bike on trainer is the
        # default; pad is the alternative the user can pick at workout time.
        "location": "downstairs gym",
        "equipment": ["bike on trainer", "walking pad"],
        "first_lift": None,
    },
    "cardio_intervals": {
        # Intervals require real intensity bursts. Walking pad is NOT
        # appropriate here. Choices are water rower OR bike on trainer
        # (intervals); user picks at workout time.
        "location": "downstairs gym",
        "equipment": ["water rower", "bike on trainer (intervals)"],
        "first_lift": None,
    },
    "walk": {
        # Default = outside. Weather branch swaps to indoor walking pad
        # when cold (<40°F) or rainy.
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

# Sessions whose bike sub-equipment is decided by weather + user_override.
# (cardio_intervals and cardio_z2 both have a bike option in their list.)
_BIKE_SESSIONS = {"cardio_z2", "cardio_intervals"}

# Per-session-type "alternative" equipment that's preserved in the final
# list even after the bike branch decides indoor/outdoor. The user picks
# bike vs alternative at workout time — both surfaces in the calibrated post.
_BIKE_ALTERNATIVE = {
    "cardio_intervals": "water rower",
    "cardio_z2": "walking pad",
}


def _blocks_use_bike(blocks) -> bool:
    """True if a blocks payload describes an actual bike session.

    Discriminator: equipment mentions a bike. Empty equipment with a run/walk
    display_name is explicitly NOT a bike session (Sunday run-walk)."""
    b = _coerce_blocks(blocks)
    equip = b.get("equipment") or []
    if any("bike" in str(e).lower() for e in equip):
        return True
    if not equip:
        dn = str(b.get("display_name") or "").lower()
        return not ("run" in dn or "walk" in dn)
    return False


def is_bike_session(plan: dict) -> bool:
    """True only for cardio sessions that actually use a bike.

    Sat (long Z2 ride) and Sun (run-walk) both map to session_type='cardio_z2',
    so weather / indoor-outdoor resolution must key on the blocks payload, not
    session_type alone — otherwise Sunday's run-walk would wrongly get
    bike-weather handling. Used by the scheduler to gate override + weather.
    """
    if (plan.get("session_type") or "") not in _BIKE_SESSIONS:
        return False
    return _blocks_use_bike(plan.get("blocks"))


def resolve_equipment_and_location(
    session_type: str,
    weather: dict | None = None,
    user_override: str | None = None,
    blocks: dict | None = None,
) -> dict:
    """Return {'location': str, 'equipment': list[str], 'notes': str | None,
                'first_lift': str | None}.

    For bike-based sessions (cardio_z2, cardio_intervals) the bike's
    indoor/outdoor location is chosen by:
        1. user_override='indoor' or 'outdoor' wins outright (with note)
        2. otherwise: temp_f < 40 OR precip_next_90min → indoor
        3. otherwise → outdoor
    The non-bike alternative (water rower for intervals, walking pad for Z2)
    is always preserved in the equipment list — user picks at workout time.

    For walk: weather alone drives the indoor (walking pad) vs outdoor
    decision. user_override does NOT apply to walks.

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

    # ── Walk branch: weather-driven indoor swap ────────────────────────
    if session_type == "walk":
        w = weather or {}
        temp_f = w.get("temp_f", 50.0)
        precip = bool(w.get("precip_next_90min", False))
        if precip:
            result["location"] = "downstairs gym (walking pad)"
            result["equipment"] = ["walking pad"]
            result["notes"] = "Rain expected — walking pad indoor."
        elif temp_f < 40:
            result["location"] = "downstairs gym (walking pad)"
            result["equipment"] = ["walking pad"]
            result["notes"] = f"Cold ({temp_f:.0f}°F) — walking pad indoor."
        return result

    # Strength / rest_mobility — no weather logic
    if session_type not in _BIKE_SESSIONS:
        return result

    # ── Non-bike cardio guard (PB-009) ─────────────────────────────────
    # A cardio_z2/cardio_intervals row whose blocks don't use a bike (e.g.
    # Sunday run-walk, now mapped to cardio_z2) must NOT get bike/weather
    # handling. Only applies when blocks are provided (legacy callers that pass
    # no blocks keep the original bike behavior).
    if blocks is not None and not _blocks_use_bike(blocks):
        b = _coerce_blocks(blocks)
        return {
            "location": "outside (run-walk)",
            "equipment": list(b.get("equipment") or []),
            "first_lift": None,
            "notes": "Run-walk session — outdoor; weather/bike setup not applicable.",
        }

    # ── Bike branch (cardio_intervals, cardio_z2) ──────────────────────
    # Preserve the non-bike alternative (water rower / walking pad)
    # alongside whichever bike configuration the override/weather picks.
    alt = _BIKE_ALTERNATIVE.get(session_type)

    def _with_alt(bike_equip: list[str]) -> list[str]:
        return ([alt] if alt else []) + bike_equip

    if user_override == "indoor":
        result["location"] = "downstairs gym (bike on trainer)"
        result["equipment"] = _with_alt(["bike on trainer", "fan", "towel"])
        result["notes"] = "Per your override: indoor."
        return result

    if user_override == "outdoor":
        result["location"] = "outside (road bike)"
        result["equipment"] = _with_alt(["road bike", "helmet", "water bottle"])
        result["notes"] = "Per your override: outdoor."
        return result

    # No override — consult weather (or safe default if unavailable)
    w = weather or {}
    temp_f = w.get("temp_f", 50.0)
    precip = bool(w.get("precip_next_90min", False))

    if precip:
        result["location"] = "downstairs gym (bike on trainer)"
        result["equipment"] = _with_alt(["bike on trainer", "fan", "towel"])
        result["notes"] = "Rain expected in next 90 min — indoor."
    elif temp_f < 40:
        result["location"] = "downstairs gym (bike on trainer)"
        result["equipment"] = _with_alt(["bike on trainer", "fan", "towel"])
        result["notes"] = f"Cold ({temp_f:.0f}°F) — indoor."
    else:
        result["location"] = "outside (road bike)"
        result["equipment"] = _with_alt(["road bike", "helmet", "water bottle"])
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


# ============================================================================
# PB-009 — Conversational workout session loop
#
# Build 2: session-state cursor (no new table)
# Build 3: conversational loop (log a set → confirm → next exercise → suggestion)
# Build 4: dictation-tolerant single-set parser
# Build 5: history Q&A (handle_plan_query)
#
# Hard constraints honored: only existing health.plan / health.session_log
# columns, no migrations, no schema changes. The legacy life_ops SQLite path
# is never touched — main.py routes these handlers ahead of _try_life_ops.
# ============================================================================

# Session-types that are NOT loggable lifting sessions (no conversational loop).
_NON_WORKOUT_SESSIONS = ("rest_mobility", "walk")


# ----------------------------------------------------------------------------
# Build 2a — session-state marker
#
# "Active" = today's plan is a real workout AND "let's workout" was said AND
# "done" not yet said. We persist that fact in the existing system_state KV
# (artemis.quiet_hours.get/set_system_value) rather than an in-memory dict.
#
# Justification: the deploy procedure runs `sudo systemctl restart acos`, which
# would wipe any in-process state mid-session. system_state is a durable SQLite
# KV already used for per-day prompt idempotency (already_prompted_today), so it
# survives restarts, needs no migration, and is naturally namespaced per day.
# ----------------------------------------------------------------------------

def _session_state_key(d: date | None = None) -> str:
    d = d or datetime.now(CT).date()
    return f"health_session:{d.isoformat()}"


def _get_session_marker(d: date | None = None) -> str | None:
    """Return 'active', 'ended', or None for the given day (default today CT)."""
    from artemis.quiet_hours import get_system_value
    return get_system_value(_session_state_key(d))


def _set_session_marker(value: str, d: date | None = None) -> None:
    from artemis.quiet_hours import set_system_value
    set_system_value(_session_state_key(d), value)


def get_active_session() -> dict | None:
    """Return today's plan row IFF a workout session is currently active.

    Active requires all of:
      - today's plan exists, is not skipped, and is a real workout
        (session_type not in rest_mobility / walk), AND
      - the session marker for today is 'active' (set by "let's workout",
        cleared to 'ended' by "done").

    Returns the plan dict (so callers get plan_id + blocks) or None.
    """
    plan = get_today_plan()
    if not plan:
        return None
    if plan.get("is_skipped") or plan.get("session_type") in _NON_WORKOUT_SESSIONS:
        return None
    if _get_session_marker() == "active":
        return plan
    return None


# ----------------------------------------------------------------------------
# Build 2b — reading the plan's ordered exercise list (real blocks shape)
# ----------------------------------------------------------------------------

def _plan_exercises(plan: dict) -> list[dict]:
    """Return the ordered exercise dicts from plan.blocks.

    Strength/circuit days carry blocks['exercises']. cardio_intervals has no
    top-level list — its only per-exercise list is blocks['finisher']['exercises'].
    We read the real shape rather than assume one.
    """
    blocks = plan.get("blocks")
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except (ValueError, TypeError):
            blocks = {}
    blocks = blocks or {}
    exercises = blocks.get("exercises")
    if isinstance(exercises, list) and exercises:
        return [e for e in exercises if isinstance(e, dict)]
    finisher = blocks.get("finisher")
    if isinstance(finisher, dict) and isinstance(finisher.get("exercises"), list):
        return [e for e in finisher["exercises"] if isinstance(e, dict)]
    return []


def _plan_exercise_names(plan: dict) -> list[str]:
    return [str(e.get("name")) for e in _plan_exercises(plan) if e.get("name")]


def _is_plan_exercise(name: str | None, plan: dict) -> bool:
    """True if `name` is one of today's plan exercises (not an off-plan literal)."""
    if not name:
        return False
    return name.lower() in {n.lower() for n in _plan_exercise_names(plan)}


def _exercise_target(ex: dict) -> dict:
    """Normalize a plan exercise dict to the fields the loop needs."""
    return {
        "name": str(ex.get("name", "")).strip(),
        "format": ex.get("format"),
        "target_reps": ex.get("target_reps"),
        "target_load_lbs": ex.get("target_load_lbs"),
        "duration_sec": ex.get("duration_sec"),
    }


def _logged_exercise_names_today(plan_id: int) -> set[str]:
    """Lowercased exercise names that already have >=1 logged set today."""
    from knowledge.db import execute_query
    rows = execute_query(
        """SELECT DISTINCT LOWER(exercise) AS ex
           FROM health.session_log
           WHERE plan_id = %s AND log_type IN ('strength_set', 'cardio_block')""",
        (plan_id,),
    )
    return {r["ex"] for r in rows if r.get("ex")}


def _name_is_logged(name: str, logged: set[str]) -> bool:
    ln = name.lower().strip()
    for g in logged:
        if g == ln or g in ln or ln in g:
            return True
    return False


def next_exercise(plan: dict) -> dict | None:
    """First exercise in plan.blocks (in order) with zero session_log rows today.

    Returns {name, format, target_reps, target_load_lbs, duration_sec} or None
    when every exercise on the card has at least one logged set.
    """
    exercises = _plan_exercises(plan)
    if not exercises:
        return None
    logged = _logged_exercise_names_today(plan["plan_id"])
    for ex in exercises:
        name = str(ex.get("name", "")).strip()
        if name and not _name_is_logged(name, logged):
            return _exercise_target(ex)
    return None


# ----------------------------------------------------------------------------
# Build 2c — last_time(): prior history for an exercise
# ----------------------------------------------------------------------------

def last_time(exercise: str, session_type: str | None = None) -> dict | None:
    """Most recent PRIOR (before today) session_log rows for an exercise.

    Returns {'plan_date': date, 'sets': [rows]} for the latest day that
    exercise was logged, or None if never logged before today.

    session_type is accepted for context and used as a *soft* filter: we try
    to scope to the same session_type first, then fall back to any session_type.
    The same lift (e.g. Goblet squat) appears in multiple session_types, and the
    genuinely useful answer to "what did I do last time" is the true last time —
    so we don't hard-restrict to one session_type.
    """
    from knowledge.db import execute_query
    today = datetime.now(CT).date()

    base_sql = """
        SELECT p.plan_date, p.session_type, sl.set_num, sl.reps_done,
               sl.weight_lbs, sl.rpe_actual, sl.duration_sec
        FROM health.session_log sl
        JOIN health.plan p ON sl.plan_id = p.plan_id
        WHERE LOWER(sl.exercise) LIKE LOWER(%s)
          AND p.plan_date < %s
          AND sl.is_skipped = FALSE
          AND sl.log_type IN ('strength_set', 'cardio_block')
        {extra}
        ORDER BY p.plan_date DESC, sl.set_num ASC NULLS LAST
    """
    like = f"%{exercise.strip()}%"

    rows = []
    if session_type:
        rows = execute_query(
            base_sql.format(extra="AND p.session_type = %s"),
            (like, today, session_type),
        )
    if not rows:
        rows = execute_query(base_sql.format(extra=""), (like, today))
    if not rows:
        return None

    recent_date = rows[0]["plan_date"]
    sets = [r for r in rows if r["plan_date"] == recent_date]
    return {"plan_date": recent_date, "sets": sets}


# ----------------------------------------------------------------------------
# Build 4 — dictation-tolerant single-set parser
# ----------------------------------------------------------------------------

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
# Homophones speech-to-text commonly emits for RPE values.
_RPE_VALUE_WORDS = dict(_NUM_WORDS)
_RPE_VALUE_WORDS.update({"for": 4, "to": 2, "too": 2, "tu": 2, "ate": 8, "won": 1})

# Words that are never part of an exercise name once numbers/units are stripped.
_SET_STOPWORDS = {
    "at", "for", "the", "a", "an", "did", "i", "and", "with", "of", "got", "do",
    "done", "just", "reps", "rep", "lbs", "lb", "pounds", "pound", "rpe", "set",
    "sets", "x", "to", "on", "was", "were", "felt", "feeling", "that", "then",
    "round", "rounds",
}


def _words_to_numbers(text: str) -> str:
    """Convert spelled cardinal numbers (incl. compounds like 'twenty five') to
    digits. Leaves non-number words untouched."""
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        bare = words[i].strip(",.!?;:")
        if bare in _TENS_WORDS:
            val = _TENS_WORDS[bare]
            if i + 1 < len(words):
                nxt = words[i + 1].strip(",.!?;:")
                if nxt in _NUM_WORDS and 1 <= _NUM_WORDS[nxt] <= 9:
                    val += _NUM_WORDS[nxt]
                    out.append(str(val))
                    i += 2
                    continue
            out.append(str(val))
            i += 1
            continue
        if bare in _NUM_WORDS:
            out.append(str(_NUM_WORDS[bare]))
            i += 1
            continue
        out.append(words[i])
        i += 1
    return " ".join(out)


def _pop_rpe(text: str) -> tuple[float | None, str]:
    """Extract RPE, absorbing dictation noise ('rpe four', 'RP4', 'are pee 4',
    'r p e 4'). Returns (rpe_or_None, text_with_rpe_removed)."""
    t = text
    # Normalize spelled-out RPE markers to the token 'rpe'.
    t = re.sub(r"\bare\s+pee\b", "rpe", t)
    t = re.sub(r"\br\s*\.?\s*p\s*\.?\s*e\b", "rpe", t)   # 'r p e' / 'r.p.e'
    t = re.sub(r"\barpe\b", "rpe", t)
    t = re.sub(r"\brp(?=\s*\d)", "rpe ", t)              # 'RP4' / 'rp 4'

    m = re.search(r"\brpe\b\s*(?:of|is|at|was|=|:)?\s*([0-9]+(?:\.[0-9]+)?|[a-z]+)", t)
    if not m:
        return None, text

    tok = m.group(1)
    val: float | None = None
    if tok[0].isdigit():
        try:
            val = float(tok)
        except ValueError:
            val = None
    elif tok in _RPE_VALUE_WORDS:
        val = float(_RPE_VALUE_WORDS[tok])

    if val is None or not (1 <= val <= 10):
        # Couldn't resolve a sane RPE — strip the marker but report no value.
        cleaned = (t[:m.start()] + " " + t[m.end():]).strip()
        return None, cleaned

    cleaned = (t[:m.start()] + " " + t[m.end():]).strip()
    return val, cleaned


def _pop_weight(text: str, allow_bare: bool = True) -> tuple[float | None, str]:
    """Extract weight: '12 lbs' / '12lb' / '12 pounds' / '12#' always; '@ 30' /
    'at 30' (no unit) only when allow_bare is True — i.e. the line already looks
    like a set — so chatter like 'remind me at 5' isn't read as a weight."""
    m = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:lbs?|pounds?|#)\b", text)
    if not m and allow_bare:
        m = re.search(r"(?:@|\bat)\s*([0-9]+(?:\.[0-9]+)?)\b", text)
    if not m:
        return None, text
    val = float(m.group(1))
    cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
    return val, cleaned


def _pop_duration(text: str) -> tuple[int | None, str]:
    """Extract a held-duration: '30s' / '30 sec' / '2 min' (→ seconds)."""
    m = re.search(r"\b([0-9]+)\s*(?:s|sec|secs|seconds?)\b", text)
    if m:
        cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
        return int(m.group(1)), cleaned
    m = re.search(r"\b([0-9]+)\s*(?:m|min|mins|minutes?)\b", text)
    if m:
        cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
        return int(m.group(1)) * 60, cleaned
    return None, text


def _pop_reps(text: str) -> tuple[int | None, str]:
    """Extract reps: '13 reps' / 'x13' / '13x' / '3x10' (sets x reps → reps)."""
    # 'SETSxREPS' — take the second number as reps.
    m = re.search(r"\b([0-9]+)\s*x\s*([0-9]+)\b", text)
    if m:
        cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
        return int(m.group(2)), cleaned
    for pat in (r"\b([0-9]+)\s*(?:reps?)\b", r"\bx\s*([0-9]+)\b", r"\b([0-9]+)\s*x\b"):
        m = re.search(pat, text)
        if m:
            cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
            return int(m.group(1)), cleaned
    return None, text


def _pop_set_num(text: str) -> tuple[int | None, str]:
    m = re.search(r"\bset\s*([0-9]+)\b", text)
    if not m:
        return None, text
    cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
    return int(m.group(1)), cleaned


def _match_exercise(leftover: str, plan: dict) -> str | None:
    """Fuzzy-match the residual words against today's plan exercises.

    A plan exercise is matched when the candidate shares a meaningful token
    with it (len >= 3, e.g. 'rdl' ~ 'DB RDL', 'squat' ~ 'Goblet squat') or the
    full strings are highly similar. Otherwise, if the words still look like a
    (short) exercise name, they're returned title-cased so off-script exercises
    (e.g. 'bicep curl' on an ad-hoc day) still get logged. Returns None when
    nothing name-like remains — the caller then attributes the set to the
    cursor's next exercise.
    """
    tokens = [t for t in re.findall(r"[a-z]+", leftover.lower()) if t not in _SET_STOPWORDS]
    if not tokens:
        return None
    cand = " ".join(tokens)
    cand_set = set(tokens)

    best_name, best_score = None, 0.0
    for name in _plan_exercise_names(plan):
        nlow = name.lower()
        nset = set(re.findall(r"[a-z]+", nlow))
        shared = [w for w in (cand_set & nset) if len(w) >= 3]
        if shared:
            score = 0.6 + 0.1 * len(shared)
        else:
            seq = difflib.SequenceMatcher(None, cand, nlow).ratio()
            score = seq if seq >= 0.62 else 0.0
        if score > best_score:
            best_name, best_score = name, score

    if best_name and best_score >= 0.6:
        return best_name
    if 1 <= len(tokens) <= 3:
        return " ".join(t.capitalize() for t in tokens)
    return None


def parse_set_line(text: str, plan: dict) -> dict | None:
    """Parse one dictated set into {exercise, reps, weight, duration, rpe, set_num}.

    Tolerates speech-to-text noise and flexible word order. Returns None when
    the line has no set signal (so the caller falls through to other handlers).
    A set with numbers but no recognizable exercise returns exercise=None — the
    session handler then attributes it to the current cursor exercise.
    """
    work = text.lower().strip()
    if not work:
        return None

    rpe, work = _pop_rpe(work)
    work = _words_to_numbers(work)
    reps, work = _pop_reps(work)
    duration, work = _pop_duration(work)
    # Bare 'at N'/'@N' only counts as weight once the line already looks like a set.
    allow_bare = any(v is not None for v in (reps, duration, rpe))
    weight, work = _pop_weight(work, allow_bare=allow_bare)
    set_num, work = _pop_set_num(work)

    exercise = _match_exercise(work, plan)

    # Bare-number reps fallback: a lone integer (e.g. "goblet squat 10 @ 30",
    # where 10 has no "reps"/"x" marker) only counts as reps when the line is
    # clearly a set — a weight/RPE is present, or a real plan exercise matched.
    if reps is None and duration is None:
        bare_nums = re.findall(r"\b([0-9]+)\b", work)
        if bare_nums and (weight is not None or rpe is not None or _is_plan_exercise(exercise, plan)):
            reps = int(bare_nums[0])

    has_signal = any(v is not None for v in (reps, weight, duration, rpe))

    # No numbers at all → not a set to log (a bare name or chatter falls through).
    if not has_signal:
        return None

    return {
        "exercise": exercise,
        "reps": reps,
        "weight": weight,
        "duration": duration,
        "rpe": rpe,
        "set_num": set_num,
    }


# ----------------------------------------------------------------------------
# Build 3 — conversational loop (formatters + writers)
# ----------------------------------------------------------------------------

_START_RE = re.compile(
    r"^\s*(?:let'?s|lets|time\s+to|start(?:ing)?|begin(?:ning)?|gonna|about\s+to)"
    r"\s+(?:work\s?out|lift(?:ing)?|train(?:ing)?)\b"
    r"|^\s*(?:work\s?out|lifting)\s+time\b",
    re.IGNORECASE,
)
# Anchored to a bare end phrase (optional trailing punctuation only), so an
# inbox command like "done <thread_id>" or a stray "done squats" is NOT treated
# as a session-end and falls through to its real handler.
_END_RE = re.compile(
    r"^\s*(?:done|i'?m\s+done|im\s+done|i\s+am\s+done|all\s+done|that'?s\s+it|"
    r"thats\s+it|finished|i'?m\s+finished|im\s+finished|we'?re\s+done|were\s+done|"
    r"workout\s+done|session\s+done|that'?s\s+a\s+wrap|thats\s+a\s+wrap)\s*[.!]*\s*$",
    re.IGNORECASE,
)


def _fmt_num(x) -> str:
    """Drop a trailing .0 from numeric values for clean display."""
    if x is None:
        return ""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else str(f)


def _top_set(sets: list[dict]) -> dict:
    """Pick the representative set — heaviest, else first."""
    def weight_key(s):
        w = s.get("weight_lbs")
        return float(w) if w is not None else -1.0
    return max(sets, key=weight_key) if sets else {}


def _format_set_detail(s: dict) -> str:
    reps, wt = s.get("reps_done"), s.get("weight_lbs")
    rpe, dur = s.get("rpe_actual"), s.get("duration_sec")
    if reps is not None and wt is not None:
        base = f"{reps}×{_fmt_num(wt)}lb"
    elif wt is not None:
        base = f"{_fmt_num(wt)}lb"
    elif reps is not None:
        base = f"{reps} reps"
    elif dur is not None:
        base = f"{dur}s"
    else:
        base = "logged"
    if rpe is not None:
        base += f" RPE {_fmt_num(rpe)}"
    return base


def _format_last(last: dict) -> str:
    return _format_set_detail(_top_set(last.get("sets", [])))


def _format_target_only(nxt: dict) -> str:
    reps, load = nxt.get("target_reps"), nxt.get("target_load_lbs")
    dur = nxt.get("duration_sec")
    if reps is not None and load is not None:
        return f"{reps}×{_fmt_num(load)}lb"
    if reps is not None:
        return f"{reps} reps"
    if dur is not None:
        return f"{dur}s"
    return ""


def _suggestion_from_last(last: dict) -> str:
    """Trainer suggestion driven by last time's RPE on the top set."""
    top = _top_set(last.get("sets", []))
    rpe = top.get("rpe_actual")
    wt = top.get("weight_lbs")
    if rpe is None:
        return f"Match or beat {_fmt_num(wt)}lb." if wt is not None else "Match or beat it."
    rpe = float(rpe)
    if rpe <= 5:
        # Low RPE → add load.
        return f"Try {_fmt_num(float(wt) + 5)}." if wt is not None else "Add load."
    if rpe >= 9:
        # High RPE → hold or back off.
        return f"Hold {_fmt_num(wt)}lb or drop a touch." if wt is not None else "Hold or reduce."
    return f"Match or beat {_fmt_num(wt)}lb." if wt is not None else "Match or beat it."


def _format_next_line(nxt: dict, session_type: str, prefix: str = "Next") -> str:
    """'Next: tricep extension — last time 20lb RPE 6. Try 25. Waiting.'"""
    last = last_time(nxt["name"], session_type)
    seg = f"{prefix}: {nxt['name']}"
    if last and last.get("sets"):
        seg += f" — last time {_format_last(last)}."
        sug = _suggestion_from_last(last)
        if sug:
            seg += f" {sug}"
    else:
        tgt = _format_target_only(nxt)
        seg += " — no prior data."
        if tgt:
            seg += f" Target {tgt}."
    return seg + " Waiting."


def _format_logged_line(parsed: dict) -> str:
    ex = parsed["exercise"]
    reps, wt = parsed.get("reps"), parsed.get("weight")
    rpe, dur = parsed.get("rpe"), parsed.get("duration")
    if reps is not None and wt is not None:
        body = f"{ex} {reps}×{_fmt_num(wt)}lb"
    elif reps is not None:
        body = f"{ex} {reps} reps"
    elif dur is not None:
        body = f"{ex} {dur}s"
    elif wt is not None:
        body = f"{ex} {_fmt_num(wt)}lb"
    else:
        body = str(ex)
    if rpe is not None:
        body += f", RPE {_fmt_num(rpe)}"
    return f"💪 Logged: {body}."


def _next_set_num(plan_id: int, exercise: str) -> int:
    from knowledge.db import execute_one
    row = execute_one(
        """SELECT COALESCE(MAX(set_num), 0) AS m
           FROM health.session_log
           WHERE plan_id = %s AND LOWER(exercise) = LOWER(%s)""",
        (plan_id, exercise),
    )
    return int((row or {}).get("m") or 0) + 1


def _insert_set_row(plan_id: int, log_type: str, p: dict) -> None:
    from knowledge.db import execute_write
    execute_write(
        """INSERT INTO health.session_log (
               plan_id, log_type, exercise, set_num, reps_done,
               weight_lbs, duration_sec, rpe_actual, logged_via
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'mattermost')""",
        (
            plan_id, log_type, p["exercise"], p.get("set_num"),
            p.get("reps"), p.get("weight"), p.get("duration"), p.get("rpe"),
        ),
    )


def _start_session() -> str:
    """Begin (or re-surface) today's session and announce the first exercise."""
    plan = get_today_plan()
    if not plan:
        return "No plan on the calendar for today. Nothing to start."
    if plan.get("is_skipped") or plan.get("session_type") in _NON_WORKOUT_SESSIONS:
        pretty = _session_pretty_name(plan.get("session_type", "?"))
        return f"Today isn't a lifting day — {pretty}. Nothing to start."

    _set_session_marker("active")
    nxt = next_exercise(plan)
    if not nxt:
        # Either no readable exercise list, or everything already logged.
        return (
            f"Session on — {_session_pretty_name(plan['session_type'])}. "
            "Call out your sets as you go (e.g. `goblet squat 10 @ 30 rpe 7`)."
        )
    return "Session on. " + _format_next_line(nxt, plan["session_type"], prefix="First")


def _today_logged_sets(plan_id: int) -> list[dict]:
    from knowledge.db import execute_query
    return execute_query(
        """SELECT exercise, set_num, reps_done, weight_lbs, duration_sec, rpe_actual
           FROM health.session_log
           WHERE plan_id = %s AND log_type IN ('strength_set', 'cardio_block')
           ORDER BY log_id""",
        (plan_id,),
    )


def _ensure_session_summary(plan_id: int, rows: list[dict]) -> None:
    """Write a session_summary row if none exists yet, so the 21:50 inferred
    placeholder / 23:00 nag don't fire for a session the user already closed."""
    from knowledge.db import execute_one, execute_write
    existing = execute_one(
        "SELECT 1 AS x FROM health.session_log WHERE plan_id = %s AND log_type = 'session_summary' LIMIT 1",
        (plan_id,),
    )
    if existing:
        return
    rpes = [float(r["rpe_actual"]) for r in rows if r.get("rpe_actual") is not None]
    avg = round(sum(rpes) / len(rpes), 1) if rpes else None
    execute_write(
        """INSERT INTO health.session_log (
               plan_id, log_type, exercise, rpe_actual, notes, logged_via
           ) VALUES (%s, 'session_summary', 'session_summary', %s,
                     'conversational session closed', 'mattermost')""",
        (plan_id, avg),
    )


def _format_session_summary(plan: dict, rows: list[dict]) -> str:
    by: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        k = r["exercise"]
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(r)

    lines = [f"Session done — {_session_pretty_name(plan['session_type'])}."]
    all_rpe: list[float] = []
    for k in order:
        sets = by[k]
        n = len(sets)
        detail = _format_last({"sets": sets})
        lines.append(f"• {k}: {n} set{'s' if n != 1 else ''} ({detail})")
        all_rpe += [float(s["rpe_actual"]) for s in sets if s.get("rpe_actual") is not None]
    if all_rpe:
        lines.append(f"Overall ~RPE {_fmt_num(round(sum(all_rpe) / len(all_rpe), 1))}.")
    lines.append("Logged. Get some protein.")
    return "\n".join(lines)


def _end_session(plan: dict) -> str:
    plan_id = plan["plan_id"]
    rows = _today_logged_sets(plan_id)
    _set_session_marker("ended")
    try:
        _ensure_session_summary(plan_id, rows)
    except Exception:
        logger.exception("Failed to write session_summary on done")
    if not rows:
        return "Session closed. Nothing logged — next time call out your sets as you go."
    return _format_session_summary(plan, rows)


def _log_set_and_advance(plan: dict, parsed: dict) -> str | None:
    plan_id = plan["plan_id"]
    session_type = plan["session_type"]

    # Attribute a name-less set to the cursor's current exercise.
    if not parsed.get("exercise"):
        nxt = next_exercise(plan)
        if not nxt:
            return None  # nothing to attribute to → fall through
        parsed["exercise"] = nxt["name"]

    if parsed.get("set_num") is None:
        parsed["set_num"] = _next_set_num(plan_id, parsed["exercise"])

    log_type = "cardio_block" if session_type.startswith("cardio") else "strength_set"
    try:
        _insert_set_row(plan_id, log_type, parsed)
    except Exception:
        logger.exception("Failed to log set")
        return "⚠️ Couldn't save that set — check DB."

    conf = _format_logged_line(parsed)
    nxt = next_exercise(plan)
    if nxt:
        return conf + " " + _format_next_line(nxt, session_type)
    return conf + " That's the whole card. Say `done` to close it out."


def handle_workout_session(message: str) -> str | None:
    """Conversational workout dispatcher (Build 3).

    Returns a reply string when this message belongs to the session loop
    (start / log-a-set / done), or None to fall through to other handlers.

    Set-logging and 'done' only act while a session is active, so when no
    session is running this only ever claims an explicit start trigger.
    """
    msg = (message or "").strip()
    if not msg:
        return None

    # Start a session (wins over the legacy life_ops "let's work out" handler).
    if _START_RE.search(msg):
        return _start_session()

    plan = get_active_session()
    if not plan:
        return None

    if _END_RE.match(msg):
        return _end_session(plan)

    parsed = parse_set_line(msg, plan)
    if parsed is None:
        return None  # not a set line — let other handlers try

    return _log_set_and_advance(plan, parsed)


# ----------------------------------------------------------------------------
# Build 5 — history Q&A (handle_plan_query)
# ----------------------------------------------------------------------------

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Aliases for the struggle query → canonical session_type.
_SESSION_TYPE_ALIASES = {
    "strength_a": "strength_a", "strength a": "strength_a",
    "strength_b": "strength_b", "strength b": "strength_b",
    "strength_c": "strength_c", "strength c": "strength_c",
    "cardio_intervals": "cardio_intervals", "cardio intervals": "cardio_intervals",
    "intervals": "cardio_intervals", "hiit": "cardio_intervals",
    "cardio_z2": "cardio_z2", "cardio z2": "cardio_z2", "zone 2": "cardio_z2",
    "z2": "cardio_z2",
}


def _detect_weekday(q: str) -> int | None:
    for name, idx in _WEEKDAYS.items():
        if re.search(rf"\b{name}(?:'s|s)?\b", q):  # matches saturday / saturdays / saturday's
            return idx
    return None


def _detect_session_type(q: str) -> str | None:
    for alias, canonical in _SESSION_TYPE_ALIASES.items():
        if alias in q:
            return canonical
    return None


def _relative_day_label(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%A %b %-d")


def _format_plan_overview(plan: dict, today: date) -> str:
    when = _relative_day_label(plan["plan_date"], today)
    lines = [f"{when}: {_session_pretty_name(plan['session_type'])}."]
    if plan.get("is_skipped"):
        lines.append("_(marked skipped)_")
    for ex in _plan_exercises(plan):
        name = str(ex.get("name", "")).strip()
        if not name:
            continue
        tgt = _format_target_only(_exercise_target(ex))
        lines.append(f"• {name}" + (f" — {tgt}" if tgt else ""))
    return "\n".join(lines)


def _query_next_workout() -> str:
    from knowledge.db import execute_one
    today = datetime.now(CT).date()
    row = execute_one(
        """SELECT * FROM health.plan
           WHERE plan_date >= %s AND is_skipped = FALSE
             AND session_type NOT IN ('rest_mobility', 'walk')
           ORDER BY plan_date LIMIT 1""",
        (today,),
    )
    if not row:
        return "No upcoming workout on the calendar."
    return _format_plan_overview(row, today)


def _query_day(target: date) -> str:
    from knowledge.db import execute_one
    today = datetime.now(CT).date()
    row = execute_one("SELECT * FROM health.plan WHERE plan_date = %s", (target,))
    if not row:
        return f"No plan for {target.strftime('%A %b %-d')}."
    return _format_plan_overview(row, today)


def _query_weekday_workout(weekday_idx: int) -> str:
    today = datetime.now(CT).date()
    delta = (weekday_idx - today.weekday()) % 7  # next occurrence (today counts)
    return _query_day(today + timedelta(days=delta))


def _query_last_workout() -> str:
    from knowledge.db import execute_one, execute_query
    today = datetime.now(CT).date()
    plan = execute_one(
        """SELECT p.plan_id, p.plan_date, p.session_type
           FROM health.plan p
           WHERE p.plan_date <= %s
             AND EXISTS (
                 SELECT 1 FROM health.session_log sl
                 WHERE sl.plan_id = p.plan_id
                   AND sl.log_type IN ('strength_set', 'cardio_block')
                   AND sl.logged_via <> 'inferred')
           ORDER BY p.plan_date DESC LIMIT 1""",
        (today,),
    )
    if not plan:
        return "No logged workouts yet."

    rows = execute_query(
        """SELECT exercise, set_num, reps_done, weight_lbs, duration_sec, rpe_actual
           FROM health.session_log
           WHERE plan_id = %s AND log_type IN ('strength_set', 'cardio_block')
           ORDER BY log_id""",
        (plan["plan_id"],),
    )
    summary = execute_one(
        """SELECT rpe_actual FROM health.session_log
           WHERE plan_id = %s AND log_type = 'session_summary'
           ORDER BY log_id DESC LIMIT 1""",
        (plan["plan_id"],),
    )

    when = _relative_day_label(plan["plan_date"], today)
    lines = [f"Last workout — {when}, {_session_pretty_name(plan['session_type'])}:"]

    by: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        k = r["exercise"]
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(r)
    for k in order:
        lines.append(f"• {k}: {_format_last({'sets': by[k]})}")

    if summary and summary.get("rpe_actual") is not None:
        lines.append(f"Overall RPE {_fmt_num(summary['rpe_actual'])}.")
    return "\n".join(lines)


def _query_struggle(session_type: str) -> str:
    from knowledge.db import execute_query
    pretty = _session_pretty_name(session_type)
    rows = execute_query(
        """SELECT sl.exercise,
                  AVG(sl.rpe_actual) AS avg_rpe,
                  MAX(sl.rpe_actual) AS max_rpe,
                  COUNT(*) AS n
           FROM health.session_log sl
           JOIN health.plan p ON sl.plan_id = p.plan_id
           WHERE p.session_type = %s
             AND sl.log_type = 'strength_set'
             AND sl.rpe_actual IS NOT NULL
           GROUP BY sl.exercise
           ORDER BY avg_rpe DESC
           LIMIT 5""",
        (session_type,),
    )
    if not rows:
        return f"No logged sets for {pretty} yet."
    lines = [f"Toughest in {pretty} (by RPE):"]
    for r in rows:
        lines.append(
            f"• {r['exercise']}: avg RPE {_fmt_num(round(float(r['avg_rpe']), 1))} "
            f"(peak {_fmt_num(r['max_rpe'])}, {r['n']} set{'s' if r['n'] != 1 else ''})"
        )
    return "\n".join(lines)


def handle_plan_query(message: str) -> str | None:
    """History / plan read-intent Q&A (Build 5).

    Routed in main.py BEFORE _try_life_ops so RDS reads always win over the
    legacy SQLite path. Returns a reply string, or None to fall through.
    Conservative: only fires on clear plan/workout phrasing.
    """
    q = (message or "").lower().strip()
    if not q:
        return None

    # 1. "where did I struggle in strength_c" — most specific.
    if re.search(r"\b(struggl\w*|hardest|toughest|worst|where\s+did\s+i)\b", q):
        st = _detect_session_type(q)
        if st:
            return _query_struggle(st)

    workout_noun = re.search(r"\b(workout|session|plan|training|lift\w*)\b", q)

    # 2. "what's Saturday's workout" — weekday → next occurrence.
    wd = _detect_weekday(q)
    if wd is not None and workout_noun:
        return _query_weekday_workout(wd)

    # 3. "how was my last workout".
    if re.search(r"\b(last|previous|yesterday'?s|recent)\b", q) and (
        workout_noun or re.search(r"\b(how\s+(was|did|'?d)|recap)\b", q)
    ):
        return _query_last_workout()

    # 4. "what's my next workout" / "today's workout" / "tomorrow's workout".
    if workout_noun and re.search(
        r"\b(next|today'?s|todays|tomorrow'?s|tomorrows|upcoming|what'?s|whats|what\s+is|my)\b", q
    ):
        today = datetime.now(CT).date()
        if re.search(r"\btomorrow", q):
            return _query_day(today + timedelta(days=1))
        if re.search(r"\btoday", q):
            return _query_day(today)
        return _query_next_workout()

    return None


# ----------------------------------------------------------------------------
# PB-009 plan_lookup — the routing-bug fix.
#
# get_plan_lookup() is the dedicated handler for the 'plan_lookup' intent. It:
#   * anchors ALL date math to America/Chicago (CT) — never naive date.today()
#     / UTC, which is what mislabeled Saturday as Jun 7 and shifted the week;
#   * reads health.plan for the requested range and returns session_type + the
#     REAL exercise names from blocks.exercises (and the finisher if present);
#   * HARD GUARD: a requested date with no DB row yields exactly
#     "No plan seeded for <date>." It never fabricates exercises and (via the
#     main.py wiring) never falls through to the LLM general_reply path.
# ----------------------------------------------------------------------------

def _coerce_blocks(blocks) -> dict:
    if isinstance(blocks, str):
        try:
            return json.loads(blocks)
        except (ValueError, TypeError):
            return {}
    return blocks or {}


def _exercise_names_from_blocks(blocks) -> tuple[list[str], list[str]]:
    """Return (main_exercise_names, finisher_exercise_names) from a blocks dict.

    Reads the real contract shape: blocks['exercises'] for the main circuit and
    blocks['finisher']['exercises'] for the core finisher. Either may be absent
    (e.g. cardio intervals have no per-exercise list)."""
    b = _coerce_blocks(blocks)
    main = [str(e.get("name")) for e in (b.get("exercises") or []) if isinstance(e, dict) and e.get("name")]
    fin: list[str] = []
    finisher = b.get("finisher")
    if isinstance(finisher, dict):
        fin = [str(e.get("name")) for e in (finisher.get("exercises") or []) if isinstance(e, dict) and e.get("name")]
    return main, fin


def _fetch_plan_row(d: date) -> dict | None:
    from knowledge.db import execute_one
    return execute_one(
        """SELECT plan_date, session_type, target_rpe, est_duration_min,
                  is_skipped, blocks
           FROM health.plan WHERE plan_date = %s""",
        (d,),
    )


def _format_plan_lookup_day(d: date, row: dict, today: date) -> str:
    # Weekday label is derived from the plan_date itself → it can never be
    # mislabeled the way the UTC/naive bug did.
    label = d.strftime("%A %b %-d")
    if d == today:
        label += " (today)"
    elif d == today + timedelta(days=1):
        label += " (tomorrow)"

    # Prefer the canonical program name from blocks.display_name; fall back to
    # the legacy session_type pretty label only when it's absent.
    blocks = _coerce_blocks(row.get("blocks"))
    session = blocks.get("display_name") or _session_pretty_name(row.get("session_type", "?"))
    line = f"**{label}** — {session}"
    if row.get("is_skipped"):
        line += " _(skipped)_"

    main, fin = _exercise_names_from_blocks(row.get("blocks"))
    parts = [line]
    if main:
        parts.append("• " + ", ".join(main))
    if fin:
        parts.append("• finisher: " + ", ".join(fin))
    if not main and not fin:
        # Cardio/rest days legitimately have no exercise list — show the type.
        btype = blocks.get("type")
        if btype:
            parts.append(f"• ({btype})")
    # One item per line so Mattermost doesn't run the day together.
    return "\n".join(parts)


def get_plan_lookup(message: str) -> str:
    """Handle the 'plan_lookup' intent (PB-009 routing-bug fix).

    Always returns a non-empty string (so the caller posts it and returns,
    never reaching general_reply). Missing dates yield exactly
    "No plan seeded for <date>.".
    """
    q = (message or "").lower()
    today = datetime.now(CT).date()  # CT-anchored — the bug fix

    header: str | None = None
    m = re.search(r"next\s+(\d+)\s+days?", q)
    if m:
        n = max(1, min(int(m.group(1)), 31))
        dates = [today + timedelta(days=i) for i in range(n)]
        header = f"Next {n} days"
    elif "this week" in q:
        monday = today - timedelta(days=today.weekday())  # CT week start
        dates = [monday + timedelta(days=i) for i in range(7)]
        header = "This week"
    elif "tomorrow" in q:
        dates = [today + timedelta(days=1)]
    elif "today" in q or "tonight" in q:
        dates = [today]
    else:
        wd = _detect_weekday(q)
        if wd is not None:
            delta = (wd - today.weekday()) % 7  # next occurrence (today counts)
            dates = [today + timedelta(days=delta)]
        elif re.search(r"\b(plan|week|workouts|schedule)\b", q) or "show" in q:
            # "show my plan" with no temporal cue → the upcoming 7 days.
            dates = [today + timedelta(days=i) for i in range(7)]
            header = "Next 7 days"
        else:
            dates = [today]

    blocks_out: list[str] = []
    if header:
        blocks_out.append(f"**{header}:**")
    for d in dates:
        row = _fetch_plan_row(d)
        if row is None:
            # HARD GUARD — exact string, no fabrication, no LLM.
            blocks_out.append(f"No plan seeded for {d.isoformat()}.")
        else:
            blocks_out.append(_format_plan_lookup_day(d, row, today))
    # Blank line between days so the multi-day view isn't a run-on.
    return "\n\n".join(blocks_out)


# ----------------------------------------------------------------------------
# PB-009 plan_detail — single-day DEPTH: render the FULL block + coaching note.
#
# get_plan_detail() expands everything health.plan.blocks already carries that
# the breadth view discards: duration, RPE, HR zone, warmup/cooldown, every
# exercise with sets/reps/load/rest, and the finisher's rep schemes. A short
# trainer-voice coaching note is appended via the LLM — constrained to THIS
# block only (no invention, no history). Missing dates use the same exact
# "No plan seeded for <date>." guard and never reach the LLM.
# ----------------------------------------------------------------------------

_HR_ZONE_LABEL = {
    1: "Zone 1 — very easy, recovery",
    2: "Zone 2 — conversational pace",
    3: "Zone 3 — steady, controlled effort",
    4: "Zone 4 — hard, near threshold",
    5: "Zone 5 — max effort",
}


def _hr_zone_label(zone) -> str:
    try:
        return _HR_ZONE_LABEL.get(int(zone), f"HR zone {zone}")
    except (TypeError, ValueError):
        return f"HR zone {zone}"


def _format_exercise_detail(ex: dict) -> str:
    """Format-aware one-line detail: reps (× load) or held duration, + side
    note and rest."""
    name = str(ex.get("name", "")).strip() or "exercise"
    fmt = ex.get("format")
    dur = ex.get("duration_sec")
    reps = ex.get("target_reps")
    if fmt == "duration" or (dur is not None and reps is None):
        core = f"{dur}s" if dur is not None else "hold"
    elif reps is not None:
        core = f"{reps} reps"
        load = ex.get("target_load_lbs")
        if load is not None:
            core += f" @ {_fmt_num(load)} lb"
    else:
        core = "as prescribed"
    seg = f"{name} — {core}"
    note = ex.get("notes")
    if note:
        seg += f" ({note})"
    rest = ex.get("rest_after_sec")
    if rest is not None:
        seg += f" · rest {rest}s"
    return seg


def _render_finisher(fin: dict) -> str:
    rounds = fin.get("rounds")
    head = f"**Core finisher** — {rounds} rounds:" if rounds else "**Core finisher:**"
    out = [head]
    for ex in fin.get("exercises") or []:
        if isinstance(ex, dict) and ex.get("name"):
            out.append(f"• {_format_exercise_detail(ex)}")
    return "\n".join(out)


def _render_circuit(blocks: dict) -> list[str]:
    sections: list[str] = []
    if blocks.get("warmup"):
        sections.append(f"**Warmup:** {blocks['warmup']}")

    rounds = blocks.get("rounds")
    rbr = blocks.get("rest_between_rounds_sec")
    head = f"**Circuit** — {rounds} rounds" if rounds else "**Circuit**"
    if rbr:
        head += f" ({rbr}s rest between rounds)"
    head += ":"
    lines = [head]
    for i, ex in enumerate(blocks.get("exercises") or [], 1):
        if isinstance(ex, dict) and ex.get("name"):
            lines.append(f"{i}. {_format_exercise_detail(ex)}")
    sections.append("\n".join(lines))

    fin = blocks.get("finisher")
    if isinstance(fin, dict):
        sections.append(_render_finisher(fin))
    if blocks.get("cooldown"):
        sections.append(f"**Cooldown:** {blocks['cooldown']}")
    return sections


def _render_cardio(blocks: dict) -> list[str]:
    sections: list[str] = []
    it = blocks.get("intervals_template") if isinstance(blocks.get("intervals_template"), dict) else None
    if it:
        rounds = blocks.get("rounds")
        work, wset = it.get("work_sec"), it.get("work_settings")
        rest, rset = it.get("rest_sec"), it.get("rest_settings")
        seg = f"**Intervals** — {rounds} rounds: " if rounds else "**Intervals:** "
        seg += f"{work}s work" + (f" ({wset})" if wset else "")
        seg += f" / {rest}s easy" + (f" ({rset})" if rset else "")
        sections.append(seg)
    else:
        dur = blocks.get("duration_min")
        rng = blocks.get("target_range_min")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            dstr = f"{rng[0]}–{rng[1]} min"
        elif dur:
            dstr = f"{dur} min"
        else:
            dstr = ""
        inten = blocks.get("intensity")
        seg = "**Steady**"
        if dstr:
            seg += f": {dstr}"
        if inten:
            seg += f" at {inten}"
        sections.append(seg)

    wc = []
    wu, cd = blocks.get("warmup_sec"), blocks.get("cooldown_sec")
    if wu:
        wc.append(f"Warmup {wu // 60} min" + (f" ({blocks.get('warmup_settings')})" if blocks.get("warmup_settings") else ""))
    if cd:
        wc.append(f"Cooldown {cd // 60} min" + (f" ({blocks.get('cooldown_settings')})" if blocks.get("cooldown_settings") else ""))
    if wc:
        sections.append(" · ".join(wc))

    equip = blocks.get("equipment")
    if equip:
        sections.append("Equipment: " + ", ".join(str(e) for e in equip))

    fin = blocks.get("finisher")
    if isinstance(fin, dict):
        sections.append(_render_finisher(fin))

    notes = blocks.get("setup_notes")
    if notes:
        sections.append("Notes: " + "; ".join(str(n) for n in notes))
    return sections


def _render_mobility(blocks: dict) -> list[str]:
    sections: list[str] = []
    if blocks.get("notes"):
        sections.append(str(blocks["notes"]))
    dur = blocks.get("duration_min")
    if dur:
        sections.append(f"Duration: {dur} min")
    return sections


def _render_full_block(d: date, row: dict, today: date) -> str:
    """Render the full session from a plan row's blocks + columns."""
    blocks = _coerce_blocks(row.get("blocks"))
    display = blocks.get("display_name") or _session_pretty_name(row.get("session_type", "?"))

    header = f"**{_relative_day_label(d, today)} — {display}**"
    if row.get("is_skipped"):
        header += " _(skipped)_"

    meta = []
    est = row.get("est_duration_min")
    if est:
        meta.append(f"~{est} min")
    rpe = row.get("target_rpe")
    if rpe is not None:
        meta.append(f"target RPE {_fmt_num(rpe)}")
    zone = row.get("target_hr_zone")
    if zone is not None:
        meta.append(_hr_zone_label(zone))
    elif blocks.get("intensity"):
        meta.append(str(blocks["intensity"]))

    btype = blocks.get("type")
    if btype == "circuit":
        body = _render_circuit(blocks)
    elif btype in ("intervals", "steady"):
        body = _render_cardio(blocks)
    elif btype == "mobility":
        body = _render_mobility(blocks)
    else:
        body = []

    sections = [header]
    if meta:
        sections.append(" · ".join(meta))
    sections.extend(body)

    # Zone 2 sessions get an explicit HR + talk-test cue.
    try:
        if int(zone) == 2:
            sections.append(
                "**Zone 2 cue:** keep HR ~120–140 — full-sentence conversational "
                "pace (if you can't talk in full sentences, ease off)."
            )
    except (TypeError, ValueError):
        pass

    # Blank line between sections → readable in Mattermost.
    return "\n\n".join(s for s in sections if s)


def _coach_note(structured_text: str, display_name: str) -> str | None:
    """1-2 sentences of trainer guidance for THIS session, via the trainer-voice
    LLM. Constrained to the given block; returns None if the LLM is unavailable
    so the caller falls back to the structured render alone."""
    system = TRAINER_VOICE_PROMPT + (
        "\n\nYou are given the EXACT prescription for ONE workout session below. "
        "Add 1-2 short sentences of practical coaching for THIS session only. "
        "Explain only what is in the block — do NOT invent exercises, do NOT change "
        "any loads, reps, sets, or timings, and do NOT reference past sessions or "
        "any conversation. No emojis, no hype, no lists."
    )
    user = (
        f"Session: {display_name}\n\n"
        f"{structured_text}\n\n"
        "Give 1-2 sentences of coaching for this exact session."
    )
    try:
        note = _call_claude_text(system, user, max_tokens=140).strip()
        return note or None
    except Exception:
        logger.debug("Coaching note unavailable — returning structured render only", exc_info=True)
        return None


def _fetch_plan_full(d: date) -> dict | None:
    from knowledge.db import execute_one
    return execute_one("SELECT * FROM health.plan WHERE plan_date = %s", (d,))


def _plan_date_range() -> tuple[date, date] | None:
    """Return (first_plan_date, last_plan_date) from health.plan, or None if the
    table has no rows. Used to make 'no plan for this date' answers concrete —
    never a denial that the database exists."""
    from knowledge.db import execute_one
    try:
        row = execute_one("SELECT MIN(plan_date) AS first, MAX(plan_date) AS last FROM health.plan")
    except Exception:
        logger.debug("Could not read plan date range", exc_info=True)
        return None
    if row and row.get("first") and row.get("last"):
        return row["first"], row["last"]
    return None


def _no_plan_message(target: date) -> str:
    """The ONLY 'missing data' answer. States the seeded window when known.
    NEVER claims the database doesn't exist or that data came from chat."""
    rng = _plan_date_range()
    if rng:
        return (f"No plan seeded for {target.isoformat()} — "
                f"your plan runs {rng[0].isoformat()} to {rng[1].isoformat()}.")
    return f"No plan seeded for {target.isoformat()}."


def get_plan_detail(message: str) -> str:
    """Handle the 'plan_detail' intent — full single-day session breakdown.

    Resolves the target date (CT-anchored; today / tomorrow / weekday), renders
    the FULL block, then appends a constrained trainer-voice coaching note.
    Missing dates yield a concrete "No plan seeded for <date> — your plan runs
    <first> to <last>." (never a DB denial) and never reach the LLM. Always
    returns a non-empty string.
    """
    q = (message or "").lower()
    today = datetime.now(CT).date()

    if "tomorrow" in q:
        target = today + timedelta(days=1)
    elif "today" in q or "tonight" in q:
        target = today
    else:
        wd = _detect_weekday(q)
        target = today + timedelta(days=(wd - today.weekday()) % 7) if wd is not None else today

    row = _fetch_plan_full(target)
    if row is None:
        # HARD GUARD — concrete missing-data message, no fabrication, no LLM,
        # and NEVER a denial that the workout database exists.
        return _no_plan_message(target)

    structured = _render_full_block(target, row, today)
    blocks = _coerce_blocks(row.get("blocks"))
    display = blocks.get("display_name") or _session_pretty_name(row.get("session_type", "?"))
    coach = _coach_note(structured, display)
    return f"{structured}\n\n{coach}" if coach else structured


# ----------------------------------------------------------------------------
# Anti-confabulation scrubber for the general_reply (free-text LLM) path.
# Artemis must NEVER deny that it has a workout database / training data, or
# claim that workout info "came from the chat thread". If a drafted general
# reply contains such a denial, discard it and return the real plan detail.
# ----------------------------------------------------------------------------

_DB_DENIAL_RE = re.compile(
    r"don'?t\s+have\s+(?:a\s+|any\s+|access\s+to\s+a\s+)?(?:\w+\s+){0,3}"
    r"(?:workout|training|fitness|exercise|plan)\s+(?:database|data|table|plan)"
    r"|no\s+(?:connected\s+|access\s+to\s+a\s+)?(?:workout|training|fitness)?\s*database"
    r"|(?:shared|came|provided|posted)\s+(?:directly\s+)?in\s+(?:the\s+|this\s+)?chat"
    r"|from\s+(?:the\s+|this\s+)?chat\s+(?:thread|history|conversation)"
    r"|don'?t\s+have\s+access\s+to\s+(?:your\s+)?(?:workout|training|fitness)"
    r"|no\s+(?:workout|training)\s+(?:database|data|plan)\b"
    r"|i\s+don'?t\s+(?:have|keep|store)\s+(?:a\s+)?(?:workout|training|fitness)",
    re.IGNORECASE,
)


def scrub_db_denial(response: str | None, message: str) -> str:
    """Belt-and-suspenders guard on the general_reply path.

    If the drafted reply denies that a workout/training database exists, or
    claims the data came from chat, discard it and return the real plan detail
    for the requested day (default today). Logs when it fires so leaks are
    visible. Otherwise returns the response unchanged.
    """
    if response and _DB_DENIAL_RE.search(response):
        logger.warning(
            "general_reply produced a workout-DB denial — scrubbing and routing "
            "to plan_detail. message=%r draft=%r", message, response[:200],
        )
        return get_plan_detail(message or "today's workout")
    return response or ""


# ----------------------------------------------------------------------------
# Compact health slice for the general_reply (trainer-voice) context.
#
# general_reply was historically BLIND to health.plan (its context held only
# email/calendar/commitments/inbox), which is the root cause of the "I don't have
# a workout database" confabulation AND of uninformed answers to conceptual /
# progress questions. This slice gives the trainer voice just enough real data —
# today + next few days, and the last few logged sessions — to answer
# conversational workout questions truthfully. It is a SUMMARY (token-bounded),
# never full blocks; full detail still comes from get_plan_detail.
# ----------------------------------------------------------------------------

def _one_line_block_summary(blocks) -> str:
    b = _coerce_blocks(blocks)
    t = b.get("type")
    if t == "circuit":
        n = len([e for e in (b.get("exercises") or []) if isinstance(e, dict)])
        rounds = b.get("rounds")
        s = f"{n}-exercise circuit"
        return s + (f", {rounds} rounds" if rounds else "")
    if t in ("intervals", "steady"):
        dur = b.get("duration_min")
        rng = b.get("target_range_min")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            dstr = f"~{rng[0]}–{rng[1]} min"
        elif dur:
            dstr = f"~{dur} min"
        else:
            dstr = ""
        label = "intervals" if t == "intervals" else "steady"
        return (f"{label}, {dstr}".rstrip(", ")) if dstr else label
    if t == "mobility":
        return "mobility / rest"
    return t or ""


def build_context_slice(days_ahead: int = 3, recent: int = 3) -> str:
    """Return a compact, token-bounded training summary for the general_reply
    LLM context: today + next `days_ahead` days (session + one-liner) and the
    last `recent` logged sessions (date, type, how it went). Empty string on any
    error — never raises into the mention path."""
    try:
        from knowledge.db import execute_query
        today = datetime.now(CT).date()

        plan_rows = execute_query(
            "SELECT plan_date, session_type, blocks FROM health.plan "
            "WHERE plan_date BETWEEN %s AND %s ORDER BY plan_date",
            (today, today + timedelta(days=days_ahead)),
        )
        logged = execute_query(
            """SELECT p.plan_date, p.session_type, sl.rpe_actual, sl.notes
               FROM health.session_log sl
               JOIN health.plan p ON p.plan_id = sl.plan_id
               WHERE sl.log_type = 'session_summary'
               ORDER BY sl.logged_at DESC
               LIMIT %s""",
            (recent,),
        )

        lines: list[str] = []
        if plan_rows:
            lines.append("Upcoming (you DO have this from health.plan):")
            for r in plan_rows:
                b = _coerce_blocks(r.get("blocks"))
                disp = b.get("display_name") or _session_pretty_name(r["session_type"])
                summ = _one_line_block_summary(b)
                label = _relative_day_label(r["plan_date"], today)
                lines.append(f"  - {label}: {disp}" + (f" — {summ}" if summ else "")
                             + f"  [session_type={r['session_type']}]")
        if logged:
            lines.append("Recent logged sessions (health.session_log):")
            for r in logged:
                rpe = r.get("rpe_actual")
                note = (r.get("notes") or "").strip()
                bits = []
                if rpe is not None:
                    bits.append(f"RPE {_fmt_num(rpe)}")
                if note:
                    bits.append(note[:60])
                d = r["plan_date"]
                lines.append(
                    f"  - {d.isoformat()} {_session_pretty_name(r['session_type'])}"
                    + (f" ({'; '.join(bits)})" if bits else "")
                )

        if not lines:
            return ""
        return "**Training plan & recent sessions (health.plan / health.session_log):**\n" + "\n".join(lines)
    except Exception:
        logger.debug("build_context_slice failed", exc_info=True)
        return ""
