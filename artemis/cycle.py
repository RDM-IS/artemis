"""CYCLE-1 — the 14-day pay-period cycle: day types, locations, day boundaries.

The ONE definition of "where is Ryan on date D, and when does his day start
and end". Both the cron registry (artemis.scheduler) and the boundary helpers
(artemis.quiet_hours.wake_time_on / open_time_on / quiet_start_on / next_wake /
next_open) read from here, so they can never disagree — a split source is how
a 04:30 wake survives a 06:00 Richfield morning.

Cycle position is DERIVED: (date - anchor) % 14. Nothing is stored per day, so
it survives DST and holidays and never drifts. Hand-set exceptions live in
acos.cycle_day_overrides (migration 035); the anchor and the locations registry
live in acos.system_state, with the constants below as fallbacks when the DB is
unreachable.

Two axes, deliberately independent:
  * day_type — work status: msp_work | msp_home | wi | travel
  * location — where he is: office | richfield | brown_deer | msp_home | outside

`wi` is NOT split per place: the wi Sunday is at Brown Deer until 17:00 and at
Richfield after, so location resolves per (date, time of day).

Wake follows the LOCATION (Ryan, 2026-09-19: the travel Monday is a Richfield
morning, so keying wake off the day type would need a special case). Business
open and quiet-hours start follow the DAY TYPE, since they are about working,
not about geography.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

from artemis import config

logger = logging.getLogger(__name__)

CYCLE_LEN = 14
DEFAULT_ANCHOR = date(2026, 9, 20)          # Sun — week 1, day 1 (confirmed)

ANCHOR_KEY = "cycle_anchor"
LOCATIONS_KEY = "cycle_locations"
SEGMENTS_KEY = "cycle_day_segments"

# Position 0..13 from the anchor: 8 msp_work, 2 msp_home, 3 wi, 1 travel.
DAY_TYPES: tuple[str, ...] = (
    "msp_home", "msp_work", "msp_work", "msp_work", "msp_work", "wi", "wi",      # wk 1
    "wi", "travel", "msp_work", "msp_work", "msp_work", "msp_work", "msp_home",  # wk 2
)

# ── Where he is, in two layers ───────────────────────────────────────────────
#
# DAY_LOCATIONS is the day's ANCHOR: where he WAKES. It is what "the day's
# location" means — the wake time comes from it, and a seeded plan row carries
# it. It never changes during the day, and it is never `transit`.
#
# DAY_SEGMENTS is where he is THROUGH the day, because the day structure is
# becoming a morning session plus an evening one (Ryan, 2026-09-22) and an
# evening session must not read the office gym's inventory. Each entry is
# (location, start) in order; the first start is None, meaning local midnight.
#
# WAKE READS THE ANCHOR, NEVER THE 00:00 SEGMENT. An msp_work day starts at
# msp_home, so a wake keyed off the segment would return 07:30 instead of 04:30
# and move every weekday.
FRIDAY_MOVE = time(16, 0)
SUNDAY_MOVE = time(17, 0)
DAY_LOCATIONS: dict[int, str] = {
    0: "msp_home", 1: "office", 2: "office", 3: "office", 4: "office",
    5: "richfield",
    6: "brown_deer",
    7: "brown_deer",         # the wi Sunday moves to Richfield at 17:00
    8: "richfield",          # travel: the morning is a Richfield morning
    9: "office", 10: "office", 11: "office", 12: "office", 13: "msp_home",
}

# Office hours ~06:00-16:30 with a 30 min commute either side (Ryan's times).
_WORK_DAY = (("msp_home", None), ("office", "05:00"), ("msp_home", "17:00"))
DAY_SEGMENTS: dict[int, tuple] = {
    0: (("msp_home", None),),
    1: _WORK_DAY, 2: _WORK_DAY, 3: _WORK_DAY,
    # wk 1 Thursday: an office day that ends with the 5 h drive to the farm.
    4: (("msp_home", None), ("office", "05:00"), ("transit", "16:30"),
        ("richfield", "21:30")),
    5: (("richfield", None), ("brown_deer", "16:00")),
    6: (("brown_deer", None),),
    7: (("brown_deer", None), ("richfield", "17:00")),
    # travel Monday: leaves the farm at 11:00, ~5 h to MSP.
    8: (("richfield", None), ("transit", "11:00"), ("msp_home", "16:00")),
    9: _WORK_DAY, 10: _WORK_DAY, 11: _WORK_DAY, 12: _WORK_DAY,
    13: (("msp_home", None),),
}

# The registry. Wake is per location (Ryan's table). `tz` is here because
# LOCATION-1 calls for it; every current location is Central.
# `tz` comes from config: never hard-code the home zone outside config.py
# (the schedule follows a `set timezone` override — see tests/test_date_anchoring).
_HOME_TZ = config.HOME_TIMEZONE
DEFAULT_LOCATIONS: dict[str, dict] = {
    "office":     {"display": "office gym", "tz": _HOME_TZ, "wake": "04:30"},
    # `transit` is a pseudo-location: on the road, no equipment. NO SESSION IS
    # EVER PLACED IN A TRANSIT SEGMENT — one that would land there is reported,
    # not relocated (Ryan, 2026-09-22). It is never a day's anchor, so its wake
    # is only a fallback that nothing should read.
    "transit":    {"display": "on the road", "tz": _HOME_TZ, "wake": "07:30",
                   "transit": True, "equipment": []},
    "richfield":  {"display": "Richfield",  "tz": _HOME_TZ, "wake": "06:00"},
    "brown_deer": {"display": "Brown Deer", "tz": _HOME_TZ, "wake": "07:30"},
    "msp_home":   {"display": "home",       "tz": _HOME_TZ, "wake": "07:30"},
    "outside":    {"display": "outside",    "tz": _HOME_TZ, "wake": "07:30"},
}

# Business hours follow work status, not geography. travel counts as a work
# day: the drive displaces the morning, it doesn't make it a weekend.
DAY_TYPE_HOURS: dict[str, tuple[str, str]] = {
    "msp_work": ("06:30", "17:00"),
    "travel":   ("06:30", "17:00"),
    "msp_home": ("08:30", "22:30"),
    "wi":       ("08:30", "22:30"),
}

_VALID_DAY_TYPES = frozenset(DAY_TYPES)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# ---------------------------------------------------------------------------
# Config (acos.system_state, with the constants above as the fallback)
# ---------------------------------------------------------------------------

def anchor() -> date:
    """The cycle anchor. system_state wins; DEFAULT_ANCHOR is the fallback."""
    try:
        from artemis.quiet_hours import get_system_value
        raw = get_system_value(ANCHOR_KEY)
        if raw:
            return date.fromisoformat(raw.strip()[:10])
    except Exception:
        logger.debug("cycle anchor: falling back to the built-in", exc_info=True)
    return DEFAULT_ANCHOR


def locations() -> dict[str, dict]:
    """The locations registry. system_state wins; DEFAULT_LOCATIONS is the
    fallback. A malformed value falls back rather than raising."""
    try:
        from artemis.quiet_hours import get_system_value
        raw = get_system_value(LOCATIONS_KEY)
        if raw:
            got = json.loads(raw)
            if isinstance(got, dict) and got:
                return got
    except Exception:
        logger.debug("cycle locations: falling back to the built-in", exc_info=True)
    return DEFAULT_LOCATIONS


def known_location(key: str | None) -> bool:
    return bool(key) and key in locations()


# ---------------------------------------------------------------------------
# Overrides (acos.cycle_day_overrides, migration 035)
# ---------------------------------------------------------------------------

class OverrideLookupError(RuntimeError):
    """The override table could not be read. NOT "there is no override"."""


def override_for(d: date) -> dict | None:
    """The live override covering `d`, newest first. None when there is none.

    FAIL-CLOSED (2026-09-26). This used to swallow every exception and return
    None, so an unreachable or unreadable `acos.cycle_day_overrides` answered
    "no override" — indistinguishable from the real answer, and the base pattern
    then resolved cleanly and wrote cleanly. Eleven plan rows were rebuilt on
    the strength of exactly that during the leave-week write: the read happened
    on a second connection that could not see the still-uncommitted overrides.

    `None` now means one thing only: the table was read and covers nothing.
    A read that FAILS raises `OverrideLookupError`. A caller that genuinely
    wants the base pattern says so with `use_overrides=False` — nothing gets it
    by accident from a failed read.
    """
    from knowledge.db import execute_one
    from knowledge.dbguard import RealDbInTestError
    try:
        return execute_one(
            "SELECT override_id, start_date, end_date, day_type, location, reason "
            "FROM acos.cycle_day_overrides "
            "WHERE revoked_at IS NULL AND start_date <= %s AND end_date >= %s "
            "ORDER BY created_at DESC LIMIT 1",
            (d, d),
        )
    except RealDbInTestError:
        # The ONE swallow, and it is not a failed read: TEST-DB-GUARD raises this
        # only when a test is running, and no test has an overrides table, so
        # "nothing covers this day" is the honest answer rather than a guess.
        # It cannot fire in production — the guard needs ARTEMIS_TEST_NO_DB or
        # pytest imported. Every real failure mode (refused, auth, timeout,
        # UndefinedTable, permission) still raises below.
        #
        # NARROW ON PURPOSE: this catches one exception type, raised by one guard
        # whose entire job is to say "you are in a test". WIDENING IT — to
        # Exception, to psycopg2.Error, to anything that can occur in production —
        # RE-CREATES THE 2026-09-26 DEFECT, where a failed read answered "no
        # override" and eleven plan rows were rebuilt against the base pattern and
        # committed. See FAIL-CLOSED-RESOLVERS in CLAUDE.md.
        return None
    except Exception as exc:
        raise OverrideLookupError(
            f"could not read acos.cycle_day_overrides for {d}: {exc}") from exc


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def cycle_pos(d: date, anchor_date: date | None = None) -> int:
    """0..13. Derived from the anchor, so it never drifts."""
    return (d - (anchor_date or anchor())).days % CYCLE_LEN


def day_type(d: date, *, override: dict | None = None, use_overrides: bool = True) -> str:
    """Work status on `d`. An override's day_type wins over the derived one."""
    ov = override if override is not None else (override_for(d) if use_overrides else None)
    if ov and ov.get("day_type") in _VALID_DAY_TYPES:
        return ov["day_type"]
    return DAY_TYPES[cycle_pos(d)]


def segments() -> dict[int, tuple]:
    """The day-segment table. system_state wins; DAY_SEGMENTS is the fallback.
    A malformed or unusable value falls back rather than raising, and a segment
    naming an unknown location is dropped."""
    try:
        from artemis.quiet_hours import get_system_value
        raw = get_system_value(SEGMENTS_KEY)
        if not raw:
            return DAY_SEGMENTS
        got = json.loads(raw)
        out: dict[int, tuple] = {}
        for pos, segs in (got or {}).items():
            clean = []
            for loc, start in segs:
                if not known_location(loc):
                    raise ValueError(f"unknown location {loc!r}")
                clean.append((loc, start))
            if not clean or clean[0][1] is not None:
                raise ValueError(f"position {pos}: the first segment must start at midnight")
            out[int(pos) % CYCLE_LEN] = tuple(clean)
        if out:
            return {**DAY_SEGMENTS, **out}
    except Exception:
        logger.warning("cycle day segments: falling back to the built-in table",
                       exc_info=True)
    return DAY_SEGMENTS


def is_transit(location: str | None) -> bool:
    """True for the `transit` pseudo-location — on the road, no equipment, and
    never a place a session is put."""
    return bool((locations().get(location or "") or {}).get("transit"))


def anchor_location(d: date, *, override: dict | None = None,
                    use_overrides: bool = True) -> str:
    """The day's location: where he WAKES. Never a segment, never `transit`."""
    ov = override if override is not None else (override_for(d) if use_overrides else None)
    if ov and ov.get("location"):
        if known_location(ov["location"]):
            return ov["location"]
        logger.warning("cycle override %s names unknown location %r — ignoring it",
                       ov.get("override_id"), ov.get("location"))
    return DAY_LOCATIONS[cycle_pos(d)]


def location_at(d: date, t: time | None = None, *, override: dict | None = None,
                use_overrides: bool = True) -> str:
    """Where he is on `d` at `t`. With no time, the day's anchor (the morning).
    An override's location wins for the whole day."""
    ov = override if override is not None else (override_for(d) if use_overrides else None)
    if ov and ov.get("location") and known_location(ov["location"]):
        return ov["location"]
    anchor_loc = anchor_location(d, override=ov, use_overrides=False)
    if t is None:
        return anchor_loc
    segs = segments().get(cycle_pos(d)) or ((anchor_loc, None),)
    here = segs[0][0]
    for loc, start in segs[1:]:
        if t >= _parse_hhmm(start):
            here = loc
        else:
            break
    return here


def wake_on(d: date, **kw) -> time:
    """Wake follows the day's ANCHOR location — never the 00:00 segment, which
    on an msp_work day is msp_home and would move every weekday to 07:30."""
    loc = anchor_location(d, **kw)
    cfg = locations().get(loc) or DEFAULT_LOCATIONS["office"]
    return _parse_hhmm(cfg.get("wake") or DEFAULT_LOCATIONS["office"]["wake"])


def open_on(d: date, **kw) -> time:
    """Business open follows the DAY TYPE (working or not)."""
    return _parse_hhmm(DAY_TYPE_HOURS[day_type(d, **kw)][0])


def quiet_on(d: date, **kw) -> time:
    """Quiet-hours start follows the DAY TYPE."""
    return _parse_hhmm(DAY_TYPE_HOURS[day_type(d, **kw)][1])


def boundaries(d: date, **kw) -> dict:
    """Everything the schedule needs for one local date, in one read."""
    ov = kw.pop("override", None)
    if ov is None and kw.get("use_overrides", True):
        ov = override_for(d)
    kw.pop("use_overrides", None)
    quiet = _parse_hhmm(DAY_TYPE_HOURS[day_type(d, override=ov)][1])
    return {
        "date": d,
        "pos": cycle_pos(d),
        "day_type": day_type(d, override=ov),
        "location": anchor_location(d, override=ov),
        # where he is when quiet hours start — the evening half of the day
        "evening_location": location_at(d, quiet, override=ov),
        "wake": wake_on(d, override=ov),
        "open": open_on(d, override=ov),
        "quiet": quiet,
        "override_id": (ov or {}).get("override_id"),
    }


def describe(d: date) -> str:
    """One line for a post or a log: what this date is."""
    b = boundaries(d)
    disp = (locations().get(b["location"]) or {}).get("display", b["location"])
    return (f"{d:%a %-m/%d} — {b['day_type']} at {disp} · wake {b['wake']:%H:%M} · "
            f"open {b['open']:%H:%M} · quiet {b['quiet']:%H:%M}")


def apply_override_effect(start: date, end: date, now: datetime) -> dict:
    """What an override spanning [start, end] actually changes, as of `now`.

    Ryan, 2026-09-19: overrides take effect from the NEXT day by default. One
    that covers today still applies to a boundary that hasn't passed yet; one
    whose new time is already behind us is skipped FOR TODAY ONLY, and the
    confirmation has to say so. Never a silent skip.

    Returns {"effective_from": date, "today_applies": [...], "today_skipped":
    [(kind, time), ...]} — the caller turns that into the reply.
    """
    today = now.date()
    if start > today:
        return {"effective_from": start, "today_applies": [], "today_skipped": []}
    applies, skipped = [], []
    for kind, get in (("wake", wake_on), ("open", open_on), ("quiet", quiet_on)):
        t = get(today)
        (applies if now.time() < t else skipped).append((kind, t))
    return {
        "effective_from": today,
        "today_applies": [k for k, _ in applies],
        "today_skipped": skipped,
    }


def describe_override_effect(effect: dict) -> str:
    """The confirmation line. States a same-day skip explicitly."""
    if not effect["today_applies"] and not effect["today_skipped"]:
        return f"In effect from {effect['effective_from']:%a %-m/%d}."
    parts = []
    if effect["today_applies"]:
        parts.append("today's " + ", ".join(effect["today_applies"]) + " move")
    if effect["today_skipped"]:
        past = ", ".join(f"{k} ({t:%H:%M})" for k, t in effect["today_skipped"])
        parts.append(f"{past} already passed today — unchanged until tomorrow")
    return "In effect now: " + "; ".join(parts) + "."


def next_boundary(kind: str, now: datetime) -> datetime:
    """The next `wake` / `open` / `quiet` instant at or after `now` (local)."""
    get = {"wake": wake_on, "open": open_on, "quiet": quiet_on}[kind]
    for offset in range(0, 3):
        d = now.date() + timedelta(days=offset)
        cand = datetime.combine(d, get(d), tzinfo=now.tzinfo)
        if cand > now:
            return cand
    raise AssertionError("unreachable")
