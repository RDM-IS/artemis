"""PREP-1 — Prep board endpoints: the shopping list and the pantry.

GET  /api/prep/stay              → the stay being planned (proposed by the box)
POST /api/prep/stay/{id}/confirm → Ryan confirms it, optionally editing the dates
GET  /api/prep/shopping          → the list, grouped store → aisle
GET  /api/prep/pantry            → every countable ingredient + its package
POST /api/prep/pantry            → record one count
GET  /api/prep/macros            → per-day planned macros vs the open target

Every query lives in `knowledge/prep_store.py` and every calculation in
`knowledge/prep_math.py`. This module is transport: authenticate, adapt the
session to a `fetch` callable, serialise. It contains no SQL and no arithmetic,
so the list the Lambda serves cannot drift from the list the box computes.

THE LIST IS RECOMPUTED ON EVERY READ. It is not cached and there is no stored
copy to go stale: a pantry count recorded by POST /pantry changes the next GET
/shopping immediately. A cached shopping list is wrong in the shop, where he has
no way to tell.

Auth: the same X-API-Key the rest of gym-display's calls use, validated against
`rdmis/dev/health-api-key`. The Cloudflare Access proxy attaches it server-side.
"""

import logging
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from knowledge import prep_store

from ..database import get_db
from .health import _active_timezone, verify_health_api_key

logger = logging.getLogger(__name__)

router = APIRouter()


def _local_today(db: Session) -> date:
    """Today in the ACTIVE timezone, resolved the way the health router does.

    RDS runs UTC and a bare current_date is a day ahead of Ryan after ~19:00
    local, which would move the stay a shopping trip early. The zone comes from
    `_active_timezone(db)` — the same helper the health router uses — so a
    `set timezone to <place>` override moves Prep with everything else. It is
    never hard-coded here and never interpolated into SQL.

    NOT `knowledge.config`: config.py lives in artemis/, and the Lambda package
    ships only api/, knowledge/ and migrations/ (PACKAGE-IDENTITY). Importing it
    here raised ImportError on every stay-dependent route, caught by hitting the
    deployed endpoints rather than by any test — the pantry route, which needs no
    date, was the only one that worked.
    """
    return datetime.now(ZoneInfo(_active_timezone(db))).date()


def _fetch_for(db: Session):
    """A `fetch(sql, params) -> list[dict]` over a SQLAlchemy session.

    `exec_driver_sql` hands the statement to psycopg2 unchanged, so the `%s`
    placeholders in knowledge/prep_store.py work here exactly as they do on the
    box. Adapting the driver is the whole reason this function exists: it is what
    lets ONE copy of each query serve both sides.
    """
    def fetch(sql, params=()):
        result = db.connection().exec_driver_sql(sql, tuple(params))
        if result.returns_rows:
            return [dict(row) for row in result.mappings()]
        return []
    return fetch


def _resolve_stay(fetch, stay_id: Optional[int], db: Session) -> dict:
    stay = (prep_store.get_stay(fetch, stay_id) if stay_id
            else prep_store.current_stay(fetch, _local_today(db)))
    if stay is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "no_stay",
                    "message": "No stay has been proposed yet. The box proposes "
                               "one from the cycle on its next run."})
    return stay


# ── models ──────────────────────────────────────────────────────────────────

class StayOut(BaseModel):
    id: int
    start_date: str
    end_date: str
    days: int
    shop_date: Optional[str] = None
    source: str
    confirmed: bool
    menu_days: int = 0
    meals: int = 0


class ConfirmIn(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    shop_date: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class CountIn(BaseModel):
    ingredient_id: str = Field(min_length=1, max_length=64)
    #: exactly one of these. Packages is what the stepper sends; base units are
    #: for an ingredient that has no package size at all.
    packages: Optional[float] = Field(default=None, ge=0, le=999)
    base: Optional[float] = Field(default=None, ge=0, le=1_000_000)


# ── stay ────────────────────────────────────────────────────────────────────

@router.get("/stay", response_model=StayOut)
def get_stay(stay_id: Optional[int] = Query(default=None),
             db: Session = Depends(get_db),
             _=Depends(verify_health_api_key)):
    fetch = _fetch_for(db)
    stay = _resolve_stay(fetch, stay_id, db)
    rows = fetch("SELECT day_date, count(*) AS meals FROM nutrition.prep_stay_day "
                 "WHERE stay_id = %s GROUP BY day_date", (stay["id"],))
    return StayOut(
        id=stay["id"], start_date=stay["start_date"].isoformat(),
        end_date=stay["end_date"].isoformat(),
        days=(stay["end_date"] - stay["start_date"]).days + 1,
        shop_date=stay["shop_date"].isoformat() if stay["shop_date"] else None,
        source=stay["source"], confirmed=stay["confirmed_at"] is not None,
        menu_days=len(rows), meals=sum(r["meals"] for r in rows))


@router.post("/stay/{stay_id}/confirm")
def confirm_stay(stay_id: int, body: ConfirmIn,
                 db: Session = Depends(get_db),
                 _=Depends(verify_health_api_key)):
    """Ryan confirms the proposed stay, optionally editing the dates.

    Confirming is what stops the box re-dating the stay from the cycle on its next
    run (see `artemis.prep.upsert_stay`): a proposal that keeps proposing after an
    answer is not propose-then-confirm, it is overwriting a decision.

    EDITING THE DATES DOES NOT REBUILD THE MENU. The menu needs Notion and only
    the box can read it, so the response says the menu is stale and for which
    range. Silently serving a list built for the old dates would be a list for the
    wrong week that looks authoritative.
    """
    fetch = _fetch_for(db)
    stay = prep_store.get_stay(fetch, stay_id)
    if stay is None:
        raise HTTPException(status_code=404, detail={"error": "no_stay"})
    start = body.start_date or stay["start_date"]
    end = body.end_date or stay["end_date"]
    if end < start:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_dates", "message": "end_date is before start_date"})
    dates_changed = (start != stay["start_date"] or end != stay["end_date"])
    try:
        db.connection().exec_driver_sql(
            "UPDATE nutrition.prep_stay SET start_date = %s, end_date = %s, "
            "shop_date = %s, notes = COALESCE(%s, notes), source = %s, "
            "confirmed_at = now(), updated_at = now() WHERE id = %s",
            (start, end, body.shop_date or stay["shop_date"], body.notes,
             "ryan" if dates_changed else stay["source"], stay_id))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("prep stay confirm failed for %s", stay_id)
        raise HTTPException(status_code=500, detail={"error": "write_failed"})
    return {"stay_id": stay_id, "start_date": start.isoformat(),
            "end_date": end.isoformat(), "confirmed": True,
            "menu_stale": dates_changed,
            "menu_note": ("the menu was resolved for the previous dates; the box "
                          "rebuilds it on its next run") if dates_changed else None}


# ── shopping ────────────────────────────────────────────────────────────────

@router.get("/shopping")
def shopping(stay_id: Optional[int] = Query(default=None),
             db: Session = Depends(get_db),
             _=Depends(verify_health_api_key)) -> dict[str, Any]:
    fetch = _fetch_for(db)
    stay = _resolve_stay(fetch, stay_id, db)
    try:
        return prep_store.shopping_list(fetch, stay["id"])
    except SQLAlchemyError:
        logger.exception("prep shopping list failed for stay %s", stay["id"])
        raise HTTPException(status_code=500, detail={"error": "read_failed"})


@router.get("/macros")
def macros(stay_id: Optional[int] = Query(default=None),
           db: Session = Depends(get_db),
           _=Depends(verify_health_api_key)) -> dict[str, Any]:
    fetch = _fetch_for(db)
    stay = _resolve_stay(fetch, stay_id, db)
    try:
        return prep_store.macro_check(fetch, stay["id"])
    except SQLAlchemyError:
        logger.exception("prep macro check failed for stay %s", stay["id"])
        raise HTTPException(status_code=500, detail={"error": "read_failed"})


# ── pantry ──────────────────────────────────────────────────────────────────

@router.get("/pantry")
def pantry(db: Session = Depends(get_db),
           _=Depends(verify_health_api_key)) -> dict[str, Any]:
    fetch = _fetch_for(db)
    try:
        rows = prep_store.pantry_rows(fetch)
    except SQLAlchemyError:
        logger.exception("prep pantry read failed")
        raise HTTPException(status_code=500, detail={"error": "read_failed"})
    out = []
    for r in rows:
        out.append({
            "ingredient_id": r["ingredient_id"], "name": r["name"],
            "category": r["category"], "unit": r["unit"],
            "on_hand_base": _num(r["on_hand_base"]),
            "on_hand_pkgs": _num(r["on_hand_pkgs"]),
            "counted_at": r["on_hand_at"].isoformat() if r["on_hand_at"] else None,
            "count_source": r["on_hand_source"],
            "package_size": _num(r["package_size"]),
            "package_label": r["package_label"],
            "store": r["store_name"],
            "par_level_pkgs": _num(r["par_level_pkgs"]),
            "shelf_life_days": r["shelf_life_days"],
            # The stepper needs to know whether packages are even meaningful here.
            "countable_in_packages": bool(r["package_size"] and float(r["package_size"]) > 0),
        })
    return {"items": out, "count": len(out),
            "uncounted": sum(1 for r in out if r["on_hand_base"] is None)}


@router.post("/pantry")
def set_count(body: CountIn, db: Session = Depends(get_db),
              _=Depends(verify_health_api_key)) -> dict[str, Any]:
    """Record one pantry count. Stored in base units whatever the input was.

    A package count for an ingredient with no package size is REFUSED with 400,
    not stored: writing "3" into a grams column because the stepper said three
    bottles would be a wrong number that reads as a measurement forever after.
    """
    if (body.packages is None) == (body.base is None):
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_input",
                    "message": "send exactly one of packages or base"})
    fetch = _fetch_for(db)
    try:
        base = (body.base if body.base is not None
                else prep_store.base_from_packages(fetch, body.ingredient_id,
                                                   body.packages))
    except ValueError as exc:
        raise HTTPException(status_code=400,
                            detail={"error": "no_package_size", "message": str(exc)})
    try:
        result = db.connection().exec_driver_sql(
            prep_store.SET_ON_HAND_SQL, (base, "app", body.ingredient_id))
        if not result.rowcount:
            db.rollback()
            raise HTTPException(
                status_code=404,
                detail={"error": "no_ingredient", "message": body.ingredient_id})
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("prep pantry write failed for %s", body.ingredient_id)
        raise HTTPException(status_code=500, detail={"error": "write_failed"})
    return {"ingredient_id": body.ingredient_id, "on_hand_base": float(base),
            "counted_at": datetime.now(ZoneInfo(_active_timezone(db)))
                                  .isoformat(timespec="seconds")}


def _num(v):
    """A JSON number, or None. psycopg2 hands back Decimal for NUMERIC columns and
    Decimal is not JSON-serialisable; None must survive as None, because None and
    0 are different answers throughout this subsystem."""
    return None if v is None else float(v)
