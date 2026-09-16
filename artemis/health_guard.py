"""Plan-claim validator for LLM text (FRIDAY-1 part C).

The general LLM fallback can still talk about training. On 2026-09-16 it
invented a "calibrated plan" (a Rope Pushdown the plan never had, an RPE
"same as your last two sessions" that never happened). This module checks a
draft against the data and rejects it when it names:

  * an exercise that is not in the plan window (today ± 7 days, including any
    pre-adjustment original),
  * a load ("185 lb") that appears in no plan row, no logged set and no
    body-weight check-in,
  * a "last session" figure that isn't in the logs, or "last N sessions" when
    fewer than N real sessions were logged.

A rejected draft is replaced by the deterministic plan detail. Pure checks take
the evidence as arguments so they are testable without a database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# Exercise vocabulary the checker recognizes. A mention is allowed when it is
# covered by (contained in, or containing) a name in the plan window.
GENERIC_EXERCISES = (
    "rope pushdown", "tricep pushdown", "triceps pushdown", "pushdown", "push-down",
    "skull crusher", "overhead extension", "tricep extension", "triceps extension",
    "bicep curl", "biceps curl", "hammer curl", "preacher curl", "cable curl",
    "back squat", "front squat", "goblet squat", "split squat", "bulgarian split squat",
    "hack squat", "smith squat", "squat",
    "deadlift", "romanian deadlift", "rdl", "trap bar deadlift", "hip thrust", "glute bridge",
    "bench press", "flat db press", "flat dumbbell press", "db press", "dumbbell press",
    "incline press", "incline db press", "chest press", "floor press", "db floor press",
    "overhead press", "shoulder press", "military press", "arnold press",
    "lateral raise", "front raise", "rear delt fly", "face pull", "pec fly", "chest fly",
    "cable fly", "pec deck",
    "lat pulldown", "pulldown", "pull-up", "pullup", "chin-up", "chinup", "push-up", "pushup",
    "seated cable row", "cable row", "single-arm cable row", "bent-over row", "barbell row",
    "db row", "dumbbell row", "t-bar row", "trx row", "inverted row",
    "leg press", "leg extension", "leg curl", "seated leg curl", "lying leg curl",
    "calf raise", "calf press", "lunge", "reverse lunge", "walking lunge", "step-up",
    "back extension", "45° back extension",
    "pallof press", "plank", "side plank", "dead bug", "bird dog", "hollow hold",
    "crunch", "ab crunch", "ab machine crunch", "knee raise", "leg raise",
    "hanging leg raise", "captain's chair", "russian twist", "woodchop",
    "kettlebell swing", "farmer carry", "farmer's carry", "sled push", "burpee",
    "box jump", "thruster", "power clean", "hang clean",
)

_LOAD_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:lb|lbs|pounds)\b", re.I)
_LAST_RE = re.compile(
    r"\b(?:last|previous|prior)\s+(?:(two|three|four|five|\d+|few|couple(?:\s+of)?)\s+)?"
    r"(?:session|sessions|workout|workouts)\b", re.I)
_WORKOUT_CONTEXT_RE = re.compile(
    r"\b(?:rpe|reps?|sets?|workout|session|lift(?:s|ing)?|exercise|circuit|training|"
    r"warm-?up|cooldown|load)\b", re.I)
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "few": 2, "couple": 2, "couple of": 2}
_ANY_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def _norm(name: str) -> str:
    n = name.lower().replace("’", "'")
    n = re.sub(r"\((.*?)\)", r" \1 ", n)
    n = re.sub(r"\bdumbbell\b", "db", n)
    return " ".join(re.sub(r"[^a-z0-9°' -]", " ", n).split())


@dataclass
class Evidence:
    exercises: set = field(default_factory=set)       # normalized plan-window names
    loads: set = field(default_factory=set)           # floats
    logged_numbers: set = field(default_factory=set)  # floats from recent logs
    real_sessions: int = 0


def _covered(term: str, allowed: set) -> bool:
    t = _norm(term)
    return any(t in a or a in t for a in allowed)


def find_violations(text: str, ev: Evidence) -> list[str]:
    if not text or not _WORKOUT_CONTEXT_RE.search(text):
        return []
    low = _norm(text)
    violations: list[str] = []

    # Exercises — longest first so "rope pushdown" wins over "pushdown".
    seen = low
    for term in sorted(GENERIC_EXERCISES, key=len, reverse=True):
        nt = _norm(term)
        if re.search(rf"(?<![a-z]){re.escape(nt)}(?![a-z])", seen):
            if not _covered(nt, ev.exercises):
                violations.append(f"exercise not in plan: {term}")
            seen = re.sub(rf"(?<![a-z]){re.escape(nt)}(?![a-z])", " ", seen)

    # Loads.
    for m in _LOAD_RE.finditer(text):
        v = float(m.group(1))
        if v not in ev.loads:
            violations.append(f"load not in plan or logs: {m.group(0)}")

    # "Last session" claims.
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        for m in _LAST_RE.finditer(sentence):
            n_word = (m.group(1) or "").lower().strip()
            if n_word:
                n = int(n_word) if n_word.isdigit() else _NUMBER_WORDS.get(n_word, 2)
                if ev.real_sessions < n:
                    violations.append(f"claims {n} prior sessions; {ev.real_sessions} logged")
            for num in _ANY_NUM_RE.findall(sentence):
                if float(num) not in ev.logged_numbers:
                    violations.append(f"'{m.group(0)}' figure not in logs: {num}")
    return violations


def gather_evidence(today: date | None = None) -> Evidence:
    """Read the plan window and recent logs (read-only)."""
    from knowledge.db import execute_query
    from artemis.quiet_hours import local_today
    from artemis.health_checkin import coerce_blocks

    today = today or local_today()
    ev = Evidence()
    rows = execute_query(
        "SELECT blocks FROM health.plan WHERE plan_date BETWEEN %s AND %s",
        (today - timedelta(days=7), today + timedelta(days=7)))

    def add_blocks(b: dict) -> None:
        for ex in (b.get("exercises") or []):
            ev.exercises.add(_norm(ex.get("name", "")))
            if ex.get("target_load_lbs") is not None:
                ev.loads.add(float(ex["target_load_lbs"]))
        fin = b.get("finisher")
        if isinstance(fin, dict):
            for ex in fin.get("exercises") or []:
                ev.exercises.add(_norm(ex.get("name", "")))
        orig = b.get("original")
        if isinstance(orig, dict) and isinstance(orig.get("blocks"), dict):
            add_blocks(orig["blocks"])

    for r in rows:
        add_blocks(coerce_blocks(r.get("blocks")))
    ev.exercises.discard("")

    logs = execute_query(
        "SELECT sl.weight_lbs, sl.reps_done, sl.rpe_actual, sl.duration_sec, sl.log_type, "
        "p.plan_date FROM health.session_log sl JOIN health.plan p ON p.plan_id = sl.plan_id "
        "WHERE sl.logged_via <> 'inferred' AND p.plan_date BETWEEN %s AND %s",
        (today - timedelta(days=30), today))
    sessions = set()
    for r in logs:
        for k in ("weight_lbs", "reps_done", "rpe_actual"):
            if r.get(k) is not None:
                ev.logged_numbers.add(float(r[k]))
        if r.get("weight_lbs") is not None:
            ev.loads.add(float(r["weight_lbs"]))
        if r.get("duration_sec"):
            ev.logged_numbers.add(round(float(r["duration_sec"]) / 60))
        sessions.add(r["plan_date"])
    ev.real_sessions = len(sessions)

    for r in execute_query(
            "SELECT weight_lbs FROM health.daily_state WHERE state_date >= %s AND weight_lbs IS NOT NULL",
            (today - timedelta(days=30),)):
        ev.loads.add(float(r["weight_lbs"]))
    return ev


def guard_workout_reply(response: str, question: str) -> str:
    """Return `response`, or the deterministic plan detail when it makes a
    claim the data doesn't support. Never raises."""
    try:
        ev = gather_evidence()
        violations = find_violations(response, ev)
    except Exception:
        logger.exception("workout-claim guard failed; leaving reply unchanged")
        return response
    if not violations:
        return response
    logger.error("Rejected LLM workout claims %s for question %r", violations, question[:80])
    try:
        from knowledge.db import log_guardrail_violation
        log_guardrail_violation(
            guardrail_type="unsupported_workout_claim",
            event_summary=response[:2000],
            outcome="replaced_with_template",
            agent="artemis",
            metadata={"violations": violations, "question": question[:500]},
        )
    except Exception:
        logger.exception("Failed to log workout-claim violation")
    try:
        from artemis.health import get_plan_detail
        return get_plan_detail("today's workout")
    except Exception:
        logger.exception("plan-detail fallback failed")
        return "I can only report what's in today's plan — ask `today's workout`."
