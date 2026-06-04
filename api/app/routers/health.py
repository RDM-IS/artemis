"""Health/training plan endpoints.

GET /api/health/today   → today's training plan from health.plan
GET /api/health/status  → windowed status payload for the /status page
                          (11-day strip, most-recent logged session,
                           same-type history, RPE + body-weight trends)

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
from pydantic import BaseModel
from sqlalchemy import text
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
                   p.phase, p.week_num,
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
                   p.plan_date, p.session_type, p.phase, p.week_num
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
                       p.plan_date, p.session_type, p.phase, p.week_num
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
