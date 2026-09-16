"""Phase-aware posting — post now, or hold until the phase allows it (WAKE-1).

Every SCHEDULED post goes through post_or_hold(). Replies to Ryan's own
messages are unaffected: they post immediately in any phase.

    tier="health"    posts in wake + open   (PB-009, pre-departure)
    tier="business"  posts in open only     (email, triage, ops, commitments)

A held post is appended to a durable JSON list in acos.system_state (one key
per tier) — never an in-memory queue, so a restart between hold and flush
keeps the post. job_wake flushes "health"; job_open flushes "business".

Flush order is FIFO and each entry is removed from the stored list as soon as
its post succeeds, so a re-run flushes only what is left. (A crash between the
post and the removal can re-post one entry — the trade chosen over dropping it.)
"""

import json
import logging
from datetime import datetime

from artemis.quiet_hours import (
    PHASE_OPEN,
    PHASE_QUIET,
    get_phase,
    get_system_value,
    local_now,
    set_system_value,
)

logger = logging.getLogger(__name__)

TIERS = ("health", "business")

_HOLD_KEY = {
    "health": "held_posts:health",
    "business": "held_posts:business",
}


def _key(tier: str) -> str:
    try:
        return _HOLD_KEY[tier]
    except KeyError:
        raise ValueError(f"unknown tier {tier!r} (expected one of {TIERS})") from None


def may_post(tier: str, phase: str | None = None) -> bool:
    """True when `tier` is allowed to post in the current (or given) phase."""
    phase = phase or get_phase()
    if tier == "health":
        return phase != PHASE_QUIET
    if tier == "business":
        return phase == PHASE_OPEN
    raise ValueError(f"unknown tier {tier!r} (expected one of {TIERS})")


def read_holds(tier: str) -> list[dict]:
    raw = get_system_value(_key(tier))
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("held_posts[%s] was not valid JSON — dropping", tier)
        return []
    return [i for i in items if isinstance(i, dict) and i.get("text")]


def _write_holds(tier: str, items: list[dict]) -> None:
    set_system_value(_key(tier), json.dumps(items))


def held_count(tier: str) -> int:
    return len(read_holds(tier))


def clear_holds(tier: str) -> None:
    _write_holds(tier, [])


def hold(channel: str, text: str, tier: str) -> None:
    """Append one post to the durable hold list for `tier`."""
    items = read_holds(tier)
    items.append({
        "channel": channel,
        "text": text,
        "held_at": local_now().isoformat(),
    })
    _write_holds(tier, items)
    logger.info("Held %s post (%d queued): %s", tier, len(items), text[:80])


def post_or_hold(mm, channel: str, text: str, tier: str) -> bool:
    """Post `text` now if the phase allows `tier`, else hold it durably.

    Returns True when it was posted, False when held.
    """
    if not text:
        return False
    if may_post(tier):
        mm.post_message(channel, text)
        return True
    hold(channel, text, tier)
    return False


def flush_holds(mm, tier: str) -> int:
    """Post every held entry for `tier`, oldest first. Returns the count posted.

    Idempotent: each entry is removed once its post succeeds, so a second call
    (or a retry after a failure) sends only what remains.
    """
    posted = 0
    while True:
        items = read_holds(tier)
        if not items:
            break
        entry = items[0]
        try:
            mm.post_message(entry.get("channel"), entry["text"])
        except Exception:
            logger.exception("Flush of held %s post failed — leaving %d queued", tier, len(items))
            break
        _write_holds(tier, items[1:])
        posted += 1
    if posted:
        logger.info("Flushed %d held %s post(s)", posted, tier)
    return posted


def take_holds(tier: str) -> list[str]:
    """Remove and return the held texts for `tier` (used when the wake post
    folds held health notices into its own body)."""
    items = read_holds(tier)
    if items:
        clear_holds(tier)
    return [i["text"] for i in items]


def holds_summary() -> str:
    """One-line status for `quiet hours` / ops output."""
    parts = []
    for tier in TIERS:
        n = held_count(tier)
        if n:
            parts.append(f"{n} {tier}")
    return f"Held posts: {', '.join(parts)}." if parts else ""


def _now_iso() -> str:  # pragma: no cover - trivial
    return datetime.now().isoformat()
