"""SESSION-LIB — the library of sessions that can be started on demand.

Spec: docs/ARTEMIS_STATE.md, SESSION-LIB. The rules this module keeps:

* **The same builder that seeds plan rows.** Every entry is
  `health_office.build_row(...)` for the session type, the location and the
  current program week — nothing is a stored template, so Phase 2 Week 3 comes
  out right with nothing to update.
* **Extras every day; training only as a makeup** (Ryan, 2026-09-27). The
  general list is low-impact extras (EXTRA_TYPES: yoga now, core and mobility
  once defined), rest days included. Strength and cardio appear only as the
  makeup of the ONE session not done this program week, on a rest day.
* **Absent, not greyed.** A session a location can't support isn't listed
  there: strength needs an approved substitution table (`can_hold`), cardio
  needs a machine (`knowledge.cardio.resolve`). Recovery Flow needs a mat, which
  travels, so it is everywhere except on the road. Core and the other yoga
  flows have no definition yet and are not listed anywhere.
* **Regenerated nightly** (00:20 local, silent) and on demand
  (`python3.11 -m artemis.session_library`) into `acos.system_state`
  (`session_library`), which the Lambda serves at `GET /api/health/library`.
  The Lambda can't run the builder itself — it reads the cycle through
  box-only code.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

SYSTEM_KEY = "session_library"

from knowledge.session_types import EXTRA_TYPES, REST_TYPES, is_training  # noqa: E402

#: Extras only (Ryan, 2026-09-27): yoga / core / mobility, every day. Strength
#: and cardio appear ONLY as a makeup, on a rest day — see makeup_state().
LIBRARY_TYPES = EXTRA_TYPES

#: Pseudo-locations with no room to train in.
_NOT_A_ROOM = frozenset({"transit", "outside"})


def _supported(location_key: str, session_type: str) -> bool:
    from artemis import health_office as office
    from knowledge import cardio as cardio_cfg
    if session_type.startswith("strength"):
        return office.can_hold(location_key, session_type)
    if session_type == "cardio_z2":
        return bool(cardio_cfg.resolve(location_key).get("modality"))
    if session_type in ("recovery_flow", "core", "mobility"):
        return True           # a mat travels; the library already excludes the road
    return False


def entry_for(session_type: str, location_key: str, today: date) -> dict:
    """One launchable session — the row the seeder would write, minus the date."""
    from artemis import cycle
    from artemis import health_office as office
    row = office.build_row({
        "plan_date": today, "slot": "morning", "session_type": session_type,
        "week_num": office.week_num_for(today),
        # The same display the seeder puts on rows (health_office.day_location):
        # the builder compares it to the office's name.
        "location": (cycle.DEFAULT_LOCATIONS.get(location_key) or {}).get("display", location_key),
        "location_key": location_key, "day_type": cycle.day_type(today), "wk0": False,
    })
    return {
        "session_type": session_type,
        "display_name": row["blocks"].get("display_name") or session_type,
        "week_num": row["week_num"], "phase": row["phase"],
        "target_rpe": row["target_rpe"], "target_hr_zone": row["target_hr_zone"],
        "est_duration_min": row["est_duration_min"],
        "blocks": copy.deepcopy(row["blocks"]),
    }


# ---------------------------------------------------------------------------
# MAKEUP-2 (Ryan, 2026-09-27): one session not done in a program week can be
# made up on a later rest day of that week — the missed day becomes rest, the
# rest day becomes the session. More than one not done: nothing is offered,
# the missed stay missed, and the week repeats (proposed, then confirmed).
# "Not done" = a morning training row before today with no real log, whether
# it was skipped deliberately or simply missed.
# ---------------------------------------------------------------------------

def program_week_start(d: date) -> date | None:
    from artemis import health_office as office
    if d < office.WEEK2_START:
        return None
    return office.WEEK2_START + timedelta(days=7 * ((d - office.WEEK2_START).days // 7))


def makeup_state(rows: list[dict], today: date, week_start: date | None) -> dict:
    """Pure. `rows`: the program week's MORNING rows up to and including today,
    each {plan_id, plan_date, session_type, display_name, is_skipped, logged}
    where `logged` means a real (non-inferred) log exists."""
    out: dict = {"week_start": week_start.isoformat() if week_start else None,
                 "not_done": [], "offer": None, "repeat": False}
    if week_start is None:
        return out
    for r in rows:
        if r["plan_date"] < today and is_training(r["session_type"]) and not r["logged"]:
            out["not_done"].append({"plan_id": r["plan_id"], "plan_date": r["plan_date"].isoformat(),
                                    "session_type": r["session_type"],
                                    "display_name": r.get("display_name") or r["session_type"],
                                    "skipped": bool(r.get("is_skipped"))})
    out["repeat"] = len(out["not_done"]) > 1
    today_row = next((r for r in rows if r["plan_date"] == today), None)
    if (len(out["not_done"]) == 1 and today_row is not None
            and today_row["session_type"] in REST_TYPES and not today_row["logged"]):
        m = out["not_done"][0]
        out["offer"] = {"missed_plan_id": m["plan_id"], "missed_date": m["plan_date"],
                        "rest_plan_id": today_row["plan_id"], "session_type": m["session_type"],
                        "display_name": m["display_name"]}
    return out


def _load_week_rows(today: date, week_start: date) -> list[dict]:
    from knowledge.db import execute_query
    rows = execute_query(
        "SELECT p.plan_id, p.plan_date, p.session_type, p.is_skipped, "
        "p.blocks->>'display_name' AS display_name, "
        "EXISTS (SELECT 1 FROM health.session_log sl WHERE sl.plan_id = p.plan_id "
        "        AND sl.logged_via <> 'inferred') AS logged "
        "FROM health.plan p WHERE p.slot = 'morning' AND p.plan_date BETWEEN %s AND %s "
        "ORDER BY p.plan_date", (week_start, today))
    return [dict(r) for r in rows]


def build(today: date) -> dict:
    """The whole library for `today`, every real location. Pure apart from the
    cycle/config reads the seeder itself makes."""
    from artemis import cycle
    from artemis import health_office as office
    locs = cycle.locations()
    today_key = office.day_location_key(today)
    out: dict = {"generated_on": today.isoformat(),
                 "week_num": office.week_num_for(today),
                 "today_location_key": today_key,
                 "locations": []}
    ws = program_week_start(today)
    mk = makeup_state(_load_week_rows(today, ws) if ws else [], today, ws)
    offer = mk["offer"]
    if offer:
        # Built where he is TODAY, by the seeder — and only if this place can hold it.
        if _supported(today_key, offer["session_type"]):
            offer["location_key"] = today_key
            offer["entry"] = entry_for(offer["session_type"], today_key, today)
        else:
            mk["offer"] = None
            mk["offer_blocked"] = (f"{offer['display_name']} can't be run at "
                                   f"{(locs.get(today_key) or {}).get('display', today_key)}")
    out["makeup"] = mk
    for key, meta in locs.items():
        if key in _NOT_A_ROOM or (meta or {}).get("transit"):
            continue
        sessions = []
        for st in LIBRARY_TYPES:
            if not _supported(key, st):
                continue
            try:
                sessions.append(entry_for(st, key, today))
            except Exception:
                # One session failing to build must not hide the others, and
                # must not be replaced by a guess — it's absent and logged.
                logger.exception("session library: %s at %s failed to build", st, key)
        out["locations"].append({"key": key, "display": (meta or {}).get("display", key),
                                 "sessions": sessions})
    return out


def write(today: date | None = None) -> dict:
    from artemis.quiet_hours import local_today, set_system_value
    lib = build(today or local_today())
    set_system_value(SYSTEM_KEY, json.dumps(lib, default=str))
    return lib


def main() -> int:
    lib = write()
    for loc in lib["locations"]:
        names = ", ".join(s["display_name"] for s in loc["sessions"]) or "nothing"
        print(f"{loc['display']:>12}: {names}")
    mk = lib.get("makeup") or {}
    nd = ", ".join(f"{m['plan_date']} {m['display_name']}" for m in mk.get("not_done", [])) or "none"
    print(f"not done this week: {nd}")
    if mk.get("offer"):
        print(f"makeup on offer: {mk['offer']['display_name']} from {mk['offer']['missed_date']}")
    elif mk.get("repeat"):
        print("more than one not done — the week repeats (proposed, then confirmed)")
    print(f"week {lib['week_num']}, today at {lib['today_location_key']}, "
          f"written to acos.system_state[{SYSTEM_KEY!r}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
