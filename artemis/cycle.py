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

# Position 0..13 from the anchor: 8 msp_work, 2 msp_home, 3 wi, 1 travel.
DAY_TYPES: tuple[str, ...] = (
    "msp_home", "msp_work", "msp_work", "msp_work", "msp_work", "wi", "wi",      # wk 1
    "wi", "travel", "msp_work", "msp_work", "msp_work", "msp_work", "msp_home",  # wk 2
)

# Where each position is. A tuple means the location changes during the day:
# (before, after, boundary) — the wi Sunday moves Brown Deer -> Richfield 17:00.
SUNDAY_MOVE = time(17, 0)
DAY_LOCATIONS: dict[int, object] = {
    0: "msp_home", 1: "office", 2: "office", 3: "office", 4: "office",
    5: "richfield", 6: "brown_deer",
    7: ("brown_deer", "richfield", SUNDAY_MOVE),
    8: "richfield",          # travel: the morning is a Richfield morning
    9: "office", 10: "office", 11: "office", 12: "office", 13: "msp_home",
}

# The registry. Wake is per location (Ryan's table). `tz` is here because
# LOCATION-1 calls for it; every current location is Central.
# `tz` comes from config: never hard-code the home zone outside config.py
# (the schedule follows a `set timezone` override — see tests/test_date_anchoring).
_HOME_TZ = config.HOME_TIMEZONE
DEFAULT_LOCATIONS: dict[str, dict] = {
    "office":     {"display": "office gym", "tz": _HOME_TZ, "wake": "04:30"},
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

def override_for(d: date) -> dict | None:
    """The live override covering `d`, newest first. None when there is none.

    Read-only and fail-open: a DB problem means "no override", never an
    exception into the scheduler.
    """
    try:
        from knowledge.db import execute_one
        return execute_one(
            "SELECT override_id, start_date, end_date, day_type, location, reason "
            "FROM acos.cycle_day_overrides "
            "WHERE revoked_at IS NULL AND start_date <= %s AND end_date >= %s "
            "ORDER BY created_at DESC LIMIT 1",
            (d, d),
        )
    except Exception:
        logger.debug("cycle override lookup failed for %s — treating as none", d, exc_info=True)
        return None


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


def location_at(d: date, t: time | None = None, *, override: dict | None = None,
                use_overrides: bool = True) -> str:
    """Where he is on `d` at `t` (default: the whole-day location, which for a
    split day is the morning one). An override's location wins."""
    ov = override if override is not None else (override_for(d) if use_overrides else None)
    if ov and ov.get("location"):
        if known_location(ov["location"]):
            return ov["location"]
        logger.warning("cycle override %s names unknown location %r — ignoring it",
                       ov.get("override_id"), ov.get("location"))
    spec = DAY_LOCATIONS[cycle_pos(d)]
    if isinstance(spec, tuple):
        before, after, boundary = spec
        return after if (t is not None and t >= boundary) else before
    return spec


def wake_on(d: date, **kw) -> time:
    """Wake follows the LOCATION, resolved at the start of the day."""
    loc = location_at(d, time(0, 0), **kw)
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
        "location": location_at(d, time(0, 0), override=ov),
        # the evening location — quiet hours start after the wi Sunday's move
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
