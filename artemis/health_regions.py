"""Body regions for check-in-driven adjustments (FRIDAY-1, PAIN-1).

The ONE place that knows which muscles an exercise uses. The adjustment rules in
artemis.health_checkin read only from here; nothing is inferred by an LLM.

Regions are coarse on purpose. An exercise lists its PRIMARY regions (the muscles
it trains) and SECONDARY regions (the ones it loads or holds). The family tags
work by inclusion: every leg exercise carries "legs" *and* its specific muscle,
so "legs sore 4" catches them all while "hamstrings sore 4" only catches the
hamstring work. "back" means upper/mid back; "low back" is its own region.
"""

# Canonical vocabulary accepted in a check-in.
REGIONS = (
    "shoulder", "chest", "back", "low back", "arms", "biceps", "triceps",
    "legs", "quads", "hamstrings", "calves", "core", "knee", "hip", "neck",
)

# Spoken variants -> canonical region. Multi-word keys are matched first.
ALIASES = {
    "lower back": "low back", "low back": "low back", "lumbar": "low back",
    "upper back": "back", "mid back": "back", "lats": "back", "back": "back",
    "shoulders": "shoulder", "shoulder": "shoulder", "delts": "shoulder",
    "delt": "shoulder", "traps": "shoulder", "rotator cuff": "shoulder",
    "chest": "chest", "pecs": "chest", "pec": "chest",
    "arms": "arms", "arm": "arms", "forearms": "arms", "forearm": "arms",
    "biceps": "biceps", "bicep": "biceps",
    "triceps": "triceps", "tricep": "triceps",
    "legs": "legs", "leg": "legs", "thighs": "legs", "lower body": "legs",
    "quads": "quads", "quad": "quads", "quadriceps": "quads",
    "hamstrings": "hamstrings", "hamstring": "hamstrings", "hammies": "hamstrings",
    "calves": "calves", "calf": "calves",
    "core": "core", "abs": "core", "obliques": "core", "stomach": "core",
    "knees": "knee", "knee": "knee",
    "hips": "hip", "hip": "hip", "glutes": "hip", "glute": "hip",
    "neck": "neck",
}

# A region in this set also implies the family members listed (and vice versa
# via the exercise tags below), so "arms" soreness reaches triceps work.
FAMILIES = {
    "legs": {"quads", "hamstrings", "calves"},
    "arms": {"biceps", "triceps"},
}

LOWER_BODY = {"legs", "quads", "hamstrings", "calves", "knee", "hip", "low back"}

# exercise name (as seeded in health_office) -> (primary, secondary)
EXERCISE_REGIONS: dict[str, tuple[frozenset, frozenset]] = {}


def _reg(name: str, primary: set, secondary: set = frozenset()) -> None:
    EXERCISE_REGIONS[name] = (frozenset(primary), frozenset(secondary))


# ── Office Strength A ──
_reg("Leg press", {"legs", "quads"}, {"hip", "knee"})
_reg("DB bench press", {"chest"}, {"shoulder", "triceps", "arms"})
_reg("Lat pulldown", {"back"}, {"biceps", "arms", "shoulder"})
_reg("Seated leg curl", {"legs", "hamstrings"}, {"knee"})
_reg("Cable face pull (rope)", {"shoulder"}, {"back"})
_reg("Captain's chair knee raise", {"core"}, {"hip"})
# ── Office Strength B ──
# The goblet squat's dumbbell is held at the chest: shoulders and low back work.
_reg("DB goblet squat", {"legs", "quads"}, {"shoulder", "low back", "hip", "knee"})
_reg("Seated cable row", {"back"}, {"shoulder", "biceps"})
_reg("Incline DB press", {"chest", "shoulder", "triceps"})
_reg("Leg extension", {"legs", "quads"}, {"knee"})
_reg("Rear delt fly", {"shoulder"}, {"back"})
_reg("Cable Pallof press", {"core"})
_reg("Seated back extension", {"low back"}, {"hamstrings", "hip"})
# ── Office Strength C ──
_reg("DB Romanian deadlift", {"legs", "hamstrings", "low back"}, {"hip", "back"})
_reg("Pec fly", {"chest"}, {"shoulder"})
_reg("Single-arm cable row", {"back"}, {"shoulder", "biceps", "arms", "core"})
_reg("Seated DB shoulder press", {"shoulder"}, {"triceps", "arms"})
_reg("Calf press", {"legs", "calves"})
_reg("Ab machine crunch", {"core"})

# ── LOCATION-1 (Richfield): each mirrors the office exercise it stands in for,
# because the pain ladder reasons about REGIONS, not about equipment. A
# shoulder that rules out Pec fly rules out DB fly for the same reason.
_reg("DB fly", {"chest"}, {"shoulder"})                       # ← Pec fly
_reg("1-arm DB row", {"back"}, {"shoulder", "biceps", "arms", "core"})   # ← Single-arm cable row
_reg("Standing DB calf raise", {"legs", "calves"})            # ← Calf press
_reg("Stability-ball crunch", {"core"})                       # ← Ab machine crunch
# ── Weeks 5-6 finisher ──
_reg("Stepmill or upright bike", {"legs"}, {"knee", "hip"})
# ── Cardio (steady blocks; rules 2-4 never touch these, listed for coverage) ──
_reg("Zone 2 Cardio", {"legs"}, {"knee", "hip"})
_reg("Recovery Z2 + Mobility", {"legs"}, {"knee", "hip"})
_reg("Treadmill incline walk", {"legs"}, {"calves", "knee", "hip"})
_reg("Elliptical", {"legs"}, {"knee", "hip"})
_reg("Recumbent bike", {"legs"}, {"knee"})
_reg("Upright bike", {"legs"}, {"knee", "hip"})
_reg("Stepmill", {"legs"}, {"calves", "knee", "hip"})
# ── Walk / mobility ──
_reg("Walk", {"legs"}, {"calves", "knee", "hip"})
_reg("Recovery Walk", {"legs"}, {"calves", "knee", "hip"})
_reg("Rest / Mobility", set())

# Substitutes, in preference order. Every name must exist in EXERCISE_REGIONS
# and in health_office's exercise lists (so it can be built for any week).
def substitution_pool(location_key: str | None = None) -> tuple[str, ...]:
    """LOCATION-1: the pool the pain ladder may pick a replacement from,
    FILTERED TO THE LOCATION'S INVENTORY.

    Resolution is location first, then pain: a pain removal at Richfield must
    be replaced by something Richfield HAS. Filtering by equipment CLASS is not
    enough — the office and Richfield both have `bodyweight`, but only one has
    a captain's chair and only one has a stability ball. So the pools are
    explicit per location, like the substitution tables.
    """
    if not location_key or location_key == "office":
        return SUBSTITUTION_POOL
    return POOL_BY_LOCATION.get(location_key, ())


SUBSTITUTION_POOL = (
    "Leg press",
    "Seated leg curl",
    "Leg extension",
    "Calf press",
    "Captain's chair knee raise",
    "Seated back extension",
    "Cable Pallof press",
)


#: What the pain ladder may reach for at each non-office location. Explicit,
#: like the substitution tables — Brown Deer and MSP home have no inventory on
#: record, so they have no pool and a pain removal there becomes mobility.
POOL_BY_LOCATION: dict[str, tuple[str, ...]] = {
    "richfield": ("DB fly", "1-arm DB row", "Standing DB calf raise",
                  "Stability-ball crunch"),
}


# ── PAIN-1: region -> mobility work (Stretch Trainer + mat) ──
# Used when pain 3 replaces a region's exercises with a mobility block.
MOBILITY = {
    "shoulder": "shoulder CARs, band pull-aparts, Stretch Trainer shoulder/chest opener",
    "chest": "doorway pec stretch, Stretch Trainer chest opener",
    "back": "cat-cow, thread-the-needle, Stretch Trainer lat stretch",
    "low back": "cat-cow, child's pose, supine knee-to-chest",
    "arms": "wrist/forearm stretches, Stretch Trainer triceps stretch",
    "biceps": "wall biceps stretch, wrist/forearm stretches",
    "triceps": "overhead triceps stretch, Stretch Trainer triceps stretch",
    "legs": "Stretch Trainer hamstring/quad/calf series",
    "quads": "half-kneeling quad stretch, Stretch Trainer quad stretch",
    "hamstrings": "Stretch Trainer hamstring stretch, supine hamstring floss",
    "calves": "Stretch Trainer calf stretch, ankle circles",
    "core": "cat-cow, supine twist, child's pose",
    "knee": "heel slides, Stretch Trainer quad/hamstring (pain-free range)",
    "hip": "90/90 hip switches, figure-4 stretch, Stretch Trainer hip series",
    "neck": "chin tucks, gentle neck side-bends, upper-trap stretch",
}
MOBILITY_EQUIPMENT = ["Stretch Trainer", "mat"]


def mobility_minutes(regions) -> int:
    """10 min for one region, 15 for several."""
    return 10 if len(list(regions)) <= 1 else 15


# ── PAIN-1: reachable loads (a port of gym-display src/lib/equipment.ts) ──
PLATES_PER_SIDE = (45, 35, 25, 10, 5)
OLYMPIC_BAR_LBS = 45
SMITH_BAR_LBS = 0          # TODO(office): Icarian Smith effective bar weight
DB_MIN, DB_MAX, DB_STEP = 5, 45, 5
STACK_STEP = 10            # machines + functional trainer, until measured

_EXACT_CLASS = {"seated cable row": "machine"}
_CLASS_RULES = (
    ("smith", ("smith",)),
    ("bodyweight", ("captain's chair", "captains chair", "plank",
                    "push-up", "pushup", "dead bug", "bird dog", "hollow",
                    "mountain climber", "glute bridge")),
    ("cable", ("cable", "rope", "pallof", "face pull")),
    ("dumbbell", ("db ", "dumbbell", "goblet")),
    ("barbell", ("barbell", "back squat", "front squat")),
    ("machine", ("leg press", "pulldown", "row", "leg curl", "leg extension", "pec fly",
                 "rear delt", "calf press", "ab crunch", "ab machine", "back extension")),
)


#: Classes that carry no numeric load — there is nothing to step or lighten.
NO_LOAD_CLASSES = ("bodyweight", "bands", "trx", "cardio")


def equipment_class(name: str, explicit: str | None = None) -> str:
    """The exercise's class. EXERCISE-CLASS (2026-09-23): the ROW's
    `equipment_class` wins; the keyword rules below are a fallback for rows
    seeded before the class travelled, and they only know the office's
    vocabulary — "TRX row" reads as `machine` to them."""
    if explicit:
        return explicit
    n = (name or "").lower().strip()
    if n in _EXACT_CLASS:
        return _EXACT_CLASS[n]
    for cls, keys in _CLASS_RULES:
        if any(k in n for k in keys):
            return cls
    return "dumbbell"


def _reachable_totals(bar: int, plates: tuple = PLATES_PER_SIDE) -> list[int]:
    sums = {0}
    for p in plates:
        sums |= {s + p for s in sums}
    return sorted(bar + 2 * s for s in sums)


def lighter_load(name: str, last: float, explicit_class: str | None = None,
                 load_config: dict | None = None) -> float | None:
    """80% of `last`, rounded DOWN to a load the office can actually make.

    Never returns `last` or more: if the rounding lands there, the next lower
    reachable load is used; at the bottom of the range the minimum stays.
    None for bodyweight work (no load to lighten).
    """
    cls = equipment_class(name, explicit=explicit_class)
    # bands / TRX / cardio carry no numeric load, so there is nothing to lighten.
    if cls in NO_LOAD_CLASSES or last is None or last <= 0:
        return None
    goal = float(last) * 0.8
    # LOCATION-1: the row's own config wins. Without it this is the office,
    # which is what every row seeded before LOCATION-1 means.
    cfg = (load_config or {}).get(cls) if load_config else None
    if cfg and cfg.get("mode") == "none":
        return None
    if cfg:
        if cfg.get("plates"):
            options = [t for t in _reachable_totals(cfg.get("bar", 0), tuple(cfg["plates"]))
                       if t > 0]
        else:
            step = cfg.get("step") or 5
            lo = cfg.get("min") or step
            hi = max(int(last), int(cfg.get("max") or last))
            options = [lo + i * step for i in range(int((hi - lo) // step) + 1)]
    elif cls == "dumbbell":
        options = list(range(DB_MIN, DB_MAX + 1, DB_STEP))
    elif cls in ("machine", "cable"):
        top = int(max(last, STACK_STEP))
        options = list(range(STACK_STEP, top + STACK_STEP, STACK_STEP))
    else:
        options = [t for t in _reachable_totals(OLYMPIC_BAR_LBS if cls == "barbell" else SMITH_BAR_LBS)
                   if t > 0]
    below = [o for o in options if o <= goal and o < last]
    if below:
        return float(below[-1])
    return float(options[0])


def canonical_region(word: str) -> str | None:
    return ALIASES.get((word or "").strip().lower())


def expand(regions) -> set:
    """A region plus its family members (legs -> quads/hamstrings/calves)."""
    out = set()
    for r in regions:
        out.add(r)
        out |= FAMILIES.get(r, set())
    return out


def regions_for(exercise: str) -> tuple[frozenset, frozenset]:
    """(primary, secondary) for an exercise; unknown exercises -> empty sets."""
    return EXERCISE_REGIONS.get(exercise, (frozenset(), frozenset()))


# ── Sides (soreness / pain "right knee") ─────────────────────────────────────
# A check-in region carries a side: left, right or unspecified. A sided region
# narrows the match only for an exercise that loads ONE side; every office
# exercise is bilateral or worked on both sides, so none are listed and a
# sided region counts as the whole region for all of them.
SIDES = ("left", "right")
UNSPECIFIED = "unspecified"
EXERCISE_SIDE: dict[str, str] = {}


def side_applies(exercise: str, side: str | None) -> bool:
    ex_side = EXERCISE_SIDE.get(exercise)
    return side not in SIDES or ex_side is None or ex_side == side


def side_key(region: str, side: str | None) -> str:
    """"right knee" for a sided region, "knee" otherwise — the pattern key."""
    return f"{side} {region}" if side in SIDES else region


def split_side_key(key: str) -> tuple[str, str]:
    """"right knee" -> ("knee", "right"); "knee" -> ("knee", "unspecified")."""
    for side in SIDES:
        if key.startswith(side + " "):
            return key[len(side) + 1:], side
    return key, UNSPECIFIED


def uses_any(exercise: str, regions, *, primary_only: bool = False,
             sides: dict | None = None) -> bool:
    """True when the exercise uses any of `regions` (family-expanded).
    `sides` {region: side} drops a sided region for an exercise that loads only
    the other side."""
    if sides:
        regions = [r for r in regions if side_applies(exercise, sides.get(r))]
    primary, secondary = regions_for(exercise)
    tags = primary if primary_only else (primary | secondary)
    return bool(tags & expand(regions))
