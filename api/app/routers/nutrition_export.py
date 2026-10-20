"""HEALTH-EXPORT-1 — settled nutrition days, handed to an iOS Shortcut.

POST /api/nutrition/export/claim    → the next batch to write to Apple Health
POST /api/nutrition/export/ack      → the Shortcut wrote it
POST /api/nutrition/export/release  → the Shortcut did NOT write it
GET  /api/nutrition/export/status   → everything the export knows, for a human

Every rule lives in `knowledge/health_export.py`. This module is transport:
authenticate, adapt the session to a `fetch` callable, resolve "today" in the
active timezone, commit, serialise.

EVERY /claim REPLY CARRIES `status` AND `message`. The Shortcut branches on
`status` and shows `message`, so it needs no logic of its own about why there
is nothing to do:

  ok             items to write, then ack `batch_id`
  empty          nothing settled and unsent — the normal daily answer
  stuck          a previous batch was never confirmed; ack or release it first
  disabled       the export is switched off
  misconfigured  switched on, but the config cannot be used (see `problems`)

`claim` is a POST, not a GET, because it changes state: the items it returns
are recorded as claimed in the same transaction that returns them.

Auth: X-API-Key against `rdmis/dev/health-export-key` — a key that exists only
in the Shortcut and is accepted nowhere else.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from knowledge import health_export as hx

from ..database import get_db
from .health import _active_timezone

logger = logging.getLogger(__name__)

router = APIRouter()

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_EXPORT_KEY: Optional[str] = None


def _load_export_key() -> str:
    """Lazy-load the export key from Secrets Manager, cached per-process."""
    global _EXPORT_KEY
    if _EXPORT_KEY is None:
        from knowledge.secrets import get_health_export_key
        _EXPORT_KEY = get_health_export_key()
    return _EXPORT_KEY


def verify_health_export_key(api_key: Optional[str] = Security(_API_KEY_HEADER)):
    """These routes accept ONLY the export key — not the display key, not the
    watch key. 401 on missing or wrong, like the rest of /api/health."""
    if not api_key or api_key != _load_export_key():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"error": "unauthorized"})
    return api_key


def _fetch_for(db: Session):
    """`fetch(sql, params) -> list[dict]` over a SQLAlchemy session — the same
    adapter as the prep router's, so `%s` placeholders reach psycopg2 unchanged."""
    def fetch(sql, params=()):
        result = db.connection().exec_driver_sql(sql, tuple(params))
        if result.returns_rows:
            return [dict(row) for row in result.mappings()]
        return []
    return fetch


def _today_and_tz(db: Session):
    """Local date and zone name in the ACTIVE timezone. RDS runs UTC; a bare
    current_date is a day ahead after ~19:00 local, which here would close a
    day's correction window five hours early."""
    tz = _active_timezone(db)
    return datetime.now(ZoneInfo(tz)).date(), tz


class ClaimIn(BaseModel):
    max_days: int = Field(default=hx.DEFAULT_MAX_DAYS, ge=1, le=hx.MAX_MAX_DAYS)


class BatchIn(BaseModel):
    #: typed, so a malformed id is a 422 here and never reaches Postgres as a
    #: cast error that would read like an outage.
    batch_id: uuid.UUID


def _days_phrase(days: list) -> str:
    if not days:
        return "no days"
    if len(days) == 1:
        return days[0]
    return f"{days[0]} to {days[-1]} ({len(days)} days)"


@router.post("/claim")
def claim(body: Optional[ClaimIn] = None,
          db: Session = Depends(get_db),
          _key: str = Depends(verify_health_export_key)):
    body = body or ClaimIn()
    fetch = _fetch_for(db)
    try:
        cfg = hx.load_config(fetch)
        if not cfg.enabled:
            return {"status": "disabled", "items": [], "withheld": [],
                    "message": "Health export is switched off."}
        if cfg.problems:
            return {"status": "misconfigured", "items": [], "withheld": [],
                    "problems": cfg.problems,
                    "message": "Health export is on but its settings cannot be "
                               "used: " + "; ".join(cfg.problems)}

        stuck = hx.open_batch(fetch)
        if stuck is not None:
            return {"status": "stuck", "items": [], "withheld": [],
                    "batch_id": stuck["batch_id"], "days": stuck["days"],
                    "message": "The last export (" + _days_phrase(stuck["days"])
                               + ") was never confirmed. Check those days in "
                                 "the Health app before answering."}

        today, tz = _today_and_tz(db)
        plan = hx.plan_for(fetch, cfg, today=today, tz=tz, max_days=body.max_days)
        if not plan.items:
            db.rollback()
            note = (f" {len(plan.withheld)} settled day(s) are being held back."
                    if plan.withheld else "")
            return {"status": "empty", "items": [], "withheld": plan.withheld,
                    "message": "Nothing new to send to Health." + note}

        batch_id = hx.claim(fetch, plan)
        db.commit()
    except IntegrityError:
        # Two runs raced and the other one won the UNIQUE (day_date, slot).
        # This run claims nothing; the next one will see the other as `stuck`
        # or, once acked, find those days gone.
        db.rollback()
        raise HTTPException(status_code=409, detail={
            "error": "claimed_elsewhere",
            "message": "Another export run claimed these meals first."})
    except hx.ExportError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"error": "refused",
                                                     "message": str(exc)})
    except SQLAlchemyError:
        db.rollback()
        logger.exception("health export: claim failed")
        raise HTTPException(status_code=503, detail={
            "error": "unavailable",
            "message": "Could not read the nutrition tables."})

    more = (f" {plan.remaining_days} more day(s) will follow on the next run."
            if plan.remaining_days else "")
    return {"status": "ok", "batch_id": batch_id,
            "items": [i.as_json() for i in plan.items],
            "days": [d.isoformat() for d in plan.days],
            "withheld": plan.withheld,
            # Not "sent": nothing is in Health until the Shortcut writes it and
            # acks. The ack reply is the one that says it is done.
            "message": f"{len(plan.items)} meals ready for Health, "
                       f"{_days_phrase([d.isoformat() for d in plan.days])}." + more}


def _resolve(db: Session, fn, batch_id: uuid.UUID):
    try:
        out = fn(_fetch_for(db), str(batch_id))
        db.commit()
        return out
    except hx.ExportError as exc:
        db.rollback()
        raise HTTPException(status_code=404 if "no such" in str(exc) else 409,
                            detail={"error": "refused", "message": str(exc)})
    except SQLAlchemyError:
        db.rollback()
        logger.exception("health export: %s failed", fn.__name__)
        raise HTTPException(status_code=503, detail={
            "error": "unavailable",
            "message": "Could not update the export ledger."})


@router.post("/ack")
def ack(body: BatchIn, db: Session = Depends(get_db),
        _key: str = Depends(verify_health_export_key)):
    out = _resolve(db, hx.ack, body.batch_id)
    return {"status": "ok", **out,
            "message": f"Confirmed: {out['acked']} meals recorded as in Health."}


@router.post("/release")
def release(body: BatchIn, db: Session = Depends(get_db),
            _key: str = Depends(verify_health_export_key)):
    n = _resolve(db, hx.release, body.batch_id)
    return {"status": "ok", "released": n,
            "message": f"Released {n} meals. They will be offered again on "
                       "the next run."}


@router.get("/status")
def get_status(db: Session = Depends(get_db),
               _key: str = Depends(verify_health_export_key)):
    try:
        today, tz = _today_and_tz(db)
        return hx.status(_fetch_for(db), today=today, tz=tz)
    except SQLAlchemyError:
        logger.exception("health export: status failed")
        raise HTTPException(status_code=503, detail={
            "error": "unavailable",
            "message": "Could not read the export ledger."})
