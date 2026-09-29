"""PROGRAM-2 — heart-rate zones. ONE definition, shared by every consumer.

These are ESTIMATES and the module says so in the data, not only in a comment:
every row carries `source`, so a screen showing a range can say where it came
from. ZONE-1 will replace the estimate with measured values, and when it does it
replaces THIS module's numbers — not a second copy somewhere else.

The estimate: **Tanaka** (208 − 0.7 × age) at age 49 → HRmax 174.
  * Z2 = 60–70 % of HRmax → 105–122 bpm
  * Z4 = 80–90 % of HRmax → 139–157 bpm

Why Tanaka rather than 220 − age: 220 − age overestimates HRmax for adults over
about 40, and an overestimated max makes every derived zone too high — which for
a Zone 2 session means training above the intensity the session exists for.
"""

from __future__ import annotations

#: The estimate every range below is derived from.
AGE = 49
HR_MAX = 174
SOURCE = "estimate, Tanaka HRmax 174, age 49; ZONE-1 replaces"

#: The %HRmax band each zone occupies. ONE scheme for all five, and the one the
#: approved Z2 (60–70 %) and Z4 (80–90 %) already sat in — Z1, Z3 and Z5 fill the
#: gaps rather than introducing a second model.
PERCENTS: dict[str, tuple[float, float]] = {
    "Z1": (0.50, 0.60),
    "Z2": (0.60, 0.70),
    "Z3": (0.70, 0.80),
    "Z4": (0.80, 0.90),
    "Z5": (0.90, 1.00),
}

#: bpm ranges, inclusive. Keyed by the zone name a session card shows.
#:
#: Z2 AND Z4 ARE RYAN'S APPROVED NUMBERS, NOT A FORMULA'S. Applying `round()` to
#: the percentages above gives Z2 = 104–122, one bpm below the approved 105;
#: every other bound matches. Rather than quietly restate his numbers as a
#: derivation that does not quite reproduce them, the approved pair is written
#: out and the other three are derived — and a test asserts the derivation stays
#: within 1 bpm of the approved pair, so changing HR_MAX surfaces the difference
#: instead of silently moving a number he signed off.
ZONES: dict[str, tuple[int, int]] = {
    "Z1": (round(PERCENTS["Z1"][0] * HR_MAX), round(PERCENTS["Z1"][1] * HR_MAX)),
    "Z2": (105, 122),
    "Z3": (round(PERCENTS["Z3"][0] * HR_MAX), round(PERCENTS["Z3"][1] * HR_MAX)),
    "Z4": (139, 157),
    "Z5": (round(PERCENTS["Z5"][0] * HR_MAX), round(PERCENTS["Z5"][1] * HR_MAX)),
}

#: Zones from hardest to easiest, for classification.
_DESCENDING = ("Z5", "Z4", "Z3", "Z2", "Z1")


def classify_bpm(bpm) -> str | None:
    """Which zone a single reading falls in, or None below Z1.

    The published ranges OVERLAP at their edges — Z2 ends at 122 and Z3 starts at
    122 — because each is rounded independently for display. Classification
    therefore uses the LOW bound of each zone, descending: a reading belongs to
    the hardest zone it reaches. Without a rule like this, 122 bpm would be
    counted in two zones and the minutes would not sum to the session.
    """
    try:
        value = int(bpm)
    except (TypeError, ValueError):
        return None
    for zone in _DESCENDING:
        if value >= ZONES[zone][0]:
            return zone
    return None


def zone_range(zone: str) -> tuple[int, int] | None:
    """(low, high) bpm, or None for a zone with no recorded range.

    None is the explicit unknown: a caller renders "not set" rather than a
    guessed range, for the same reason an absent warmup renders as unknown.
    """
    return ZONES.get(zone)


def zone_block(zone: str) -> dict | None:
    """What a plan row carries, so the iPad and the box read one answer.

    Shape::

        {"zone": "Z2", "low_bpm": 105, "high_bpm": 122, "source": "estimate, …"}
    """
    rng = zone_range(zone)
    if rng is None:
        return None
    return {"zone": zone, "low_bpm": rng[0], "high_bpm": rng[1], "source": SOURCE}


def describe(zone: str) -> str:
    """One human line for a card or a post."""
    rng = zone_range(zone)
    if rng is None:
        return f"{zone} — no range recorded"
    return f"{zone} {rng[0]}–{rng[1]} bpm"


# ── ZONE-0: minutes in each zone, from watch samples ────────────────────────
#
# WATCH-1 records heart rate continuously, so a cardio session's samples already
# exist even though no logged session carries an `hr_avg`. This turns those
# samples into minutes per zone.
#
# It is deliberately dumb about time: each sample is credited with the seconds
# until the NEXT sample, capped. It does not interpolate across a gap, and it
# does not spread the window evenly over however many samples happen to exist.
# A watch that stopped recording for ten minutes should produce ten uncounted
# minutes, not ten minutes attributed to whatever zone the last reading was in.

#: A sample is credited with at most this many seconds. Beyond it the watch was
#: not measuring, and the time is reported as unaccounted rather than assigned.
MAX_SAMPLE_GAP_SEC = 120

#: Below this many samples per minute of window, the answer is "insufficient",
#: never a number. One a minute is the floor the handoff set.
MIN_SAMPLES_PER_MIN = 1.0


def minutes_by_zone(samples, window_start, window_end) -> dict:
    """Minutes in each zone for one session window.

    `samples` is an iterable of (measured_at, bpm); order does not matter.

    Returns::

        {"status": "ok" | "insufficient_hr_data" | "no_samples" | "bad_window",
         "zones": {"Z1": 0, …, "Z5": 0} or None,
         "sample_count": int, "window_sec": int,
         "counted_sec": int, "unaccounted_sec": int}

    FAIL-CLOSED: `zones` is None for every status except "ok". A caller must not
    be able to read a zero out of a session that was never measured — "no
    samples" and "no time in Z4" are different answers.
    """
    window_sec = 0
    if window_start is not None and window_end is not None:
        window_sec = int((window_end - window_start).total_seconds())
    if window_sec <= 0:
        return {"status": "bad_window", "zones": None, "sample_count": 0,
                "window_sec": 0, "counted_sec": 0, "unaccounted_sec": 0}

    inside = sorted(
        (ts, bpm) for ts, bpm in samples
        if ts is not None and window_start <= ts <= window_end and bpm is not None)
    if not inside:
        return {"status": "no_samples", "zones": None, "sample_count": 0,
                "window_sec": window_sec, "counted_sec": 0,
                "unaccounted_sec": window_sec}

    needed = (window_sec / 60.0) * MIN_SAMPLES_PER_MIN
    if len(inside) < needed:
        # Sparse enough that any number would be mostly invention.
        return {"status": "insufficient_hr_data", "zones": None,
                "sample_count": len(inside), "window_sec": window_sec,
                "counted_sec": 0, "unaccounted_sec": window_sec}

    seconds = {z: 0 for z in PERCENTS}
    counted = 0
    for i, (ts, bpm) in enumerate(inside):
        nxt = inside[i + 1][0] if i + 1 < len(inside) else window_end
        gap = int((nxt - ts).total_seconds())
        # A gap longer than the cap is the watch not measuring, not a long hold
        # in that zone. Credit the cap; the rest is unaccounted.
        held = max(0, min(gap, MAX_SAMPLE_GAP_SEC))
        zone = classify_bpm(bpm)
        if zone is not None:
            seconds[zone] += held
        counted += held

    return {"status": "ok",
            "zones": {z: int(round(seconds[z] / 60.0)) for z in PERCENTS},
            "sample_count": len(inside), "window_sec": window_sec,
            "counted_sec": counted, "unaccounted_sec": max(0, window_sec - counted)}
