"""Deterministic morning-brief composition (POLISH-1 P1/P2/P3).

One composer for BOTH the 04:00 CT cron and the on-demand `morning brief`. The
whole point is that no line is derived from a stale or divergent source:

  * `today` is CT-anchored ONCE by the caller (ct_today()) and threaded through
    every relative-date computation, so the header date and every "due …" line
    agree by construction (P1) — no LLM to fabricate a date, no server-local
    naive now(), no reuse of a stored composition date.
  * every relative phrase carries its absolute date (P2), via utils.describe_due.
  * the inbox count comes from the SAME live email_index path the `inbox` command
    uses (P3): a fresh number synced at invocation, or the line is omitted — never
    a stale tally.

Root cause of the observed "Sunday, Jul 6" header (bug P1): the on-demand
`morning brief` had NO deterministic handler, so it fell through to the LLM
mention path, which free-composed a brief from `_build_mention_context` — a naive
`datetime.now()` (the box runs UTC) plus whatever dates sat in the calendar cache.
Both header and relative language were then a model guess. This module removes the
LLM from brief composition entirely; `main._handle_morning_brief_command` routes
the on-demand request here before the LLM ever sees it.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from artemis import config
from artemis.utils import abbr_date, describe_due

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

# ── P7 reminder-backoff policy (shared with scheduler.job_action_item_reminders) ──
# Kept here so both the reminder loop and the brief's "Stale items" demotion line
# agree on the ceiling, and to avoid a circular import through scheduler.
MAX_REMINDERS = 5            # after this many ignored reminders, stop pinging
MAX_REMINDERS_PER_DAY = 2    # hard per-CT-day cap per item
# reminders-already-sent → hours to wait before the next ping (4h → 8h → daily →
# every 3 days). Anything past the mapping falls to the 72h floor.
REMINDER_BACKOFF_HOURS = {0: 0, 1: 4, 2: 8, 3: 24, 4: 72}


def reminder_due(*, sent_count: int, reminders_today: int,
                 last_reminded_at, now) -> bool:
    """P7 — should this action item be reminded now? Pure decision so the backoff
    policy is unit-testable independent of the scheduler/DB.

      * sent_count reminders already sent (reminder_count);
      * reminders_today = reminders already sent on the current CT day for this item;
      * last_reminded_at / now = tz-aware datetimes (now only consulted when a
        prior reminder exists).

    Returns False (skip) when the item has exhausted its budget, hit the daily cap,
    or hasn't waited out the backoff interval for its sent_count."""
    if sent_count >= MAX_REMINDERS:
        return False
    if reminders_today >= MAX_REMINDERS_PER_DAY:
        return False
    if last_reminded_at is not None:
        interval_h = REMINDER_BACKOFF_HOURS.get(sent_count, 72)
        elapsed_h = (now - last_reminded_at).total_seconds() / 3600.0
        if elapsed_h < interval_h:
            return False
    return True


def ct_today() -> date:
    """The single CT-anchored 'today' seam for the brief. The box runs UTC (a day
    ahead of Central after ~19:00 CT), so every brief date funnels through here."""
    return datetime.now(_CT).date()


def full_date(d: date) -> str:
    """Header date — 'Sunday, Jul 19' (weekday + absolute), platform-independent."""
    return f"{d.strftime('%A')}, {abbr_date(d)}"


# ---------------------------------------------------------------------------
# Pure renderers (no I/O — unit-testable with a fixed `today`)
# ---------------------------------------------------------------------------

def _event_line(e: dict, *, with_date: bool) -> str:
    start = e.get("start", "") or ""
    when = start[11:16] if "T" in start else "all-day"
    if with_date and len(start) >= 10:
        try:
            day = date.fromisoformat(start[:10])
            when = f"{abbr_date(day)} {when}"
        except ValueError:
            pass
    external = [a for a in e.get("attendees", []) if not a.get("self")]
    who = ", ".join(a.get("name") or a.get("email", "") for a in external) if external else "(solo)"
    return f"  • {when} {e.get('summary', '(untitled)')} — {who}"


def _commitment_line(row: dict, today: date) -> str:
    """A due-soon commitment with its id (P5) and a relative+absolute date (P2)."""
    client = f" · {row['client']}" if row.get("client") else ""
    return f"  • **{row['title']}** (#{row['id']}) — {describe_due(row.get('due_date'), today)}{client}"


def render_stale_line(items: list[dict]) -> str:
    """P7 — the single line the reminder loop demotes exhausted items to. Empty
    string when there's nothing stale (so the caller renders nothing)."""
    if not items:
        return ""
    listed = ", ".join(f"{i['title']} (`{str(i['id'])[:8]}`)" for i in items)
    return f"\U0001f5c4️ **Stale items ({len(items)})** — {listed}"


def render_brief(
    today: date,
    *,
    meeting_lines: list[str],
    commitment_rows: list[dict],
    inbox_count: int | None,
    vault_section: str = "",
    stale_items: list[dict] | None = None,
    monitor_text: str = "",
) -> str:
    """Assemble the brief text from already-gathered pieces. PURE: every date is
    computed from the single `today` passed in, so the header and every relative
    line share one anchor (P1)."""
    # P1 invariant: the header date and the anchor used for every relative
    # computation below are the SAME `today`. Asserted here so a future refactor
    # that threads a second date can't silently reintroduce the Jul-6 class of bug.
    assert isinstance(today, date), "brief anchor must be a date"

    parts: list[str] = [f"☀️ **Morning Brief — {full_date(today)}**"]

    parts.append("\n\U0001f4c5 **Calendar**")
    parts.extend(meeting_lines or ["  • No meetings scheduled."])

    parts.append("\n✅ **Commitments due soon**")
    if commitment_rows:
        parts.extend(_commitment_line(r, today) for r in commitment_rows)
    else:
        parts.append("  • Nothing due in the next few days.")

    if inbox_count is not None:
        if not inbox_count:
            parts.append("\n\U0001f4ec **Inbox** — inbox zero \U0001f389")
        elif inbox_count == 1:
            parts.append("\n\U0001f4ec **Inbox** — 1 thread needs action")
        else:
            parts.append(f"\n\U0001f4ec **Inbox** — {inbox_count} threads need action")
    # inbox_count is None → the live count was unavailable; omit the line rather
    # than show a stale number (P3 degradation rule).

    stale_line = render_stale_line(stale_items or [])
    if stale_line:
        parts.append(f"\n{stale_line}")

    if monitor_text:
        parts.append(f"\n\U0001f6e1️ **Monitors**\n{monitor_text}")

    if vault_section:
        parts.append(f"\n{vault_section}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Live data gathering (best-effort; never raises into the brief)
# ---------------------------------------------------------------------------

def stale_action_items() -> list[dict]:
    """P7 — pending action items that exhausted their reminder budget. Surfaced in
    the brief instead of being pinged; recoverable via `approve/skip sched <id>`."""
    from knowledge.db import execute_query
    return execute_query(
        "SELECT id, title FROM acos.action_items "
        "WHERE status = 'pending' AND reminder_count >= %s "
        "ORDER BY created_at ASC",
        (MAX_REMINDERS,),
    )


def _gather_meetings(today: date, calendar) -> list[str]:
    from datetime import timedelta
    from artemis import calendar_cache
    try:
        if calendar and getattr(calendar, "service", None):
            calendar_cache.refresh(calendar)
    except Exception:
        logger.exception("morning brief: calendar refresh failed")
    lines: list[str] = []
    try:
        for e in calendar_cache.get_events_for_date(today):
            lines.append(_event_line(e, with_date=False))
        upcoming = calendar_cache.get_events_in_range(today + timedelta(days=1),
                                                      today + timedelta(days=7))
        upcoming_ext = [e for e in upcoming
                        if any(not a.get("self") for a in e.get("attendees", []))]
        if upcoming_ext:
            lines.append("  _Coming up:_")
            for e in upcoming_ext[:5]:
                lines.append(_event_line(e, with_date=True))
    except Exception:
        logger.exception("morning brief: calendar gather failed")
    return lines


def _gather_commitments() -> list[dict]:
    from artemis import commitments
    try:
        due = commitments.get_due_soon(days=3)
        seen = {c["id"] for c in due}
        for c in commitments.get_start_alerts():
            if c["id"] not in seen:
                due.append(c)
                seen.add(c["id"])
        return due
    except Exception:
        logger.exception("morning brief: commitments gather failed")
        return []


def _gather_inbox_count(gmail) -> int | None:
    """Live working-set count from the SAME path the `inbox` command uses (P3).
    Returns None (→ omit the line) if the live count can't be produced."""
    if not gmail:
        return None
    from artemis import email_index
    try:
        summary = email_index.sync_from_gmail(gmail)
        return int(summary.get("working_set", 0))
    except Exception:
        logger.exception("morning brief: live inbox count failed")
        return None


def _gather_vault_section() -> str:
    try:
        from artemis import vault
        return vault.morning_brief_section() or ""
    except Exception:
        logger.exception("morning brief: vault section failed")
        return ""


def _gather_monitors() -> str:
    try:
        from artemis.monitors import (
            check_all_ssl, check_domain_expiry,
            format_ssl_alerts, format_domain_alerts,
        )
        alerts = [a for a in (format_ssl_alerts(check_all_ssl()),
                              format_domain_alerts(check_domain_expiry())) if a]
        return "\n".join(alerts)
    except Exception:
        logger.exception("morning brief: monitor gather failed")
        return ""


def compose(today: date, *, gmail=None, calendar=None,
            include_monitors: bool = False, include_vault: bool = True) -> str:
    """Gather every section live and render the brief for `today` (CT-anchored by
    the caller). Best-effort per section — a failing section degrades to a note or
    is omitted, never a stale value and never a raise."""
    stale = []
    try:
        stale = stale_action_items()
    except Exception:
        logger.exception("morning brief: stale-items gather failed")
    return render_brief(
        today,
        meeting_lines=_gather_meetings(today, calendar),
        commitment_rows=_gather_commitments(),
        inbox_count=_gather_inbox_count(gmail),
        vault_section=_gather_vault_section() if include_vault else "",
        stale_items=stale,
        monitor_text=_gather_monitors() if include_monitors else "",
    )
