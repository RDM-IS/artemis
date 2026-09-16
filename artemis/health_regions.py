"""Body regions for check-in-driven adjustments (FRIDAY-1).

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
_reg("45° back extension", {"low back"}, {"hamstrings", "hip"})
# ── Office Strength C ──
_reg("DB Romanian deadlift", {"legs", "hamstrings", "low back"}, {"hip", "back"})
_reg("Pec fly", {"chest"}, {"shoulder"})
_reg("Single-arm cable row", {"back"}, {"shoulder", "biceps", "arms", "core"})
_reg("Seated DB shoulder press", {"shoulder"}, {"triceps", "arms"})
_reg("Calf press", {"legs", "calves"})
_reg("Ab machine crunch", {"core"})
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
SUBSTITUTION_POOL = (
    "Leg press",
    "Seated leg curl",
    "Leg extension",
    "Calf press",
    "Captain's chair knee raise",
    "45° back extension",
    "Cable Pallof press",
)


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


def uses_any(exercise: str, regions, *, primary_only: bool = False) -> bool:
    primary, secondary = regions_for(exercise)
    tags = primary if primary_only else (primary | secondary)
    return bool(tags & expand(regions))
