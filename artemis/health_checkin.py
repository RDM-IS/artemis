"""Check-in-driven morning (FRIDAY-1).

The morning is event-driven:

  04:30  wake post (plan-exact) opens today's check-in (`checkin_open:<date>`)
  reply  check-in parsed HERE, deterministically. If no sets are logged yet,
         the adjustment rules run and today's health.plan row is rewritten;
         otherwise the check-in is stored and the reply is just "Logged."
  05:15  training days only: one nudge if no check-in arrived.

No LLM parses the check-in or decides the plan.

Every rating is 0-5 (0 = none, 5 = can't use it): energy, soreness, pain.
Sleep stays in hours. "x/10" is halved and rounded up (8/10 -> 4); "x/5" is
taken as is; a bare number above 5 is refused ("Ratings are 0–5.") and
nothing is stored. "sore 0" is stored as {"overall": 0}. A region named
without a number is stored unscored (null) and changes nothing.

Rules, in order — they never add volume, load or RPE:

  1. Day swap   2+ SORENESS regions at 4-5 -> Recovery Z2 (20-30 min,
                recumbent) + mobility. Pain never swaps the day.
  2. Replace    soreness 4-5 or pain 4-5 in a region -> remove every exercise
                with it as primary or secondary; refill to the same count from
                the pool, avoiding every affected region, no duplicates.
  3. Lighten    soreness 2-3 -> exercises with it as PRIMARY: -1 set (min 1),
                RPE cap -1.
  4. Lighten    pain 1-3 -> exercises with it as PRIMARY: load -20%, plus a
                mobility block for the region.
  5. Recovery   sleep < 6 or energy <= 2 -> RPE cap -1 everywhere (stacks with
                rule 3, floor 1), sets capped at 2, Z2 duration -25%.
  6. Otherwise  no change. High energy or long sleep never adds work.

The adjusted blocks are written to TODAY'S health.plan row with
blocks.original (the untouched blocks + row fields) and blocks.adjustment
({reason, rules_fired, checkin_id, at, ...}). `original` restores them.
config.CHECKIN_ADJUST=0 keeps parsing/storing but never rewrites the plan.
Replies state the plan change only — never advice.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from artemis import config
from artemis import health_regions as hr

logger = logging.getLogger(__name__)

# ============================================================================
# Parsing
# ============================================================================

PAIN_WORDS = ("pain", "painful", "sharp", "injury", "injured", "tweaked", "tweak",
              "strain", "strained", "pulled")
SORE_WORDS = ("sore", "soreness", "tight", "ache", "aching", "achy", "stiff") + PAIN_WORDS

RATINGS_ERROR = "Ratings are 0–5."

_NUM = r"(\d{1,2}(?:\.\d)?)"
_SCALE = r"(\s*(?:/|out\s+of)\s*(?:10|5))?"

_SLEEP_RE = re.compile(
    rf"\b(?:slept|sleep)\s*(?:for|of|was|:)?\s*(?:about|around|~)?\s*{_NUM}\s*(?:h\b|hrs?\b|hours?\b)?"
    rf"|\b{_NUM}\s*(?:h|hrs?|hours?)\s*(?:of\s+)?sleep\b",
    re.I)
_ENERGY_RE = re.compile(rf"\benergy\s*(?:is|was|of|at|:)?\s*{_NUM}{_SCALE}", re.I)
_WEIGHT_RE = re.compile(
    r"\b(?:weight|weighed|wt|scale)\s*(?:is|was|in\s+at|at|of|:)?\s*(\d{2,3}(?:\.\d{1,2})?)\s*(?:lbs?|pounds)?\b"
    r"|\b(\d{3}(?:\.\d{1,2})?)\s*(?:lbs?|pounds)\b",
    re.I)
_RHR_RE = re.compile(r"\b(?:rhr|resting\s*(?:hr|heart\s*rate))\s*(?:is|was|of|:)?\s*(\d{2,3})\b", re.I)
_ZERO_SORE_RE = re.compile(
    r"\b(?:sore(?:ness)?\s*(?:is|:)?\s*(?:0|zero|none)(?:\s*/\s*(?:10|5))?|no\s+soreness|not\s+sore|nothing\s+sore)\b",
    re.I)
_SENTENCE_SPLIT = re.compile(r"[,;\n]+|\.(?!\d)")
_AND_SPLIT = re.compile(r"\band\b|\bplus\b|&", re.I)
_SCORE_RE = re.compile(rf"(?<![\d.]){_NUM}{_SCALE}(?![\d.])", re.I)
_WORD_RE = re.compile(r"[a-z']+")

# Words that sit next to "sore" but are not regions.
_NOT_REGIONS = {
    "sore", "soreness", "is", "was", "a", "bit", "little", "very", "really", "kind", "of",
    "my", "the", "in", "at", "slightly", "some", "super", "pretty", "and", "but", "still",
    "energy", "weight", "slept", "sleep", "hours", "hrs", "feel", "feeling", "today",
    "tight", "ache", "achy", "aching", "stiff", "pain", "painful", "sharp", "injury",
    "injured", "tweaked", "tweak", "strain", "strained", "pulled", "left", "right",
    "rhr", "resting", "hr", "out", "lbs", "lb", "none", "zero", "no", "not", "overall",
    "all", "over", "everywhere", "body", "good", "fine", "ok", "great", "i", "im", "i'm",
    "after", "from", "since", "yesterday", "workout", "session", "lifting", "training",
    "again", "today's", "this", "morning", "last", "night", "day", "days", "with", "for",
    "getting", "got", "feels", "be", "been", "am", "are", "so", "too", "just", "only",
    "kinda", "somewhat", "quite", "less", "more", "than", "better", "worse", "same", "plus",
}


class RatingError(ValueError):
    """A rating outside 0-5."""


@dataclass
class CheckIn:
    sleep_hrs: float | None = None
    energy: int | None = None
    weight_lbs: float | None = None
    resting_hr: int | None = None
    soreness: dict = field(default_factory=dict)      # region -> 0..5 (None = unscored)
    pain: dict = field(default_factory=dict)          # region -> 0..5 (None = unscored)
    unknown_regions: dict = field(default_factory=dict)
    free_text: str | None = None
    rating_error: bool = False

    @property
    def has_data(self) -> bool:
        return any(v is not None for v in (self.sleep_hrs, self.energy, self.weight_lbs,
                                           self.resting_hr)) \
            or bool(self.soreness) or bool(self.pain) or bool(self.unknown_regions)

    def soreness_json(self) -> dict | None:
        """health.daily_state.soreness — every value 0-5 (or null = unscored).
        Pain is kept apart: {"pain": {"shoulder": 2}}."""
        if not (self.soreness or self.pain or self.unknown_regions):
            return None
        out = dict(self.soreness)
        for r, sc in self.unknown_regions.items():
            out[r] = sc
        if self.pain:
            out["pain"] = dict(self.pain)
        return out


def normalize_score(value: float, scale: str | None) -> int:
    """A rating on 0-5. x/10 -> halved, rounded up; x/5 as is. Raises
    RatingError for anything outside 0-5."""
    scale = (scale or "").replace(" ", "").lower()
    if scale.endswith("10"):
        if value < 0 or value > 10:
            raise RatingError(value)
        return int(math.ceil(value / 2))
    if value < 0 or value > 5:
        raise RatingError(value)
    return int(math.ceil(value)) if value != int(value) else int(value)


def _blank(text: str, span: tuple[int, int]) -> str:
    a, b = span
    return text[:a] + " " * (b - a) + text[b:]


def _find_regions(clause: str) -> tuple[list[str], list[str]]:
    """(known canonical regions, unknown region-ish words) in a clause."""
    low = clause.lower()
    found: list[str] = []
    consumed = low
    for alias in sorted(hr.ALIASES, key=len, reverse=True):
        for m in re.finditer(rf"\b{re.escape(alias)}\b", consumed):
            canon = hr.ALIASES[alias]
            if canon not in found:
                found.append(canon)
            consumed = _blank(consumed, m.span())
    unknown: list[str] = []
    # Only a word right next to a soreness/pain word (fillers skipped) can be an
    # unrecognized region: "sore elbow 3", "elbow sore 3".
    words = _WORD_RE.findall(consumed)
    for i, w in enumerate(words):
        if w not in SORE_WORDS:
            continue
        for j in (i - 1, i + 1, i - 2, i + 2):
            if 0 <= j < len(words):
                cand = words[j]
                if cand in SORE_WORDS or cand in _NOT_REGIONS or len(cand) <= 2:
                    continue
                if cand not in unknown:
                    unknown.append(cand)
                break
    return found, unknown


def parse_checkin(text: str) -> CheckIn:
    """Deterministic check-in parser. Never raises; an out-of-range rating sets
    `rating_error` (the caller replies RATINGS_ERROR and stores nothing)."""
    ci = CheckIn()
    work = f" {text or ''} "
    try:
        m = _SLEEP_RE.search(work)
        if m:
            ci.sleep_hrs = float(m.group(1) or m.group(2))
            work = _blank(work, m.span())
        m = _ENERGY_RE.search(work)
        if m:
            ci.energy = normalize_score(float(m.group(1)), m.group(2))
            work = _blank(work, m.span())
        m = _RHR_RE.search(work)
        if m:
            ci.resting_hr = int(m.group(1))
            work = _blank(work, m.span())
        m = _WEIGHT_RE.search(work)
        if m:
            ci.weight_lbs = float(m.group(1) or m.group(2))
            work = _blank(work, m.span())

        zero = _ZERO_SORE_RE.search(work)
        if zero:
            work = _blank(work, zero.span())

        if any(re.search(rf"\b{w}\b", work, re.I) for w in SORE_WORDS):
            # Sentences (comma/semicolon/newline/period) are independent; inside
            # one, "and"-joined parts share the next score: "shoulder and neck
            # sore 3". A pain word marks only the regions in its own part.
            for sentence in _SENTENCE_SPLIT.split(work):
                pending: list[tuple[str, bool]] = []
                pending_unknown: list[str] = []
                for part in _AND_SPLIT.split(sentence):
                    if not part.strip():
                        continue
                    regions, unknown = _find_regions(part)
                    is_pain = any(re.search(rf"\b{w}\b", part, re.I) for w in PAIN_WORDS)
                    sm = _SCORE_RE.search(part)
                    score = normalize_score(float(sm.group(1)), sm.group(2)) if sm else None
                    pending += [(r, is_pain) for r in regions]
                    pending_unknown += unknown
                    if score is None:
                        continue
                    if pending or pending_unknown:
                        for r, pain in pending:
                            target = ci.pain if pain else ci.soreness
                            target[r] = max(score, target.get(r) or 0)
                        for u in pending_unknown:
                            ci.unknown_regions[u] = score
                        pending, pending_unknown = [], []
                    else:
                        ci.soreness.setdefault("overall", score)
                # Named with no number: stored unscored, changes nothing.
                for r, pain in pending:
                    (ci.pain if pain else ci.soreness).setdefault(r, None)
                for u in pending_unknown:
                    ci.unknown_regions.setdefault(u, None)
    except RatingError:
        return CheckIn(rating_error=True)

    if zero and not ci.soreness:
        ci.soreness = {"overall": 0}

    leftover = " ".join(work.split())
    ci.free_text = leftover or None
    return ci


# ============================================================================
# Plan helpers
# ============================================================================

STRENGTH_TYPES = ("strength_a", "strength_b", "strength_c")
LIGHT_TYPES = ("rest_mobility", "walk")
_SESSION_LETTER = {"strength_a": "A", "strength_b": "B", "strength_c": "C"}


def coerce_blocks(blocks) -> dict:
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except (ValueError, TypeError):
            return {}
    return blocks if isinstance(blocks, dict) else {}


def session_label(session_type: str, blocks: dict | None = None) -> str:
    if session_type in _SESSION_LETTER:
        return f"Session {_SESSION_LETTER[session_type]}"
    b = blocks or {}
    return b.get("display_name") or session_type.replace("_", " ").title()


_NOTE_SETS_RE = re.compile(r"^\s*(\d+)\s*×\s*([^;]+?)\s*(;|$)")


def _rep_range(ex: dict) -> str:
    m = _NOTE_SETS_RE.match(ex.get("notes") or "")
    if m:
        return m.group(2)
    if ex.get("target_reps"):
        return str(ex["target_reps"])
    return ""


def exercise_sets(ex: dict, blocks: dict) -> int:
    if ex.get("sets") is not None:
        return int(ex["sets"])
    return int(blocks.get("rounds") or 1)


def _set_note_sets(ex: dict, sets: int) -> None:
    notes = ex.get("notes") or ""
    m = _NOTE_SETS_RE.match(notes)
    if m:
        ex["notes"] = f"{sets}×{notes[m.start(2):]}"


def _office_exercise(name: str, sets: int, week_num: int) -> dict:
    """Build a pool exercise exactly as health_office seeds it for this week."""
    from artemis import health_office as office
    for spec in (x for lst in office._EXERCISES.values() for x in lst):
        if spec[0] == name:
            ex = office._exercise(*spec, sets, week_num)
            ex["notes"] = re.sub(r"^\d+×", f"{sets}×", ex["notes"])
            return ex
    raise KeyError(f"{name} is not an office exercise")


# ============================================================================
# Rules
# ============================================================================

@dataclass
class Adjustment:
    changed: bool
    blocks: dict
    session_type: str
    target_rpe: float | None
    est_duration_min: int | None
    rules_fired: list = field(default_factory=list)
    lines: list = field(default_factory=list)      # human, plan-exact diff lines
    removed: list = field(default_factory=list)
    added: list = field(default_factory=list)
    eased: list = field(default_factory=list)
    reason: str = ""


def _fmt(scores: dict, regions, word: str = "") -> str:
    w = f" {word}" if word else ""
    return " + ".join(f"{r.title()}{w} {scores[r]}/5" for r in regions)


def _cap(value, base):
    if value is None:
        return base
    return min(value, base)


def _scored(d: dict) -> dict:
    return {r: v for r, v in d.items() if r in hr.REGIONS and isinstance(v, int)}


def compute_adjustment(plan: dict, ci: CheckIn) -> Adjustment:
    """Pure: today's plan row + a parsed check-in -> the adjusted plan.

    `plan` needs session_type, blocks, target_rpe, est_duration_min, week_num.
    Blocks are taken from blocks.original when present, so a second check-in
    recomputes from the plan as written instead of stacking.
    """
    live = coerce_blocks(plan.get("blocks"))
    orig_row = (live.get("original") or {}) if isinstance(live.get("original"), dict) else {}
    base = copy.deepcopy(orig_row.get("blocks") or {k: v for k, v in live.items()
                                                      if k not in ("original", "adjustment")})
    session_type = orig_row.get("session_type") or plan.get("session_type")
    base_rpe = orig_row.get("target_rpe", plan.get("target_rpe"))
    base_rpe = float(base_rpe) if base_rpe is not None else None
    base_dur = orig_row.get("est_duration_min", plan.get("est_duration_min"))
    week_num = int(plan.get("week_num") or 1)

    adj = Adjustment(changed=False, blocks=copy.deepcopy(base), session_type=session_type,
                     target_rpe=base_rpe, est_duration_min=base_dur)
    if session_type in LIGHT_TYPES:
        return adj

    sore = _scored(ci.soreness)
    pain = _scored(ci.pain)
    # Regions keep the order Ryan wrote them in.
    sore_heavy = [r for r, v in sore.items() if v >= 4]
    pain_heavy = [r for r, v in pain.items() if v >= 4]
    sore_mid = [r for r, v in sore.items() if 2 <= v <= 3]
    pain_mid = [r for r, v in pain.items() if 1 <= v <= 3]
    replace_regions = sore_heavy + [r for r in pain_heavy if r not in sore_heavy]
    # Every region with something going on — substitutes must avoid them all.
    affected = set(replace_regions) | set(sore_mid) | set(pain_mid)
    recovery = (ci.sleep_hrs is not None and ci.sleep_hrs < 6) or \
               (ci.energy is not None and ci.energy <= 2)
    blocks = adj.blocks

    # ── Rule 1: day swap — soreness only ────────────────────────────────────
    if len(sore_heavy) >= 2:
        from artemis import health_office as office
        z2_hi = office.RAMP.get(week_num, office.RAMP[1])[2][1]
        minutes = max(20, min(30, z2_hi))
        adj.rules_fired.append("day_swap")
        adj.session_type = "cardio_z2"
        adj.target_rpe = 4.0
        adj.blocks = blocks = {
            "type": "steady",
            "display_name": "Recovery Z2 + Mobility",
            "location": base.get("location") or "office gym",
            "duration_min": minutes,
            "intensity": "Zone 2",
            "equipment": ["recumbent bike"],
            "setup_notes": ["recumbent bike (lowest impact) — conversational pace",
                            "then 10 min mobility (mat or Stretch Trainer)"],
            "mobility_focus": list(sore_heavy),
            "mobility_min": 10,
        }
        adj.est_duration_min = minutes + 10
        adj.lines.append(
            f"{_fmt(sore, sore_heavy)} → today is Recovery Z2 + Mobility: "
            f"{minutes} min recumbent bike, then 10 min mobility.")

    exercises = blocks.get("exercises") or []
    swapped = "day_swap" in adj.rules_fired

    # ── Rule 2: replace (soreness 4-5 or pain 4-5) ──────────────────────────
    if replace_regions and not swapped and exercises:
        present = {e["name"] for e in exercises}
        pool = [x for x in hr.SUBSTITUTION_POOL if not hr.uses_any(x, affected)]
        out_list, removed, added = [], [], []
        for ex in exercises:
            if hr.uses_any(ex["name"], replace_regions):
                removed.append(ex["name"])
                sub = next((x for x in pool if x not in present), None)
                if sub is not None:
                    present.add(sub)
                    new = _office_exercise(sub, exercise_sets(ex, blocks), week_num)
                    new["added_by"] = "checkin"
                    new["replaces"] = ex["name"]
                    out_list.append(new)
                    added.append(sub)
            else:
                out_list.append(ex)
        if removed:
            blocks["exercises"] = exercises = out_list
            blocks["equipment"] = _equipment_for(exercises, blocks.get("equipment") or [])
            adj.rules_fired.append("replace")
            adj.removed, adj.added = removed, added
            who = " + ".join(filter(None, [_fmt(sore, sore_heavy), _fmt(pain, pain_heavy, "pain")]))
            line = f"{who} → removed {_join(removed)}."
            if added:
                line += f" Added {_join(added)}."
            if len(added) < len(removed):
                n = len(removed) - len(added)
                line += f" No substitute left for {n} slot{'s' if n != 1 else ''}."
            adj.lines.append(line)
        fin = blocks.get("finisher")
        if isinstance(fin, dict) and any(hr.uses_any(e.get("name", ""), replace_regions)
                                         for e in fin.get("exercises") or []):
            blocks.pop("finisher")
            adj.rules_fired.append("drop_finisher")
            adj.lines.append("Conditioning finisher removed.")

    # ── Rule 3: lighten, soreness 2-3 (primary) ─────────────────────────────
    if sore_mid and exercises and not swapped:
        names = []
        for ex in exercises:
            if ex.get("added_by") != "checkin" and hr.uses_any(ex["name"], sore_mid, primary_only=True):
                s_ = max(1, exercise_sets(ex, blocks) - 1)
                ex["sets"] = s_
                _set_note_sets(ex, s_)
                if base_rpe is not None:
                    ex["rpe_cap"] = _cap(ex.get("rpe_cap"), base_rpe) - 1
                names.append(ex["name"])
        if names:
            adj.rules_fired.append("lighten_sore")
            adj.eased += names
            first = _by_name(exercises, names[0])
            n_sets = exercise_sets(first, blocks)
            cap = f", RPE ≤{_n(first['rpe_cap'])}" if first.get("rpe_cap") is not None else ""
            adj.lines.append(f"{_fmt(sore, sore_mid)} → {_join(names)}: "
                             f"{n_sets} set{'s' if n_sets != 1 else ''}{cap}.")

    # ── Rule 4: lighten, pain 1-3 (primary): load −20% + mobility ───────────
    if pain_mid and not swapped:
        names = []
        for ex in exercises:
            if ex.get("added_by") != "checkin" and hr.uses_any(ex["name"], pain_mid, primary_only=True):
                ex["load_pct"] = 80
                names.append(ex["name"])
                if ex["name"] not in adj.eased:
                    adj.eased.append(ex["name"])
        blocks["mobility_focus"] = sorted(set(blocks.get("mobility_focus") or []) | set(pain_mid))
        blocks["mobility_min"] = 5 * len(blocks["mobility_focus"])
        adj.rules_fired.append("lighten_pain")
        what = f"{_join(names)}: load −20%. " if names else ""
        adj.lines.append(f"{_fmt(pain, pain_mid, 'pain')} → {what}"
                         f"Added {blocks['mobility_min']} min {', '.join(blocks['mobility_focus'])} mobility.")

    # ── Rule 5: global recovery ─────────────────────────────────────────────
    if recovery:
        adj.rules_fired.append("recovery")
        why = []
        if ci.sleep_hrs is not None and ci.sleep_hrs < 6:
            why.append(f"sleep {_n(ci.sleep_hrs)}h")
        if ci.energy is not None and ci.energy <= 2:
            why.append(f"energy {ci.energy}/5")
        what = []
        if blocks.get("exercises"):
            for ex in blocks["exercises"]:
                if base_rpe is not None:
                    ex["rpe_cap"] = _cap(ex.get("rpe_cap"), base_rpe) - 1
                if exercise_sets(ex, blocks) > 2:
                    ex["sets"] = 2
                    _set_note_sets(ex, 2)
            if base_rpe is not None:
                what.append(f"RPE ≤{_n(base_rpe - 1)} on every exercise")
            if int(blocks.get("rounds") or 1) > 2:
                what.append("sets capped at 2")
        if blocks.get("type") == "steady" and blocks.get("duration_min"):
            before = int(blocks["duration_min"])
            after = max(1, int(round(before * 0.75)))
            blocks["duration_min"] = after
            if blocks.get("target_range_min"):
                blocks["target_range_min"] = [max(1, int(round(x * 0.75)))
                                              for x in blocks["target_range_min"]]
            if adj.est_duration_min:
                adj.est_duration_min = int(adj.est_duration_min) - (before - after)
            what.append(f"Z2 {before} → {after} min")
        if adj.target_rpe is not None:
            adj.target_rpe = max(1.0, float(adj.target_rpe) - 1)
            blocks["rpe_cap"] = adj.target_rpe
        adj.lines.append(f"{' / '.join(why).capitalize()} → {'; '.join(what) or 'easier day'}.")

    for ex in blocks.get("exercises") or []:
        if ex.get("rpe_cap") is not None:
            ex["rpe_cap"] = max(1.0, float(ex["rpe_cap"]))

    adj.changed = bool(adj.rules_fired)
    adj.reason = " ".join(adj.lines)
    return adj


def _by_name(exercises, name):
    return next(e for e in exercises if e["name"] == name)


def _n(x) -> str:
    x = float(x)
    return str(int(x)) if x == int(x) else f"{x:g}"


def _join(names) -> str:
    names = [n[0].lower() + n[1:] if n and not n[:2].isupper() else n for n in names]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", " + names[-1]


def _equipment_for(exercises, fallback) -> list:
    from artemis import health_office as office
    eq_by_ex = {
        "Leg press": office.EQ_LEG_PRESS, "Seated leg curl": office.EQ_LEG_CURL,
        "Leg extension": office.EQ_LEG_EXT, "Calf press": office.EQ_CALF_PRESS,
        "Captain's chair knee raise": office.EQ_CAPTAINS,
        "45° back extension": office.EQ_BACK_EXT, "Cable Pallof press": office.EQ_CABLE,
    }
    out = list(fallback)
    for ex in exercises:
        eq = eq_by_ex.get(ex["name"])
        if eq and eq not in out:
            out.append(eq)
    return out


def apply_to_blocks(adj: Adjustment, plan: dict, checkin_id: str, now: datetime) -> dict:
    """The blocks to write: adjusted blocks + original + adjustment metadata."""
    live = coerce_blocks(plan.get("blocks"))
    original = live.get("original") if isinstance(live.get("original"), dict) else {
        "blocks": {k: v for k, v in live.items() if k not in ("original", "adjustment")},
        "session_type": plan.get("session_type"),
        "target_rpe": float(plan["target_rpe"]) if plan.get("target_rpe") is not None else None,
        "est_duration_min": plan.get("est_duration_min"),
    }
    out = copy.deepcopy(adj.blocks)
    out["original"] = original
    out["adjustment"] = {
        "reason": adj.reason,
        "rules_fired": adj.rules_fired,
        "checkin_id": checkin_id,
        "at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "removed": adj.removed,
        "added": adj.added,
        "eased": adj.eased,
        "summary": adj.lines,
    }
    return out


# ============================================================================
# Plan-exact rendering (wake post + replies)
# ============================================================================

def render_plan_lines(plan: dict) -> list[str]:
    """Exercises / sets / reps / RPE / load — read ONLY from the plan row."""
    b = coerce_blocks(plan.get("blocks"))
    session_rpe = b.get("rpe_cap", plan.get("target_rpe"))
    lines: list[str] = []
    if b.get("type") == "steady":
        eq = ", ".join(b.get("equipment") or [])
        rng = b.get("target_range_min")
        mins = f"{rng[0]}–{rng[1]}" if rng else b.get("duration_min")
        lines.append(f"· {mins} min {b.get('intensity') or ''} — {eq}".replace("  ", " "))
        if b.get("mobility_min"):
            lines.append(f"· {b['mobility_min']} min mobility ({', '.join(b.get('mobility_focus') or [])})")
        return lines
    for i, ex in enumerate(b.get("exercises") or [], 1):
        sets = exercise_sets(ex, b)
        rng = _rep_range(ex)
        if ex.get("format") == "duration" and ex.get("duration_sec"):
            work = f"{sets}× {ex['duration_sec']}s"
        else:
            work = f"{sets}×{rng}" if rng else f"{sets} sets"
        bits = [work]
        cap = ex.get("rpe_cap", session_rpe)
        if cap is not None:
            bits.append(f"RPE ≤{_n(cap)}")
        if ex.get("target_load_lbs") is not None:
            bits.append(f"{_n(ex['target_load_lbs'])} lb")
        if ex.get("load_pct"):
            bits.append(f"load −{100 - int(ex['load_pct'])}%")
        tag = " _(added)_" if ex.get("added_by") == "checkin" else ""
        lines.append(f"{i}. {ex['name']} — {' · '.join(bits)}{tag}")
    fin = b.get("finisher")
    if isinstance(fin, dict):
        for ex in fin.get("exercises") or []:
            lines.append(f"+ {fin.get('display_name', 'Finisher')}: {fin.get('rounds', 1)}× "
                         f"{ex.get('duration_sec', '')}s {ex['name']} ({ex.get('notes', '')})")
    if b.get("mobility_min"):
        lines.append(f"+ {b['mobility_min']} min mobility ({', '.join(b.get('mobility_focus') or [])})")
    return lines


# ============================================================================
# Storage — every function takes a DB-API cursor (plain tuples)
# ============================================================================

def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_plan(cur, day: date) -> dict | None:
    cur.execute(
        "SELECT plan_id, plan_date, phase, week_num, session_type, target_rpe, "
        "target_hr_zone, est_duration_min, blocks, is_skipped "
        "FROM health.plan WHERE plan_date = %s", (day,))
    rows = _rows(cur)
    if not rows:
        return None
    row = rows[0]
    row["blocks"] = coerce_blocks(row["blocks"])
    return row


def logged_set_count(cur, plan_id: int) -> int:
    cur.execute(
        "SELECT count(*) FROM health.session_log WHERE plan_id = %s "
        "AND logged_via <> 'inferred' AND log_type IN ('strength_set', 'cardio_block')",
        (plan_id,))
    return int(cur.fetchone()[0])


def has_checkin(cur, day: date) -> bool:
    cur.execute("SELECT 1 FROM health.daily_state WHERE state_date = %s", (day,))
    return cur.fetchone() is not None


def store_checkin(cur, day: date, ci: CheckIn) -> None:
    sore = ci.soreness_json()
    cur.execute(
        """INSERT INTO health.daily_state
           (state_date, weight_lbs, sleep_hrs, energy, soreness, resting_hr, free_text)
           VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
           ON CONFLICT (state_date) DO UPDATE SET
               weight_lbs = COALESCE(EXCLUDED.weight_lbs, health.daily_state.weight_lbs),
               sleep_hrs  = COALESCE(EXCLUDED.sleep_hrs,  health.daily_state.sleep_hrs),
               energy     = COALESCE(EXCLUDED.energy,     health.daily_state.energy),
               soreness   = COALESCE(EXCLUDED.soreness,   health.daily_state.soreness),
               resting_hr = COALESCE(EXCLUDED.resting_hr, health.daily_state.resting_hr),
               free_text  = COALESCE(EXCLUDED.free_text,  health.daily_state.free_text),
               logged_at  = NOW()""",
        (day, ci.weight_lbs, ci.sleep_hrs, ci.energy,
         json.dumps(sore) if sore is not None else None, ci.resting_hr, ci.free_text))


def write_plan(cur, plan_id: int, blocks: dict, session_type: str,
               target_rpe, est_duration_min) -> None:
    cur.execute(
        "UPDATE health.plan SET blocks = %s::jsonb, session_type = %s, target_rpe = %s, "
        "est_duration_min = %s WHERE plan_id = %s",
        (json.dumps(blocks), session_type, target_rpe, est_duration_min, plan_id))


def audit(cur, action: str, outcome: str, metadata: dict) -> None:
    cur.execute(
        "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
        "outcome, token_count, api_cost_usd, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        ("health_checkin", None, action, "health", None, outcome, 0, 0.0,
         json.dumps(metadata, default=str)))


# ============================================================================
# Flows
# ============================================================================

def checkin_key(day: date) -> str:
    return f"checkin_open:{day.isoformat()}"


def process_checkin(cur, text: str, day: date, *, checkin_id: str,
                    now: datetime | None = None, adjust: bool | None = None) -> str:
    """Store a check-in and (maybe) adjust today's plan. Returns the reply."""
    now = now or datetime.now(timezone.utc)
    adjust = config.CHECKIN_ADJUST if adjust is None else adjust
    ci = parse_checkin(text)
    if ci.rating_error:
        return RATINGS_ERROR
    if not ci.has_data:
        return ("I couldn't read a check-in there. Try: "
                "`slept 7 energy 4 sore 0 weight 283`.")
    store_checkin(cur, day, ci)

    plan = load_plan(cur, day)
    notes = []
    if ci.unknown_regions:
        notes.append("Didn't recognize region " + ", ".join(sorted(ci.unknown_regions))
                     + " — logged, no change for it.")
    if plan is None:
        return "\n".join(["Check-in logged — no plan today."] + notes)

    label = session_label(plan["session_type"], plan["blocks"])
    if logged_set_count(cur, plan["plan_id"]) > 0:
        audit(cur, "checkin_logged", "no_adjust_sets_logged", {"plan_id": plan["plan_id"]})
        return "\n".join(["Logged."] + notes)

    if plan["session_type"] in LIGHT_TYPES:
        what = "rest day" if plan["session_type"] == "rest_mobility" else "walk"
        return "\n".join([f"Check-in logged — {what} as planned."] + notes)

    adj = compute_adjustment(plan, ci)
    if not adj.changed or not adjust:
        if adj.changed and not adjust:
            logger.info("CHECKIN_ADJUST=0 — would have applied: %s", adj.reason)
            audit(cur, "checkin_adjust_suppressed", "flag_off",
                  {"plan_id": plan["plan_id"], "rules": adj.rules_fired})
        if "adjustment" in plan["blocks"] and adjust:
            # A newer check-in that no longer warrants changes: back to as-written.
            restore_original(cur, plan)
            return "\n".join([f"Check-in logged — back to {label} as written."] + notes)
        return "\n".join([f"Check-in logged — run {label} as written."] + notes)

    blocks = apply_to_blocks(adj, plan, checkin_id, now)
    write_plan(cur, plan["plan_id"], blocks, adj.session_type, adj.target_rpe,
               adj.est_duration_min)
    audit(cur, "checkin_adjust", ",".join(adj.rules_fired),
          {"plan_id": plan["plan_id"], "checkin_id": checkin_id, "removed": adj.removed,
           "added": adj.added, "eased": adj.eased})
    # Plan-exact diff only — no advice, no health text.
    return "\n".join(adj.lines + notes + ["Reply `original` to undo."])


def restore_original(cur, plan: dict) -> bool:
    b = plan["blocks"]
    orig = b.get("original")
    if not isinstance(orig, dict):
        return False
    write_plan(cur, plan["plan_id"], orig.get("blocks") or {}, orig.get("session_type"),
               orig.get("target_rpe"), orig.get("est_duration_min"))
    audit(cur, "checkin_restore", "restored", {"plan_id": plan["plan_id"]})
    return True


def process_original(cur, day: date) -> str:
    plan = load_plan(cur, day)
    if plan is None or not restore_original(cur, plan):
        return "No adjustment to undo — today's plan is as written."
    written = load_plan(cur, day)
    label = session_label(written["session_type"], written["blocks"])
    return f"Restored — run {label} as written."


def process_done(cur, day: date) -> str:
    plan = load_plan(cur, day)
    if plan is None:
        return "No plan today."
    cur.execute(
        "SELECT exercise, log_type FROM health.session_log WHERE plan_id = %s "
        "AND logged_via <> 'inferred'", (plan["plan_id"],))
    rows = cur.fetchall()
    sets = [r for r in rows if r[1] in ("strength_set", "cardio_block")]
    label = session_label(plan["session_type"], plan["blocks"])
    if not sets:
        return f"I don't see any sets for today yet — log them in the app and {label} will show as done."
    n_ex = len({r[0] for r in sets})
    summary = any(r[1] == "session_summary" for r in rows)
    tail = "" if summary else " (no session summary yet — finish it in the app)"
    return f"{label} logged: {len(sets)} set{'s' if len(sets) != 1 else ''} across {n_ex} exercise{'s' if n_ex != 1 else ''}{tail}."


def process_ack(cur, day: date) -> str:
    plan = load_plan(cur, day)
    if plan is None:
        return "Got it."
    label = session_label(plan["session_type"], plan["blocks"])
    if "adjustment" in plan["blocks"]:
        return f"Got it — run the adjusted {label}. Reply `original` to go back."
    if plan["session_type"] in LIGHT_TYPES:
        return "Got it."
    return f"Got it — run {label} as written."


def nudge_text(plan: dict) -> str | None:
    if plan is None or plan["session_type"] in LIGHT_TYPES or plan.get("is_skipped"):
        return None
    return f"No check-in yet — run {session_label(plan['session_type'], plan['blocks'])} as written."


# ============================================================================
# Routing — which short replies belong to the morning flow
# ============================================================================

# Replies to "anything to fix?" — deliberately narrow: "ok"/"thanks" are not claimed.
_ACK_RE = re.compile(
    r"^\s*(?:nope|no|nah|all\s+good|looks\s+good|nothing\s+to\s+fix|no\s+changes?|we'?re\s+good)"
    r"\s*[.!]*\s*$", re.I)
# "done" alone (or "workout done", "done with the workout") — never
# "done <thread-id>" / "done <commitment>", which belong to the inbox handlers.
_DONE_RE = re.compile(
    r"^\s*(?:(?:workout|session|lift|training|all)\s+)?(?:done|complete|completed|finished)"
    r"(?:\s*[.!]+|\s*$|\s+(?:with\s+)?(?:the\s+|my\s+|today'?s\s+)?"
    r"(?:workout|session|lift|training|for\s+today)\b)"
    r"|\blogged\s+(?:it\s+)?in\s+(?:the\s+)?app\b",
    re.I)
_ORIGINAL_RE = re.compile(
    r"^\s*(?:original|original\s+plan|use\s+(?:the\s+)?original|as\s+written)\s*[.!]*\s*$", re.I)
_CHECKIN_SHAPE_RE = re.compile(
    r"\b(?:slept|sleep|energy|sore(?:ness)?|rhr|resting\s*hr)\b|\bweight\s*\d", re.I)


def classify(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    if _ORIGINAL_RE.match(t):
        return "original"
    if _CHECKIN_SHAPE_RE.search(t):
        ci = parse_checkin(t)
        if ci.has_data or ci.rating_error:
            return "checkin"
    if _DONE_RE.search(t) and len(t.split()) <= 8:
        return "done"
    if _ACK_RE.match(t):
        return "ack"
    return None
