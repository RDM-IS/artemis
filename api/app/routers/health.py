"""Health/training plan endpoints.

GET  /api/health/today          → today's training plan from health.plan
GET  /api/health/today/logged   → which exercises today already have logs
GET  /api/health/last_logged    → batch: most-recent prior set per exercise
                                  (used by gym-display to pre-fill steppers)
GET  /api/health/status         → windowed status payload for /status page
POST /api/health/log            → insert N session_log rows for one exercise.
                                  Optional session_rpe → also writes one
                                  session_summary row in the same txn.

Auth: X-API-Key header validated against AWS Secrets Manager
secret `rdmis/dev/health-api-key`. Returns 401 (per contract) on
missing/invalid key — distinct from the CRM API which returns 403.

CORS: handled by the global CORSMiddleware in api/app/main.py
(`allow_origins=["*"]` + `allow_methods=["*"]` + `allow_headers=["*"]`),
which already serves preflight requests from `https://gym.rdm.is`.
TRUSTED_ORIGINS below is informational — used by the smoke test to
assert the contract is satisfied.

Note on layout: existing routers (commitments, contacts, deals) define
pydantic models inline. Following that convention to avoid restructuring
api/app/models.py from flat file → package, which would break every
existing import.
"""

from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db

router = APIRouter()

CT = ZoneInfo("America/Chicago")

# Origins explicitly trusted by this endpoint. Currently the global
# CORSMiddleware allows everything ("*"), so this list is documentation
# + test fixture only. If the global middleware tightens, this becomes
# the canonical list for /api/health/*.
TRUSTED_ORIGINS = {
    "https://gym.rdm.is",
}

# ---------------------------------------------------------------------------
# Auth — health-specific, returns 401 (per contract). Distinct from the CRM
# API's 403 to make it explicit that the gym-display frontend is a separate
# consumer with its own key rotation.
# ---------------------------------------------------------------------------

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_HEALTH_API_KEY: Optional[str] = None


def _load_health_key() -> str:
    """Lazy-load the health API key from Secrets Manager, cached per-process."""
    global _HEALTH_API_KEY
    if _HEALTH_API_KEY is None:
        from knowledge.secrets import get_health_api_key
        _HEALTH_API_KEY = get_health_api_key()
    return _HEALTH_API_KEY


def verify_health_api_key(api_key: Optional[str] = Security(_API_KEY_HEADER)):
    """Validate X-API-Key header. Returns 401 on missing/invalid (per spec).

    Distinct from verify_api_key() in main.py which returns 403 — keeps the
    health endpoint's contract independent of CRM API behavior.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
        )
    expected = _load_health_key()
    if api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
        )
    return api_key


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PlanResponse(BaseModel):
    """Response model for GET /api/health/today.

    blocks is returned as a JSON object (dict), never a stringified blob —
    psycopg2 + SQLAlchemy decode JSONB to dict for us.
    """

    plan_id: int
    plan_date: date
    phase: int
    week_num: int
    session_type: str
    display_name: Optional[str] = None
    target_rpe: Optional[float] = None
    target_hr_zone: Optional[int] = None
    est_duration_min: Optional[int] = None
    is_skipped: bool = False
    blocks: dict[str, Any]


class NoPlanResponse(BaseModel):
    """Returned when no row exists for today."""

    error: str = "no_plan"
    fallback: str = "rest day or check Mattermost"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_ct() -> date:
    """Return today's date in America/Chicago.

    Ryan is in West Bend, WI (Central Time). DST handled by zoneinfo.
    """
    return datetime.now(CT).date()


# Legacy fallback labels (kept local — this FastAPI app doesn't import artemis.*).
# Used only when a row predates the v2 reseed and lacks blocks.display_name.
_LEGACY_PRETTY = {
    "strength_a": "Strength A — Push/Legs",
    "strength_b": "Strength B — Pull/Hinge",
    "strength_c": "Strength C — Full Body",
    "cardio_intervals": "Cardio Intervals",
    "cardio_z2": "Cardio Zone 2",
    "walk": "Walk + mobility",
    "rest_mobility": "Rest / Mobility",
}


def _display_name(blocks: Any, session_type: Optional[str]) -> Optional[str]:
    """Canonical human program name. Prefer blocks['display_name'] (written by
    reseed_health_plan_v2); fall back to the legacy session_type label."""
    if isinstance(blocks, dict):
        dn = blocks.get("display_name")
        if dn:
            return dn
    if session_type is None:
        return None
    return _LEGACY_PRETTY.get(session_type, session_type)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/today")
def get_today(
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """Return the training plan for today (Central Time).

    200 → PlanResponse JSON
    404 → {"error": "no_plan", "fallback": "..."}
    401 → {"error": "unauthorized"}  (handled by verify_health_api_key)
    """
    today = _today_ct()

    row = db.execute(
        text("""
            SELECT plan_id, plan_date, phase, week_num, session_type,
                   target_rpe, target_hr_zone, est_duration_min,
                   is_skipped, blocks
            FROM health.plan
            WHERE plan_date = :d
        """),
        {"d": today},
    ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_plan",
                "fallback": "rest day or check Mattermost",
            },
        )

    return PlanResponse(
        plan_id=row["plan_id"],
        plan_date=row["plan_date"],
        phase=row["phase"],
        week_num=row["week_num"],
        session_type=row["session_type"],
        display_name=_display_name(row["blocks"], row["session_type"]),
        target_rpe=float(row["target_rpe"]) if row["target_rpe"] is not None else None,
        target_hr_zone=row["target_hr_zone"],
        est_duration_min=row["est_duration_min"],
        is_skipped=bool(row["is_skipped"]),
        blocks=row["blocks"],
    )


# ---------------------------------------------------------------------------
# /status — windowed payload for the gym-display /status page
# ---------------------------------------------------------------------------

WINDOW_DAYS = 5         # today ±5 → 11-day strip
TREND_DAYS = 30         # rolling trend window
HISTORY_COUNT = 3       # prior same-type sessions to compare against


class TrendPoint(BaseModel):
    date: date
    value: float


class DayStripEntry(BaseModel):
    plan_date: date
    session_type: Optional[str] = None
    display_name: Optional[str] = None
    is_skipped: bool = False
    is_logged: bool = False
    is_today: bool = False
    phase: Optional[int] = None
    week_num: Optional[int] = None


class ExerciseLog(BaseModel):
    log_type: str
    exercise: Optional[str] = None
    set_num: Optional[int] = None
    reps_done: Optional[int] = None
    weight_lbs: Optional[float] = None
    duration_sec: Optional[int] = None
    distance_m: Optional[float] = None
    hr_avg: Optional[int] = None
    hr_peak: Optional[int] = None
    rpe_actual: Optional[float] = None
    notes: Optional[str] = None


class LoggedSession(BaseModel):
    plan_id: int
    plan_date: date
    session_type: str
    display_name: Optional[str] = None
    phase: int
    week_num: int
    rpe_actual: Optional[float] = None
    logged_at: datetime
    notes: Optional[str] = None
    exercises: list[ExerciseLog]


class Banner(BaseModel):
    phase: int
    week_num: int
    phase_name: Optional[str] = None
    as_of_date: date


class TodaySummary(BaseModel):
    plan_id: Optional[int] = None
    session_type: Optional[str] = None
    display_name: Optional[str] = None
    is_skipped: bool = False
    is_logged: bool = False
    exists: bool = False


class StatusResponse(BaseModel):
    today: date
    window_start: date
    window_end: date
    today_summary: TodaySummary
    banner: Optional[Banner] = None
    day_strip: list[DayStripEntry]
    most_recent_session: Optional[LoggedSession] = None
    same_type_history: list[LoggedSession]
    rpe_trend: list[TrendPoint]
    weight_trend: list[TrendPoint]


def _serialize_log_row(r: dict[str, Any]) -> ExerciseLog:
    return ExerciseLog(
        log_type=r["log_type"],
        exercise=r["exercise"],
        set_num=r["set_num"],
        reps_done=r["reps_done"],
        weight_lbs=float(r["weight_lbs"]) if r["weight_lbs"] is not None else None,
        duration_sec=r["duration_sec"],
        distance_m=float(r["distance_m"]) if r["distance_m"] is not None else None,
        hr_avg=r["hr_avg"],
        hr_peak=r["hr_peak"],
        rpe_actual=float(r["rpe_actual"]) if r["rpe_actual"] is not None else None,
        notes=r["notes"],
    )


def _build_logged_session(db: Session, summary_row: dict[str, Any]) -> LoggedSession:
    exercises = db.execute(
        text("""
            SELECT log_type, exercise, set_num, reps_done, weight_lbs,
                   duration_sec, distance_m, hr_avg, hr_peak, rpe_actual, notes
            FROM health.session_log
            WHERE plan_id = :pid
              AND log_type IN ('strength_set', 'cardio_block')
            ORDER BY log_id
        """),
        {"pid": summary_row["plan_id"]},
    ).mappings().all()
    return LoggedSession(
        plan_id=summary_row["plan_id"],
        plan_date=summary_row["plan_date"],
        session_type=summary_row["session_type"],
        display_name=_display_name(summary_row.get("blocks"), summary_row["session_type"]),
        phase=summary_row["phase"],
        week_num=summary_row["week_num"],
        rpe_actual=float(summary_row["rpe_actual"]) if summary_row["rpe_actual"] is not None else None,
        logged_at=summary_row["logged_at"],
        notes=summary_row["notes"],
        exercises=[_serialize_log_row(dict(e)) for e in exercises],
    )


@router.get("/status", response_model=StatusResponse)
def get_status(
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """Windowed status payload for the /status page.

    Returns:
      * today_summary    — flags for /today → /status redirect logic
      * banner           — phase / week from today's plan (or most-recent)
      * day_strip        — 11 calendar days (today ±5) with logged flag
      * most_recent_session — most recent session_summary + its exercises
      * same_type_history   — prior 3 sessions of the same session_type
      * rpe_trend        — last 30 days of session_summary.rpe_actual
      * weight_trend     — last 30 days of daily_state.weight_lbs

    Series with no rows return empty arrays — never null.
    """
    today = _today_ct()
    window_start = today - timedelta(days=WINDOW_DAYS)
    window_end = today + timedelta(days=WINDOW_DAYS)
    trend_start = today - timedelta(days=TREND_DAYS)

    # 1) Day strip (today ±5)
    strip_rows = db.execute(
        text("""
            SELECT p.plan_id, p.plan_date, p.session_type, p.is_skipped,
                   p.phase, p.week_num, p.blocks,
                   EXISTS (
                     SELECT 1 FROM health.session_log sl
                     WHERE sl.plan_id = p.plan_id
                       AND sl.log_type = 'session_summary'
                   ) AS is_logged
            FROM health.plan p
            WHERE p.plan_date BETWEEN :s AND :e
            ORDER BY p.plan_date
        """),
        {"s": window_start, "e": window_end},
    ).mappings().all()

    rows_by_date = {r["plan_date"]: r for r in strip_rows}
    day_strip: list[DayStripEntry] = []
    for i in range(WINDOW_DAYS * 2 + 1):
        d = window_start + timedelta(days=i)
        r = rows_by_date.get(d)
        if r is None:
            day_strip.append(DayStripEntry(plan_date=d, is_today=(d == today)))
        else:
            day_strip.append(DayStripEntry(
                plan_date=r["plan_date"],
                session_type=r["session_type"],
                display_name=_display_name(r.get("blocks"), r["session_type"]),
                is_skipped=bool(r["is_skipped"]),
                is_logged=bool(r["is_logged"]),
                is_today=(r["plan_date"] == today),
                phase=r["phase"],
                week_num=r["week_num"],
            ))

    # 2) Today summary
    today_row = rows_by_date.get(today)
    today_summary = TodaySummary(
        plan_id=today_row["plan_id"] if today_row else None,
        session_type=today_row["session_type"] if today_row else None,
        display_name=_display_name(today_row.get("blocks"), today_row["session_type"]) if today_row else None,
        is_skipped=bool(today_row["is_skipped"]) if today_row else False,
        is_logged=bool(today_row["is_logged"]) if today_row else False,
        exists=today_row is not None,
    )

    # 3) Banner — phase/week of today (or most-recent past plan)
    banner: Optional[Banner] = None
    banner_src = today_row
    if banner_src is None:
        prev = db.execute(
            text("""
                SELECT phase, week_num, plan_date FROM health.plan
                WHERE plan_date <= :today
                ORDER BY plan_date DESC LIMIT 1
            """),
            {"today": today},
        ).mappings().first()
        banner_src = prev
    if banner_src is not None:
        phase_name_row = db.execute(
            text("SELECT phase_name FROM health.phase_config WHERE phase = :p"),
            {"p": banner_src["phase"]},
        ).mappings().first()
        banner = Banner(
            phase=banner_src["phase"],
            week_num=banner_src["week_num"],
            phase_name=phase_name_row["phase_name"] if phase_name_row else None,
            as_of_date=banner_src["plan_date"],
        )

    # 4) Most-recent logged session
    most_recent_row = db.execute(
        text("""
            SELECT sl.plan_id, sl.logged_at, sl.rpe_actual, sl.notes,
                   p.plan_date, p.session_type, p.phase, p.week_num, p.blocks
            FROM health.session_log sl
            JOIN health.plan p ON p.plan_id = sl.plan_id
            WHERE sl.log_type = 'session_summary'
            ORDER BY sl.logged_at DESC
            LIMIT 1
        """),
    ).mappings().first()

    most_recent_session: Optional[LoggedSession] = None
    same_type_history: list[LoggedSession] = []
    if most_recent_row is not None:
        most_recent_session = _build_logged_session(db, dict(most_recent_row))

        history_rows = db.execute(
            text("""
                SELECT sl.plan_id, sl.logged_at, sl.rpe_actual, sl.notes,
                       p.plan_date, p.session_type, p.phase, p.week_num, p.blocks
                FROM health.session_log sl
                JOIN health.plan p ON p.plan_id = sl.plan_id
                WHERE sl.log_type = 'session_summary'
                  AND p.session_type = :stype
                  AND sl.plan_id <> :exclude
                ORDER BY p.plan_date DESC
                LIMIT :n
            """),
            {
                "stype": most_recent_row["session_type"],
                "exclude": most_recent_row["plan_id"],
                "n": HISTORY_COUNT,
            },
        ).mappings().all()
        for hr in history_rows:
            same_type_history.append(_build_logged_session(db, dict(hr)))

    # 5) RPE trend (last 30 days, session_summary rows)
    rpe_rows = db.execute(
        text("""
            SELECT p.plan_date AS d, sl.rpe_actual AS v
            FROM health.session_log sl
            JOIN health.plan p ON p.plan_id = sl.plan_id
            WHERE sl.log_type = 'session_summary'
              AND sl.rpe_actual IS NOT NULL
              AND p.plan_date BETWEEN :s AND :t
            ORDER BY p.plan_date
        """),
        {"s": trend_start, "t": today},
    ).mappings().all()
    rpe_trend = [TrendPoint(date=r["d"], value=float(r["v"])) for r in rpe_rows]

    # 6) Body-weight trend (last 30 days, daily_state)
    weight_rows = db.execute(
        text("""
            SELECT state_date AS d, weight_lbs AS v
            FROM health.daily_state
            WHERE state_date BETWEEN :s AND :t
              AND weight_lbs IS NOT NULL
            ORDER BY state_date
        """),
        {"s": trend_start, "t": today},
    ).mappings().all()
    weight_trend = [TrendPoint(date=r["d"], value=float(r["v"])) for r in weight_rows]

    return StatusResponse(
        today=today,
        window_start=window_start,
        window_end=window_end,
        today_summary=today_summary,
        banner=banner,
        day_strip=day_strip,
        most_recent_session=most_recent_session,
        same_type_history=same_type_history,
        rpe_trend=rpe_trend,
        weight_trend=weight_trend,
    )


# ---------------------------------------------------------------------------
# /log — POST: insert N session_log rows for one exercise (or session_summary)
# ---------------------------------------------------------------------------
#
# Schema notes (see migrations/013_health_schema.sql):
#   - log_type     IN ('strength_set', 'cardio_block', 'session_summary')
#   - logged_via   IN ('mattermost', 'voice', 'manual', 'inferred')
#
# `gym-display` writes use logged_via='manual'. The CHECK constraint does NOT
# accept 'gym_display' today; widening it is a future migration if a separate
# value is wanted.

ALLOWED_LOG_TYPES = ("strength_set", "cardio_block", "session_summary")
LOGGED_VIA_GYM_DISPLAY = "manual"  # closest existing CHECK value


class LogSetIn(BaseModel):
    """One set of a strength exercise OR one cardio block (single set)."""
    set_num: Optional[int] = Field(default=None, ge=1, le=99)
    reps_done: Optional[int] = Field(default=None, ge=0, le=999)
    weight_lbs: Optional[float] = Field(default=None, ge=0, le=9999)
    rpe_actual: Optional[float] = Field(default=None, ge=1, le=10)
    duration_sec: Optional[int] = Field(default=None, ge=0, le=86_400)
    distance_m: Optional[float] = Field(default=None, ge=0, le=999_999)
    hr_avg: Optional[int] = Field(default=None, ge=0, le=300)
    hr_peak: Optional[int] = Field(default=None, ge=0, le=300)
    is_skipped: bool = False
    notes: Optional[str] = None


class LogExerciseIn(BaseModel):
    """Payload posted by gym-display for one exercise's sets.

    plan_id is optional — resolved to today's plan when omitted.
    log_type chooses the column shape: strength_set / cardio_block /
    session_summary.  For session_summary, `exercise` should be null and
    `sets` should be a single entry carrying rpe_actual + notes.

    session_rpe (optional) — when present, an additional session_summary
    row is INSERTed in the same transaction. Lets the "Finish workout"
    action be a single POST instead of two round trips.
    """
    plan_id: Optional[int] = None
    exercise: Optional[str] = None
    log_type: str = Field(default="strength_set")
    sets: list[LogSetIn] = Field(default_factory=list)
    notes: Optional[str] = None
    session_rpe: Optional[float] = Field(default=None, ge=1, le=10)


class LogRowOut(BaseModel):
    log_id: int
    plan_id: Optional[int] = None
    log_type: str
    exercise: Optional[str] = None
    set_num: Optional[int] = None
    reps_done: Optional[int] = None
    weight_lbs: Optional[float] = None
    duration_sec: Optional[int] = None
    distance_m: Optional[float] = None
    hr_avg: Optional[int] = None
    hr_peak: Optional[int] = None
    rpe_actual: Optional[float] = None
    notes: Optional[str] = None
    is_skipped: bool = False
    logged_at: datetime
    logged_via: str


class LogResponse(BaseModel):
    plan_id: Optional[int] = None
    inserted: int
    rows: list[LogRowOut]


def _resolve_plan_id(db: Session, supplied: Optional[int]) -> Optional[int]:
    if supplied is not None:
        return supplied
    today = _today_ct()
    row = db.execute(
        text("SELECT plan_id FROM health.plan WHERE plan_date = :d"),
        {"d": today},
    ).mappings().first()
    return row["plan_id"] if row else None


@router.post("/log", response_model=LogResponse)
def post_log(
    body: LogExerciseIn,
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """Insert N session_log rows for one exercise (or a session_summary).

    All rows are inserted in a single transaction. Returns the inserted
    rows (with server-assigned log_id + logged_at) so the UI can mark the
    exercise as logged without a follow-up GET.

    400 on invalid log_type or empty sets list.
    """
    if body.log_type not in ALLOWED_LOG_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_log_type",
                "allowed": list(ALLOWED_LOG_TYPES),
            },
        )
    if not body.sets:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_sets"},
        )

    plan_id = _resolve_plan_id(db, body.plan_id)
    inserted_rows: list[dict[str, Any]] = []

    insert_sql = text("""
        INSERT INTO health.session_log (
            plan_id, log_type, exercise,
            set_num, reps_done, weight_lbs,
            duration_sec, distance_m,
            hr_avg, hr_peak, rpe_actual,
            notes, is_skipped, logged_via
        ) VALUES (
            :plan_id, :log_type, :exercise,
            :set_num, :reps_done, :weight_lbs,
            :duration_sec, :distance_m,
            :hr_avg, :hr_peak, :rpe_actual,
            :notes, :is_skipped, :logged_via
        )
        RETURNING log_id, plan_id, log_type, exercise,
                  set_num, reps_done, weight_lbs,
                  duration_sec, distance_m,
                  hr_avg, hr_peak, rpe_actual,
                  notes, is_skipped, logged_at, logged_via
    """)

    try:
        for s in body.sets:
            row = db.execute(
                insert_sql,
                {
                    "plan_id": plan_id,
                    "log_type": body.log_type,
                    "exercise": body.exercise,
                    "set_num": s.set_num,
                    "reps_done": s.reps_done,
                    "weight_lbs": s.weight_lbs,
                    "duration_sec": s.duration_sec,
                    "distance_m": s.distance_m,
                    "hr_avg": s.hr_avg,
                    "hr_peak": s.hr_peak,
                    "rpe_actual": s.rpe_actual,
                    "notes": s.notes if s.notes is not None else body.notes,
                    "is_skipped": s.is_skipped,
                    "logged_via": LOGGED_VIA_GYM_DISPLAY,
                },
            ).mappings().first()
            if row is not None:
                inserted_rows.append(dict(row))
        # Optional in-band session_summary write — same transaction.
        if body.session_rpe is not None and body.log_type != "session_summary":
            row = db.execute(
                insert_sql,
                {
                    "plan_id": plan_id,
                    "log_type": "session_summary",
                    "exercise": None,
                    "set_num": None,
                    "reps_done": None,
                    "weight_lbs": None,
                    "duration_sec": None,
                    "distance_m": None,
                    "hr_avg": None,
                    "hr_peak": None,
                    "rpe_actual": body.session_rpe,
                    "notes": body.notes,
                    "is_skipped": False,
                    "logged_via": LOGGED_VIA_GYM_DISPLAY,
                },
            ).mappings().first()
            if row is not None:
                inserted_rows.append(dict(row))
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "insert_failed", "message": str(e)},
        )

    return LogResponse(
        plan_id=plan_id,
        inserted=len(inserted_rows),
        rows=[
            LogRowOut(
                log_id=r["log_id"],
                plan_id=r["plan_id"],
                log_type=r["log_type"],
                exercise=r["exercise"],
                set_num=r["set_num"],
                reps_done=r["reps_done"],
                weight_lbs=float(r["weight_lbs"]) if r["weight_lbs"] is not None else None,
                duration_sec=r["duration_sec"],
                distance_m=float(r["distance_m"]) if r["distance_m"] is not None else None,
                hr_avg=r["hr_avg"],
                hr_peak=r["hr_peak"],
                rpe_actual=float(r["rpe_actual"]) if r["rpe_actual"] is not None else None,
                notes=r["notes"],
                is_skipped=bool(r["is_skipped"]),
                logged_at=r["logged_at"],
                logged_via=r["logged_via"],
            )
            for r in inserted_rows
        ],
    )


# ---------------------------------------------------------------------------
# /today/logged — which exercises today already have logs (UI completion state)
# ---------------------------------------------------------------------------

class LoggedExerciseEntry(BaseModel):
    exercise: str
    log_type: str
    set_count: int


class LoggedTodayResponse(BaseModel):
    plan_id: Optional[int] = None
    exercises: list[LoggedExerciseEntry]
    has_session_summary: bool


@router.get("/today/logged", response_model=LoggedTodayResponse)
def get_today_logged(
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """List the exercises already logged for today's plan.

    Returned in distinct (exercise, log_type) groups with the set count.
    Exercises with no rows are simply absent from the list.
    The UI uses this to mark exercises as ✓ done on first load.
    """
    today = _today_ct()
    plan_row = db.execute(
        text("SELECT plan_id FROM health.plan WHERE plan_date = :d"),
        {"d": today},
    ).mappings().first()
    if plan_row is None:
        return LoggedTodayResponse(plan_id=None, exercises=[], has_session_summary=False)

    plan_id = plan_row["plan_id"]
    rows = db.execute(
        text("""
            SELECT exercise, log_type, COUNT(*) AS n
            FROM health.session_log
            WHERE plan_id = :pid
              AND exercise IS NOT NULL
              AND log_type IN ('strength_set', 'cardio_block')
            GROUP BY exercise, log_type
            ORDER BY exercise
        """),
        {"pid": plan_id},
    ).mappings().all()

    summary_row = db.execute(
        text("""
            SELECT 1 FROM health.session_log
            WHERE plan_id = :pid AND log_type = 'session_summary'
            LIMIT 1
        """),
        {"pid": plan_id},
    ).mappings().first()

    return LoggedTodayResponse(
        plan_id=plan_id,
        exercises=[
            LoggedExerciseEntry(
                exercise=r["exercise"],
                log_type=r["log_type"],
                set_count=int(r["n"]),
            )
            for r in rows
        ],
        has_session_summary=summary_row is not None,
    )


# ---------------------------------------------------------------------------
# /last_logged — pre-fill source for gym-display stepper UI
# ---------------------------------------------------------------------------

class LastLoggedEntry(BaseModel):
    exercise: str
    plan_date: Optional[date] = None
    weight_lbs: Optional[float] = None
    reps_done: Optional[int] = None
    rpe_actual: Optional[float] = None
    duration_sec: Optional[int] = None
    distance_m: Optional[float] = None
    hr_avg: Optional[int] = None
    hr_peak: Optional[int] = None


class LastLoggedResponse(BaseModel):
    by_exercise: dict[str, LastLoggedEntry]


@router.get("/last_logged", response_model=LastLoggedResponse)
def get_last_logged(
    exercises: str = "",
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """Batch lookup: the most-recent prior strength_set / cardio_block per
    exercise name. Used by gym-display to pre-fill stepper defaults so
    the common case is zero edits.

    Query: /last_logged?exercises=Goblet+squat,Plank,Hollow+hold
    Response keys: exact exercise names from the query (unknowns are
    silently absent from the result).
    """
    names = [n.strip() for n in exercises.split(",") if n.strip()]
    if not names:
        return LastLoggedResponse(by_exercise={})

    rows = db.execute(
        text("""
            SELECT DISTINCT ON (sl.exercise)
                sl.exercise,
                sl.weight_lbs, sl.reps_done, sl.rpe_actual,
                sl.duration_sec, sl.distance_m, sl.hr_avg, sl.hr_peak,
                p.plan_date
            FROM health.session_log sl
            JOIN health.plan p ON p.plan_id = sl.plan_id
            WHERE sl.exercise = ANY(:names)
              AND sl.log_type IN ('strength_set', 'cardio_block')
            ORDER BY sl.exercise, sl.logged_at DESC
        """),
        {"names": names},
    ).mappings().all()

    out: dict[str, LastLoggedEntry] = {}
    for r in rows:
        out[r["exercise"]] = LastLoggedEntry(
            exercise=r["exercise"],
            plan_date=r["plan_date"],
            weight_lbs=float(r["weight_lbs"]) if r["weight_lbs"] is not None else None,
            reps_done=r["reps_done"],
            rpe_actual=float(r["rpe_actual"]) if r["rpe_actual"] is not None else None,
            duration_sec=r["duration_sec"],
            distance_m=float(r["distance_m"]) if r["distance_m"] is not None else None,
            hr_avg=r["hr_avg"],
            hr_peak=r["hr_peak"],
        )
    return LastLoggedResponse(by_exercise=out)
