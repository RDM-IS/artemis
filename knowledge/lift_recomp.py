"""LIFT-RECOMP — the lifting prescription for a calorie deficit. ONE definition.

Ryan, 2026-09-29: *"we don't need (or want?) big lifts, I need to maintain muscle
and shred fat — if that means 30 reps at 50 instead of 5 reps at 100, fine. The
overwhelming ultimate goal is melt fat away."*

So strength's job changed. It is no longer maximal strength; it is **muscle
retention during a deficit, plus calorie burn**. Goal order: **fat loss ≫ muscle
retention > endurance.**

WHAT THAT CHANGES, AND WHAT IT DOES NOT

Hypertrophy is roughly rep-range-indifferent from about 6 to 30 reps *provided
the set ends near failure*, so moving to higher reps at lighter loads keeps the
muscle-retention stimulus while making the session denser and cheaper on the
joints. It does not change the requirement to work hard: the whole effect rests
on **RPE 7–8, meaning 2–3 reps in reserve**. Higher reps taken easy is just time
spent.

Two consequences worth stating because they are the point:

  * **A load cap stops being a stall.** The hotel rack ends at 50 lb and the
    PowerBlocks at 90. Under a 5-rep scheme that is a ceiling you grind into;
    under this one it just means more reps.
  * **Density becomes a goal, not a side effect.** Exercises are paired as
    supersets so heart rate stays up while lifting, which is calorie burn that
    the old 90-second-rest scheme threw away — and it is what brings Strength B
    and C back inside the 60-minute office window they currently exceed (62 and
    67 minutes).

This module is PURE: rep ranges, the RPE band, the pairing rule and the duration
model, with no database and no knowledge of any particular gym. The per-exercise
facts it needs — which class a lift is, what movement pattern it trains, which
station it occupies — are supplied by the caller, because those are location
facts and they live beside the other exercise facts in `artemis/health_office.py`.
"""

from __future__ import annotations

#: The first program week this scheme applies to. Week 5 begins **2026-10-11**
#: (verified against `health.plan`: week 5 = 10-11..10-17). Earlier weeks are
#: already logged or already in front of him, and a reseed must not touch them.
FIRST_WEEK = 5

COMPOUND, ACCESSORY = "compound", "accessory"

#: Rep ranges by lift class (Ryan, 2026-09-29). Compounds sit lower because the
#: systemic cost of 25 reps of a squat pattern is not the same as 25 reps of a
#: rear delt fly, and the accessory ranges are where the cheap extra volume goes.
REP_RANGES: dict[str, tuple[int, int]] = {
    COMPOUND: (12, 20),
    ACCESSORY: (15, 25),
}

#: RPE 7–8 = 2–3 reps in reserve. THE LOAD-BEARING PART OF THE WHOLE SCHEME:
#: high reps only retain muscle if the set finishes near failure. A deload week
#: is allowed below this band; a working week is not.
RPE_RANGE = (7.0, 8.0)

#: Movement patterns, for pairing. `core` pairs with anything; two of the same
#: pattern never pair, which is what "push/pull, upper/lower" means in practice.
PUSH, PULL, LOWER, CORE = "push", "pull", "lower", "core"

#: Patterns that make a good superset. Order-insensitive.
_GOOD_PAIRS = frozenset({
    frozenset({PUSH, PULL}),      # push/pull
    frozenset({PUSH, LOWER}),     # upper/lower
    frozenset({PULL, LOWER}),     # upper/lower
    frozenset({PUSH, CORE}),
    frozenset({PULL, CORE}),
    frozenset({LOWER, CORE}),
})


# ── holds are prescribed in SECONDS, never in reps ──────────────────────────
#
# A side plank has no rep count. "3x15-25" on one is not a slightly odd
# prescription, it is a meaningless one — and worse, it PARSES: the double
# progression reads a range out of the exercise's notes, so "3x30-45s" would have
# been read as 30-45 reps and the load advice computed from it.
#
# So a hold declares its seconds here and the builder emits `format: "duration"`
# with no `target_reps` at all. `progression.rep_range` refuses a duration-format
# exercise outright, which is what its docstring already claimed and nothing
# enforced.
HOLD_SECONDS: dict[str, tuple[int, int]] = {
    # Ryan's number for the hotel table, 2026-09-29.
    "Side plank": (30, 45),
    # The other holds this program can emit, for the same reason: so that adding
    # one to a strength table cannot silently acquire a rep range.
    "Plank": (30, 60),
    "Hollow hold": (20, 40),
    "Ball plank": (30, 45),
}


def is_hold(name: str) -> bool:
    return name in HOLD_SECONDS


def hold_seconds(name: str) -> tuple[int, int]:
    """(low, high) seconds. Raises on a movement that is not a declared hold, so
    a caller cannot ask for seconds and silently receive a default."""
    try:
        return HOLD_SECONDS[name]
    except KeyError:
        raise KeyError(f"{name!r} is not a declared hold; "
                       f"known: {sorted(HOLD_SECONDS)}") from None


def hold_label(name: str) -> str:
    lo, hi = hold_seconds(name)
    return f"{lo}-{hi}s"


def applies(week_num) -> bool:
    """True for a week this scheme governs. Weeks before FIRST_WEEK keep the old
    prescription, which is what makes the reseed safe: nothing before 10/11 moves."""
    try:
        return int(week_num) >= FIRST_WEEK
    except (TypeError, ValueError):
        # An unknown week is NOT assumed to be a new one. Fail closed: a row
        # whose week cannot be read keeps the prescription it already has.
        return False


def reps_for(lift_class: str) -> tuple[int, int]:
    """(low, high) for a class. Raises on an unknown class.

    Raising rather than defaulting, for the reason `health_office.class_for`
    raises: a lift nobody classified is a build error, and a silent default would
    quietly prescribe accessory volume for a squat.
    """
    try:
        return REP_RANGES[lift_class]
    except KeyError:
        raise KeyError(
            f"no rep range for lift class {lift_class!r}; "
            f"known: {sorted(REP_RANGES)}") from None


def rep_label(lift_class: str) -> str:
    lo, hi = reps_for(lift_class)
    return f"{lo}-{hi}"


def top_reps(lift_class: str) -> int:
    return reps_for(lift_class)[1]


def rpe_in_band(rpe) -> bool:
    if rpe is None:
        return False
    return RPE_RANGE[0] <= float(rpe) <= RPE_RANGE[1]


# ── progression ─────────────────────────────────────────────────────────────

def progression_note(lift_class: str) -> str:
    lo, hi = reps_for(lift_class)
    return (f"Add reps first: work up to {hi} on EVERY set, then one load step "
            f"and back to {lo}. Stop 2-3 reps shy of failure (RPE 7-8).")


def ready_for_load_step(set_reps, lift_class: str) -> bool:
    """True when every set hit the TOP of the new range — the load-step trigger.

    Double progression (#20) keeps its load-step logic; only the trigger moves.
    "Every set" is deliberate and unchanged: one set reaching the top while the
    others trail means the load is right and the fatigue is real, not that it is
    time to add weight.
    """
    reps = [r for r in (set_reps or []) if r is not None]
    if not reps:
        return False
    return all(int(r) >= top_reps(lift_class) for r in reps)


# ── density: superset pairing ───────────────────────────────────────────────

def pair_for_density(items, *, station_of, pattern_of):
    """Split `items` into superset pairs and leftover singles.

    `items` is any sequence of exercise identifiers. `station_of(item)` returns
    the station it occupies — the thing there is exactly ONE of and which must
    stay occupied for the whole set — or None when it needs no station.
    `pattern_of(item)` returns one of PUSH / PULL / LOWER / CORE.

    TWO RULES, and the first is a hard constraint rather than a preference:

      1. **Never pair two exercises that need the same station.** A superset
         means leaving the first station between sets; if both movements need the
         one adjustable bench, or the one functional trainer, the "superset" is
         just the same exercise with extra walking. A shared station is also the
         failure a substitution introduces without anyone noticing — a Richfield
         or hotel table can easily put two band movements on one anchor.
      2. Prefer complementary patterns (push/pull, upper/lower). Two pushes back
         to back share the same fatigue, which defeats the point.

    Order is preserved: pairs are formed greedily from the front, so the session
    keeps its intended sequence and the compounds stay early. Returns
    `(pairs, singles)` where pairs is a list of 2-tuples.
    """
    remaining = list(items)
    pairs, singles = [], []
    while remaining:
        first = remaining.pop(0)
        partner_idx = None
        # first choice: a complementary pattern on a different station
        for i, cand in enumerate(remaining):
            if _stations_clash(first, cand, station_of):
                continue
            if frozenset({pattern_of(first), pattern_of(cand)}) in _GOOD_PAIRS:
                partner_idx = i
                break
        if partner_idx is None:
            # second choice: any different station. Same-pattern pairing is worse
            # than complementary but still denser than resting alone, and the
            # station rule is never relaxed to achieve it.
            for i, cand in enumerate(remaining):
                if not _stations_clash(first, cand, station_of):
                    partner_idx = i
                    break
        if partner_idx is None:
            singles.append(first)
        else:
            pairs.append((first, remaining.pop(partner_idx)))
    return pairs, singles


def _stations_clash(a, b, station_of) -> bool:
    sa, sb = station_of(a), station_of(b)
    if sa is None or sb is None:
        return False            # no station to contend for
    return sa == sb


# ── duration ────────────────────────────────────────────────────────────────
#
# The old estimate was `10 + sets x exercises x 2.5` — ONE constant covering
# work, rest, transitions, machine setup and logging together, so nothing could
# be checked against anything and changing the rest interval moved the answer by
# zero. Supersets change the rest structure specifically, which that shape cannot
# express at all.
#
# So the components are named. Each one is either Ryan's instruction or an
# ASSUMPTION marked as such, and `CALIBRATED` records what is actually measured.
# Five logged sessions under the OLD scheme averaged 2.13 min per exercise-set,
# but all five were 2-set sessions, so it is not known to hold for three.

#: Warmup + cooldown allowance. Unchanged: the existing builder already folds
#: both into a flat 10, and this round is not the place to re-open it.
WARMUP_COOLDOWN_MIN = 10

#: Work plus logging for one set. ASSUMED. 12-25 controlled reps is roughly
#: 30-60 s of work, and a set is logged on the iPad before the next one starts.
WORK_SEC_PER_SET = 60

#: One-off cost of arriving at a station: finding it, setting the seat and pin.
#: ASSUMED, per exercise per session, not per set.
SETUP_SEC_PER_EXERCISE = 30

#: Moving between the two stations of a superset. ASSUMED.
INTRA_PAIR_SEC = 15

#: Rest after completing both halves of a pair. RYAN'S INSTRUCTION (~60 s).
REST_BETWEEN_PAIRS_SEC = 60

#: Rest between sets of an exercise that could not be paired. The old scheme's
#: interval, kept: an unpaired exercise has not become denser.
REST_BETWEEN_SETS_SEC = 90

#: What is measured rather than assumed. Deliberately nearly empty — see above.
CALIBRATED: dict[str, str] = {
    "REST_BETWEEN_PAIRS_SEC": "Ryan's instruction, 2026-09-29",
    "REST_BETWEEN_SETS_SEC": "the pre-existing scheme's interval",
}


def session_minutes(*, n_pairs: int, n_singles: int, sets: int,
                    finisher_min: int = 0) -> int:
    """Estimated minutes for a paired strength session.

    A pair costs one rest for TWO exercises; a single costs one rest for one.
    That is the whole arithmetic of the density change, and it is why this takes
    pairs and singles rather than a count of exercises.
    """
    n_exercises = n_pairs * 2 + n_singles
    setup = n_exercises * SETUP_SEC_PER_EXERCISE
    pair_rounds = n_pairs * sets * (
        2 * WORK_SEC_PER_SET + INTRA_PAIR_SEC + REST_BETWEEN_PAIRS_SEC)
    single_rounds = n_singles * sets * (
        WORK_SEC_PER_SET + REST_BETWEEN_SETS_SEC)
    work_min = (setup + pair_rounds + single_rounds) / 60.0
    return int(round(WARMUP_COOLDOWN_MIN + work_min + finisher_min))


def min_per_exercise_set(*, n_pairs: int, n_singles: int, sets: int) -> float:
    """What this model implies per exercise-set, for comparison against the 2.13
    min measured under the old scheme. Reported, not used as an input."""
    n = (n_pairs * 2 + n_singles) * sets
    if not n:
        return 0.0
    return round((session_minutes(n_pairs=n_pairs, n_singles=n_singles, sets=sets)
                  - WARMUP_COOLDOWN_MIN) / n, 2)


#: The office window a lift has to fit inside.
OFFICE_CAP_MIN = 60

#: The density expectation, recorded as an EXPECTATION and not as a target.
#:
#: Ryan's reason for supersets is that heart rate stays up — Z2-Z3 while lifting.
#: That is stated on the row so the intent is visible, but it is deliberately NOT
#: written as `target_hr_zone`: a strength row carrying a cardio target would be
#: a prescription nobody set, and ZONE-0 already measures what the session
#: actually did. The measurement settles it; the row should not pre-empt it.
DENSITY_NOTE = ("Supersets, ~60s between pairs. Expect HR in Z2-Z3 while "
                "lifting — that is the point, not a target to chase.")
