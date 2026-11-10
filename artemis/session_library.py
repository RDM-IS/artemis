"""SESSION-LIB — the library of sessions that can be started on demand.

Spec: docs/ARTEMIS_STATE.md, SESSION-LIB. The rules this module keeps:

* **The same builder that seeds plan rows.** Every entry is
  `health_office.build_row(...)` for the session type, the location and the
  current program week — nothing is a stored template, so Phase 2 Week 3 comes
  out right with nothing to update.
* **Extras every day** (Ryan, 2026-09-27). The general list is low-impact
  extras (EXTRA_TYPES), rest days included. The MAKEUP-2 offer that used to sit
  beside it is RETIRED (SESSION-OPEN, 2026-11-09): `makeup` keeps its keys so
  older gym-display builds render nothing, and `offer` is always null.
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

from knowledge.session_types import EXTRA_TYPES, STRENGTH_BW, STRENGTH_TYPES  # noqa: E402

#: Extras (Ryan, 2026-09-27): yoga / core / mobility, every day.
#: QUICK-START (Ryan, 2026-10-20): "give me buttons to decide what I want to do
#: and when" -- so strength A/B/C are in the library too, wherever the room can
#: hold them, and gym-display's Strength button picks the next in rotation.
#: The Extras list on the screen still shows only EXTRA_TYPES.
#: WEEK-TEMPLATE (2026-11-01): strength_full joins; A/B/C stay launchable.
#: SESSION-OPEN 3a (2026-11-09): strength_bw joins — bodyweight is strength.
LIBRARY_TYPES = EXTRA_TYPES + STRENGTH_TYPES + (STRENGTH_BW,)

#: Pseudo-locations with no room to train in.
_NOT_A_ROOM = frozenset({"transit", "outside"})


def _supported(location_key: str, session_type: str) -> bool:
    from artemis import health_office as office
    from knowledge import cardio as cardio_cfg
    if session_type == STRENGTH_BW:
        # SESSION-OPEN 3a: a floor travels, so every real room (the library
        # already skips transit and outside). Its builder still resolves the row
        # and cardio from the room's inventory and RAISES where that inventory is
        # unknown — build() then logs the failure loudly and leaves it out, rather
        # than this answer hiding it as "not supported".
        return True
    if session_type.startswith("strength"):
        return office.can_hold(location_key, session_type)
    if session_type == "cardio_z2":
        return bool(cardio_cfg.resolve(location_key).get("modality"))
    if session_type in ("recovery_flow", "core", "mobility", "yoga_strength"):
        return True           # a mat travels; the library already excludes the road
    return False


def entry_for(session_type: str, location_key: str, today: date) -> dict:
    """One launchable session — the row the seeder would write, minus the date."""
    from artemis import block2
    from artemis import cycle
    from artemis import health_office as office
    # QUICK-START: past block 1's last day, office.week_num_for() runs off the
    # end of block 1's ramp and strength would quietly vanish from the library
    # on 11/01. Inside block 2, build with block 2's week and ramp.
    wk2 = block2.week_num_for(today)
    builder = block2._build_one if wk2 else office.build_row
    row = builder({
        "plan_date": today, "slot": "morning", "session_type": session_type,
        "week_num": wk2 or office.week_num_for(today),
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
# MAKEUP-2 — RETIRED by SESSION-OPEN (Ryan, 2026-11-09). Plan rows are
# suggestions; the week is judged by knowledge.strength_quota. The shape stays
# (gym-display's LibraryScreen reads `makeup.offer` and `makeup.repeat`) so an
# older build shows nothing rather than breaking.
# ---------------------------------------------------------------------------

MAKEUP_RETIRED: dict = {"week_start": None, "not_done": [], "offer": None,
                        "repeat": False, "retired": True}


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
    out["makeup"] = dict(MAKEUP_RETIRED)
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
    print(f"week {lib['week_num']}, today at {lib['today_location_key']}, "
          f"written to acos.system_state[{SYSTEM_KEY!r}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
