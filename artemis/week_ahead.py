"""WEEK-AHEAD — see the gaps before they happen. Read-only.

Built 2026-09-28 because 10/04 is a travel day with no travel default in Notion,
and nothing told Ryan. The pre-fill would have recorded `no_plan` at 00:15 on the
day itself — correct behaviour, silent, and far too late to pick a menu.

**ONE CODE PATH, and it is the whole point.** The meal source comes from
`nutrition.resolve_meal_source()`, the same function the 00:15 pre-fill uses;
whether a location can hold a session comes from `session_library._supported()`,
the same predicate the library uses to decide what to offer. A lookahead that
re-implemented either would drift from it, and the drift would show up as Ryan
being told a day was covered when it was not — the failure this exists to catch,
reintroduced one level up.

Deterministic throughout. Every flag is a fact read out of config or Notion and
compared; nothing is scored, ranked or predicted, and no LLM is involved.

FAIL-CLOSED: a day whose resolution cannot be read is reported `unknown` and
flagged. It is never shown as having a source. "I could not tell" and "you are
covered" must not look the same, which is the same rule as `gaps` refusing to
report an empty week it failed to read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 7

#: Flag → the one-line explanation shown to Ryan. Order is display order: the
#: ones he can still act on come first.
FLAG_TEXT: dict[str, str] = {
    "no_meal_source": "no meal source — pick a menu in Notion or it stays empty",
    "session_cant_be_held": "the session can't be run at that location",
    "no_warmup_configured": "no warmup/cooldown configured for that location",
    "travel_day": "travel day",
    "unknown": "could not resolve this day — treat as unknown, not as covered",
}
FLAG_ORDER: tuple[str, ...] = tuple(FLAG_TEXT)


@dataclass
class DayAhead:
    day: date
    day_type: str | None = None
    location_key: str | None = None
    location_display: str | None = None
    sessions: list[dict] = field(default_factory=list)   # {slot, session_type, display_name}
    meal_outcome: str = "unknown"      # planned | no_plan | unavailable | unknown
    meal_chosen: str | None = None
    meal_detail: str | None = None
    flags: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # the sessions that can't be held

    @property
    def flagged(self) -> bool:
        return bool(self.flags)


def _sessions_for(cur, d: date) -> list[dict]:
    """Both slots, in slot order. A day with no rows returns []."""
    cur.execute(
        "SELECT slot, session_type, blocks->>'display_name' AS display_name "
        "FROM health.plan WHERE plan_date = %s ORDER BY slot DESC", (d,))
    out = []
    for r in cur.fetchall():
        slot, stype, name = (r["slot"], r["session_type"], r["display_name"]) \
            if isinstance(r, dict) else r
        out.append({"slot": slot, "session_type": stype,
                    "display_name": name or stype})
    return out


def _is_light(session_type: str) -> bool:
    """Rest and recovery rows need no location support and no warmup."""
    from knowledge.session_types import REST_TYPES
    return session_type in REST_TYPES


def day_ahead(cur, d: date) -> DayAhead:
    """One day, resolved. Never raises: an unreadable day is `unknown`."""
    from artemis import cycle, health_office as office, session_library as sl
    from artemis import nutrition
    from knowledge import warmup

    out = DayAhead(day=d)
    try:
        out.day_type = cycle.day_type(d)
        out.location_key = office.day_location_key(d)
        out.location_display = (cycle.locations().get(out.location_key) or {}).get(
            "display", out.location_key)
        out.sessions = _sessions_for(cur, d)
    except Exception:                                           # noqa: BLE001
        logger.warning("week-ahead: could not resolve %s", d, exc_info=True)
        out.flags = ["unknown"]
        return out

    try:
        src = nutrition.resolve_meal_source(d)
        out.meal_outcome, out.meal_chosen = src.outcome, src.chosen
        out.meal_detail = src.detail
    except Exception:                                           # noqa: BLE001
        # FAIL-CLOSED: never fall back to a guess about the source.
        logger.warning("week-ahead: could not resolve the meal source for %s", d,
                       exc_info=True)
        out.meal_outcome, out.meal_detail = "unknown", "the meal source could not be read"

    flags: list[str] = []
    if out.meal_outcome in ("no_plan", "unavailable"):
        flags.append("no_meal_source")
    elif out.meal_outcome == "unknown":
        flags.append("unknown")

    training = [s for s in out.sessions if not _is_light(s["session_type"])]
    for s in training:
        try:
            supported = sl._supported(out.location_key, s["session_type"])
        except Exception:                                       # noqa: BLE001
            logger.warning("week-ahead: could not resolve support for %s %s",
                           d, s["session_type"], exc_info=True)
            if "unknown" not in flags:
                flags.append("unknown")
            continue
        if not supported:
            out.unsupported.append(f"{s['slot']} {s['display_name']}")
    if out.unsupported:
        flags.append("session_cant_be_held")

    # A warmup only matters on a day that trains. A rest day needing no warmup is
    # not a gap, and flagging it would bury the days that are.
    if training and not warmup.is_known(out.location_key):
        flags.append("no_warmup_configured")

    if out.day_type == "travel":
        flags.append("travel_day")

    out.flags = [f for f in FLAG_ORDER if f in flags]
    return out


def week_ahead(cur, start: date, days: int = DEFAULT_DAYS) -> list[DayAhead]:
    """`days` days from `start`, inclusive. Read-only."""
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    return [day_ahead(cur, start + timedelta(n)) for n in range(days)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _session_text(d: DayAhead) -> str:
    if not d.sessions:
        return "no plan row"
    return " · ".join(s["display_name"] for s in d.sessions)


def _meal_text(d: DayAhead) -> str:
    return {"planned": f"meals: {d.meal_chosen}",
            "no_plan": "meals: none",
            "unavailable": "meals: Notion unreachable",
            "unknown": "meals: unknown"}.get(d.meal_outcome, "meals: unknown")


def flag_lines(week: list[DayAhead]) -> list[str]:
    """One line per flagged day, flags only. Empty when there is nothing wrong —
    the caller adds nothing in that case rather than posting "all clear", because
    a weekly "nothing to report" trains you to skim past the week it isn't.

    ONE EXCEPTION, and it is about noise rather than truth: when the ONLY thing
    wrong with every flagged day is that it could not be resolved, that is one
    fact about the lookahead, not seven about the week. A transient database
    problem used to render as seven identical "could not resolve" lines, which
    buries the signal it is trying to raise.
    """
    flagged = [d for d in week if d.flagged]
    if flagged and all(d.flags == ["unknown"] for d in flagged):
        return [f"  **{len(flagged)} day(s) could not be resolved** — "
                f"{FLAG_TEXT['unknown']}"]
    lines = []
    for d in flagged:
        what = "; ".join(FLAG_TEXT[f] for f in d.flags)
        lines.append(f"  **{d.day:%a %-m/%-d}** ({d.location_display}) — {what}")
    return lines


def render(week: list[DayAhead]) -> str:
    """The `week ahead` reply: flags first, then one line per day."""
    if not week:
        return "Nothing to look ahead at."
    head = f"**Week ahead — {week[0].day:%a %-m/%-d} to {week[-1].day:%a %-m/%-d}**"
    flags = flag_lines(week)
    out = [head]
    if flags:
        out.append(f"\n⚠️ **{len(flags)} day(s) need something:**")
        out.extend(flags)
    else:
        out.append("\nNo gaps found in these 7 days.")
    out.append("")
    for d in week:
        mark = "⚠️ " if d.flagged else "   "
        out.append(f"{mark}{d.day:%a %-m/%-d}  {d.day_type or '?':<9} "
                   f"{(d.location_display or '?'):<12} {_session_text(d)} — {_meal_text(d)}")
    return "\n".join(out)
