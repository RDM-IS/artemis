"""Health/training plan endpoints.

GET /api/health/today  → today's training plan from health.plan

Read-only. No auth (private network). Consumed by gym-display frontend
(separate repo) on the gym TV at gym.rdm.is.

Note on layout: the existing repo convention defines pydantic response
models inline in the router (see commitments.py). Keeping that pattern
to avoid restructuring api/app/models.py from flat file → package, which
would break every existing import.
"""

from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import get_db

router = APIRouter()

CT = ZoneInfo("America/Chicago")


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


def _today_ct() -> date:
    """Return today's date in America/Chicago.

    Ryan is in West Bend, WI (Central Time). DST handled by zoneinfo.
    """
    return datetime.now(CT).date()


@router.get("/today")
def get_today(db: Session = Depends(get_db)):
    """Return the training plan for today (Central Time).

    200 → PlanResponse JSON
    404 → {"error": "no_plan", "fallback": "..."}
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
        # Use HTTPException so the response envelope matches FastAPI conventions,
        # but customize the body to the contract spec.
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no_plan",
                "fallback": "rest day or check Mattermost",
            },
        )

    # blocks comes back as a dict (JSONB native). target_rpe is Decimal → float.
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
