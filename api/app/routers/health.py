"""Health/training plan endpoints.

GET  /api/health/today          → today's training plan from health.plan
GET  /api/health/today/logged   → which exercises today already have logs
GET  /api/health/last_logged    → batch: most-recent prior set per exercise
                                  (used by gym-display to pre-fill steppers)
GET  /api/health/status         → windowed status payload for /status page
GET  /api/health/plan           → read-only plan range (≤ 14 days) with a
                                  derived status per day (Tomorrow / Week)
GET  /api/health/overview       → Status page (STATUS-1): program, this week,
                                  today's progress + check-in, strength
                                  progress, check-in trends, patterns, flags,
                                  body weight — scoped to the current program
GET  /api/health/sessions       → per-day plan + per-set rows + computed
                                  aggregates + outlier flags for the
                                  Status page facts read-back (no
                                  generated coaching prose)
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

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from knowledge.machine_setup import parse_setup

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


_WATCH_INGEST_KEY: Optional[str] = None


def _load_watch_key() -> str:
    """Lazy-load the WATCH-1 ingest key, cached per-process."""
    global _WATCH_INGEST_KEY
    if _WATCH_INGEST_KEY is None:
        from knowledge.secrets import get_watch_ingest_key
        _WATCH_INGEST_KEY = get_watch_ingest_key()
    return _WATCH_INGEST_KEY


def verify_watch_ingest_key(api_key: Optional[str] = Security(_API_KEY_HEADER)):
    """WATCH-1: POST /ingest accepts ONLY the watch key.

    The display key (gym-display, the Shortcut) is rejected here, and this key
    is rejected on every other route — they are checked by different
    dependencies against different secrets. A leaked watch key can write
    samples; it cannot read the plan or log a session.
    """
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"error": "unauthorized"})
    if api_key != _load_watch_key():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"error": "unauthorized"})
    return api_key


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
    "recovery_flow": "Recovery Flow",
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
    # Per-set notes of that row — gym-display parses `seat=/pad=/range=` (and a
    # legacy `setting=<n>`) from it to prefill the machine setup fields.
    notes: Optional[str] = None


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
                sl.notes, p.plan_date
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
            notes=r["notes"],
        )
    return LastLoggedResponse(by_exercise=out)


# ---------------------------------------------------------------------------
# /sessions — per-day plan + per-set rows + aggregates + outliers
# ---------------------------------------------------------------------------
#
# Facts read-back for the Status page. No generated prose; no trainer-voice
# synthesis — just the rows + computed comparisons. The user gets to validate
# their writes by reading exactly what landed in session_log.
#
# Window: today-(days-1) .. today in America/Chicago. Default 7 days.

DEFAULT_SESSIONS_DAYS = 7
MAX_SESSIONS_DAYS = 30
HIGH_RPE_THRESHOLD = 9.0
PAIN_KEYWORDS = ("pain", "injury", "hurt", "tweak")


class SessionSetRow(BaseModel):
    log_id: int
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


class SessionSummaryRow(BaseModel):
    rpe_actual: Optional[float] = None
    notes: Optional[str] = None
    logged_at: datetime


class OutlierFlags(BaseModel):
    """Pure data flags — outliers that EXIST in the rows. No interpretation."""
    high_rpe_sets: list[dict[str, Any]] = Field(default_factory=list)
    incomplete: bool = False
    incomplete_logged: int = 0
    incomplete_planned: int = 0
    pain_notes: list[str] = Field(default_factory=list)


class SessionDayRow(BaseModel):
    plan_date: date
    plan_id: Optional[int] = None
    session_type: Optional[str] = None
    display_name: Optional[str] = None
    phase: Optional[int] = None
    week_num: Optional[int] = None
    target_rpe: Optional[float] = None
    target_hr_zone: Optional[int] = None
    is_skipped: bool = False
    is_today: bool = False
    planned_set_count: int = 0
    logged_set_count: int = 0
    sets: list[SessionSetRow] = Field(default_factory=list)
    session_summary: Optional[SessionSummaryRow] = None
    avg_set_rpe: Optional[float] = None
    hr_avg: Optional[int] = None      # avg across cardio_block rows
    hr_peak: Optional[int] = None     # max across cardio_block rows
    total_work_sec: int = 0
    outliers: OutlierFlags = Field(default_factory=OutlierFlags)


class SessionsResponse(BaseModel):
    today: date
    window_start: date
    window_end: date
    days: list[SessionDayRow]


def _planned_set_count(blocks: Any) -> int:
    """Walk a plan.blocks JSONB and return the total expected set count.

    Mirrors src/lib/log-state.ts totalSetsFor: circuit rounds × occurrences
    summed across main + finisher; intervals/steady/walk/mobility = 1.
    """
    if not isinstance(blocks, dict):
        return 0
    t = blocks.get("type")
    total = 0
    if t == "circuit":
        rounds = max(1, int(blocks.get("rounds") or 1))
        exs = blocks.get("exercises")
        n = len(exs) if isinstance(exs, list) else 0
        total += rounds * n
    elif t in ("intervals", "steady", "walk", "mobility"):
        total += 1
    # recovery_flow: 0 — a flow logs one session_summary, never sets, so a
    # planned set would read "0 of 1" on the Status page for a finished flow.
    fin = blocks.get("finisher")
    if isinstance(fin, dict):
        f_rounds = max(1, int(fin.get("rounds") or 1))
        f_exs = fin.get("exercises")
        f_n = len(f_exs) if isinstance(f_exs, list) else 0
        total += f_rounds * f_n
    return total


def _contains_pain_keyword(s: Optional[str]) -> bool:
    if not s:
        return False
    lo = s.lower()
    return any(k in lo for k in PAIN_KEYWORDS)


@router.get("/sessions", response_model=SessionsResponse)
def get_sessions(
    days: int = DEFAULT_SESSIONS_DAYS,
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """Per-day plan + per-set rows + computed aggregates for the Status page.

    Returns one row per calendar day in [today-(days-1), today] (America/
    Chicago). Per-day fields cover:

      * plan metadata + planned_set_count derived from blocks JSONB
      * sets[] — every session_log row of log_type strength_set / cardio_block
      * session_summary if present
      * avg_set_rpe — avg of non-null rpe_actual across the day's sets
      * hr_avg, hr_peak — aggregated from cardio_block rows only
      * total_work_sec — sum of duration_sec across the day's sets
      * outliers — {high_rpe_sets, incomplete, pain_notes}: pure data flags

    No generated coaching prose. The page renders facts + computed
    comparisons; trainer-voice synthesis is the future autoregulator.
    """
    days = max(1, min(MAX_SESSIONS_DAYS, days))
    today = _today_ct()
    window_start = today - timedelta(days=days - 1)
    window_end = today

    # Plans in the window.
    plan_rows = db.execute(
        text("""
            SELECT plan_id, plan_date, phase, week_num, session_type, blocks,
                   target_rpe, target_hr_zone, is_skipped
            FROM health.plan
            WHERE plan_date BETWEEN :s AND :e
            ORDER BY plan_date
        """),
        {"s": window_start, "e": window_end},
    ).mappings().all()

    plans_by_date: dict[date, dict[str, Any]] = {r["plan_date"]: dict(r) for r in plan_rows}
    plan_ids = [p["plan_id"] for p in plan_rows]

    # All session_log rows tied to these plan_ids in one query.
    log_rows: list[dict[str, Any]] = []
    if plan_ids:
        log_rows = [
            dict(r)
            for r in db.execute(
                text("""
                    SELECT log_id, plan_id, log_type, exercise, set_num,
                           reps_done, weight_lbs, duration_sec, distance_m,
                           hr_avg, hr_peak, rpe_actual, notes, is_skipped,
                           logged_at, logged_via
                    FROM health.session_log
                    WHERE plan_id = ANY(:pids)
                    ORDER BY plan_id, log_id
                """),
                {"pids": plan_ids},
            ).mappings().all()
        ]

    logs_by_plan: dict[int, list[dict[str, Any]]] = {}
    for r in log_rows:
        logs_by_plan.setdefault(r["plan_id"], []).append(r)

    out_days: list[SessionDayRow] = []
    for i in range(days):
        d = window_start + timedelta(days=i)
        plan = plans_by_date.get(d)
        sets: list[SessionSetRow] = []
        summary: Optional[SessionSummaryRow] = None
        avg_rpe: Optional[float] = None
        agg_hr_avg: Optional[int] = None
        agg_hr_peak: Optional[int] = None
        total_work_sec = 0
        high_rpe: list[dict[str, Any]] = []
        pain_notes: list[str] = []
        logged_set_count = 0

        if plan is not None:
            for r in logs_by_plan.get(plan["plan_id"], []):
                if r["log_type"] == "session_summary":
                    summary = SessionSummaryRow(
                        rpe_actual=float(r["rpe_actual"]) if r["rpe_actual"] is not None else None,
                        notes=r["notes"],
                        logged_at=r["logged_at"],
                    )
                    if _contains_pain_keyword(r["notes"]):
                        pain_notes.append(r["notes"])
                    continue
                if r["log_type"] not in ("strength_set", "cardio_block"):
                    continue
                row = SessionSetRow(
                    log_id=r["log_id"],
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
                sets.append(row)
                logged_set_count += 1
                if row.duration_sec:
                    total_work_sec += row.duration_sec
                if row.rpe_actual is not None and row.rpe_actual >= HIGH_RPE_THRESHOLD:
                    high_rpe.append({
                        "exercise": row.exercise,
                        "set_num": row.set_num,
                        "rpe_actual": row.rpe_actual,
                    })
                if _contains_pain_keyword(row.notes):
                    pain_notes.append(row.notes or "")

            # Aggregates: avg of non-null RPE; HR from cardio rows only.
            rpes = [s.rpe_actual for s in sets if s.rpe_actual is not None]
            if rpes:
                avg_rpe = sum(rpes) / len(rpes)
            cardio = [s for s in sets if s.log_type == "cardio_block"]
            avgs = [s.hr_avg for s in cardio if s.hr_avg is not None]
            peaks = [s.hr_peak for s in cardio if s.hr_peak is not None]
            if avgs:
                agg_hr_avg = round(sum(avgs) / len(avgs))
            if peaks:
                agg_hr_peak = max(peaks)

        planned = _planned_set_count(plan.get("blocks") if plan else None)
        # Incomplete = at least one set logged but fewer than planned.
        incomplete = logged_set_count > 0 and planned > 0 and logged_set_count < planned

        out_days.append(SessionDayRow(
            plan_date=d,
            plan_id=plan["plan_id"] if plan else None,
            session_type=plan["session_type"] if plan else None,
            display_name=_display_name(plan.get("blocks") if plan else None,
                                       plan["session_type"] if plan else None),
            phase=plan["phase"] if plan else None,
            week_num=plan["week_num"] if plan else None,
            target_rpe=float(plan["target_rpe"]) if plan and plan["target_rpe"] is not None else None,
            target_hr_zone=plan["target_hr_zone"] if plan else None,
            is_skipped=bool(plan["is_skipped"]) if plan else False,
            is_today=(d == today),
            planned_set_count=planned,
            logged_set_count=logged_set_count,
            sets=sets,
            session_summary=summary,
            avg_set_rpe=round(avg_rpe, 2) if avg_rpe is not None else None,
            hr_avg=agg_hr_avg,
            hr_peak=agg_hr_peak,
            total_work_sec=total_work_sec,
            outliers=OutlierFlags(
                high_rpe_sets=high_rpe,
                incomplete=incomplete,
                incomplete_logged=logged_set_count,
                incomplete_planned=planned,
                pain_notes=pain_notes,
            ),
        ))

    return SessionsResponse(
        today=today,
        window_start=window_start,
        window_end=window_end,
        days=out_days,
    )


# ---------------------------------------------------------------------------
# /plan — GET: read-only plan range for gym-display Tomorrow / Week (GD-WEEK)
# ---------------------------------------------------------------------------
#
#   GET /api/health/plan?from=YYYY-MM-DD&to=YYYY-MM-DD   (inclusive, ≤ 14 days)
#
# `from` defaults to today and `to` to from+6, both in the ACTIVE timezone
# (acos.timezone_overrides, else home). Each day carries the stored blocks
# (adjustment included) and a status derived from session_log + plan.status:
#
#   done      a finished session: a real session_summary (flow: "complete"),
#             all planned sets logged, plan.status='completed', or a rest day
#             that has passed
#   partial   some real logs but not finished (flow: a "partial" summary)
#   missed    a past training day with no real logs (inferred rows don't count)
#   today     today, nothing logged yet
#   upcoming  a future day
#
# Past and current days also return `logged` — per-exercise sets actually done.

PLAN_RANGE_MAX_DAYS = 14
PLAN_STATUSES = ("done", "partial", "missed", "upcoming", "today")
HOME_TIMEZONE = "America/Chicago"   # mirrors artemis.config.HOME_TIMEZONE


class LoggedExercise(BaseModel):
    exercise: str
    log_type: str
    sets: int
    reps: list[Optional[int]] = Field(default_factory=list)
    top_weight_lbs: Optional[float] = None
    duration_sec: Optional[int] = None
    skipped: int = 0


class PlanDay(BaseModel):
    plan_id: int
    plan_date: date
    session_type: str
    display_name: Optional[str] = None
    phase: int
    week_num: int
    target_rpe: Optional[float] = None
    est_duration_min: Optional[int] = None
    location: Optional[str] = None
    is_skipped: bool = False
    adjusted: bool = False
    status: str
    blocks: dict[str, Any]
    logged: list[LoggedExercise] = Field(default_factory=list)
    summary_notes: Optional[str] = None


class PlanRangeResponse(BaseModel):
    today: date
    timezone: str
    range_from: date
    range_to: date
    days: list[PlanDay]


def _active_timezone(db: Session) -> str:
    """Override if set and unexpired (same rule as artemis.quiet_hours), else home."""
    try:
        row = db.execute(
            text("SELECT timezone FROM acos.timezone_overrides "
                 "WHERE id = 1 AND expires_at > now()")
        ).mappings().first()
        if row and row.get("timezone"):
            ZoneInfo(row["timezone"])
            return row["timezone"]
    except Exception:  # missing table, bad zone name — fall back to home
        pass
    return HOME_TIMEZONE


def _is_rest_day(session_type: Optional[str], blocks: Any) -> bool:
    """A true rest day (incl. a check-in day off) — never 'missed'. A Recovery
    Flow is a session, even on a rest_mobility row."""
    b = blocks if isinstance(blocks, dict) else {}
    if b.get("type") == "recovery_flow" or session_type == "recovery_flow":
        return False
    return session_type == "rest_mobility" or b.get("type") == "mobility"


def derive_day_status(plan: dict[str, Any], logs: list[dict[str, Any]], today: date) -> str:
    """Pure: one day's status from its plan row and session_log rows."""
    real = [r for r in logs if r.get("logged_via") != "inferred"]
    summaries = [r for r in real if r["log_type"] == "session_summary"]
    notes = [(r.get("notes") or "") for r in summaries]
    work = [r for r in real if r["log_type"] in ("strength_set", "cardio_block")]
    done_sets = [r for r in work if not r.get("is_skipped")]
    blocks = plan.get("blocks") if isinstance(plan.get("blocks"), dict) else {}
    d = plan["plan_date"]

    if any(n.startswith("recovery_flow: complete") for n in notes):
        return "done"
    if plan.get("status") == "completed":
        return "done"
    if summaries and not all(n.startswith("recovery_flow: partial") for n in notes):
        return "done"
    planned = _planned_set_count(blocks)
    if work:
        return "done" if planned and len(done_sets) >= planned else "partial"
    if summaries:            # only flow "partial" summaries
        return "partial"
    if d > today:
        return "upcoming"
    if d == today:
        return "today"
    if _is_rest_day(plan.get("session_type"), blocks) and not plan.get("is_skipped"):
        return "done"
    return "missed"


def summarize_logs(logs: list[dict[str, Any]]) -> list[LoggedExercise]:
    """Per-exercise sets actually logged (inferred rows excluded), in log order."""
    out: dict[str, dict[str, Any]] = {}
    for r in logs:
        if r.get("logged_via") == "inferred" or r["log_type"] not in ("strength_set", "cardio_block"):
            continue
        key = r.get("exercise") or r["log_type"]
        e = out.setdefault(key, {"exercise": key, "log_type": r["log_type"], "sets": 0,
                                 "reps": [], "top_weight_lbs": None, "duration_sec": None,
                                 "skipped": 0})
        if r.get("is_skipped"):
            e["skipped"] += 1
            continue
        e["sets"] += 1
        e["reps"].append(r.get("reps_done"))
        w = r.get("weight_lbs")
        if w is not None and (e["top_weight_lbs"] is None or float(w) > e["top_weight_lbs"]):
            e["top_weight_lbs"] = float(w)
        if r.get("duration_sec"):
            e["duration_sec"] = (e["duration_sec"] or 0) + int(r["duration_sec"])
    return [LoggedExercise(**e) for e in out.values()]


def _parse_day(value: Optional[str], name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "bad_date", "param": name})


def _plan_days(db: Session, start: date, end: date,
               today: date) -> tuple[list["PlanDay"], dict[int, list[dict[str, Any]]]]:
    """Plan rows in [start, end] as PlanDay (shared status derivation), plus
    the raw session_log rows per plan_id. Used by /plan and /overview."""
    plan_rows = db.execute(
        text("""
            SELECT plan_id, plan_date, phase, week_num, session_type, blocks,
                   target_rpe, est_duration_min, is_skipped, status
            FROM health.plan
            WHERE plan_date BETWEEN :s AND :e
            ORDER BY plan_date
        """),
        {"s": start, "e": end},
    ).mappings().all()
    plans = [dict(r) for r in plan_rows]
    pids = [p["plan_id"] for p in plans]

    logs_by_plan: dict[int, list[dict[str, Any]]] = {}
    if pids:
        for r in db.execute(
            text("""
                SELECT plan_id, log_type, exercise, set_num, reps_done, weight_lbs,
                       duration_sec, rpe_actual, notes, is_skipped, logged_via
                FROM health.session_log
                WHERE plan_id = ANY(:pids)
                ORDER BY plan_id, log_id
            """),
            {"pids": pids},
        ).mappings().all():
            logs_by_plan.setdefault(r["plan_id"], []).append(dict(r))

    days: list[PlanDay] = []
    for p in plans:
        blocks = p["blocks"] if isinstance(p["blocks"], dict) else {}
        logs = logs_by_plan.get(p["plan_id"], [])
        summary = next((r.get("notes") for r in reversed(logs)
                        if r["log_type"] == "session_summary" and r.get("logged_via") != "inferred"),
                       None)
        days.append(PlanDay(
            plan_id=p["plan_id"],
            plan_date=p["plan_date"],
            session_type=p["session_type"],
            display_name=_display_name(blocks, p["session_type"]),
            phase=p["phase"],
            week_num=p["week_num"],
            target_rpe=float(p["target_rpe"]) if p["target_rpe"] is not None else None,
            est_duration_min=p["est_duration_min"],
            location=blocks.get("location"),
            is_skipped=bool(p["is_skipped"]),
            adjusted=isinstance(blocks.get("adjustment"), dict),
            status=derive_day_status(p, logs, today),
            blocks=blocks,
            logged=summarize_logs(logs) if p["plan_date"] <= today else [],
            summary_notes=summary if p["plan_date"] <= today else None,
        ))
    return days, logs_by_plan


@router.get("/plan", response_model=PlanRangeResponse)
def get_plan_range(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """Plan rows for [from, to] with derived status. Read-only.

    400 → {"error": "range_too_large", "max_days": 14} | {"error": "bad_range"}
          | {"error": "bad_date", "param": ...}
    """
    tz_name = _active_timezone(db)
    today = datetime.now(ZoneInfo(tz_name)).date()
    start = _parse_day(from_, "from") or today
    end = _parse_day(to, "to") or (start + timedelta(days=6))
    if end < start:
        raise HTTPException(status_code=400, detail={"error": "bad_range"})
    if (end - start).days + 1 > PLAN_RANGE_MAX_DAYS:
        raise HTTPException(status_code=400,
                            detail={"error": "range_too_large", "max_days": PLAN_RANGE_MAX_DAYS})

    days, _ = _plan_days(db, start, end, today)
    return PlanRangeResponse(today=today, timezone=tz_name, range_from=start,
                             range_to=end, days=days)



# ---------------------------------------------------------------------------
# /overview — GET: the Status page (STATUS-1)
# ---------------------------------------------------------------------------
#
# Everything except body weight is scoped to the CURRENT program
# (plan_date >= program.anchor). The program comes from acos.system_state
# key `health_program` (written by the reseed script); without it, it is
# derived from the plan rows: the earliest week-1 row of today's phase within
# this program's span. Day status reuses the /plan derivation (_plan_days).

PROGRAM_STATE_KEY = "health_program"
OVERVIEW_CHECKIN_DAYS = 14
OVERVIEW_WEIGHT_DAYS = 30
TREND_TOLERANCE = 0.02          # ±2% of load × reps counts as flat

_PAIN_NOTE_RE = re.compile(r"(?:^|;)\s*pain=([a-z][a-z ]*?)\s*:\s*(\d)\s*(?=;|$)", re.I)


class ProgramInfo(BaseModel):
    name: Optional[str] = None
    phase: int
    week: int
    weeks_total: int
    anchor: date
    deload_week: Optional[int] = None
    weeks_to_deload: Optional[int] = None
    week_start: date
    week_end: date
    sessions_done: int = 0
    sessions_planned: int = 0
    source: str = "state"          # "state" | "derived"


class ProgressOut(BaseModel):
    unit: str                      # "sets" | "minutes" | "rest"
    done: Optional[float] = None
    planned: Optional[float] = None


class CheckinOut(BaseModel):
    date: date
    sleep_hrs: Optional[float] = None
    energy: Optional[int] = None
    weight_lbs: Optional[float] = None
    resting_hr: Optional[int] = None
    soreness: dict[str, int] = Field(default_factory=dict)
    pain: dict[str, int] = Field(default_factory=dict)


class AdjustmentOut(BaseModel):
    summary: list[str] = Field(default_factory=list)
    rules_fired: list[str] = Field(default_factory=list)


class TodayOverview(BaseModel):
    date: date
    day: Optional[PlanDay] = None
    progress: Optional[ProgressOut] = None
    checkin: Optional[CheckinOut] = None
    adjustment: Optional[AdjustmentOut] = None


class TopSet(BaseModel):
    date: date
    weight_lbs: Optional[float] = None
    reps: Optional[int] = None
    score: float


class StrengthProgressRow(BaseModel):
    exercise: str
    sessions: int = 0
    last: Optional[TopSet] = None
    previous: Optional[TopSet] = None
    best: Optional[TopSet] = None
    trend: Optional[str] = None     # "up" | "flat" | "down"
    setting: Optional[float] = None     # = setup["seat"]; kept for older gym-display builds
    setup: Optional[dict[str, float]] = None   # MACHINE-SETUP: {"seat": 4, "pad": 3, "range": 2}


class PatternOut(BaseModel):
    id: int
    exercise: str
    region: str
    hits: int
    exposures: int
    text: str
    last_reflection_at: Optional[datetime] = None


class FlagOut(BaseModel):
    date: date
    kind: str                       # "rpe" | "pain" | "missed" | "partial"
    text: str


class WeightSummary(BaseModel):
    first: TrendPoint
    latest: TrendPoint
    change: float


class OverviewResponse(BaseModel):
    date: date
    timezone: str
    program: Optional[ProgramInfo] = None
    week_days: list[PlanDay] = Field(default_factory=list)
    today: TodayOverview
    strength_progress: list[StrengthProgressRow] = Field(default_factory=list)
    checkins_14d: list[CheckinOut] = Field(default_factory=list)
    patterns: list[PatternOut] = Field(default_factory=list)
    flags: list[FlagOut] = Field(default_factory=list)
    weight_30d: list[TrendPoint] = Field(default_factory=list)
    weight_summary: Optional[WeightSummary] = None
    previous_program_end: Optional[date] = None


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def _num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:g}"


def _program(db: Session, today: date) -> Optional[dict[str, Any]]:
    """{name, phase, anchor, weeks_total, deload_week, source} or None."""
    row = db.execute(
        text("SELECT value FROM acos.system_state WHERE key = :k"),
        {"k": PROGRAM_STATE_KEY},
    ).mappings().first()
    if row and row.get("value"):
        try:
            v = json.loads(row["value"])
            return {"name": v.get("name"), "phase": int(v["phase"]),
                    "anchor": date.fromisoformat(v["anchor"]),
                    "weeks_total": int(v["weeks_total"]),
                    "deload_week": v.get("deload_week"), "source": "state"}
        except (ValueError, KeyError, TypeError):
            pass
    ref = db.execute(
        text("""
            SELECT phase, week_num, plan_date FROM health.plan
            WHERE plan_date <= :t ORDER BY plan_date DESC LIMIT 1
        """),
        {"t": today},
    ).mappings().first()
    if ref is None:
        return None
    lo = ref["plan_date"] - timedelta(days=7 * int(ref["week_num"]) + 7)
    anchor = db.execute(
        text("""
            SELECT min(plan_date) AS anchor FROM health.plan
            WHERE phase = :p AND week_num = 1 AND plan_date > :lo AND plan_date <= :t
        """),
        {"p": ref["phase"], "lo": lo, "t": ref["plan_date"]},
    ).mappings().first()
    if not anchor or anchor["anchor"] is None:
        return None
    total = db.execute(
        text("""
            SELECT max(week_num) AS weeks FROM health.plan
            WHERE phase = :p AND plan_date >= :a
        """),
        {"p": ref["phase"], "a": anchor["anchor"]},
    ).mappings().first()
    name = db.execute(
        text("SELECT phase_name FROM health.phase_config WHERE phase = :p"),
        {"p": ref["phase"]},
    ).mappings().first()
    weeks = int(total["weeks"]) if total and total["weeks"] else int(ref["week_num"])
    return {"name": name["phase_name"] if name else None, "phase": int(ref["phase"]),
            "anchor": anchor["anchor"], "weeks_total": weeks, "deload_week": weeks,
            "source": "derived"}


def _scores(soreness: Any) -> tuple[dict[str, int], dict[str, int]]:
    if isinstance(soreness, str):
        try:
            soreness = json.loads(soreness)
        except ValueError:
            soreness = {}
    if not isinstance(soreness, dict):
        return {}, {}
    sore = {k: int(v) for k, v in soreness.items()
            if k != "pain" and isinstance(v, (int, float)) and not isinstance(v, bool)}
    pain_src = soreness.get("pain") if isinstance(soreness.get("pain"), dict) else {}
    pain = {k: int(v) for k, v in pain_src.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return sore, pain


def _checkin(r: dict[str, Any]) -> CheckinOut:
    sore, pain = _scores(r.get("soreness"))
    return CheckinOut(
        date=r["state_date"],
        sleep_hrs=float(r["sleep_hrs"]) if r.get("sleep_hrs") is not None else None,
        energy=r.get("energy"),
        weight_lbs=float(r["weight_lbs"]) if r.get("weight_lbs") is not None else None,
        resting_hr=r.get("resting_hr"),
        soreness=sore,
        pain=pain,
    )


def _real(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in logs if r.get("logged_via") != "inferred"]


def progress_for(day: "PlanDay", logs: list[dict[str, Any]]) -> ProgressOut:
    """Today's progress in the unit that fits the session."""
    b = day.blocks or {}
    t = b.get("type")
    real = _real(logs)
    if _is_rest_day(day.session_type, b):
        return ProgressOut(unit="rest")
    if t == "recovery_flow":
        planned_sec = b.get("total_sec") or (day.est_duration_min or 0) * 60
        done_sec = max([int(r.get("duration_sec") or 0) for r in real
                        if r["log_type"] == "session_summary"
                        and (r.get("notes") or "").startswith("recovery_flow")] or [0])
        return ProgressOut(unit="minutes", done=round(done_sec / 60), planned=round(planned_sec / 60))
    if t == "circuit":
        done = sum(1 for r in real if r["log_type"] == "strength_set" and not r.get("is_skipped"))
        return ProgressOut(unit="sets", done=done, planned=_planned_set_count(b))
    done_sec = sum(int(r.get("duration_sec") or 0) for r in real
                   if r["log_type"] == "cardio_block" and not r.get("is_skipped"))
    return ProgressOut(unit="minutes", done=round(done_sec / 60), planned=day.est_duration_min)


def trend_of(last: Optional[TopSet], prev: Optional[TopSet]) -> Optional[str]:
    """↑ / → / ↓ by estimated load × reps (±2% is flat)."""
    if last is None or prev is None:
        return None
    if prev.score <= 0:
        return "up" if last.score > 0 else "flat"
    change = (last.score - prev.score) / prev.score
    if change > TREND_TOLERANCE:
        return "up"
    if change < -TREND_TOLERANCE:
        return "down"
    return "flat"


def _score(weight: Any, reps: Any) -> float:
    r = int(reps or 0)
    return float(weight) * r if weight else float(r)


def strength_progress(names: list[str], rows: list[dict[str, Any]]) -> list[StrengthProgressRow]:
    """Pure: rows = [{exercise, plan_date, weight_lbs, reps_done, notes}] (real,
    non-skipped strength sets in the program window)."""
    by_ex: dict[str, dict[date, dict[str, Any]]] = {}
    settings: dict[str, tuple[date, dict[str, float]]] = {}
    for r in rows:
        ex, d = r["exercise"], r["plan_date"]
        sc = _score(r.get("weight_lbs"), r.get("reps_done"))
        cur = by_ex.setdefault(ex, {}).get(d)
        if cur is None or sc > cur["score"]:
            by_ex[ex][d] = {"date": d, "weight_lbs": float(r["weight_lbs"]) if r.get("weight_lbs") is not None else None,
                            "reps": r.get("reps_done"), "score": sc}
        setup = parse_setup(r.get("notes"))
        if setup and (ex not in settings or d >= settings[ex][0]):
            settings[ex] = (d, {k: float(v) for k, v in setup.items()})
    out = []
    for name in names:
        sessions = sorted(by_ex.get(name, {}).values(), key=lambda x: x["date"])
        last = TopSet(**sessions[-1]) if sessions else None
        prev = TopSet(**sessions[-2]) if len(sessions) > 1 else None
        best = TopSet(**max(sessions, key=lambda x: (x["score"], x["date"]))) if sessions else None
        setup = settings.get(name, (None, None))[1]
        out.append(StrengthProgressRow(
            exercise=name, sessions=len(sessions), last=last, previous=prev, best=best,
            trend=trend_of(last, prev), setup=setup, setting=(setup or {}).get("seat")))
    return out


def _exercise_names(days: list["PlanDay"]) -> list[str]:
    """Strength exercises of the program week as WRITTEN (not check-in swaps)."""
    names: list[str] = []
    for d in days:
        b = d.blocks or {}
        orig = b.get("original") if isinstance(b.get("original"), dict) else None
        src = (orig or {}).get("blocks") if orig else b
        if not isinstance(src, dict) or src.get("type") != "circuit":
            continue
        for ex in src.get("exercises") or []:
            n = ex.get("name")
            if n and n not in names:
                names.append(n)
    return names


def _rpe_cap(day: "PlanDay", exercise: str) -> Optional[float]:
    b = day.blocks or {}
    for ex in b.get("exercises") or []:
        if ex.get("name") == exercise and ex.get("rpe_cap") is not None:
            return float(ex["rpe_cap"])
    if b.get("rpe_cap") is not None:
        return float(b["rpe_cap"])
    return day.target_rpe


def build_flags(days: list["PlanDay"], logs_by_plan: dict[int, list[dict[str, Any]]],
                today: date) -> list[FlagOut]:
    """Plain-language data flags, newest first. Facts only."""
    flags: list[FlagOut] = []
    for day in days:
        if day.plan_date > today:
            continue
        name = day.display_name or day.session_type
        logs = _real(logs_by_plan.get(day.plan_id, []))
        if day.status == "missed":
            flags.append(FlagOut(date=day.plan_date, kind="missed", text=f"Missed: {name} ({_md(day.plan_date)})"))
        elif day.status == "partial" and day.plan_date < today:
            p = progress_for(day, logs)
            unit = "sets" if p.unit == "sets" else "min"
            flags.append(FlagOut(date=day.plan_date, kind="partial",
                                 text=f"Partial: {name} ({_md(day.plan_date)}) — "
                                      f"{_num(p.done or 0)} of {_num(p.planned or 0)} {unit}"))
        top_rpe: dict[str, float] = {}
        for r in logs:
            if r["log_type"] == "strength_set" and r.get("rpe_actual") is not None and r.get("exercise"):
                top_rpe[r["exercise"]] = max(top_rpe.get(r["exercise"], 0.0), float(r["rpe_actual"]))
        for ex, rpe in top_rpe.items():
            cap = _rpe_cap(day, ex)
            if cap is not None and rpe > cap:
                flags.append(FlagOut(date=day.plan_date, kind="rpe",
                                     text=f"RPE {_num(rpe)} on {ex} ({_md(day.plan_date)}), cap was {_num(cap)}"))
        seen: set[tuple] = set()
        for r in logs:
            notes = r.get("notes") or ""
            chips = [(m.group(1).strip().lower(), int(m.group(2))) for m in _PAIN_NOTE_RE.finditer(notes)]
            where = f" on {r['exercise']}" if r.get("exercise") else ""
            if chips:
                for region, n in chips:
                    key = (region, n, r.get("exercise"))
                    if n >= 1 and key not in seen:
                        seen.add(key)
                        flags.append(FlagOut(date=day.plan_date, kind="pain",
                                             text=f"Pain chip: {region} {n}{where} ({_md(day.plan_date)})"))
            elif _contains_pain_keyword(notes):
                key = (notes, r.get("exercise"))
                if key not in seen:
                    seen.add(key)
                    flags.append(FlagOut(date=day.plan_date, kind="pain",
                                         text=f"Pain note{where} ({_md(day.plan_date)}): “{notes}”"))
    order = {"missed": 0, "partial": 1, "rpe": 2, "pain": 3}
    flags.sort(key=lambda f: (-f.date.toordinal(), order.get(f.kind, 9), f.text))
    return flags


def weight_summary(points: list[TrendPoint]) -> Optional[WeightSummary]:
    if not points:
        return None
    first, latest = points[0], points[-1]
    return WeightSummary(first=first, latest=latest, change=round(latest.value - first.value, 1))


def _patterns(db: Session) -> list[PatternOut]:
    """Open pain patterns; [] until migration 032 creates the tables."""
    exists = db.execute(
        text("SELECT to_regclass('health.pain_pattern') AS t, to_regclass('health.reflection') AS r")
    ).mappings().first()
    if not exists or not exists.get("t"):
        return []
    has_reflection = bool(exists.get("r"))
    rows = db.execute(
        text("""
            SELECT p.id, p.exercise, p.region, p.hits, p.exposures, p.evidence,
                   """ + ("""(SELECT max(r.created_at) FROM health.reflection r
                    WHERE r.pattern_id = p.id)""" if has_reflection else "NULL") + """ AS last_reflection_at
            FROM health.pain_pattern p
            WHERE p.status = 'open' AND p.qualifies
            ORDER BY p.hits DESC, p.id
        """)
    ).mappings().all()
    out = []
    for r in rows:
        ev = r.get("evidence") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except ValueError:
                ev = {}
        shared = ev.get("shared") or []
        also = f" (also that day: {', '.join(shared)})" if shared else ""
        out.append(PatternOut(
            id=r["id"], exercise=r["exercise"], region=r["region"], hits=r["hits"],
            exposures=r["exposures"], last_reflection_at=r.get("last_reflection_at"),
            text=f"{r['region']} pain ≥2 after {r['exercise']} — {r['hits']} of {r['exposures']} sessions{also}",
        ))
    return out


# ── WATCH-1: Health Auto Export ingest ─────────────────────────────────────

class IngestResponse(BaseModel):
    received: dict
    inserted: dict
    duplicates: dict
    note: Optional[str] = None


@router.post("/ingest", response_model=IngestResponse)
def post_ingest(
    payload: dict,
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_watch_ingest_key),
):
    """Accept a Health Auto Export payload (WATCH-1).

    Idempotent: samples are keyed (metric, measured_at) and workouts
    (kind, started_at), so a re-sent or overlapping export inserts nothing and
    reports the duplicate counts instead.

    Forgiving by design: a sample this parser can't read is stored raw with a
    NULL value rather than rejecting the payload, and an unknown metric is
    stored under its own name. Only a payload that isn't an object is refused.
    The shape is an ASSUMPTION until a real export lands — keeping `raw` means
    a parser fix can be replayed without re-exporting from the phone.
    """
    from knowledge.watch_payload import parse_counts, parse_samples, parse_workouts

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "payload must be a JSON object"})

    counts = parse_counts(payload)
    samples = parse_samples(payload)
    workouts = parse_workouts(payload)
    ins_s = dup_s = ins_w = dup_w = skipped = 0

    for row in samples:
        if row["measured_at"] is None:
            skipped += 1
            continue
        local_day = row["measured_at"].astimezone(CT).date() \
            if row["measured_at"].tzinfo else row["measured_at"].date()
        res = db.execute(text("""
            INSERT INTO health.watch_sample
                (metric, measured_at, local_date, value, unit, raw)
            VALUES (:metric, :measured_at, :local_date, :value, :unit, CAST(:raw AS jsonb))
            ON CONFLICT (metric, measured_at) DO NOTHING
            RETURNING sample_id"""), {
                "metric": row["metric"], "measured_at": row["measured_at"],
                "local_date": local_day, "value": row["value"], "unit": row["unit"],
                "raw": json.dumps(row["raw"], default=str)})
        if res.first():
            ins_s += 1
        else:
            dup_s += 1

    for w in workouts:
        local_day = w["started_at"].astimezone(CT).date() \
            if w["started_at"].tzinfo else w["started_at"].date()
        res = db.execute(text("""
            INSERT INTO health.watch_workout
                (kind, started_at, ended_at, local_date, duration_sec,
                 hr_avg, hr_max, kcal, raw)
            VALUES (:kind, :started_at, :ended_at, :local_date, :duration_sec,
                    :hr_avg, :hr_max, :kcal, CAST(:raw AS jsonb))
            ON CONFLICT (kind, started_at) DO NOTHING
            RETURNING workout_id"""), {
                **{k: w[k] for k in ("kind", "started_at", "ended_at", "duration_sec",
                                     "hr_avg", "hr_max", "kcal")},
                "local_date": local_day, "raw": json.dumps(w["raw"], default=str)})
        if res.first():
            ins_w += 1
        else:
            dup_w += 1

    db.commit()
    note = None
    if skipped or counts["unparsed_metrics"]:
        bits = []
        if skipped:
            bits.append(f"{skipped} sample(s) had no readable date and were not stored")
        if counts["unparsed_metrics"]:
            bits.append("stored unparsed: " + ", ".join(counts["unparsed_metrics"][:10]))
        note = "; ".join(bits)
    return IngestResponse(
        received=counts,
        inserted={"samples": ins_s, "workouts": ins_w},
        duplicates={"samples": dup_s, "workouts": dup_w},
        note=note,
    )


@router.get("/overview", response_model=OverviewResponse)
def get_overview(
    db: Session = Depends(get_db),
    _api_key: str = Depends(verify_health_api_key),
):
    """The Status page in one read. Dates in the active timezone."""
    tz_name = _active_timezone(db)
    today = datetime.now(ZoneInfo(tz_name)).date()
    prog = _program(db, today)

    program: Optional[ProgramInfo] = None
    week_days: list[PlanDay] = []
    history_days: list[PlanDay] = []
    logs_by_plan: dict[int, list[dict[str, Any]]] = {}
    scope_start = today
    if prog:
        anchor = prog["anchor"]
        scope_start = anchor
        week = max(1, min(prog["weeks_total"], (today - anchor).days // 7 + 1))
        week_start = anchor + timedelta(days=7 * (week - 1))
        week_end = week_start + timedelta(days=6)
        history_days, logs_by_plan = _plan_days(db, anchor, max(today, week_end), today)
        week_days = [d for d in history_days if week_start <= d.plan_date <= week_end]
        sessions = [d for d in week_days if not _is_rest_day(d.session_type, d.blocks)]
        deload = prog.get("deload_week")
        program = ProgramInfo(
            name=prog.get("name"), phase=prog["phase"], week=week,
            weeks_total=prog["weeks_total"], anchor=anchor, deload_week=deload,
            weeks_to_deload=max(0, deload - week) if deload else None,
            week_start=week_start, week_end=week_end,
            sessions_done=sum(1 for d in sessions if d.status == "done"),
            sessions_planned=len(sessions), source=prog["source"],
        )

    # Today
    today_day = next((d for d in history_days if d.plan_date == today), None)
    if today_day is None and not prog:
        found, more = _plan_days(db, today, today, today)
        today_day = found[0] if found else None
        logs_by_plan.update(more)
    checkin_row = db.execute(
        text("""
            SELECT state_date, sleep_hrs, energy, weight_lbs, resting_hr, soreness
            FROM health.daily_state WHERE state_date = :d
        """),
        {"d": today},
    ).mappings().first()
    adj = (today_day.blocks or {}).get("adjustment") if today_day else None
    today_out = TodayOverview(
        date=today,
        day=today_day,
        progress=progress_for(today_day, logs_by_plan.get(today_day.plan_id, [])) if today_day else None,
        checkin=_checkin(dict(checkin_row)) if checkin_row else None,
        adjustment=AdjustmentOut(summary=list(adj.get("summary") or []),
                                 rules_fired=list(adj.get("rules_fired") or []))
        if isinstance(adj, dict) else None,
    )

    # Strength progress (program window only)
    names = _exercise_names(week_days)
    set_rows: list[dict[str, Any]] = []
    if names and prog:
        set_rows = [dict(r) for r in db.execute(
            text("""
                SELECT sl.exercise, p.plan_date, sl.weight_lbs, sl.reps_done, sl.notes
                FROM health.session_log sl
                JOIN health.plan p ON p.plan_id = sl.plan_id
                WHERE sl.log_type = 'strength_set'
                  AND sl.logged_via <> 'inferred'
                  AND NOT COALESCE(sl.is_skipped, FALSE)
                  AND sl.exercise = ANY(:names)
                  AND p.plan_date BETWEEN :s AND :t
                ORDER BY p.plan_date, sl.log_id
            """),
            {"names": names, "s": scope_start, "t": today},
        ).mappings().all()]

    # Check-ins (14 days, program window only)
    ci_start = max(today - timedelta(days=OVERVIEW_CHECKIN_DAYS - 1), scope_start)
    checkins = [_checkin(dict(r)) for r in db.execute(
        text("""
            SELECT state_date, sleep_hrs, energy, weight_lbs, resting_hr, soreness
            FROM health.daily_state
            WHERE state_date BETWEEN :s AND :t
            ORDER BY state_date
        """),
        {"s": ci_start, "t": today},
    ).mappings().all()] if ci_start <= today else []

    # Body weight (30 days, not program-scoped)
    weight = [TrendPoint(date=r["d"], value=float(r["v"])) for r in db.execute(
        text("""
            SELECT state_date AS d, weight_lbs AS v
            FROM health.daily_state
            WHERE state_date BETWEEN :s AND :t AND weight_lbs IS NOT NULL
            ORDER BY state_date
        """),
        {"s": today - timedelta(days=OVERVIEW_WEIGHT_DAYS - 1), "t": today},
    ).mappings().all()]

    previous_end = None
    if prog:
        older = db.execute(
            text("SELECT max(plan_date) AS d FROM health.plan WHERE plan_date < :a"),
            {"a": prog["anchor"]},
        ).mappings().first()
        previous_end = older["d"] if older else None

    return OverviewResponse(
        date=today,
        timezone=tz_name,
        program=program,
        week_days=week_days,
        today=today_out,
        strength_progress=strength_progress(names, set_rows),
        checkins_14d=checkins,
        patterns=_patterns(db),
        flags=build_flags(history_days, logs_by_plan, today),
        weight_30d=weight,
        weight_summary=weight_summary(weight),
        previous_program_end=previous_end,
    )
