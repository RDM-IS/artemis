"""Health/training plan endpoints.

GET /api/health/today  → today's training plan from health.plan

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

from datetime import date, datetime
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
