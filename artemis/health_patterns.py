"""Pain pattern surfacing (PAIN-1 §4).

Deterministic. It counts and reports; it never asserts a cause and never
changes a plan.

  Exposure   exercise E logged on day D (a non-skipped set, or any set that
             carries a pain note).
  Hit        an in-session `pain=<region>:<n>` note on E with n >= 2 (counts
             for that region, check-in or not), OR the D+1 morning check-in has
             pain >= 2 in a region E uses (primary or secondary). At most one
             hit per exposure per region.
  Candidate  E x region with >= 3 hits and hits / exposures >= 60% over the
             last 8 weeks. Reported as hits/exposures, with the other
             exercises that day that share the region ("also that day").

Surfacing:
  - nightly 21:55 recompute (silent) keeps health.pain_pattern current;
  - Sunday 08:30 health review (the weekend open) posts each new or changed open candidate;
  - the check-in that completes a candidate mentions it once, in one line.

Ryan's thread reply to a pattern post is stored verbatim in health.reflection.
`dismiss` hides a pattern until it gains 2 more hits; `resolved` closes it
until new data re-qualifies it.

In-session notes (`pain=shoulder:2; felt off`) are read HERE only — never by
the check-in adjustment rules.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from artemis import health_regions as hr

logger = logging.getLogger(__name__)

WINDOW_DAYS = 56          # 8 weeks
MIN_HITS = 3
MIN_RATE = 0.6
HIT_PAIN = 2              # pain >= 2 is a hit
DISMISS_REARM_HITS = 2

_PAIN_NOTE_RE = re.compile(r"(?:^|;)\s*pain=([a-z][a-z ]*?)\s*:\s*(\d)\s*(?=;|$)", re.I)


def parse_pain_notes(notes: str | None) -> list[tuple[str, int]]:
    """[(region key, 0-5)] from a session_log notes string. The key carries a
    side when the note names one (see health_regions.side_key).

    `pain=shoulder:2; pain=right knee:3` -> [("shoulder", 2), ("right knee", 3)].
    Unknown regions and ratings above 5 are ignored.
    """
    out = []
    for m in _PAIN_NOTE_RE.finditer(notes or ""):
        raw, side = hr.split_side_key(m.group(1).strip().lower())
        region = hr.canonical_region(raw) or (raw if raw in hr.REGIONS else None)
        n = int(m.group(2))
        if region and 0 <= n <= 5:
            out.append((hr.side_key(region, side), n))
    return out


# ============================================================================
# Pure computation
# ============================================================================

@dataclass
class Tally:
    exercise: str
    region: str
    hits: int = 0
    exposures: int = 0
    hit_days: list = field(default_factory=list)
    exposure_days: list = field(default_factory=list)
    shared: list = field(default_factory=list)

    @property
    def qualifies(self) -> bool:
        return (self.hits >= MIN_HITS and self.exposures > 0
                and self.hits / self.exposures >= MIN_RATE)

    def evidence(self) -> dict:
        return {"hit_days": [d.isoformat() for d in self.hit_days],
                "exposure_days": [d.isoformat() for d in self.exposure_days],
                "shared": list(self.shared)}


def _pain_of(soreness) -> dict:
    if isinstance(soreness, str):
        try:
            soreness = json.loads(soreness)
        except ValueError:
            return {}
    if not isinstance(soreness, dict):
        return {}
    pain = soreness.get("pain") or {}
    sides = soreness.get("pain_sides") or {}
    # Keyed on region + side ("right knee"), so a pattern splits by side.
    return {hr.side_key(r, sides.get(r)): v for r, v in pain.items() if isinstance(v, int)}


def tally(logs, checkins: dict, today: date) -> dict:
    """`logs`: [(plan_date, exercise, notes, is_skipped)];
    `checkins`: {state_date: soreness json}. -> {(exercise, region): Tally}."""
    start = today - timedelta(days=WINDOW_DAYS - 1)
    # day -> exercise -> {"real": bool, "notes": [(region, n)]}
    days: dict[date, dict[str, dict]] = {}
    for d, exercise, notes, skipped in logs:
        if not exercise or d < start or d > today:
            continue
        slot = days.setdefault(d, {}).setdefault(exercise, {"real": False, "notes": []})
        slot["real"] |= not skipped
        slot["notes"] += parse_pain_notes(notes)

    exposures: dict[str, list[date]] = {}
    hits: dict[tuple[str, str], list[date]] = {}
    for d in sorted(days):
        next_pain = _pain_of(checkins.get(d + timedelta(days=1))) if d < today else {}
        for exercise, slot in days[d].items():
            if not (slot["real"] or slot["notes"]):
                continue
            exposures.setdefault(exercise, []).append(d)
            regions = []   # region keys, side included ("right knee")
            for key, n in slot["notes"]:
                if n >= HIT_PAIN and key not in regions:
                    regions.append(key)
            for key, n in next_pain.items():
                base, side = hr.split_side_key(key)
                if n >= HIT_PAIN and base in hr.REGIONS and key not in regions \
                        and hr.uses_any(exercise, [base], sides={base: side}):
                    regions.append(key)
            for region in regions:
                hits.setdefault((exercise, region), []).append(d)

    out = {}
    for (exercise, region), hit_days in hits.items():
        t = Tally(exercise, region, hits=len(hit_days), exposures=len(exposures[exercise]),
                  hit_days=hit_days, exposure_days=exposures[exercise])
        base, side = hr.split_side_key(region)
        for d in hit_days:
            for other in days[d]:
                if other != exercise and other not in t.shared \
                        and hr.uses_any(other, [base], sides={base: side}):
                    t.shared.append(other)
        out[(exercise, region)] = t
    return out


def _lc(name: str) -> str:
    return name[0].lower() + name[1:] if name and not name[:2].isupper() else name


def render(row: dict) -> str:
    """One line. Counts and co-occurrence only — never a cause."""
    shared = (row.get("evidence") or {}).get("shared") or []
    also = f" (also that day: {', '.join(_lc(s) for s in shared)})" if shared else ""
    return (f"Pattern: {row['region']} pain ≥{HIT_PAIN} after **{_lc(row['exercise'])}** — "
            f"{row['hits']} of {row['exposures']} sessions{also}. What do you notice?")


# ============================================================================
# Storage
# ============================================================================

_COLS = ("id", "exercise", "region", "hits", "exposures", "qualifies", "first_seen",
         "last_seen", "last_surfaced", "surfaced_hits", "surfaced_exposures",
         "mentioned_at", "post_ids", "status", "dismissed_at_hits", "resolved_at",
         "resolved_at_hits", "evidence")


def _rows(cur) -> list[dict]:
    out = []
    for r in cur.fetchall():
        row = dict(zip(_COLS, r))
        if isinstance(row["evidence"], str):
            row["evidence"] = json.loads(row["evidence"])
        row["post_ids"] = list(row["post_ids"] or [])
        out.append(row)
    return out


def load_patterns(cur) -> list[dict]:
    cur.execute(f"SELECT {', '.join(_COLS)} FROM health.pain_pattern ORDER BY id")
    return _rows(cur)


def _load_inputs(cur, today: date):
    start = today - timedelta(days=WINDOW_DAYS - 1)
    cur.execute(
        "SELECT p.plan_date, sl.exercise, sl.notes, COALESCE(sl.is_skipped, FALSE) "
        "FROM health.session_log sl JOIN health.plan p ON p.plan_id = sl.plan_id "
        "WHERE p.plan_date BETWEEN %s AND %s AND sl.log_type = 'strength_set' "
        "AND sl.logged_via <> 'inferred'",
        (start, today))
    logs = cur.fetchall()
    cur.execute(
        "SELECT state_date, soreness FROM health.daily_state "
        "WHERE state_date BETWEEN %s AND %s",
        (start + timedelta(days=1), today))
    checkins = {d: s for d, s in cur.fetchall()}
    return logs, checkins


def recompute(cur, today: date, now: datetime | None = None) -> list[dict]:
    """Refresh health.pain_pattern from the last 8 weeks. Silent.

    New rows are written only for candidates; existing rows are always
    refreshed (a pattern can stop qualifying). Lifecycle:
      dismissed -> open when hits >= dismissed_at_hits + 2
      resolved  -> open when it qualifies again with a hit after resolution
    """
    now = now or datetime.now(timezone.utc)
    logs, checkins = _load_inputs(cur, today)
    tallies = tally(logs, checkins, today)
    existing = {(r["exercise"], r["region"]): r for r in load_patterns(cur)}

    for key, row in existing.items():
        if key not in tallies:
            tallies[key] = Tally(*key)
    for key, t in tallies.items():
        row = existing.get(key)
        if row is None and not t.qualifies:
            continue
        status = row["status"] if row else "open"
        dismissed_at = row["dismissed_at_hits"] if row else None
        if status == "dismissed" and dismissed_at is not None \
                and t.hits >= dismissed_at + DISMISS_REARM_HITS:
            status, dismissed_at = "open", None
        if status == "resolved" and t.qualifies and row.get("resolved_at") is not None \
                and t.hit_days and t.hit_days[-1] > _as_date(row["resolved_at"]):
            status = "open"
        first = t.hit_days[0] if t.hit_days else None
        last = t.hit_days[-1] if t.hit_days else None
        cur.execute(
            """INSERT INTO health.pain_pattern
                   (exercise, region, hits, exposures, qualifies, first_seen, last_seen,
                    status, dismissed_at_hits, evidence, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
               ON CONFLICT (exercise, region) DO UPDATE SET
                   hits = EXCLUDED.hits, exposures = EXCLUDED.exposures,
                   qualifies = EXCLUDED.qualifies,
                   first_seen = COALESCE(EXCLUDED.first_seen, health.pain_pattern.first_seen),
                   last_seen = COALESCE(EXCLUDED.last_seen, health.pain_pattern.last_seen),
                   status = EXCLUDED.status, dismissed_at_hits = EXCLUDED.dismissed_at_hits,
                   evidence = EXCLUDED.evidence, updated_at = EXCLUDED.updated_at""",
            (t.exercise, t.region, t.hits, t.exposures, t.qualifies, first, last,
             status, dismissed_at, json.dumps(t.evidence()), now))
    return load_patterns(cur)


def _as_date(v) -> date:
    return v.date() if isinstance(v, datetime) else v


def to_surface(rows) -> list[dict]:
    """Open candidates that are new or changed since they were last posted."""
    return [r for r in rows
            if r["status"] == "open" and r["qualifies"]
            and (r["surfaced_hits"], r["surfaced_exposures"]) != (r["hits"], r["exposures"])]


def mark_surfaced(cur, row: dict, post_id: str | None, now: datetime) -> None:
    cur.execute(
        "UPDATE health.pain_pattern SET last_surfaced = %s, surfaced_hits = %s, "
        "surfaced_exposures = %s, post_ids = CASE WHEN %s::text IS NULL THEN post_ids "
        "ELSE array_append(post_ids, %s::text) END WHERE id = %s",
        (now, row["hits"], row["exposures"], post_id, post_id, row["id"]))


def mention_on_checkin(cur, day: date, now: datetime) -> tuple[list[int], list[str]]:
    """The one-line mention for a candidate THIS check-in completed.

    Completed = it qualifies now, didn't before, and yesterday's session is one
    of its hit days (so the hit came from this morning's pain). Mentioned once
    ever (mentioned_at). Runs in a savepoint: a pattern failure never costs
    the check-in.
    """
    cur.execute("SAVEPOINT pain_patterns")
    try:
        before = {(r["exercise"], r["region"]): r["qualifies"] for r in load_patterns(cur)}
        rows = recompute(cur, day, now)
        yesterday = (day - timedelta(days=1)).isoformat()
        fresh = [r for r in rows
                 if r["qualifies"] and not before.get((r["exercise"], r["region"]))
                 and r["status"] == "open" and r["mentioned_at"] is None
                 and yesterday in (r["evidence"] or {}).get("hit_days", [])]
        if not fresh:
            cur.execute("RELEASE SAVEPOINT pain_patterns")
            return [], []
        row = fresh[0]
        cur.execute("UPDATE health.pain_pattern SET mentioned_at = %s WHERE id = %s",
                    (now, row["id"]))
        cur.execute("RELEASE SAVEPOINT pain_patterns")
        return [row["id"]], [render(row)]
    except Exception:
        logger.exception("Pain pattern check on check-in failed — check-in kept")
        cur.execute("ROLLBACK TO SAVEPOINT pain_patterns")
        return [], []


# ============================================================================
# Threads: reflections, dismiss, resolved
# ============================================================================

def patterns_for_post(cur, post_id: str) -> list[int]:
    if not post_id:
        return []
    cur.execute("SELECT id FROM health.pain_pattern WHERE %s = ANY(post_ids) ORDER BY id",
                (post_id,))
    return [r[0] for r in cur.fetchall()]


def store_reflection(cur, pattern_ids: list[int], text: str, post_id: str,
                     source_post_id: str | None) -> None:
    """Verbatim. Linked to the pattern when the thread is about exactly one."""
    cur.execute(
        "INSERT INTO health.reflection (pattern_id, text, post_id, source_post_id) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (source_post_id) DO NOTHING",
        (pattern_ids[0] if len(pattern_ids) == 1 else None, text, post_id,
         source_post_id or None))


def set_status(cur, pattern_ids: list[int], status: str, now: datetime) -> None:
    for pid in pattern_ids:
        if status == "dismissed":
            cur.execute("UPDATE health.pain_pattern SET status = 'dismissed', "
                        "dismissed_at_hits = hits, updated_at = %s WHERE id = %s", (now, pid))
        elif status == "resolved":
            cur.execute("UPDATE health.pain_pattern SET status = 'resolved', resolved_at = %s, "
                        "resolved_at_hits = hits, updated_at = %s WHERE id = %s",
                        (now, now, pid))


_DISMISS_RE = re.compile(r"^\s*dismiss(?:ed)?\s*[.!]*\s*$", re.I)
_RESOLVED_RE = re.compile(r"^\s*resolved?\s*[.!]*\s*$", re.I)


def classify_thread_reply(text: str) -> str:
    if _DISMISS_RE.match(text or ""):
        return "dismissed"
    if _RESOLVED_RE.match(text or ""):
        return "resolved"
    return "reflection"


def checkin_thread_key(root_id: str) -> str:
    return f"pain_pattern_thread:{root_id}"
