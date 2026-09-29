"""AWAY — work trips, vacations and hunting, as cycle overrides.

ONE override kind with two attributes, not four day types. `work + hotel_gym`
and `vacation + no_gym` differ in what they PLAN, not in what kind of day they
are, and four day types would have meant four ENUM-EXPANDs and four chances to
miss a consumer.

    purpose  work | vacation | hunting
    lodging  hotel_gym | no_gym          (irrelevant for hunting)

**Nothing here writes a plan row.** `policy_for()` answers "what does this day
get", and the seeder/builder asks it. Keeping the policy separate from the write
is what lets the dry run show a trip without touching anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

DAY_TYPE = "away"

WORK, VACATION, HUNTING = "work", "vacation", "hunting"
HOTEL_GYM, NO_GYM = "hotel_gym", "no_gym"

PURPOSES = (WORK, VACATION, HUNTING)
LODGINGS = (HOTEL_GYM, NO_GYM)

#: Defaults per the spec: a work trip is usually a hotel with a gym; a vacation
#: usually is not. Both are overridable on the command.
DEFAULT_LODGING = {WORK: HOTEL_GYM, VACATION: NO_GYM, HUNTING: NO_GYM}

#: The generic hotel gym. DELIBERATELY CONSERVATIVE -- assume only what almost
#: every hotel gym has. Assuming a cable stack and being wrong means he walks to
#: a machine that is not there; assuming dumbbells and a treadmill and being
#: wrong is recoverable in a way the reverse is not. `hotel gym has <items>`
#: replaces this for one stay.
HOTEL_GYM_INVENTORY = {
    "dumbbell": {"mode": "numeric", "step": 5, "min": 5, "max": 50,
                 "note": "assumed: most hotel racks stop at 50 lb"},
    "bench": {"mode": "flat_only", "note": "flat only — no incline assumed"},
    "treadmill": {"mode": "present"},
    "bike": {"mode": "present", "note": "upright"},
}


@dataclass
class Stay:
    """One away period."""
    start: date
    end: date
    purpose: str
    lodging: str
    #: Packed kit ("trx", "bands") and a hotel's real inventory when told.
    attrs: dict = field(default_factory=dict)
    reason: str | None = None

    @property
    def kit(self) -> set:
        return set(self.attrs.get("kit") or [])

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass
class DayPolicy:
    """What an away day gets. `strength`/`cardio` are session_type-ish labels the
    builder resolves; None means "nothing scheduled", which for hunting is the
    ANSWER rather than a gap."""
    purpose: str
    lodging: str
    strength: str | None
    cardio: str | None
    progression: bool
    intervals: bool
    #: Never counts as missed for MAKEUP-2 / REPEAT-WEEK.
    counts_as_missed: bool = False
    #: Meals: no pre-fill, logging only.
    meal_kind: str = "travel"
    mobility_suggested: bool = True
    note: str = ""


def policy_for(stay: Stay) -> DayPolicy:
    """What this stay plans, per purpose x lodging.

    `counts_as_missed` is False for every away day without exception. A trip is
    not a lapse, and letting one feed MAKEUP-2 or REPEAT-WEEK would hold the
    program back a week for going to a conference.
    """
    p, l = stay.purpose, stay.lodging

    if p == HUNTING:
        # Planned rest. Nothing is ever scheduled, so nothing can be missed, and
        # a walk only counts if he logs one -- Artemis does not infer that he
        # walked because he was in a deer stand.
        return DayPolicy(purpose=p, lodging=l, strength=None, cardio=None,
                         progression=False, intervals=False,
                         note="planned rest — walking counts if logged "
                              "(`log walk <min>`)")

    if p == WORK and l == HOTEL_GYM:
        # The program continues: the only thing that changed is the room.
        return DayPolicy(purpose=p, lodging=l, strength="hotel_gym",
                         cardio="treadmill_or_bike", progression=True,
                         intervals=True,
                         note="program continues in the hotel gym")

    if p == WORK and l == NO_GYM:
        return DayPolicy(purpose=p, lodging=l, strength="bodyweight_circuit",
                         cardio="walk_z2", progression=False, intervals=False,
                         note="bodyweight circuit and a brisk walk — "
                              "progression paused")

    # Vacation, either lodging: maintenance, not training.
    return DayPolicy(
        purpose=p, lodging=l,
        strength="hotel_gym" if l == HOTEL_GYM else "bodyweight_circuit",
        cardio="walk_z2", progression=False, intervals=False,
        note="maintenance — 2 short sessions a week, walking counts as cardio")


def load_stays(cur, start: date, end: date) -> list[Stay]:
    """Active away stays overlapping the window. Raises -- callers fail closed.

    FAIL-CLOSED-RESOLVERS: a caller that cannot read the overrides must NOT
    plan the days as if he were home. That is the mistake this whole feature
    exists to prevent.
    """
    cur.execute(
        "SELECT start_date, end_date, purpose, lodging, attrs, reason "
        "FROM acos.cycle_day_overrides "
        "WHERE day_type = %s AND revoked_at IS NULL "
        "  AND start_date <= %s AND end_date >= %s "
        "ORDER BY start_date", (DAY_TYPE, end, start))
    out = []
    for r in cur.fetchall():
        g = (lambda k, i: r[k] if isinstance(r, dict) else r[i])
        attrs = g("attrs", 4)
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = {}
        out.append(Stay(start=g("start_date", 0), end=g("end_date", 1),
                        purpose=g("purpose", 2),
                        lodging=g("lodging", 3) or DEFAULT_LODGING.get(g("purpose", 2), NO_GYM),
                        attrs=attrs or {}, reason=g("reason", 5)))
    return out


def stay_on(stays: list[Stay], d: date) -> Stay | None:
    for s in stays:
        if s.covers(d):
            return s
    return None


def insert(cur, stay: Stay) -> int:
    cur.execute(
        "INSERT INTO acos.cycle_day_overrides "
        "  (start_date, end_date, day_type, purpose, lodging, attrs, reason, set_by) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 'ryan') RETURNING override_id",
        (stay.start, stay.end, DAY_TYPE, stay.purpose, stay.lodging,
         json.dumps(stay.attrs or {}), stay.reason))
    row = cur.fetchone()
    return row["override_id"] if isinstance(row, dict) else row[0]


def cancel(cur, start: date) -> int:
    """Revoke rather than delete: a trip that was planned and called off is part
    of why a week looks the way it does."""
    cur.execute(
        "UPDATE acos.cycle_day_overrides SET revoked_at = now() "
        "WHERE day_type = %s AND start_date = %s AND revoked_at IS NULL",
        (DAY_TYPE, start))
    return cur.rowcount


def set_attr(cur, start: date, key: str, value) -> int:
    """`hotel gym has …` / `packed trx` — per-stay, never global."""
    cur.execute(
        "UPDATE acos.cycle_day_overrides "
        "SET attrs = attrs || %s::jsonb "
        "WHERE day_type = %s AND start_date = %s AND revoked_at IS NULL",
        (json.dumps({key: value}), DAY_TYPE, start))
    return cur.rowcount


# ── Per-date session placement ──────────────────────────────────────────────
#
# An override can carry a SESSION, not only a location. Ryan's 11/19 -> 12/4
# placement names what happens on each day (Strength A on Fri 11/20, the circuit
# on Thu 11/26, rest while hunting), and the block-2 builder has to honour that
# rather than re-deriving the day from the 14-day template.
#
# It lives in `attrs.session` on the same override row, so one date's plan is
# one row: a separate placement table would let a location and a session for the
# same day drift apart, and then nothing could say which was current.

def placements(cur, start: date, end: date) -> dict:
    """{date: session_type} from overrides carrying `attrs.session`.

    Raises like `load_stays` -- a builder that cannot read the placements must
    not fall back to the template, because the template is exactly what the
    placement is there to overrule.
    """
    cur.execute(
        "SELECT start_date, end_date, attrs, location FROM acos.cycle_day_overrides "
        "WHERE revoked_at IS NULL AND attrs ? 'session' "
        "  AND start_date <= %s AND end_date >= %s "
        "ORDER BY created_at", (end, start))
    out: dict = {}
    from datetime import timedelta as _td
    for r in cur.fetchall():
        g = (lambda k, i: r[k] if isinstance(r, dict) else r[i])
        attrs = g("attrs", 2)
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                continue
        session = (attrs or {}).get("session")
        if not session:
            continue
        # The location rides on the SAME row, so one date's plan is one row. A
        # separate placement table would let a location and a session for the
        # same day drift apart, and then nothing could say which was current.
        entry = {"session": session, "location_key": g("location", 3)}
        d, last = g("start_date", 0), g("end_date", 1)
        while d <= last:
            if start <= d <= end:
                out[d] = entry            # later rows win, hence ORDER BY created_at
            d += _td(days=1)
    return out
