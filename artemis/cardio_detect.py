"""CARDIO-DETECT — the watch proposes a cardio log, Ryan confirms it.

The bottleneck stopped being code a while ago. One cardio session has ever been
logged, so the interval gate blocks on "only 1 of the last 6 are logged",
ZONE-0 has almost nothing to compute over, and the Status cardio tile reads 0.
The watch recorded those sessions the whole time.

So: find the block of elevated heart rate the watch already holds, and offer it
as ONE line he can accept with two words. **Artemis never writes the session
itself** — the proposal is a draft and `log cardio` is the confirmation, which
is the Brad Spaits rule applied to his training record. A log Artemis invented
would corrupt the same history the gate and the reports read.

FAIL-CLOSED throughout: an unreadable plan or an unreadable heart-rate series
posts NOTHING. A missing proposal costs him one manual log; a wrong proposal
costs the truth of his training record, and he would have to notice to undo it.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from knowledge import zones

logger = logging.getLogger(__name__)

#: Standing automation ⇒ human-gated, DEFAULT OFF.
ENABLED_KEY = "cardio_detect_enabled"
#: One pending proposal, keyed by the plan row it belongs to.
PENDING_KEY = "cardio_detect_pending"

#: A block has to be worth proposing. Fifteen minutes is the floor the handoff
#: set; below it the odds of catching a brisk walk to the car rise sharply.
MIN_BLOCK_MIN = 15
#: The same density rule ZONE-0 uses -- one sample a minute -- so a block and its
#: zone breakdown can never disagree about whether the data was good enough.
MIN_SAMPLES_PER_MIN = zones.MIN_SAMPLES_PER_MIN
#: A gap longer than this ENDS the block. It is the same cap ZONE-0 credits a
#: sample with: past it the watch was not measuring, and stitching across it
#: would invent one long session out of two short ones.
MAX_GAP_SEC = zones.MAX_SAMPLE_GAP_SEC

CARDIO_TYPES = ("cardio_z2", "cardio_intervals")


def is_enabled(cur) -> bool:
    """Default OFF. An unreadable flag is OFF too -- a standing automation that
    turns itself on because a read failed is the failure mode that matters."""
    try:
        cur.execute("SELECT value FROM acos.system_state WHERE key = %s", (ENABLED_KEY,))
        row = cur.fetchone()
    except Exception:                                           # noqa: BLE001
        logger.warning("cardio detect: could not read the enable flag", exc_info=True)
        return False
    if row is None:
        return False
    value = row["value"] if isinstance(row, dict) else row[0]
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def blocks_from(samples, *, min_block_min=MIN_BLOCK_MIN) -> list[dict]:
    """Continuous elevated-HR blocks in a sorted (measured_at, bpm) series.

    "Continuous" means no gap longer than MAX_GAP_SEC and every sample at or
    above the Z1 floor. A single dip below the floor ENDS the block rather than
    being smoothed over: a rest that drops him out of Z1 for two minutes is two
    efforts, and calling it one would overstate the session.
    """
    floor = zones.ZONES["Z1"][0]
    ordered = sorted((ts, bpm) for ts, bpm in samples if ts is not None and bpm is not None)
    out: list[dict] = []
    run: list = []

    def close(run):
        if len(run) < 2:
            return
        start, end = run[0][0], run[-1][0]
        secs = (end - start).total_seconds()
        if secs < min_block_min * 60:
            return
        bpms = sorted(int(b) for _t, b in run)
        mid = len(bpms) // 2
        median = bpms[mid] if len(bpms) % 2 else (bpms[mid - 1] + bpms[mid]) / 2
        out.append({"start": start, "end": end, "minutes": int(round(secs / 60.0)),
                    "sample_count": len(run), "median_bpm": int(median),
                    "avg_bpm": int(round(sum(bpms) / len(bpms))),
                    "dense": len(run) >= (secs / 60.0) * MIN_SAMPLES_PER_MIN})

    for ts, bpm in ordered:
        if int(bpm) < floor:
            close(run)
            run = []
            continue
        if run and (ts - run[-1][0]).total_seconds() > MAX_GAP_SEC:
            close(run)
            run = []
        run.append((ts, bpm))
    close(run)
    return out


def best_block(samples, *, window=None, min_block_min=MIN_BLOCK_MIN) -> dict | None:
    """The longest dense block, optionally restricted to a (start, end) window.

    A sparse block is DISCARDED rather than proposed with a caveat: the minutes
    are what he is being asked to confirm, and minutes measured from four
    samples an hour are not minutes.
    """
    if window and window[0] and window[1]:
        samples = [(t, b) for t, b in samples if window[0] <= t <= window[1]]
    dense = [b for b in blocks_from(samples, min_block_min=min_block_min) if b["dense"]]
    if not dense:
        return None
    return max(dense, key=lambda b: b["minutes"])


def _rows_needing_a_log(cur, day: date) -> list[dict]:
    """Today's cardio rows with no real log. Raises -- the caller fails closed."""
    cur.execute(
        "SELECT p.plan_id, p.slot, p.session_type, p.blocks "
        "FROM health.plan p "
        "WHERE p.plan_date = %s AND p.session_type IN %s "
        "  AND NOT EXISTS (SELECT 1 FROM health.session_log sl "
        "                  WHERE sl.plan_id = p.plan_id AND sl.logged_via <> 'inferred') "
        "ORDER BY p.slot", (day, CARDIO_TYPES))
    out = []
    for r in cur.fetchall():
        blocks = r["blocks"] if isinstance(r, dict) else r[3]
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except ValueError:
                blocks = {}
        out.append({"plan_id": r["plan_id"] if isinstance(r, dict) else r[0],
                    "slot": r["slot"] if isinstance(r, dict) else r[1],
                    "session_type": r["session_type"] if isinstance(r, dict) else r[2],
                    "blocks": blocks or {}})
    return out


def _claimed_windows(cur, day: date) -> list[tuple]:
    """Windows already accounted for by ANOTHER session logged that day.

    The box proof found this: 9/21, 9/23 and 9/24 each hold a 16-18 min block
    above the Z1 floor at ~10:30 AM, and those are his STRENGTH sessions -- the
    same windows ZONE-0 computed Z2 minutes for. A lift raises the heart rate
    like anything else, so on a day carrying both a lift and unlogged cardio the
    longest elevated block is quite likely the lift. Proposing it would put a
    cardio session in his record that never happened, and he would have to
    notice to undo it.
    """
    cur.execute(
        "SELECT z.window_start, z.window_end FROM health.session_hr_zones z "
        "JOIN health.plan p ON p.plan_id = z.plan_id "
        "WHERE p.plan_date = %s AND p.session_type NOT IN %s", (day, CARDIO_TYPES))
    return [((r["window_start"], r["window_end"]) if isinstance(r, dict)
             else (r[0], r[1])) for r in cur.fetchall()]


def _overlaps(block: dict, windows) -> bool:
    for w_start, w_end in windows:
        if w_start and w_end and block["start"] <= w_end and w_start <= block["end"]:
            return True
    return False


def _samples(cur, start: datetime, end: datetime) -> list:
    cur.execute(
        "SELECT measured_at, bpm FROM health.watch_heart_rate "
        "WHERE measured_at BETWEEN %s AND %s AND bpm IS NOT NULL "
        "ORDER BY measured_at", (start, end))
    return [(r["measured_at"], r["bpm"]) if isinstance(r, dict) else (r[0], r[1])
            for r in cur.fetchall()]


def propose_line(row: dict, block: dict, zone_line: str | None = None) -> str:
    """The ONE line posted. Names what was seen and what confirming would record,
    so he is agreeing to something specific rather than to Artemis's judgement."""
    name = (row.get("blocks") or {}).get("display_name") or row["session_type"]
    when = block["start"].strftime("%-I:%M %p").lstrip("0")
    zones_part = f" ({zone_line})" if zone_line else ""
    return (f"\U0001f4c8 Watch shows {block['minutes']} min at {when}, "
            f"avg {block['avg_bpm']} bpm{zones_part}. "
            f"Reply `log cardio` to record it as today's {name}.")


def detect(cur, day: date, *, tz=None) -> dict:
    """Find a proposable block for today's unlogged cardio. Never raises.

    Returns {"ok", "reason", "row", "block", "already"}; `ok` False means post
    nothing. Deliberately does NOT write anything: a proposal is a draft.
    """
    try:
        rows = _rows_needing_a_log(cur, day)
    except Exception:                                           # noqa: BLE001
        logger.warning("cardio detect: could not read today's plan", exc_info=True)
        return {"ok": False, "reason": "couldn't read the plan", "row": None, "block": None}
    if not rows:
        return {"ok": False, "reason": "no unlogged cardio today", "row": None, "block": None}

    start = datetime.combine(day, datetime.min.time())
    if tz is not None:
        start = start.replace(tzinfo=tz)
    end = start + timedelta(days=1)
    try:
        samples = _samples(cur, start, end)
    except Exception:                                           # noqa: BLE001
        logger.warning("cardio detect: could not read heart rate", exc_info=True)
        return {"ok": False, "reason": "couldn't read the watch", "row": None, "block": None}

    try:
        claimed = _claimed_windows(cur, day)
    except Exception:                                           # noqa: BLE001
        # FAIL-CLOSED: without knowing which windows belong to other sessions,
        # any block might be one of them. Say nothing.
        logger.warning("cardio detect: could not read the day's other sessions",
                       exc_info=True)
        return {"ok": False, "reason": "couldn't check the day's other sessions",
                "row": None, "block": None}

    for row in rows:
        candidates = [b for b in blocks_from(samples) if b["dense"]
                      and not _overlaps(b, claimed)]
        if candidates:
            return {"ok": True, "reason": None, "row": row,
                    "block": max(candidates, key=lambda b: b["minutes"])}
    reason = ("no elevated block long or dense enough"
              if not claimed else
              "no elevated block that another logged session doesn't already explain")
    return {"ok": False, "reason": reason, "row": rows[0], "block": None}


def pending(cur) -> dict | None:
    """The proposal awaiting confirmation, or None."""
    try:
        cur.execute("SELECT value FROM acos.system_state WHERE key = %s", (PENDING_KEY,))
        row = cur.fetchone()
    except Exception:                                           # noqa: BLE001
        logger.warning("cardio detect: could not read the pending proposal", exc_info=True)
        return None
    if row is None:
        return None
    raw = row["value"] if isinstance(row, dict) else row[0]
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_pending(cur, value: dict | None) -> None:
    if value is None:
        cur.execute("DELETE FROM acos.system_state WHERE key = %s", (PENDING_KEY,))
        return
    cur.execute(
        "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (PENDING_KEY, json.dumps(value, default=str)))


def clear_expired(cur, day: date) -> bool:
    """A proposal expires at the next wake. Yesterday's watch block is not an
    answer to today's question, and a stale pending would attach a confirmation
    to the wrong session."""
    p = pending(cur)
    if p and str(p.get("day")) != day.isoformat():
        set_pending(cur, None)
        return True
    return False


# ── The confirmation ────────────────────────────────────────────────────────
# `log cardio` writes the SAME rows Finish cardio writes, so a confirmed session
# is indistinguishable downstream from one logged on the iPad -- except for
# `logged_via`, which records that the numbers came from a heart-rate trace he
# agreed with rather than from him reading a machine.

LOGGED_VIA = "watch_confirmed"


def confirm(cur, prop: dict, *, on: date | None = None) -> dict:
    """Write the cardio block and the session summary for a pending proposal.

    Raises on a database failure -- the caller reports it. A half-written
    session is worse than none, and both rows go in one transaction.
    """
    from knowledge import cardio as cardio_cfg

    plan_id = prop["plan_id"]
    minutes = int(prop["minutes"])
    duration_sec = minutes * 60
    end = prop["end"]
    if isinstance(end, str):
        end = datetime.fromisoformat(end)

    location_key = prop.get("location_key") or "office"
    modality = device = None
    try:
        resolved = cardio_cfg.resolve(location_key, on)
        modality, device = resolved.get("modality"), resolved.get("device")
    except Exception:                                           # noqa: BLE001
        # A missing modality is a NULL, not a guess: the CHECK only allows the
        # four known ones, and inventing "row" because it is usually the rower
        # would put a device in his record that he never touched.
        logger.warning("cardio detect: could not resolve the modality", exc_info=True)

    cur.execute(
        "INSERT INTO health.session_log (plan_id, log_type, exercise, duration_sec, "
        "  rpe_actual, notes, logged_via, logged_at, modality, device) "
        "VALUES (%s, 'cardio_block', %s, %s, NULL, %s, %s, %s, %s, %s)",
        (plan_id, prop.get("display_name"), duration_sec,
         f"Confirmed from the watch: {minutes} min, avg {prop.get('avg_bpm')} bpm",
         LOGGED_VIA, end, modality, device))
    cur.execute(
        "INSERT INTO health.session_log (plan_id, log_type, exercise, duration_sec, "
        "  rpe_actual, notes, logged_via, logged_at) "
        "VALUES (%s, 'session_summary', NULL, %s, NULL, NULL, %s, %s)",
        (plan_id, duration_sec, LOGGED_VIA, end))
    return {"plan_id": plan_id, "minutes": minutes, "modality": modality,
            "device": device, "logged_via": LOGGED_VIA}


def record_decision(cur, day: date, row: dict | None, block: dict | None,
                    outcome: str, reason: str | None = None) -> None:
    """One cognition row per detection, so a proposal can be argued with later."""
    from knowledge import cognition
    cognition.log_decision(
        cur, agent="cardio_detect", action="cardio_detect", domain="health",
        outcome=outcome, manual_gap=False,
        metadata={"day": day.isoformat(),
                  "plan_id": (row or {}).get("plan_id"),
                  "session_type": (row or {}).get("session_type")},
        assumptions={
            "rule": f"a continuous block >= {MIN_BLOCK_MIN} min, every sample at "
                    f"or above the Z1 floor ({zones.ZONES['Z1'][0]} bpm), no gap "
                    f"over {MAX_GAP_SEC}s, at least {MIN_SAMPLES_PER_MIN}/min",
            "reason": reason,
            "window_start": str((block or {}).get("start")),
            "window_end": str((block or {}).get("end")),
            "minutes": (block or {}).get("minutes"),
            "sample_count": (block or {}).get("sample_count"),
            "median_bpm": (block or {}).get("median_bpm"),
            "artemis_never_writes_the_session": True,
        })
