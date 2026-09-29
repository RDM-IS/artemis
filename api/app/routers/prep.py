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

import json
import logging
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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


# ── PREP-2: the prep board ──────────────────────────────────────────────────
#
# The SCHEDULER IS NOT HERE, on purpose. It runs in the browser
# (gym-display/src/lib/prep-schedule.ts) so that starting a card early, marking one
# done or adding five minutes re-flows the board with the Wi-Fi off — which is the
# normal state of a kitchen. These endpoints serve the steps and the kitchen
# profile it needs, and record what happened.
#
# `schedule_json` on a session is therefore a RECORD, never the authority: the
# board recomputes on open. A stored schedule the code no longer agrees with is the
# same stale-answer problem the shopping list avoids by recomputing every read.

class StepIn(BaseModel):
    step_no: int = Field(ge=1, le=99)
    name: str = Field(min_length=1, max_length=120)
    resource: str
    mode: str = "active"
    base_min: float = Field(default=0, ge=0, le=600)
    per_serving_min: float = Field(default=0, ge=0, le=120)
    temp_f: Optional[int] = Field(default=None, ge=100, le=600)
    batch_key: Optional[str] = Field(default=None, max_length=60)
    keep_separate: bool = False
    keep_separate_note: Optional[str] = Field(default=None, max_length=200)
    shortcut_key: Optional[str] = Field(default=None, max_length=60)
    notes: Optional[str] = Field(default=None, max_length=300)


class StepsIn(BaseModel):
    recipe_id: str = Field(min_length=1, max_length=64)
    steps: list[StepIn] = Field(default_factory=list, max_length=40)


_RESOURCES = ("hands", "oven", "stove", "air_fryer", "counter", "fridge")
_MODES = ("active", "passive", "unattended")


@router.get("/steps")
def get_steps(db: Session = Depends(get_db),
              _=Depends(verify_health_api_key)) -> dict[str, Any]:
    """Every recipe with its steps, for the editor.

    Recipes with NO steps are included — "this one has none yet" is precisely what
    the editor exists to fix, and filtering them out would hide the work.
    """
    fetch = _fetch_for(db)
    try:
        rows = prep_store.all_steps(fetch)
    except SQLAlchemyError:
        logger.exception("prep steps read failed")
        raise HTTPException(status_code=500, detail={"error": "read_failed"})
    out: dict[str, dict] = {}
    for r in rows:
        rec = out.setdefault(r["notion_id"], {
            "recipe_id": r["notion_id"], "name": r["name"], "slug": r["slug"],
            "servings": _num(r["servings"]), "plan_eligible": r["plan_eligible"],
            "steps": []})
        if r["step_no"] is None:
            continue
        rec["steps"].append(_step_out(r))
    recipes = sorted(out.values(), key=lambda x: (x["name"] or "").lower())
    return {"recipes": recipes,
            "with_steps": sum(1 for r in recipes if r["steps"]),
            "without_steps": sum(1 for r in recipes if not r["steps"]),
            "resources": list(_RESOURCES), "modes": list(_MODES)}


@router.put("/steps")
def put_steps(body: StepsIn, db: Session = Depends(get_db),
              _=Depends(verify_health_api_key)) -> dict[str, Any]:
    """Replace one recipe's steps.

    DELETE-THEN-INSERT in one transaction rather than a diff: the editor sends the
    whole list, and there is no state in which half the steps are the new ones.
    Sending an empty list is a legitimate "this recipe is not batch-prepped", which
    is the right answer for anything cooked fresh on the day.
    """
    for s in body.steps:
        if s.resource not in _RESOURCES:
            raise HTTPException(status_code=400, detail={
                "error": "bad_resource", "message": f"{s.resource!r} is not one of "
                f"{', '.join(_RESOURCES)}"})
        if s.mode not in _MODES:
            raise HTTPException(status_code=400, detail={
                "error": "bad_mode", "message": f"{s.mode!r} is not one of "
                f"{', '.join(_MODES)}"})
        if s.resource == "oven" and s.temp_f is None:
            # An oven step with no temperature cannot be shared with another oven
            # step and the scheduler would have to guess. The database CHECK says
            # the same thing; refusing here gives a message instead of a 500.
            raise HTTPException(status_code=400, detail={
                "error": "oven_needs_temp",
                "message": f"{s.name!r} is an oven step and needs a temperature"})
    numbers = [s.step_no for s in body.steps]
    if len(set(numbers)) != len(numbers):
        raise HTTPException(status_code=400, detail={
            "error": "duplicate_step_no", "message": "step numbers must be unique"})
    try:
        conn = db.connection()
        conn.exec_driver_sql(prep_store.DELETE_STEPS_SQL, (body.recipe_id,))
        for s in sorted(body.steps, key=lambda x: x.step_no):
            conn.exec_driver_sql(prep_store.INSERT_STEP_SQL, (
                body.recipe_id, s.step_no, s.name, s.resource, s.mode,
                s.base_min, s.per_serving_min, s.temp_f, s.batch_key,
                s.keep_separate, s.keep_separate_note, s.shortcut_key, s.notes))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("prep steps write failed for %s", body.recipe_id)
        raise HTTPException(status_code=500, detail={"error": "write_failed"})
    return {"recipe_id": body.recipe_id, "steps": len(body.steps)}


@router.get("/board")
def get_board(stay_id: Optional[int] = Query(default=None),
              db: Session = Depends(get_db),
              _=Depends(verify_health_api_key)) -> dict[str, Any]:
    """Everything the browser needs to SCHEDULE the session itself.

    Deliberately not a schedule: the recipes, their servings still to cook, their
    steps and the kitchen. The board computes the plan, so it can recompute it
    offline when a card starts late.
    """
    fetch = _fetch_for(db)
    stay = _resolve_stay(fetch, stay_id, db)
    try:
        rows = prep_store.steps_for_stay(fetch, stay["id"])
        kitchen = prep_store.kitchen_profile(fetch)
    except SQLAlchemyError:
        logger.exception("prep board read failed for stay %s", stay["id"])
        raise HTTPException(status_code=500, detail={"error": "read_failed"})

    recipes: dict[str, dict] = {}
    stepless: list[str] = []
    for r in rows:
        rec = recipes.setdefault(r["notion_id"], {
            "notionId": r["notion_id"], "name": r["name"],
            "servings": _num(r["servings"]) or 0, "grams": None, "steps": []})
        if r["step_no"] is None:
            continue
        rec["steps"].append({
            "stepNo": r["step_no"], "name": r["step_name"],
            "resource": r["resource"], "mode": r["mode"],
            "baseMin": _num(r["base_min"]) or 0,
            "perServingMin": _num(r["per_serving_min"]) or 0,
            "tempF": r["temp_f"], "batchKey": r["batch_key"],
            "keepSeparate": r["keep_separate"],
            "keepSeparateNote": r["keep_separate_note"],
            "shortcutKey": r["shortcut_key"], "notes": r["notes"]})
    for rec in recipes.values():
        if not rec["steps"]:
            stepless.append(rec["name"])
    return {
        "stay": {"id": stay["id"], "start_date": stay["start_date"].isoformat(),
                 "end_date": stay["end_date"].isoformat()},
        "recipes": [r for r in recipes.values() if r["steps"]],
        # Named, not dropped: a recipe with servings to cook and no steps is work
        # the board cannot show him, and silence would look like nothing to do.
        "stepless": sorted(stepless),
        "kitchen": {
            "ovenSlots": kitchen["oven_slots"], "burners": kitchen["burners"],
            "airFryerSlots": kitchen["air_fryer_slots"],
            "preheatMin": kitchen["preheat_min"],
            "tempChangeMin": kitchen["temp_change_min"],
            "fillerMin": kitchen["filler_min"],
            "fillerName": kitchen["filler_name"]},
    }


class SessionIn(BaseModel):
    stay_id: Optional[int] = None
    session_date: date
    status: str = "planned"
    schedule_json: Optional[dict[str, Any]] = None
    shortcuts: list[str] = Field(default_factory=list, max_length=20)
    planned_min: Optional[int] = Field(default=None, ge=0, le=1440)
    hands_on_min: Optional[int] = Field(default=None, ge=0, le=1440)


class EventIn(BaseModel):
    session_id: int
    task_key: str = Field(min_length=1, max_length=120)
    task_name: Optional[str] = Field(default=None, max_length=160)
    resource: Optional[str] = None
    kind: str
    planned_min: Optional[float] = Field(default=None, ge=0, le=600)
    actual_min: Optional[float] = Field(default=None, ge=0, le=600)


_SESSION_STATUS = ("planned", "running", "done", "abandoned")
_EVENT_KINDS = ("start", "done", "extend", "skip")


@router.post("/session")
def post_session(body: SessionIn, db: Session = Depends(get_db),
                 _=Depends(verify_health_api_key)) -> dict[str, Any]:
    """Record a session. `started_at` is set when it first becomes `running`.

    That timestamp is the anchor every timer on the board is derived from. Interval
    timers drift and then die when the tab sleeps, and an iPad on a kitchen counter
    locks constantly — an absolute start plus an offset is correct after a lock, a
    reload and an aeroplane-mode blip alike.
    """
    if body.status not in _SESSION_STATUS:
        raise HTTPException(status_code=400, detail={
            "error": "bad_status", "message": f"one of {', '.join(_SESSION_STATUS)}"})
    try:
        res = db.connection().exec_driver_sql("""
            INSERT INTO nutrition.prep_session
              (stay_id, session_date, status, started_at, schedule_json, shortcuts,
               planned_min, hands_on_min)
            VALUES (%s, %s, %s, CASE WHEN %s = 'running' THEN now() ELSE NULL END,
                    %s::jsonb, %s::jsonb, %s, %s)
            RETURNING id""",
            (body.stay_id, body.session_date, body.status, body.status,
             json.dumps(body.schedule_json) if body.schedule_json else None,
             json.dumps(body.shortcuts), body.planned_min, body.hands_on_min))
        session_id = res.scalar()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=404, detail={
            "error": "no_stay", "message": f"no stay {body.stay_id}"})
    except SQLAlchemyError:
        db.rollback()
        logger.exception("prep session write failed")
        raise HTTPException(status_code=500, detail={"error": "write_failed"})
    return {"session_id": session_id, "status": body.status}


@router.post("/session/{session_id}/status")
def patch_session(session_id: int, status: str = Query(...),
                  db: Session = Depends(get_db),
                  _=Depends(verify_health_api_key)) -> dict[str, Any]:
    if status not in _SESSION_STATUS:
        raise HTTPException(status_code=400, detail={"error": "bad_status"})
    try:
        res = db.connection().exec_driver_sql("""
            UPDATE nutrition.prep_session SET
              status = %s,
              -- started_at is set ONCE. Re-starting a paused session must not move
              -- the anchor every running timer is measured from.
              started_at = CASE WHEN %s = 'running' AND started_at IS NULL
                                THEN now() ELSE started_at END,
              finished_at = CASE WHEN %s IN ('done','abandoned')
                                 THEN now() ELSE finished_at END,
              actual_min = CASE WHEN %s IN ('done','abandoned') AND started_at IS NOT NULL
                                THEN EXTRACT(EPOCH FROM (now() - started_at))/60
                                ELSE actual_min END,
              updated_at = now()
            WHERE id = %s RETURNING status, started_at, actual_min""",
            (status, status, status, status, session_id))
        row = res.fetchone()
        if row is None:
            db.rollback()
            raise HTTPException(status_code=404, detail={"error": "no_session"})
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("prep session status failed for %s", session_id)
        raise HTTPException(status_code=500, detail={"error": "write_failed"})
    return {"session_id": session_id, "status": row[0],
            "started_at": row[1].isoformat() if row[1] else None,
            "actual_min": _num(row[2])}


@router.post("/event")
def post_event(body: EventIn, db: Session = Depends(get_db),
               _=Depends(verify_health_api_key)) -> dict[str, Any]:
    """Log one start / done / extend / skip.

    `planned_min` and `actual_min` side by side is the whole point: the difference
    is the correction the duration estimates need. NOTHING READS THIS YET and
    nothing should pretend to — the estimates stay the spec's starting numbers
    until there is data. Recording from the first session is free; back-filling it
    later is impossible.
    """
    if body.kind not in _EVENT_KINDS:
        raise HTTPException(status_code=400, detail={
            "error": "bad_kind", "message": f"one of {', '.join(_EVENT_KINDS)}"})
    delta = (None if body.planned_min is None or body.actual_min is None
             else round(body.actual_min - body.planned_min, 2))
    try:
        db.connection().exec_driver_sql("""
            INSERT INTO nutrition.prep_event
              (session_id, task_key, task_name, resource, kind, planned_min,
               actual_min, delta_min)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (body.session_id, body.task_key, body.task_name, body.resource,
             body.kind, body.planned_min, body.actual_min, delta))
        db.commit()
    except IntegrityError:
        # A session id that does not exist is a KNOWN condition, not a server
        # fault: the foreign key is doing its job. A 500 here would send the
        # caller looking for a broken endpoint instead of a stale session id.
        db.rollback()
        raise HTTPException(status_code=404, detail={
            "error": "no_session", "message": f"no session {body.session_id}"})
    except SQLAlchemyError:
        db.rollback()
        logger.exception("prep event write failed")
        raise HTTPException(status_code=500, detail={"error": "write_failed"})
    return {"logged": body.kind, "delta_min": delta}


@router.get("/sessions")
def get_sessions(stay_id: Optional[int] = Query(default=None),
                 db: Session = Depends(get_db),
                 _=Depends(verify_health_api_key)) -> dict[str, Any]:
    fetch = _fetch_for(db)
    try:
        rows = prep_store.sessions_for(fetch, stay_id)
    except SQLAlchemyError:
        logger.exception("prep sessions read failed")
        raise HTTPException(status_code=500, detail={"error": "read_failed"})
    return {"sessions": [{
        "id": r["id"], "stay_id": r["stay_id"],
        "session_date": r["session_date"].isoformat(),
        "status": r["status"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        "planned_min": r["planned_min"], "hands_on_min": r["hands_on_min"],
        "actual_min": _num(r["actual_min"]), "shortcuts": r["shortcuts"],
    } for r in rows]}


def _step_out(r: dict) -> dict:
    return {
        "step_id": r.get("step_id"), "step_no": r["step_no"],
        "name": r["step_name"], "resource": r["resource"], "mode": r["mode"],
        "base_min": _num(r["base_min"]), "per_serving_min": _num(r["per_serving_min"]),
        "temp_f": r["temp_f"], "batch_key": r["batch_key"],
        "keep_separate": r["keep_separate"],
        "keep_separate_note": r["keep_separate_note"],
        "shortcut_key": r["shortcut_key"], "notes": r["notes"],
    }
